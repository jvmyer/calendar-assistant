"""Schnellerfassung: eine Textnachricht → Aufgabe (ohne KI, nur feste Muster).

    "Bericht schreiben bis 20.10. 2h #Uni/Modul-X"
    "Zahnarzt anrufen bis morgen 16:00 15min"

Erkannt werden ein Termin, ein Aufwand (`2h`, `90min`, `1,5 h`) und ein Bereich mit `#`. Alles übrige ist
der Titel. Beim Termin zählt das „bis“ (Nutzer 2026-10-28: „morgen“ hieß bisher gar nichts):

- `bis [zum] <Tag> [um] [Uhrzeit]` (auch „Frist“, „fällig am“) → Frist, darf auch früher erledigt werden
- `[am] <Tag> [um] [Uhrzeit]` ohne „bis“ → an diesem Tag erledigen: frühestens dann, Frist am Tagesende

<Tag> = heute, morgen, übermorgen, [nächsten] Wochentag, 30.10.[2026] (ohne „am“/„bis“ nur mit Punkt am Ende).
Uhrzeit = 14:00, 14.00, 14 Uhr, 14:30 Uhr.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .parsing import parse_deadline, parse_estimate
from .rules import TZ

WEEKDAYS = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6}
MAX_TITLE = 200

_DAYWORD = r"heute|morgen|übermorgen|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag"
_TIME = r"(?:\s+(?:um\s+)?(?P<time>\d{1,2}[:.]\d{2}(?:\s*uhr)?|\d{1,2}\s*uhr))?"
_DEADLINE = re.compile(   # mit „bis“: Datum auch ohne Schlusspunkt („bis 30.10“)
    r"\b(?:bis|frist:?|fällig(?:\s+am)?|deadline:?)\s+(?:zum\s+|zu\s+|spätestens\s+)?(?:(?P<date>\d{1,2}\.\d{1,2}\.?(?:\d{4})?|\d{4}-\d{2}-\d{2})(?!\d)"
    r"|(?P<next>nächste[nm]?\s+|kommende[nm]?\s+)?(?P<word>" + _DAYWORD + r")\b)" + _TIME,
    re.IGNORECASE,
)
_ON_DAY = re.compile(     # ohne „bis“: Datum nur mit Schlusspunkt oder nach „am“ (sonst wäre „Kapitel 3.2“ ein Datum)
    r"(?<![\w.])(?:(?P<am>am\s+)?(?:(?P<date>\d{1,2}\.\d{1,2}\.(?:\d{4})?)(?!\d)"
    r"|(?P<next>nächste[nm]?\s+|kommende[nm]?\s+)?(?P<word>" + _DAYWORD + r")\b))" + _TIME,
    re.IGNORECASE,
)
_LOOSE_TIME = re.compile(   # Uhrzeit irgendwo im Satz („morgen Zahnarzt anrufen um 14 Uhr“)
    r"(?:\bum\s+)?\b(?P<time>\d{1,2}(?:[:.]\d{2})?\s*uhr)\b|\bum\s+(?P<time2>\d{1,2}[:.]\d{2})\b", re.IGNORECASE)
_ESTIMATE = re.compile(r"(?:\b(?:ca\.?|circa|ungefähr)\s*)?\b(\d+(?:[.,]\d+)?)\s*(h|std|min|m)\b", re.IGNORECASE)
_AREA = re.compile(r"(?:^|\s)#([^\s#]+)")


@dataclass
class QuickTask:
    title: str
    deadline: str | None = None
    estimate_min: int | None = None
    area: str | None = None
    not_before: str | None = None   # frühester Tag (ISO-Datum), nur bei „morgen“/„am Freitag“ ohne „bis“


def _word_to_date(word: str, today: date) -> date:
    word = word.lower()
    if word == "heute":
        return today
    if word == "morgen":
        return today + timedelta(days=1)
    if word == "übermorgen":
        return today + timedelta(days=2)
    return today + timedelta(days=(WEEKDAYS[word] - today.weekday()) % 7 or 7)   # „Freitag“ am Freitag = nächste Woche


def _when(m: re.Match, today: date) -> tuple[date, str | None]:
    """Treffer → (Tag, Uhrzeit HH:MM oder None)."""
    if m.group("date"):
        raw = m.group("date")
        if not raw.endswith(".") and "-" not in raw and raw.count(".") == 1:
            raw += "."
        day = date.fromisoformat(parse_deadline(raw, today=today)[:10])
    else:
        day = _word_to_date(m.group("word"), today)
    return day, (_clock(t) if (t := m.group("time")) else None)


def _clock(raw: str) -> str | None:
    h, _, mi = re.sub(r"\s*uhr$", "", raw.strip(), flags=re.IGNORECASE).replace(".", ":").partition(":")
    return f"{int(h):02d}:{int(mi or 0):02d}" if int(h) < 24 and int(mi or 0) < 60 else None


def parse_when(text: str, *, today: date) -> tuple[str | None, str | None, list[tuple[int, int]]]:
    """Termin im Text → (Frist, frühester Tag, Fundstellen). Frist ist ein reines Datum oder ISO mit Uhrzeit."""
    m = _DEADLINE.search(text)
    on_day = m is None
    if on_day:
        m = _ON_DAY.search(text)
        if m is None:
            return None, None, []
    try:
        day, clock = _when(m, today)
    except ValueError:   # 31.02. o. Ä.: nicht raten
        return None, None, []
    spans = [m.span()]
    rest = text[: m.start()] + " " * (m.end() - m.start()) + text[m.end():]
    if clock is None and (t := _LOOSE_TIME.search(rest)) and (clock := _clock(t.group("time") or t.group("time2"))):
        spans.append(t.span())
    deadline = day.isoformat() if clock is None else \
        datetime.combine(day, datetime.strptime(clock, "%H:%M").time(), TZ).isoformat(timespec="minutes")
    return deadline, (day.isoformat() if on_day else None), spans


def parse_quick(text: str, *, today: date | None = None) -> QuickTask:
    today = today or datetime.now(TZ).date()
    work = text.strip()
    task = QuickTask(title="")

    if m := _AREA.search(work):
        task.area = m.group(1)
        work = work[: m.start()] + " " + work[m.end():]

    task.deadline, task.not_before, spans = parse_when(work, today=today)
    for a, b in sorted(spans, reverse=True):
        work = work[:a] + " " + work[b:]

    if m := _ESTIMATE.search(work):
        task.estimate_min = parse_estimate(f"{m.group(1)}{m.group(2)}")
        work = work[: m.start()] + " " + work[m.end():]

    task.title = re.sub(r"\s+", " ", work).strip(" ,;:-–")[:MAX_TITLE]
    if not task.title:
        raise ValueError("Ich habe keinen Titel erkannt. Beispiel: Bericht schreiben bis 20.10. 2h #Uni")
    return task
