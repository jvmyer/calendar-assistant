#!/usr/bin/env bash
# Richtet einen frischen Ubuntu Server (24.04 LTS) als Planer-Server ein.
# Läuft AUF DEM LAPTOP, mit sudo. Idempotent (darf mehrfach laufen) und zerstört nichts.
#
#   scp deploy/setup-server.sh deploy/planner.service <user>@<laptop>:~
#   ssh <user>@<laptop>
#   sudo bash setup-server.sh
#
# Danach einmal neu starten (sudo reboot), damit die Deckel-/Ruhezustand-Einstellungen greifen.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen: sudo bash $0" >&2; exit 1; }

APP_USER="${APP_USER:-planner}"
APP_DIR="${APP_DIR:-/opt/calendar-assistant}"
# Nur diese Netze dürfen per SSH zugreifen (private Adressbereiche = dein Heimnetz). Mit LAN_CIDRS="192.168.1.0/24" einengen.
LAN_CIDRS="${LAN_CIDRS:-192.168.0.0/16 10.0.0.0/8 172.16.0.0/12}"
HERE="$(cd "$(dirname "$0")" && pwd)"

log() { printf '\n==> %s\n' "$*"; }

log "Zeitzone und Pakete"
timedatectl set-timezone Europe/Berlin
export DEBIAN_FRONTEND=noninteractive

log "Acer-Eigenheit: GRUB direkt starten (shim startet auf diesem Gerät nicht, siehe README)"
# Läuft VOR den Updates: ein Update von shim/grub würde EFI/BOOT/BOOTX64.EFI sonst auf shim zurücksetzen,
# und der Server startet nach dem nächsten Neustart nicht mehr. Abschalten mit ACER_GRUB_DIRECT=0.
if [ "${ACER_GRUB_DIRECT:-1}" = "1" ] && [ -d /boot/efi/EFI ]; then
  cat > /usr/local/sbin/acer-grub-direct <<'HOOK'
#!/bin/sh
# Hält den Ersatz-Startweg auf GRUB (nach jedem apt-Lauf aufgerufen).
E=/boot/efi/EFI
[ -f "$E/ubuntu/grubx64.efi" ] || exit 0
mkdir -p "$E/BOOT"
cmp -s "$E/ubuntu/grubx64.efi" "$E/BOOT/BOOTX64.EFI" || cp "$E/ubuntu/grubx64.efi" "$E/BOOT/BOOTX64.EFI"
cmp -s "$E/ubuntu/grubx64.efi" "$E/BOOT/grubx64.efi" || cp "$E/ubuntu/grubx64.efi" "$E/BOOT/grubx64.efi"
[ -f "$E/BOOT/fbx64.efi" ] && rm -f "$E/BOOT/fbx64.efi"
exit 0
HOOK
  chmod 755 /usr/local/sbin/acer-grub-direct
  echo 'DPkg::Post-Invoke { "/usr/local/sbin/acer-grub-direct || true"; };' > /etc/apt/apt.conf.d/99acer-grub-direct
  /usr/local/sbin/acer-grub-direct
fi
apt-get update -y
apt-get -o Dpkg::Options::=--force-confold upgrade -y
apt-get install -y --no-install-recommends git curl ca-certificates rsync ufw unattended-upgrades tlp lm-sensors htop

log "Deckel zu = weiterlaufen, kein Ruhezustand"
install -d /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/10-planner-lid.conf <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
EOF
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

log "Firewall: eingehend nur SSH aus dem Heimnetz"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
for cidr in $LAN_CIDRS; do
  ufw allow from "$cidr" to any port 22 proto tcp
done
ufw --force enable

log "Automatische Sicherheitsupdates (mit Neustart nachts, falls nötig)"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
cat > /etc/apt/apt.conf.d/52planner-unattended.conf <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
EOF

log "Benutzer '$APP_USER' und Verzeichnisse"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$APP_DIR" "$APP_DIR/data"
install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$APP_DIR/data/secrets"

log "uv (Python-Werkzeug) für '$APP_USER'"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
# HOME explizit setzen: runuser übernimmt sonst das HOME von root, und uv würde nach /root installieren
runuser -u "$APP_USER" -- env HOME="$APP_HOME" bash -c 'test -x "$HOME/.local/bin/uv" || curl -LsSf https://astral.sh/uv/install.sh | sh'

log "systemd-Dienst installieren (noch nicht starten: Code und Geheimnisse fehlen noch)"
if [ -f "$HERE/planner.service" ]; then
  sed -e "s#^User=.*#User=$APP_USER#" -e "s#/opt/calendar-assistant#$APP_DIR#g" \
    "$HERE/planner.service" > /etc/systemd/system/planner.service
  systemctl daemon-reload
else
  echo "WARNUNG: planner.service liegt nicht neben diesem Skript – bitte nachträglich installieren." >&2
fi

log "Fertig. Zustand:"
echo "  Zeitzone : $(timedatectl show -p Timezone --value)"
echo "  Firewall : $(ufw status | head -1)"
echo "  Benutzer : $(id "$APP_USER")"
echo "  uv       : $("$APP_HOME/.local/bin/uv" --version 2>/dev/null || echo 'nicht gefunden')"
cat <<EOF

Nächste Schritte:
  1. sudo reboot                       (Deckel-/Ruhezustand-Einstellungen werden aktiv)
  2. Auf dem Mac: deploy/push.sh --first <user>@<laptop>
EOF
