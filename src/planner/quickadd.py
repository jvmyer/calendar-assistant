"""Schnellerfassung: eine Textnachricht → Aufgabe (ohne KI, nur feste Muster).

    "Bericht schreiben bis 20.10. 2h #Uni/Modul-X"
    "Zahnarzt anrufen bis morgen 16:00 15min"

Erkannt werden `bis <Datum|heute|morgen|übermorgen|Wochentag> [Uhrzeit]`, ein Aufwand
(`2h`, `90min`, `1,5 h`) und ein Bereich mit `#`. Alles übrige ist der Titel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .parsing import parse_deadline, parse_estimate
from .rules import TZ

WEEKDAYS = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6}
MAX_TITLE = 200

_DEADLINE = re.compile(
    r"\bbis\s+(?:(?P<date>\d{1,2}\.\d{1,2}\.(?:\d{4})?|\d{4}-\d{2}-\d{2})"
    r"|(?P<word>heute|morgen|übermorgen|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag))"
    r"(?:\s+(?:um\s+)?(?P<time>\d{1,2}[:.]\d{2})(?:\s*uhr)?)?",
    re.IGNORECASE,
)
_ESTIMATE = re.compile(r"(?:\b(?:ca\.?|circa|ungefähr)\s*)?\b(\d+(?:[.,]\d+)?)\s*(h|std|min|m)\b", re.IGNORECASE)
_AREA = re.compile(r"(?:^|\s)#([^\s#]+)")


@dataclass
class QuickTask:
    title: str
    deadline: str | None = None
    estimate_min: int | None = None
    area: str | None = None


def _word_to_date(word: str, today: date) -> date:
    word = word.lower()
    if word == "heute":
        return today
    if word == "morgen":
        return today + timedelta(days=1)
    if word == "übermorgen":
        return today + timedelta(days=2)
    return today + timedelta(days=(WEEKDAYS[word] - today.weekday()) % 7)   # heute eingeschlossen


def parse_quick(text: str, *, today: date | None = None) -> QuickTask:
    from datetime import datetime

    today = today or datetime.now(TZ).date()
    work = text.strip()
    task = QuickTask(title="")

    if m := _AREA.search(work):
        task.area = m.group(1)
        work = work[: m.start()] + " " + work[m.end():]

    if m := _DEADLINE.search(work):
        when = m.group("date") or _word_to_date(m.group("word"), today).isoformat()
        if m.group("time"):
            when += " " + m.group("time").replace(".", ":")
        task.deadline = parse_deadline(when, today=today)
        work = work[: m.start()] + " " + work[m.end():]

    if m := _ESTIMATE.search(work):
        task.estimate_min = parse_estimate(f"{m.group(1)}{m.group(2)}")
        work = work[: m.start()] + " " + work[m.end():]

    task.title = re.sub(r"\s+", " ", work).strip(" ,;:-–")[:MAX_TITLE]
    if not task.title:
        raise ValueError("Ich habe keinen Titel erkannt. Beispiel: Bericht schreiben bis 20.10. 2h #Uni")
    return task
