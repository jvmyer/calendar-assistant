#!/usr/bin/env bash
# Härtung nach der Einrichtung. Läuft AUF DEM SERVER: sudo bash harden.sh
# Voraussetzung: Login per SSH-Schlüssel funktioniert (ssh-copy-id), sonst sperrst du dich aus.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen: sudo bash $0" >&2; exit 1; }
ADMIN="${SUDO_USER:?Bitte per sudo als Admin-Benutzer starten}"
[ -s "/home/$ADMIN/.ssh/authorized_keys" ] || { echo "Kein SSH-Schlüssel für $ADMIN hinterlegt – Abbruch." >&2; exit 1; }

echo "==> SSH: nur Schlüssel, kein root, kein Passwort"
# 10-… wird vor 50-cloud-init.conf gelesen; bei sshd gilt der erste Wert.
cat > /etc/ssh/sshd_config.d/10-planner.conf <<CONF
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
X11Forwarding no
MaxAuthTries 3
AllowUsers $ADMIN
CONF
sshd -t
systemctl reload ssh
sshd -T | grep -Ei "^(passwordauthentication|permitrootlogin|allowusers)"

echo "==> Planer-Dienst: neue Dateien nur für den Dienst lesbar"
install -d /etc/systemd/system/planner.service.d
cat > /etc/systemd/system/planner.service.d/10-hardening.conf <<CONF
[Service]
UMask=0077
PrivateDevices=true
CONF
chmod -R go-rwx /opt/calendar-assistant/data
systemctl daemon-reload
systemctl restart planner

echo "==> Firewall"
ufw status verbose | sed -n '1,20p'

echo "==> Aufräumen"
rm -f "/home/$ADMIN/llm_eval.py" "/home/$ADMIN/setup-server.sh" "/home/$ADMIN/planner.service"
echo "Fertig."
