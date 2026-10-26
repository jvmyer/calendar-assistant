# Laptop als Planer-Server einrichten

Ziel: Der Laptop läuft zu Hause 24/7 als Server (Ubuntu Server 24.04 LTS, ohne Oberfläche). Du bedienst ihn
per SSH vom Mac. Kein Port ist am Router nach außen geöffnet (Telegram läuft über ausgehende
Verbindungen); Fernzugriff von unterwegs läuft stattdessen über Tailscale (Abschnitt 10).

Dateien hier: `setup-server.sh` (läuft auf dem Laptop), `push.sh` (läuft auf dem Mac), `planner.service`
(systemd-Dienst).

## Stand (2026-10-13) – hier geht es weiter

**Erledigt:**
- Ubuntu Server 24.04.5 LTS ist auf dem Acer installiert und startet von allein (Abschnitte 1–2).
- Anmeldung am Acer funktioniert: Benutzer **`<user>`**, Servername **`<server>`**.
- LAN-Kabel verbunden, feste Adresse **`<server-ip>`** (im Router reserviert, Router-Oberfläche → statisches DHCP).

**Als Nächstes (in dieser Reihenfolge):**
1. Abschnitt 3: feste IP im Router vergeben.
2. Abschnitt 4: SSH vom Mac testen (`ssh <user>@<server-ip>`).
3. Abschnitt 5: `setup-server.sh` ausführen (erst danach darf der **Deckel zu**, vorher schläft der Laptop ein).
4. Abschnitt 6: Planer mit `push.sh --first` ausrollen.

**Gerät:** Acer, Intel Core i7-7500U (2 Kerne/4 Threads, AVX2), 8 GB RAM, Samsung SSD 860 EVO 250 GB,
Netzwerk Realtek (LAN) + Qualcomm Atheros (WLAN, ungenutzt).

### Acer-Besonderheiten (wichtig bei einer Neuinstallation)

- **F12-Startmenü** ist ab Werk aus → im BIOS (`F2`) unter *Main* „F12 Boot Menu“ = Enabled.
- **Secure Boot ist ausgeschaltet** (BIOS → *Boot* → Secure Boot = Disabled). Dafür muss ein
  **BIOS-Supervisor-Passwort** gesetzt sein (der Nutzer kennt es; es steht bewusst nicht hier).
- **Der Ubuntu-Zwischenstarter (shim) startet auf diesem Acer nicht** – Meldung „Default Boot Device
  Missing or Boot Failed“. Lösung (vom Live-Stick aus, siehe unten): Ersatzweg und Starteintrag zeigen
  **direkt auf GRUB**:
  ```bash
  E=/boot/efi/EFI
  mkdir -p $E/BOOT/backup && cp $E/BOOT/BOOTX64.EFI $E/BOOT/backup/BOOTX64.EFI.shim
  mv $E/BOOT/fbx64.efi $E/BOOT/backup/ 2>/dev/null
  cp $E/ubuntu/grubx64.efi $E/BOOT/BOOTX64.EFI && cp $E/ubuntu/grubx64.efi $E/BOOT/grubx64.efi
  efibootmgr -c -d /dev/sda -p 1 -L ubuntu -l '\EFI\ubuntu\grubx64.efi'
  ```
  Ein Ubuntu-Update von `grub-efi-amd64-signed`/`shim-signed` würde `EFI/BOOT/BOOTX64.EFI` wieder auf
  shim setzen. Dagegen richtet `setup-server.sh` einen apt-Hook ein (`/usr/local/sbin/acer-grub-direct`,
  läuft nach jedem apt-Lauf). Startet der Server nach einem Update trotzdem nicht, ist das der erste Verdacht.
- **Im BIOS steht `HDD0: Samsung SSD` an erster Stelle** der Bootliste (nutzt den Ersatzweg `EFI/BOOT`).
- **Reparatur ohne Tastatur-Tipperei:** Live-Stick per `F12` starten → in der Sprachauswahl `F1` →
  „Help on SSH access“ zeigt `installer@<ip>` und ein Einmal-Passwort. Vom Mac per SSH verbinden; im
  Installer-Bildschirm wechselt **`F2`** in eine Root-Shell. Installiertes System einhängen:
  `vgchange -ay; mount /dev/ubuntu-vg/ubuntu-lv /mnt; mount /dev/sda2 /mnt/boot; mount /dev/sda1 /mnt/boot/efi`.
- **Bei der Installation genau hinsehen:** Benutzername und Servername werden so übernommen, wie sie
  getippt wurden. Beim Passwort an die Tastaturbelegung denken (der Installer startet ggf. mit US-Layout).

## 1. Vorbereitung am Mac

1. Ubuntu Server 24.04 LTS laden: <https://ubuntu.com/download/server> (ISO, ca. 2 GB).
2. USB-Stick (mind. 8 GB, wird gelöscht) mit **balenaEtcher** beschreiben (<https://etcher.balena.io>).
3. Optional: Zeigt dir `uname -m` auf dem Laptop später `x86_64`, ist die normale PC-ISO richtig.

## 2. Am Laptop: BIOS und Installation

**BIOS/UEFI** (beim Start `F2`, `F10`, `F12` oder `Entf`, je nach Hersteller):
- Von USB booten (einmaliges Boot-Menü, meist `F12`).
- Wenn vorhanden: **„Power on AC“ / „AC Recovery“ / „Restore on AC Power Loss“ = an**. Dann startet der
  Laptop nach einem Stromausfall von selbst.

**Installer** (empfohlene Antworten):
| Frage | Antwort |
|---|---|
| Sprache | English (Servermeldungen sind so leichter zu googeln) |
| Tastatur | German |
| Netzwerk | **LAN-Kabel** bevorzugt (stabiler als WLAN). Nur WLAN? Dann hier das WLAN eintragen |
| Installationsart | Ubuntu Server (nicht „minimized“) |
| Festplatte | Use an entire disk. **Ohne** Verschlüsselung (siehe Hinweis unten) |
| Name / Server-Name / Benutzer | z. B. `<user>` / `<server>` (so ist es tatsächlich installiert) / ein Passwort, das du dir merkst |
| OpenSSH server | **Ja installieren** (und „Allow password authentication“ vorerst lassen) |
| Featured snaps | nichts auswählen |

Nach „Reboot now“ den USB-Stick abziehen.

> **Hinweis Festplattenverschlüsselung:** Mit Verschlüsselung müsste nach jedem Neustart (Stromausfall,
> Updates) jemand das Passwort eingeben, der Server käme sonst nicht von selbst hoch. Deshalb ohne.
> Die Zugangsdaten liegen mit Dateirechten 600 nur beim Dienst-Benutzer. Gib den Laptop nicht unbeaufsichtigt an Fremde.

## 3. Router: feste Adresse

Im Router (Fritzbox o. Ä.) dem Laptop eine **feste IP** geben (DHCP-Reservierung, „Diesem Gerät immer die
gleiche IP-Adresse zuweisen“). Erledigt: `<server>` hat fest `<server-ip>`.

## 4. SSH vom Mac einrichten

```bash
ssh <user>@<server-ip>                      # erste Anmeldung mit Passwort (Fingerabdruck bestätigen)
exit
ssh-copy-id <user>@<server-ip>              # Schlüssel hinterlegen (falls noch keiner: ssh-keygen -t ed25519)
ssh <user>@<server-ip>                      # geht jetzt ohne Passwort
```

Optional, wenn alles läuft: Passwort-Anmeldung abschalten (`PasswordAuthentication no` in
`/etc/ssh/sshd_config.d/50-cloud-init.conf` bzw. `/etc/ssh/sshd_config`, dann `sudo systemctl restart ssh`).
**Erst nach** erfolgreichem Schlüssel-Login, sonst sperrst du dich aus.

## 5. Server einrichten (einmalig)

Vom Mac aus im Projektordner:

```bash
scp deploy/setup-server.sh deploy/planner.service <user>@<server-ip>:~
ssh <user>@<server-ip>
sudo bash setup-server.sh
sudo reboot
```

Das Skript (idempotent, darf wiederholt werden):
- setzt die Zeitzone, spielt Updates ein, installiert die Werkzeuge,
- **Deckel zu = weiterlaufen**, Ruhezustand/Standby gesperrt,
- Firewall: eingehend nur SSH aus privaten Netzen (mit `LAN_CIDRS="192.168.178.0/24" sudo -E bash setup-server.sh` noch enger),
- automatische Sicherheitsupdates (Neustart bei Bedarf um 04:30),
- legt den Benutzer `planner` und `/opt/calendar-assistant` an und installiert `uv`,
- installiert den systemd-Dienst (startet ihn noch nicht).

## 6. Planer ausrollen (Erstinstallation)

Auf dem Mac **zuerst den lokalen Dienst beenden** (der Telegram-Bot darf nur auf einem Rechner laufen):

```bash
pkill -f "planner run"
deploy/push.sh --first <user>@<server-ip>
```

Das überträgt Code, `.env`, Zertifikate, Google-Token und eine konsistente Kopie der Datenbank (Rechte 600,
Eigentümer `planner`), installiert die Abhängigkeiten und startet den Dienst.

Kontrolle:
```bash
ssh <user>@<server-ip> 'systemctl status planner --no-pager; sudo journalctl -u planner -n 30 --no-pager'
```
Im Log müssen „Dienst gestartet“ und „Telegram-Bot läuft“ stehen. Schreib dem Bot `/heute`.

## 7. Betrieb

| Was | Befehl |
|---|---|
| Code aktualisieren | `deploy/push.sh <user>@<server-ip>` |
| Live-Log ansehen | `ssh <user>@… 'sudo journalctl -u planner -f'` |
| Dienst neu starten | `ssh -t <user>@… 'sudo systemctl restart planner'` |
| **Google-Login erneuern (alle 7 Tage)** | auf dem Mac `uv run planner login`, dann `deploy/push.sh --token <user>@…` |
| ILIAS-Sperre aufheben | auf dem Server `sudo -u planner /opt/calendar-assistant/.venv/bin/planner ilias-login` |
| Zustand | `sudo -u planner /opt/calendar-assistant/.venv/bin/planner status` |

Der Bot erinnert dich per Telegram an den Google-Login, bevor der Token abläuft. Der Login braucht einen
Browser und läuft deshalb auf dem Mac; `push.sh --token` überträgt nur den neuen Token.

## 8. Akku schonen (optional)

Bei Dauerbetrieb am Netz hält der Akku länger, wenn er nicht auf 100 % geladen wird. `tlp` ist schon
installiert; die Ladegrenzen funktionieren aber nur bei manchen Herstellern (ThinkPad am besten). In
`/etc/tlp.conf`:

```
START_CHARGE_THRESH_BAT0=60
STOP_CHARGE_THRESH_BAT0=80
```
dann `sudo tlp start`. Ob es geht, zeigt `sudo tlp-stat -b`. Geht es nicht, ist das kein Problem.

## 9. Später auf diesem Laptop (noch nicht eingerichtet)

- **Whisper** für Sprachnachrichten und ein **lokales Sprachmodell** (Ollama) fürs Mail-Lesen. Wie viel
  Modell möglich ist, hängt von CPU (AVX2) und RAM ab. Die Gerätedaten dafür:
  ```bash
  lscpu | grep -E "Model name|^CPU\(s\)"; grep -o -m1 -E "avx2|avx512f" /proc/cpuinfo | sort -u; free -h | head -2
  ```
- Regelmäßige Sicherung der Datenbank (`/opt/calendar-assistant/data/planner.db`) auf den Pi oder einen
  gemieteten Server.

## Nach der Einrichtung (Stand 2026-10-16)

- Ollama + `qwen3:4b` installiert (nur lokal erreichbar, Port 11434 auf 127.0.0.1).
- Kurzbefehle im Home von `<user>`: `./planner.sh <kommando>` (Planer als Dienst-Benutzer, z. B. `mail-login`,
  `mail-review`), `./enable-mail.sh` (Mail-Abruf einschalten).
- Härtung: `scp deploy/harden.sh <user>@<server-ip>:~ && ssh -t <user>@<server-ip> 'sudo bash harden.sh'`
  (SSH nur mit Schlüssel, kein root, nur `<user>`; Dienst mit UMask 0077). Vorher muss der Schlüssel-Login gehen.
- Nachtruhe (eingerichtet 2026-10-26): `sudo bash nachtruhe.sh` – Ruhezustand 20:00–07:30, Wecken per RTC
  (`rtcwake -m mem`, getestet). **Nicht** `-m off`: aus dem ausgeschalteten Zustand startet der Acer nicht von
  selbst (getestet, musste per Knopf an). Abschalten: `sudo bash nachtruhe.sh --aus`; Log: `journalctl -t nachtruhe`.

## 10. Fernzugriff von unterwegs (Tailscale, eingerichtet 2026-10-19)

Tailscale verbindet Mac und Server in einem eigenen privaten Netz (WireGuard, NAT-Traversal) – ohne
Portfreigabe am Router. Server-Tailscale-IP: **`<tailscale-ip>`** (Name `<server>`, Konto
88045436+jvmyer@users.noreply.github.com, `tailscale status` zeigt alle Geräte im Netz).

Von unterwegs (ohne Heimnetz) einfach die Tailscale-IP statt der lokalen `<server-ip>` nehmen:

```bash
ssh <user>@<tailscale-ip>
deploy/push.sh <user>@<tailscale-ip>
```

Voraussetzung: Tailscale läuft auf dem Mac (App in der Menüleiste, Status „Connected“) und auf dem Server
(`systemctl status tailscaled`). Beide müssen im selben Tailscale-Konto angemeldet sein.

## 11. Claude kann eigene Aufgaben/Termine anlegen (MCP-Server)

`planner mcp` startet einen MCP-Server, der Claude (in Claude Code auf dem Mac) dieselben Werkzeuge gibt
wie die CLI: `add_task`, `propose`, `decide_proposal`, `set_task_status`, `list_open_tasks`,
`list_pending_proposals`, `area_tree` (Code: `src/planner/mcp_server.py`). Jeder Aufruf läuft über die
echten Planer-Funktionen – Change-Log, Nie-verloren-Garantie und Bereichsgewichte gelten also wie
gewohnt. Mails und Sprachnachrichten bleiben außen vor: Die Lese-Werkzeuge blenden alles mit `source`
mail/gmail/voice aus (Datenschutzregel).

**Zwei Schlösser, kein offener Port am Router:**
1. Der Server bindet standardmäßig nur an `127.0.0.1` – erreichbar nur per SSH-Tunnel oder, wenn gesetzt,
   direkt an die Tailscale-IP (`MCP_HOST=<tailscale-ip>` in `.env`).
2. Jede Anfrage braucht ein Bearer-Token (`MCP_TOKEN` in `.env`, erzeugen mit `openssl rand -hex 32`).
   Ohne Token startet der Server gar nicht.

**Einrichtung (einmalig):**
```bash
# 1. Auf dem Server: Token erzeugen und in .env eintragen
ssh <user>@<tailscale-ip>
openssl rand -hex 32            # in /opt/calendar-assistant/.env als MCP_TOKEN= eintragen
echo "MCP_HOST=<tailscale-ip>" >> /opt/calendar-assistant/.env   # direkt übers Tailnet erreichbar

# 2. Code ausrollen (vom Mac)
deploy/push.sh <user>@<tailscale-ip>

# 3. Dienst einrichten (braucht sudo – selbst ausführen)
ssh -t <user>@<tailscale-ip> 'sudo cp /opt/calendar-assistant/deploy/planner-mcp.service /etc/systemd/system/ && sudo systemctl enable --now planner-mcp'

# 4. Auf dem Mac: Claude Code mit dem Server verbinden (Token = der oben erzeugte Wert)
claude mcp add --transport http planner http://<tailscale-ip>:8765/mcp --header "Authorization: Bearer <TOKEN>"
```

Log ansehen: `ssh <user>@… 'sudo journalctl -u planner-mcp -f'`. Läuft nur im Tailnet – ohne Tailscale
(z. B. unterwegs ohne VPN) ist der Server nicht erreichbar, genau wie SSH.
