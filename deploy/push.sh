#!/usr/bin/env bash
# Spielt den Planer vom Mac auf den Laptop-Server. Läuft AUF DEM MAC, im Projektordner.
#
#   deploy/push.sh <user>@<server>            nur Code aktualisieren, Dienst neu starten
#   deploy/push.sh --first <user>@<server>    Erstinstallation: Code + .env + Zertifikate + Token + Datenbank
#   deploy/push.sh --token <user>@<server>    nur den Google-Token erneuern (Re-Login alle 7 Tage)
#
# WICHTIG: Der Telegram-Bot darf nur auf EINEM Rechner laufen. Vor --first den lokalen Dienst beenden
# (`pkill -f "planner run"`), sonst stören sich zwei Instanzen gegenseitig.
set -euo pipefail

MODE="code"
case "${1:-}" in
  --first) MODE="first"; shift ;;
  --token) MODE="token"; shift ;;
esac
HOST="${1:?Aufruf: deploy/push.sh [--first|--token] <user>@<server>}"

APP_USER="${APP_USER:-planner}"
APP_DIR="${APP_DIR:-/opt/calendar-assistant}"
STAGE="~/calendar-assistant-stage"          # Zwischenablage im Home des SSH-Benutzers (kein sudo für rsync nötig)
cd "$(dirname "$0")/.."

run() { echo "+ $*"; "$@"; }
remote() { ssh -t "$HOST" "$@"; }            # -t: sudo darf nach dem Passwort fragen

if [ "$MODE" = "token" ]; then
  [ -f data/google_token.json ] || { echo "data/google_token.json fehlt – erst 'uv run planner login' ausführen." >&2; exit 1; }
  run scp -q data/google_token.json "$HOST:google_token.json.new"
  remote "sudo install -o $APP_USER -g $APP_USER -m 600 ~/google_token.json.new $APP_DIR/data/google_token.json && rm ~/google_token.json.new && sudo systemctl restart planner"
  echo "Token erneuert, Dienst neu gestartet."
  exit 0
fi

if [ "$MODE" = "first" ] && pgrep -f "planner run" >/dev/null; then
  echo "Lokal läuft noch ein Planer-Dienst. Bitte beenden (pkill -f \"planner run\"), dann nochmal." >&2
  exit 1
fi

echo "== Code übertragen"
run rsync -az --delete \
  --exclude '.git/' --exclude '.venv/' --exclude 'data/' --exclude '.env' \
  --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
  ./ "$HOST:calendar-assistant-stage/"
remote "sudo rsync -a --delete --chown=$APP_USER:$APP_USER \
  --filter='P data/' --filter='P .env' --filter='P .venv/' \
  $STAGE/ $APP_DIR/"

if [ "$MODE" = "first" ]; then
  echo "== Geheimnisse und Daten übertragen (Rechte 600, gehören dem Dienst-Benutzer)"
  [ -f .env ] || { echo ".env fehlt" >&2; exit 1; }
  SNAP="$(mktemp -t planner-snapshot.XXXXXX)"
  trap 'rm -f "$SNAP"' EXIT
  sqlite3 data/planner.db ".backup '$SNAP'"          # konsistente Kopie, auch wenn die DB gerade benutzt wird
  run scp -q .env "$HOST:.env.new"
  run scp -q "$SNAP" "$HOST:planner.db.new"
  run scp -q data/google_token.json "$HOST:google_token.json.new"
  run scp -q -r data/secrets "$HOST:secrets.new"
  remote "set -e
    sudo install -o $APP_USER -g $APP_USER -m 600 ~/.env.new $APP_DIR/.env
    sudo install -o $APP_USER -g $APP_USER -m 600 ~/planner.db.new $APP_DIR/data/planner.db
    sudo install -o $APP_USER -g $APP_USER -m 600 ~/google_token.json.new $APP_DIR/data/google_token.json
    sudo install -d -o $APP_USER -g $APP_USER -m 700 $APP_DIR/data/secrets
    for f in ~/secrets.new/*; do sudo install -o $APP_USER -g $APP_USER -m 600 \"\$f\" $APP_DIR/data/secrets/; done
    shred -u ~/.env.new ~/planner.db.new ~/google_token.json.new ~/secrets.new/* 2>/dev/null; rmdir ~/secrets.new"
fi

echo "== Abhängigkeiten installieren (uv sync)"
remote "sudo -H -u $APP_USER bash -c 'cd $APP_DIR && ~/.local/bin/uv sync --frozen --no-dev'"   # -H: HOME des Dienst-Benutzers

echo "== Dienst starten"
if [ "$MODE" = "first" ]; then
  remote "sudo systemctl enable --now planner"
else
  remote "sudo systemctl restart planner"
fi
remote "sleep 3; systemctl is-active planner; sudo journalctl -u planner -n 8 --no-pager"
