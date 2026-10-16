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

SYSTEM = """Du liest E-Mails eines Studenten (Jonah) und entscheidest, ob daraus etwas für seinen Kalender folgt.
Heute ist {today}.
kind:
- "aufgabe": Jemand verlangt etwas von Jonah, mit Frist oder Folgen (Abgabe, Antwort auf Anfrage, Pflicht-Anmeldung, Zahlung).
- "vorschlag": freiwillige Veranstaltung oder Angebot, das ihn interessieren könnte.
- "nichts": reine Information, Newsletter, Werbung, Bestätigung ohne To-do.
Wichtig: Eine Pflicht (Anmeldung, Zahlung, Rückmeldung, Antwort), ohne die Jonah Nachteile hat, ist immer "aufgabe" – auch wenn sie sachlich formuliert ist. Bittet ihn jemand persönlich um eine Antwort, Rückmeldung, Terminvorschläge oder Unterlagen, ist es "aufgabe". Im Zweifel zwischen "aufgabe" und "nichts": "aufgabe".
Ist etwas freiwillig ("kostenlos", "freiwillig", "kommt vorbei"), ist es "vorschlag", nicht "nichts".
title: kurzer Titel auf Deutsch (max. 6 Wörter), was zu tun ist; bei "nichts" leer.
deadline_text: die Stelle aus der Mail, die die Frist nennt, WÖRTLICH kopiert (z. B. "bis Sonntagabend", "bis zum 29.01.2027", "vom 02.11. bis 13.11.2026"). Nichts umrechnen. Keine Frist → leerer Text.
effort_min: geschätzter Aufwand in Minuten (bei "nichts" 0).
priority: "hoch" (große Folgen bei Versäumnis, z. B. Prüfung, Zahlung, Exmatrikulation, knappe Frist), "mittel" (normale Aufgabe), "niedrig" (kann warten, kaum Folgen).
Falls vorher Beispiele kommen: Das sind frühere Mails mit Jonahs eigener Einschätzung. Richte dich danach, besonders bei gleichen Absendern."""

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "title": {"type": "string"},
        "deadline_text": {"type": "string"},
        "effort_min": {"type": "integer"},
        "priority": {"type": "string", "enum": ["hoch", "mittel", "niedrig"]},
    },
    "required": ["kind", "title", "deadline_text", "effort_min", "priority"],
}


SPOKEN = """Jonah hat seinem Planer eine Sprachnachricht geschickt (automatisch transkribiert, kann Hörfehler enthalten).
Heute ist {today}. Mach daraus genau eine Aufgabe.
title: kurzer Titel auf Deutsch (max. 6 Wörter), was zu tun ist.
deadline_text: die Stelle, die die Frist nennt, WÖRTLICH kopiert (z. B. "bis Freitag", "bis zum 20.10."), sonst leer. Nichts umrechnen.
effort_min: genannter oder geschätzter Aufwand in Minuten ("zwei Stunden" = 120); ohne Hinweis eine vernünftige Schätzung.
priority: "hoch", "mittel" oder "niedrig" – wie Jonah es sagt ("wichtig", "dringend" = hoch; "irgendwann", "nicht so wichtig" = niedrig), sonst "mittel".
area: passender Bereich aus dieser Liste, sonst leer: {areas}"""


@dataclass(frozen=True)
class SpokenTask:
    title: str
    deadline_text: str
    effort_min: int | None
    priority: str
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
                "priority": {"type": "string", "enum": list(PRIOS)},
                "area": {"type": "string", "enum": ["", *areas]},
            },
            "required": ["title", "deadline_text", "effort_min", "priority", "area"],
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
        prio = str(data.get("priority") or "").lower()
        area = str(data.get("area") or "")
        return SpokenTask(title=title, deadline_text=str(data.get("deadline_text") or "").strip()[:200],
                          effort_min=effort if 0 < effort <= 24 * 60 else None,
                          priority=prio if prio in PRIOS else "mittel", area=area if area in areas else "")

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
