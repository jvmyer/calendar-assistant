"""Lokales Sprachmodell (Ollama auf dem Server) – liest Mails und entscheidet: Aufgabe, Vorschlag, nichts.

Datenschutz: Mailinhalte gehen nur an Ollama auf demselben Rechner (127.0.0.1), nie ins Netz.

Das Modell rechnet keine Daten aus (kleine Modelle verrechnen sich bei „bis Sonntag“): es kopiert die
Frist-Stelle wörtlich (`deadline_text`), das Datum bestimmt `deadlines.resolve_phrase` im Code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

import httpx

KINDS = ("aufgabe", "vorschlag", "nichts")
PRIOS = ("hoch", "mittel", "niedrig")
MAX_BODY = 3000   # Zeichen; längere Mails werden gekürzt (Kontext des kleinen Modells)
# Auf dem Acer erkennt Ollama den Grafikchip im Prozessor als „GPU“; damit ist es 4× langsamer als nur auf
# der CPU (gemessen 2026-10-16: 165 s statt 41 s für eine lange Mail). Deshalb fest: keine GPU.
OPTIONS = {"temperature": 0, "num_ctx": 4096, "num_gpu": 0}
WEEKDAYS_DE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")

SYSTEM = """Du liest E-Mails eines Studenten (Jonah) und entscheidest, ob daraus eine Aufgabe für seinen Kalender folgt.
Heute ist {today}.
Die meisten Mails sind KEINE Aufgabe. Eine Aufgabe ist nur, was Jonah selbst aktiv erledigen muss.
Beantworte die Felder der Reihe nach:
handlung: Was genau muss Jonah SELBST tun? Ein konkreter Schritt mit Verb (z. B. "Formular ausfüllen und hochladen", "Lisa die Folien schicken", "Semesterbeitrag überweisen"). Leer lassen, wenn es nichts zu tun gibt außer lesen, zur Kenntnis nehmen, sich etwas merken oder ggf. hingehen.
pflicht: true nur, wenn Jonah Nachteile hat, falls er es nicht tut (Frist verpasst, Prüfung, Geld, Exmatrikulation, jemand wartet persönlich auf seine Antwort). Sonst false.
kind:
- "aufgabe": handlung ist nicht leer UND pflicht ist true.
- "vorschlag": freiwillige Veranstaltung oder Angebot, das ihn interessieren könnte (Vortrag, Workshop, Party, Exkursion, Job-Angebot).
- "nichts": alles andere.
Immer "nichts" sind zum Beispiel: Rundmails mit Informationen (Raumänderung, Ausfall, neue Regeln, Öffnungszeiten, Ankündigungen), Newsletter, Werbung, Bestätigungen (Bestellung, Anmeldung, Zahlung eingegangen), Versand- und Systembenachrichtigungen, Sicherheitshinweise, Rechnungen, die automatisch abgebucht werden, Erinnerungen an Termine, die schon feststehen, und Mails, in denen andere etwas tun müssen.
Eine Rundmail an viele ist nur dann "aufgabe", wenn darin eine Pflicht für jeden Einzelnen mit Frist steht (z. B. "Rückmeldung bis 15.01.", "Prüfungsanmeldung bis 13.11.").
Im Zweifel: "nichts" bzw. bei Angeboten "vorschlag".
title: kurzer Titel auf Deutsch (max. 6 Wörter), was zu tun ist; bei "nichts" leer.
deadline_text: die Stelle aus der Mail, die die Frist nennt, WÖRTLICH kopiert (z. B. "bis Sonntagabend", "bis zum 29.01.2027", "vom 02.11. bis 13.11.2026"). Nichts umrechnen. Keine Frist → leerer Text.
effort_min: geschätzter Aufwand in Minuten (bei "nichts" 0).
priority: "hoch" (große Folgen bei Versäumnis, z. B. Prüfung, Zahlung, Exmatrikulation, knappe Frist), "mittel" (normale Aufgabe), "niedrig" (kann warten, kaum Folgen).
Falls vorher Beispiele kommen: Das sind frühere Mails mit Jonahs eigener Einschätzung. Richte dich danach, besonders bei gleichen Absendern."""

# Reihenfolge zählt: das Modell füllt die Felder nacheinander aus, erst Handlung/Pflicht, dann die Art.
SCHEMA = {
    "type": "object",
    "properties": {
        "handlung": {"type": "string"},
        "pflicht": {"type": "boolean"},
        "kind": {"type": "string", "enum": list(KINDS)},
        "title": {"type": "string"},
        "deadline_text": {"type": "string"},
        "effort_min": {"type": "integer"},
        "priority": {"type": "string", "enum": ["hoch", "mittel", "niedrig"]},
    },
    "required": ["handlung", "pflicht", "kind", "title", "deadline_text", "effort_min", "priority"],
}


SPOKEN = """Jonah hat seinem Planer eine Sprachnachricht geschickt (automatisch transkribiert, kann Hörfehler enthalten).
Heute ist {today}. Mach daraus genau eine Aufgabe.
title: WAS zu tun ist, 1 bis 5 Wörter, wie ein Eintrag im Kalender: Sache + Tätigkeit im Infinitiv ("Mama anrufen", "Übungsblatt 3 abgeben") oder nur die Sache bei Terminen ("Zahnarzt", "Kaffee mit Familie").
Der Titel enthält NIE: Datum, Wochentag, Uhrzeit, Dauer, Bereich (Uni, Arbeit, privat), Wichtigkeit, Füllwörter (ähm, also, okay, noch, mal, bitte) und Wörter an den Planer (anlegen, eintragen, notieren, erinnern, Termin, Aufgabe, ich muss, kannst du).
Offensichtliche Hörfehler im Titel korrigieren ("Café mit Familie" → "Kaffee mit Familie").
Beispiele:
"Ähm trag mal ein, morgen um 9 Steuerberater anrufen, ist wichtig" → "Steuerberater anrufen"
"Ich muss bis Freitag noch das Übungsblatt für Mathe abgeben, so zwei Stunden" → "Mathe-Übungsblatt abgeben"
"Kaffee mit Oma am Sonntag von 15 bis 17 Uhr anlegen" → "Kaffee mit Oma"
"Erinnere mich, dass ich nächste Woche die Präsentation für die Arbeit vorbereite" → "Präsentation vorbereiten"
deadline_text: die Stelle mit Tag oder Frist, WÖRTLICH aus der Nachricht kopiert (z. B. "bis Freitag", "morgen", "am 20.10."), sonst leer. Nichts umrechnen, nichts erfinden.
effort_min: genannte Dauer in Minuten ("zwei Stunden" = 120); ohne Angabe eine vernünftige Schätzung.
area: NUR wenn Jonah den Bereich ausdrücklich nennt (z. B. "für die Uni", "privat", "Arbeit"), der passende aus dieser Liste; sonst leer – nicht raten: {areas}"""


@dataclass(frozen=True)
class SpokenTask:
    title: str
    deadline_text: str
    effort_min: int | None
    area: str


class LlmError(Exception):
    """Modell nicht erreichbar oder Antwort unbrauchbar – später erneut versuchen."""


@dataclass(frozen=True)
class MailVerdict:
    kind: str            # aufgabe | vorschlag | nichts
    title: str
    deadline_text: str
    effort_min: int | None
    priority: str = "mittel"   # hoch | mittel | niedrig


def strip_quotes(body: str) -> str:
    """Zitierte Zeilen („> …“) weg, Leerzeilen-Blöcke zusammenziehen – spart Rechenzeit."""
    lines = [ln.rstrip() for ln in body.replace("\r", "").split("\n") if not ln.lstrip().startswith(">")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def today_text(d: date) -> str:
    return f"{WEEKDAYS_DE[d.weekday()]}, {d:%d.%m.%Y}"


def parse_verdict(raw: str) -> MailVerdict:
    try:
        data = json.loads(raw)
        kind = str(data["kind"]).strip().lower()
    except (ValueError, KeyError, TypeError) as exc:
        raise LlmError(f"Antwort unbrauchbar: {type(exc).__name__}") from exc
    if kind not in KINDS:
        raise LlmError(f"unbekannte Art: {kind[:20]}")
    # Kleine Modelle sagen gern „aufgabe“, obwohl sie selbst keine Handlung/Pflicht sehen – dann gilt die Prüfung.
    action = str(data.get("handlung") or "").strip()
    if kind == "aufgabe" and not (action and data.get("pflicht") is True):
        kind = "vorschlag" if action else "nichts"
    try:
        effort = int(data.get("effort_min") or 0)
    except (ValueError, TypeError):
        effort = 0
    return MailVerdict(
        kind=kind,
        title=str(data.get("title") or "").strip()[:120],
        deadline_text=str(data.get("deadline_text") or "").strip()[:200],
        effort_min=effort if 0 < effort <= 24 * 60 else None,
        priority=prio if (prio := str(data.get("priority") or "").strip().lower()) in PRIOS else "mittel",
    )


class OllamaClient:
    def __init__(self, url: str, model: str, *, timeout: float = 600, client: httpx.Client | None = None):
        self._url, self._model = url.rstrip("/"), model
        self._http = client or httpx.Client(timeout=timeout)

    def classify_mail(self, sender: str, subject: str, body: str, *, today: date,
                      examples: list[dict] | None = None) -> MailVerdict:
        """`examples`: frühere Mails mit der Einschätzung des Nutzers (`feedback.example_messages`)."""
        payload = {
            "model": self._model, "stream": False, "think": False, "format": SCHEMA,
            "options": OPTIONS,
            "messages": [
                {"role": "system", "content": SYSTEM.format(today=today_text(today))},
                *(examples or []),
                {"role": "user", "content": f"Von: {sender}\nBetreff: {subject}\n\n{strip_quotes(body)[:MAX_BODY]}"},
            ],
        }
        try:
            resp = self._http.post(f"{self._url}/api/chat", json=payload)
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise LlmError(type(exc).__name__) from exc
        return parse_verdict(content)

    def extract_spoken_task(self, text: str, *, today: date, areas: list[str]) -> SpokenTask:
        """Aufgabe aus einer (transkribierten) Sprachnachricht. `areas`: vorhandene Bereiche (z. B. „Uni/Mathe“)."""
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "deadline_text": {"type": "string"},
                "effort_min": {"type": "integer"},
                "area": {"type": "string", "enum": ["", *areas]},
            },
            "required": ["title", "deadline_text", "effort_min", "area"],
        }
        system = SPOKEN.format(today=today_text(today), areas=", ".join(areas) or "(keine)")
        content = self._chat(system, text[:MAX_BODY], schema)
        try:
            data = json.loads(content)
            title = str(data["title"]).strip()[:120]
        except (ValueError, KeyError, TypeError) as exc:
            raise LlmError(f"Antwort unbrauchbar: {type(exc).__name__}") from exc
        if not title:
            raise LlmError("kein Titel")
        try:
            effort = int(data.get("effort_min") or 0)
        except (ValueError, TypeError):
            effort = 0
        area = str(data.get("area") or "")
        return SpokenTask(title=title, deadline_text=str(data.get("deadline_text") or "").strip()[:200],
                          effort_min=effort if 0 < effort <= 24 * 60 else None, area=area if area in areas else "")

    def _chat(self, system: str, user: str, schema: dict) -> str:
        payload = {"model": self._model, "stream": False, "think": False, "format": schema, "options": OPTIONS,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            resp = self._http.post(f"{self._url}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json()["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise LlmError(type(exc).__name__) from exc

    def close(self) -> None:
        self._http.close()
