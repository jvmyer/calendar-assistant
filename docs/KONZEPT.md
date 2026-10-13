# Konzept – Persönlicher Planungsassistent (Name TBD)

Stand: 2026-10-12 · Status: Entwurf

## 1. Ziel

Ein Hintergrunddienst, der alle relevanten Informationsquellen (Mails, ILIAS, Telegram) auswertet, daraus Aufgaben ableitet und diese automatisch als Blocker in Google Kalender einplant. Ändert sich etwas (Meeting fällt aus, neue Deadline), wird neu geplant. **Google Kalender ist die einzige Oberfläche**, der Telegram-Bot ist die Steuerzentrale.

Nutzung: nur für mich selbst (Single-User). Kein öffentliches Login-System, keine Google-App-Verifizierung nötig.

## 2. Grundprinzipien

1. **Der Mensch gewinnt immer.** Was ich in Google Kalender/Tasks oder per Bot ändere, hat die höchste Berechtigungsstufe. Manuell verschobene Blocker werden „gepinnt“ und vom Algorithmus nicht mehr angefasst.
2. **Alles nachvollziehbar.** Jede automatische Aktion landet im Change-Log mit Quelle und Begründung und ist rückgängig machbar.
3. **Stabilität vor Optimalität.** Heute und morgen bleiben stabil; nur Dringendes darf dort eingreifen. Ab übermorgen wird frei optimiert.
4. **Lokal zuerst.** Mailinhalte und Sprachnachrichten werden auf eigener Hardware verarbeitet, nicht über externe KI-Dienste.

## 3. Architektur

```
 Quellen                     Kern (Laptop-Server, 16 GB, Docker)                  Ausgabe
┌──────────────┐   ┌───────────────────────────────────────────────┐   ┌─────────────────┐
│ Gmail (IMAP) │──▶│ Connectors ──▶ Extraktion (lokales LLM)       │   │ Google Calendar │
│ HKA-Mail     │──▶│                     │                         │──▶│  (Blocker)      │
│ ILIAS (iCal) │──▶│                     ▼                         │   │ Google Tasks    │
│ Telegram     │──▶│ Task-Store (SQLite) ◀──▶ Planer (Scheduler)    │◀──│  (Erledigt ✓)   │
│  Text/Voice  │   │        │                     │                │   └─────────────────┘
└──────────────┘   │        ▼                     ▼                │   ┌─────────────────┐
                   │   Change-Log           Lern-Modul (Aufwand)   │──▶│ Telegram-Bot    │
                   └───────────────────────────────────────────────┘   │ (Infos, /undo)  │
                                                                       └─────────────────┘
```

### Module

| Modul | Aufgabe |
|---|---|
| **Connectors** | Holen periodisch neue Daten (Mails, ILIAS-Termine, Kalenderänderungen). Jede Quelle ist ein Plugin mit gleicher Schnittstelle → spätere Quellen (Arbeitskalender, WhatsApp …) einfach ergänzbar. |
| **Extraktion** | Lokales LLM entscheidet: Enthält diese Mail eine Aufgabe? Wenn ja: Titel, Deadline, Bereich, geschätzter Aufwand. Strukturierte JSON-Ausgabe. |
| **Task-Store** | SQLite. Aufgaben, Bereiche, Regeln, Verknüpfungen zu Kalender-Event- und Task-IDs. |
| **Planer** | Verteilt offene Aufgaben auf freie Zeitfenster gemäß Prioritäten und Regeln. Wird bei jeder relevanten Änderung getriggert. |
| **Lern-Modul** | Vergleicht geschätzten mit tatsächlichem Aufwand und korrigiert künftige Schätzungen. |
| **Change-Log** | Append-only Ereignisprotokoll, Basis für Bot-Meldungen und Undo. |
| **Telegram-Bot** | Eingabe (Text + Sprache via Whisper), kurze Änderungsmeldungen, Morgenübersicht, Feedback, Korrekturen. |

## 4. Datenquellen

| Quelle | Zugang | Bemerkung |
|---|---|---|
| Gmail | Gmail-API über dasselbe OAuth-Projekt wie Calendar (Fallback: IMAP + App-Passwort) | Für Eigennutzung keine Verifizierung nötig („unverified app“-Hinweis einmalig bestätigen). App-Passwörter gelten als Auslaufmodell. |
| HKA-Mail | **Entscheidung 2026-10-13: Mails werden nur vom lokalen Modell gelesen (keine regelbasierte Vorstufe, kein Cloud-Dienst), Abruf stündlich; Connector wird gebaut, sobald das lokale Modell steht.** **OWA-Webzugang** `https://owa-cert.h-ka.de`: Formular-Login (`/owa/auth.owa`) mit **HARICA-Client-Zertifikat** + RZ-Kürzel/Passwort, danach OWA-JSON-Schnittstelle (`/owa/service.svc`, Browser-User-Agent, `X-OWA-CANARY`) nur **lesend** | Kein IMAP. EWS/NTLM (offiziell laut RZ-Handout) scheitert bei uns (401 / Verbindungsabbruch), Ursache unklar. OWA-Weg ist inoffiziell → kann bei OWA-Updates brechen. Sitzung wiederverwenden, nie automatisch bei Fehler neu einloggen (Fehlerzähler!). Login-Test am 2026-10-12 erfolgreich. **2026-10-13: Abruf der 10 neuesten Mails erfolgreich** (Python `requests`): Client-Zertifikat = `hka_chain.pem` (Leaf + Zwischenzertifikat; nur das Leaf-Zertifikat führt zu „Connection closed“) + `hka_key.pem`; Header `Connection: close` (wiederverwendete Keep-Alive-Verbindungen werden gekappt); Login-Formular wird per JavaScript gebaut, POST direkt auf `/owa/auth.owa` (`destination, flags=4, forcedownlevel=0, username, password, passwordText, isUtf8=1`) → Cookie `cadata`; danach `GET /owa/` liefert `X-OWA-CANARY`; `POST /owa/service.svc?action=FindItem|GetItem`, Header `Action`, `X-OWA-CANARY`, Payload URL-kodiert in `X-OWA-UrlPostData` (Body leer), `BodyType: Text`. |
| ILIAS HKA | iCal-Abo aus dem ILIAS-Kalender (`ilias.h-ka.de/calendar.php?…token=…`) | Endpoint existiert (öffentlich erreichbar). Abo-URL muss nach Login im ILIAS-Kalender über „Abonnieren“ kopiert werden. Fallback: SOAP-Webservice (`/webservice/soap/server.php`, erreichbar) oder Scraping. **Geprüft 2026-10-13:** ILIAS 9.23, Formular-Login (kein SSO; POST an `ilias.php?baseClass=ilstartupgui&cmd=post&fallbackCmd=doStandardAuthentication`, Felder `login_form/input_3/input_4|5`), 12 Kurse unter `ilias.php?baseClass=ilmembershipoverviewgui` (Links `goto.php/crs/<ref_id>`). Aufgaben stehen **nicht** als Übungsobjekte (To-Do-Tool leer, iCal ohne Fristen), sondern in **Dateien (PDF/xlsx), Ordnern („Work assignments“, „Organisatorisches + Projekt“) und Foren**. Bester Auslöser: **„Neuigkeiten“** (`ilias.php?baseClass=ilDashboardGUI&cmd=jumpToNews`) bündelt alle Neuerungen aller Kurse (Datei hinzugefügt, Autor, Zeitstempel, Dateiname; Filter „Letzte Woche/Monat/Jahr“) → regelmäßig abrufen, neue Einträge erkennen, dann Inhalt (PDF/Forenbeitrag) lesen und per LLM Aufgabe/Frist extrahieren. SOAP ist erreichbar, für Studierende aber begrenzt (`getExerciseXML`/`getCourseXML` brauchen Rechte).**Umgesetzt (`connectors/ilias.py`):** Abruf der Neuigkeiten mit explizit gesetztem Zeitfilter (der Standardabruf ist je nach Sitzungszustand nicht stabil), Sitzung in `data/ilias_session.json` (600), höchstens ein Login-Versuch pro Lauf und danach keine automatische Wiederholung (Kontosperre; Aufhebung mit `planner ilias-login`), Erstimport still, neue Einträge per `news_id`. **Nutzerwunsch (2026-10-13): Aufgaben aus ILIAS kommen automatisch in den Kalender** (`ilias_flow.py`, `deadlines.py`, `courses.py`, `documents.py`). Pro neuem Eintrag: Datei laden (PDF/Word/Excel) bzw. Forenbeitrag lesen, Kurs über den Breadcrumb (`goto.php/<typ>/<id>`) bestimmen, Fristen regelbasiert finden („Abgabe bis spätestens 20.10. 19 Uhr“, Tabellenzeilen „Abgabe 2: Thema (8.10.)“, „bis morgen/Freitag“, „bis zur nächsten Stunde“ = nächster Stundenplan-Termin des Kurses). Plausible Zukunftsfristen werden **automatisch Aufgaben** (Google Task + Blocker über den Planer, Bereich `Uni/<Kurs>`), Meldung per Telegram mit „Verwerfen“-Button. **Nur Vergangenes (z. B. Plan aus dem Vorsemester) wird nie eingetragen**, nur gemeldet. Klingt es nach Aufgabe, aber ohne Frist → Meldung mit „Als Aufgabe anlegen“. Folien u. Ä. bleiben still. Idempotent: ausgefallene Meldungen werden wiederholt, ohne Aufgaben zu duplizieren; nach 3 Fehlversuchen wird aufgegeben. Grenzen: Präsentationstermine/„Weekly reporting“ ohne Abgabe-Hinweis werden nicht erkannt; „nächste Stunde“ braucht eine eindeutige Kurs↔Stundenplan-Zuordnung (sonst Aufgabe ohne Frist + Hinweis). Ein LLM kann später als zweite Meinung dazukommen (Aufwand, „ist das wirklich eine Aufgabe“); die Datumslogik bleibt im Code. |
| Stundenplan (raumzeit.hka-iwi.de) | persönlicher iCal-Feed, direkt abgerufen (Mindestabstand 30 min), Änderungserkennung per UID | **Entscheidung:** Direkt statt über Google-Abo (dort stundenlange Verzögerung; Google-Kalender „Uni“ war veraltet). Verschoben/entfallen/neu → Change-Log → Planer + Bot. Umgesetzt: `connectors/ical_sync.py`. |
| Telegram | Bot-API (Long Polling) | Kein öffentlicher Port nötig, läuft hinter Heim-Router. |
| Google Calendar / Tasks / Gmail | Google API, OAuth (eigenes Cloud-Projekt `calendar-assistant`, Desktop-Client) | **Entscheidung:** App bleibt privat im Teststatus (kein öffentliches Branding nötig). Folge: Refresh-Token läuft alle 7 Tage ab → Re-Login alle 7 Tage; der Bot meldet per Telegram „Bitte neu einloggen“ und der Login wird in einer Minute erledigt. Nutzer 88045436+jvmyer@users.noreply.github.com ist als Testnutzer eingetragen. |
| Arbeitskalender | – | Später. |

## 5. Darstellung im Kalender

- **Mit Zeitfenster: nur der Blocker.** Ein Event mit Uhrzeit im Kalender „Planer“ (Titel = Aufgabe, Beschreibung mit Frist). Es gibt dann **keine** ganztägige Google Task daneben (Nutzerwunsch 2026-10-13): Die Tasks-API kann kein Zeitfenster tragen, die Task wäre immer ganztägig und würde den Eintrag verdoppeln.
- **Geprüft 2026-10-13 (Live-Test + offizielle Referenz): native „Aufgabe mit Zeitraum“ ist per API nicht anlegbar.** Google speichert so eine Aufgabe als Task (nur Datum; die Tasks-API verwirft die Uhrzeit und kann sie nicht lesen) plus einen von Google erzeugten Kalendereintrag vom Typ `focusTime` im Hauptkalender (Beschreibung verweist auf `tasks.google.com/task/<id>`). `focusTime`-Einträge lehnt die API für Privatkonten ab („not an enterprise account“), und ein normaler Termin mit Task-Link wird nicht zur Aufgabe. Bestehende Zeitblock-Aufgaben lassen sich per Kalender-API verschieben (wirkt auf die Task zurück), neu anlegen geht nicht.
- **Kein Abhaken** (Nutzerentscheidung 2026-10-13: „nur der Blocker im Kalender, kein Abhaken“): Den Blocker kann man im Kalender nicht abhaken (ein normaler Termin hat kein Kästchen). Deshalb gilt eine Aufgabe als **erledigt, sobald ihre geplante Zeit komplett verstrichen ist** und nichts mehr offen ist (`planning.close_elapsed`, Grund „Geplante Zeit verstrichen“; Aufgaben mit noch unverplantem Rest bleiben offen und haben die Task als Auffangnetz). Der Termin bleibt als Erinnerung im Kalender. Erledigen/Verwerfen vorzeitig geht weiter per Telegram (`/erledigt`, `/verwerfen`, Buttons). Probelauf (`--dry-run`) schließt nie etwas.
- **Ohne Zeitfenster: Google Task als Auffangnetz** (`gtasks.py`) – nichts darf untergehen. Sie ist ganztägig am Fälligkeitstag (ohne Deadline: heute), die Notiz sagt „ohne freien Slot“, der Nutzer wird einmal pro Tag erinnert. Hier hakt man in Google ab. Findet sich später Zeit, wird die Task durch einen Blocker ersetzt (und umgekehrt, wenn die Zeit wieder wegfällt).
  - Abhaken oder Löschen der Task in Google gilt als erledigt bzw. verworfen (der Mensch gewinnt). Wird die ganze Liste gelöscht, wird sie neu angelegt und alle offenen Aufgaben ohne Zeitfenster werden wiederhergestellt.
  - Titel, Notiz und Fälligkeit gibt der Planer vor (Deadline ändert man im Planer, nicht in Google Tasks).
- **Vorschläge („gut zu wissen“, `proposals.py`):** Dinge wie Barcamp, Helfer gesucht oder Infoveranstaltungen sind keine Aufgabe. Sie werden dem Nutzer nur gemeldet (später per Telegram mit Buttons) und kommen **ausschließlich nach Bestätigung** in den Kalender (`planner accept <Nr>`); unbestätigt oder verworfen passiert nie etwas. Bestätigte Termine liegen im Kalender „Planer“ und zählen als belegte Zeit.
- Grenze „Aufgabe vs. Vorschlag“ (für die spätere Mail-Extraktion): Aufgabe = jemand verlangt etwas von mir mit Frist oder Folgen (Abgabe, Antwort auf Anfrage, Pflicht-Anmeldung); Vorschlag = freiwillig oder reine Information.
- Erkennung von Blockern über Metadaten (`extendedProperties` am Event), damit das Tool nur eigene Events verändert.
- Blocker landen in einem **eigenen Nebenkalender „Planer“** im selben Google-Konto (gleiche Ansicht, eigene Farbe, ein-/ausblendbar). Der Hauptkalender wird nur gelesen.

## 6. Priorisierung

### Bereiche in beliebiger Tiefe

Bereiche sind ein Baum mit Gewichten, die sich nach unten vererben und überschrieben werden können:

```
Arbeit (1.0)
├── Projekt A (1.2)
└── Admin (0.6)
Uni (0.8)
├── Modul X (1.0)
│   └── Abgabe 3 (1.5)
└── Modul Y (0.7)
Privat (0.5)
```

Alles zur Laufzeit änderbar (per Bot oder Config-Datei).

### Score einer Aufgabe

```
score = Bereichsgewicht × Dringlichkeit(Deadline, Restaufwand) × manueller Faktor
```

- **Dringlichkeit** steigt, je weniger freie Zeit bis zur Deadline relativ zum Restaufwand bleibt (Slack-basiert, nicht nur Kalendertage).
- **Manueller Faktor**: z. B. „heute noch“ per Telegram → hart auf heute.

### Regeln (Constraints)

Konfigurierbar, z. B.: Arbeitszeitfenster, Ruhezeiten, max. Stunden pro Bereich und Tag, Puffer zwischen Terminen, minimale/maximale Blockgröße, Bereiche nur an bestimmten Tagen.

### Algorithmus (stufenweise)

1. **MVP (umgesetzt, `scheduler.py` + `planning.py`):** Greedy – Aufgaben nach Score sortieren, in frühestes passendes Zeitfenster legen, große Aufgaben in Blöcke splitten (min. 30 Min., max. 3 h pro Aufgabe und Tag).
   - Belegt = alle Kalender außer „Planer“, Feiertags-/Geburtstagskalendern und `ignore_calendars` (abonnierte ICS-Kalender wie der Uni-Kalender zählen als belegt); Ganztägiges, „Frei“-Termine und abgelehnte Einladungen zählen nicht.
   - Bereichsgewicht gilt direkt für den Knoten (Unterbereiche überschreiben, multiplizieren nicht).
   - Stabilität: Blocker in heute + morgen bleiben stehen, außer sie kollidieren mit einem neuen Termin oder die Aufgabe ist erledigt.
   - Mensch gewinnt: im Kalender **verschobener** Blocker → gepinnt; **gelöschter** Blocker → Zeitfenster wird nicht erneut belegt.
   - Bereits vergangene Blocker zählen als gearbeitet; die Aufgabe bleibt offen, bis sie als erledigt markiert wird (`planner done`).
   - Nicht unterbringbare Restzeit und überfällige Aufgaben werden gemeldet, danach einmal pro Tag erneut (nicht alle 30 Min.), bis sie gelöst sind.
   - Reihenfolge: Scores innerhalb von 0,25 gelten als gleich, dann entscheidet die frühere Deadline (sonst nimmt eine große Aufgabe mit ferner Deadline einer knappen die frühen Slots weg).
   - Regeln in `data/rules.toml` (Vorlage: `rules.example.toml`).
2. **Später:** Constraint-Solver (Google OR-Tools CP-SAT) für globale Optimierung mit Stabilitäts-Strafterm (Umplanen kostet Punkte).

## 7. Lernende Aufwandsschätzung

1. LLM schätzt den Aufwand beim Anlegen der Aufgabe.
2. Nach Erledigung fragt der Bot kurz: *„‚Bericht X‘: geschätzt 2 h – passt? [👍] [mehr] [weniger] [Zeit eingeben]“*
3. Pro Bereich/Aufgabentyp wird ein Korrekturfaktor gelernt (gleitender Mittelwert von tatsächlich/geschätzt).
4. Später: Regressionsmodell auf Merkmalen (Bereich, Quelle, Textlänge, Schlüsselwörter) und/oder ähnliche vergangene Aufgaben als Beispiele in den LLM-Prompt (Few-Shot).

## 8. Telegram-Bot

- **Eingabe:** „Morgen Bericht schreiben, ca. 2 h, Uni“ oder Sprachnachricht → Whisper → Extraktion.
- **Meldungen (kurz):** „➕ 2 neue Aufgaben aus Mail · 🔁 ‚Lernen Mathe‘ auf 14:00 vorgezogen (Meeting entfallen)“
- **Morgenübersicht:** Tagesplan in wenigen Zeilen.
- **Befehle (Entwurf):** `/heute`, `/log`, `/undo <id>`, `/falsch <id>` (Fehlerkennung → Lernsignal), `/prio`, `/pause`.

## 9. Hardware & Deployment

| Gerät | RAM | Rolle |
|---|---|---|
| Laptop (freies Gerät, Linux) | 16 GB | **Hauptsystem**, läuft 24/7 zu Hause: Connectors, Planer, Bot, Whisper (small/medium), lokales LLM (7–8B, 4-Bit-quantisiert, ca. 5–6 GB RAM). |
| Raspberry Pi 5 | 4 GB | Nicht im Kern. Optional: Backup-Ziel oder Monitoring. |
| Gemieteter Server | ~3 GB | Nicht im Kern. Optional: verschlüsselte Offsite-Backups. |

### Laptop als Server einrichten

- Linux ohne Desktop (Debian 12 oder Ubuntu Server LTS), Zugriff per SSH.
- Weiterlaufen bei geschlossenem Deckel (`HandleLidSwitch=ignore` in `logind.conf`), Suspend/Hibernate deaktivieren.
- Nach Stromausfall automatisch starten (BIOS: „Power on AC“, falls vorhanden; sonst überbrückt der Akku).
- Akku schonen: Ladegrenze 60–80 %, falls Hardware/BIOS das unterstützt.
- Automatische Sicherheitsupdates (`unattended-upgrades`), Firewall (`ufw`, nur SSH im LAN).
- Kein offener Port nach außen nötig (Telegram Long Polling, alle APIs ausgehend).

- Deployment via Docker Compose (x86_64).
- Lokales LLM via **Ollama** auf der CPU (bzw. GPU, falls vorhanden).
- Secrets (Passwörter, Tokens) in `.env` bzw. verschlüsselt, nie im Repo.

## 10. Tech-Stack (Vorschlag)

- **Python 3.12** (beste Bibliotheken für alle Teile)
- `google-api-python-client` (Calendar, Tasks), `imapclient` / `exchangelib`, `icalendar`
- `python-telegram-bot`, `faster-whisper`
- Ollama (z. B. Qwen 2.5 7B, Llama 3.1 8B, Mistral 7B – mit echten Mails vergleichen)
- SQLite + SQLAlchemy, APScheduler für Polling-Jobs
- später OR-Tools

## 11. MVP-Reihenfolge

1. **Grundgerüst:** Projektstruktur, Config, SQLite, Change-Log.
2. **Google-Anbindung:** Kalender lesen, Blocker + Tasks schreiben, Erledigt-Status lesen.
3. **Telegram-Bot (Text):** Aufgabe anlegen → Planer → Blocker im Kalender → Meldung.
4. **Planer v1 (Greedy)** mit Bereichsbaum und Basisregeln; Umplanung bei Kalenderänderungen.
5. **Sprache:** Whisper im Bot.
6. **ILIAS-iCal.**
7. **Mails + lokales LLM:** Gmail, dann HKA.
8. **Lernende Aufwandsschätzung.**
9. **Planer v2 (CP-SAT)**, weitere Quellen.

Nach Schritt 4 ist das Tool bereits im Alltag nutzbar.

## 12. Offene Punkte

- [x] Blocker in eigenem Nebenkalender „Planer“
- [ ] Lokales LLM: welches 7–8B-Modell extrahiert Aufgaben aus deutschen Mails am zuverlässigsten? → Test mit echten Mails
- [ ] Laptop: Modell/CPU, GPU?, Ladegrenze möglich?
- [x] HKA-Mail: kein IMAP → EWS über owa-cert.h-ka.de mit HARICA-Zertifikat
- [ ] HARICA-Nutzerzertifikat (Email-only reicht vermutlich) beantragen: cm.harica.gr → Academic Login
- [ ] ILIAS: Abo-URL im Kalender kopieren und Inhalt prüfen
- [x] Google-Cloud-Projekt angelegt (Calendar, Tasks, Gmail API aktiv, OAuth-Client in `.env`), bleibt im Teststatus (7-Tage-Login)
- [ ] Bot-Funktion „Token abgelaufen → Re-Login“ einplanen (Phase 3)
- [ ] Bereichsbaum und Regeln definieren (Mechanik steht: `planner area set`, `rules.toml`; konkrete Werte festlegen)
- [ ] ILIAS: Aufgaben/Assignments aus Kurs-Ankündigungen erkennen (regelmäßig auslesen; iCal liefert keine Abgabefristen)
- [ ] Name
- [ ] Arbeitskalender (später)
