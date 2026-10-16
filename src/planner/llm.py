"""Lokales Sprachmodell (Ollama auf dem Server) – liest Mails und entscheidet: Aufgabe, Vorschlag, nichts.

Datenschutz: Mailinhalte gehen nur an Ollama auf demselben Rechner (127.0.0.1), nie ins Netz.

Das Modell rechnet keine Daten aus (kleine Modelle verrechnen sich bei „bis Sonntag“): es kopiert die
Frist-Stelle wörtlich (`deadline_text`), das Datum bestimmt `deadlines.resolve_phrase` im Code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import httpx

KINDS = ("aufgabe", "vorschlag", "nichts")
MAX_BODY = 3000   # Zeichen; längere Mails werden gekürzt (Kontext des kleinen Modells)
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
effort_min: geschätzter Aufwand in Minuten (bei "nichts" 0)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "title": {"type": "string"},
        "deadline_text": {"type": "string"},
        "effort_min": {"type": "integer"},
    },
    "required": ["kind", "title", "deadline_text", "effort_min"],
}


class LlmError(Exception):
    """Modell nicht erreichbar oder Antwort unbrauchbar – später erneut versuchen."""


@dataclass(frozen=True)
class MailVerdict:
    kind: str            # aufgabe | vorschlag | nichts
    title: str
    deadline_text: str
    effort_min: int | None


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
    )


class OllamaClient:
    def __init__(self, url: str, model: str, *, timeout: float = 600, client: httpx.Client | None = None):
        self._url, self._model = url.rstrip("/"), model
        self._http = client or httpx.Client(timeout=timeout)

    def classify_mail(self, sender: str, subject: str, body: str, *, today: date) -> MailVerdict:
        payload = {
            "model": self._model, "stream": False, "think": False, "format": SCHEMA,
            "options": {"temperature": 0, "num_ctx": 4096},
            "messages": [
                {"role": "system", "content": SYSTEM.format(today=today_text(today))},
                {"role": "user", "content": f"Von: {sender}\nBetreff: {subject}\n\n{body[:MAX_BODY]}"},
            ],
        }
        try:
            resp = self._http.post(f"{self._url}/api/chat", json=payload)
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise LlmError(type(exc).__name__) from exc
        return parse_verdict(content)

    def close(self) -> None:
        self._http.close()
