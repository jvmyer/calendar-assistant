#!/usr/bin/env bash
# Nachtruhe: Server schläft 20:00–07:30 (Ruhezustand, ~1 W) und weckt sich per eingebauter Uhr selbst.
# Läuft AUF DEM SERVER: sudo bash nachtruhe.sh   (rückgängig: sudo bash nachtruhe.sh --aus)
# Zeiten ändern: SCHLAF/WACH unten anpassen und das Skript erneut ausführen.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen: sudo bash $0" >&2; exit 1; }
SCHLAF="20:00"
WACH="07:30"

if [ "${1:-}" = "--aus" ]; then
  systemctl disable --now nachtruhe.timer 2>/dev/null || true
  rm -f /etc/systemd/system/nachtruhe.{service,timer} /usr/local/sbin/nachtruhe-schlafen
  systemctl daemon-reload
  echo "Nachtruhe ausgeschaltet."
  exit 0
fi

echo "==> Schlaf-Skript"
cat > /usr/local/sbin/nachtruhe-schlafen <<SH
#!/bin/sh
# Nächstes $WACH berechnen (heute, falls noch nicht vorbei – sonst morgen) und bis dahin schlafen.
set -eu
wach=\$(date -d "today $WACH" +%s)
[ "\$wach" -gt "\$(date +%s)" ] || wach=\$(date -d "tomorrow $WACH" +%s)
logger -t nachtruhe "schlafe bis \$(date -d @\$wach '+%d.%m. %H:%M')"
exec /usr/sbin/rtcwake -m mem -t "\$wach"
SH
chmod 755 /usr/local/sbin/nachtruhe-schlafen

echo "==> systemd-Zeitplan ($SCHLAF schlafen, $WACH aufwachen)"
cat > /etc/systemd/system/nachtruhe.service <<UNIT
[Unit]
Description=Nachtruhe: Ruhezustand bis $WACH

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/nachtruhe-schlafen
UNIT
cat > /etc/systemd/system/nachtruhe.timer <<UNIT
[Unit]
Description=Nachtruhe täglich um $SCHLAF

[Timer]
OnCalendar=*-*-* $SCHLAF:00
# Kein Nachholen: war der Server um $SCHLAF aus, soll er beim Einschalten nicht gleich wieder schlafen.
Persistent=false

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now nachtruhe.timer
systemctl list-timers nachtruhe.timer --no-pager | sed -n '1,2p'
echo "Fertig. Log: journalctl -t nachtruhe"
