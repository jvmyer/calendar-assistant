---
name: vorrat-auffuellen
description: Legt per Claude in Chrome neue „Aufgaben mit Zeitraum“ (Titel „Vorrat“) in Google Kalender an und holt sie in die Liste „Planer-Vorrat“, damit der Planer jedem Blocker ein Abhak-Kästchen geben kann. Trigger: „Vorrat auffüllen“, Telegram-Meldung „Vorrat knapp“, /vorrat-auffuellen.
---

# Vorrat auffüllen

Hintergrund: Google lässt Aufgaben **mit Uhrzeit** nicht per API anlegen, nur über die Oberfläche
(docs/KONZEPT.md §5). Der Planer verwendet einen festen Vorrat solcher Aufgaben immer wieder (`src/planner/pool.py`).
Wird er knapp, meldet sich der Bot per Telegram. Dann gilt:

1. **Wie viele?** Ohne Angabe des Nutzers so viele anlegen, dass wieder **30** frei sind. Die Telegram-Meldung
   nennt die freie Zahl („noch X frei“), sonst einfach 15 anlegen.
2. **Chrome** (Skill `anthropic-skills:chrome-browser` laden; Werkzeuge in einem ToolSearch-Aufruf holen;
   neuer Tab): `https://calendar.google.com/calendar/r/day` öffnen. Pro Aufgabe:
   - „Eintragen“ (oben links) → „Aufgabe“
   - Titel exakt `Vorrat` tippen → **Enter** speichert.
   - Datum/Uhrzeit egal (der Planer parkt sie). **Liste nicht auswählen** – das Dropdown verrutscht mit der
     Fenstergröße; Schritt 3 sortiert alles richtig ein.
   - Mehrere Aufgaben mit `browser_batch` hintereinander (je ~6 Schritte, 1–2 s Wartezeit dazwischen).
   - Vorher und am Ende einen Screenshot: Steht unten „Aufgabe erstellt“?
3. **Einsammeln** (lokal auf dem Mac, nur Google-API, keine Datenbank):
   `uv run planner vorrat-einsammeln` → verschiebt alle offenen Tasks namens „Vorrat“ in „Planer-Vorrat“.
   Der Zeitblock bleibt dabei erhalten.
4. **Prüfen**: Die Liste „Planer-Vorrat“ zählen (Tasks-API). Der Planer-Dienst auf dem Server übernimmt die neuen
   Aufgaben beim nächsten Lauf (≤ 30 Min.) und parkt sie am 01.01.2030 – dafür ist kein Server-Zugriff nötig.
5. Tab schließen, dem Nutzer kurz die Zahl melden.

Regeln:
- Nur Aufgaben mit Titel „Vorrat“ anlegen, nichts anderes im Kalender anfassen.
- Keine Titel bestehender Termine/Aufgaben ausgeben (Datenschutz: manche stammen aus Mails).
- Klappt etwas zweimal nicht (Dialog öffnet nicht, Zahl stimmt nicht): anhalten und den Nutzer fragen.
