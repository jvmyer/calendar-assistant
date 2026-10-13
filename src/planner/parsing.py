"""Eingaben des Nutzers (Aufwand, Deadline) lesen und gespeicherte Deadlines auswerten."""

from __future__ import annotations

import re
from datetime import date, datetime, time

from .rules import TZ

_EST = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(h|std|m|min)?\s*$", re.IGNORECASE)
_DE_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(?:(\d{4}))?(?:\s+(\d{1,2}):(\d{2}))?$")


def parse_estimate(text: str) -> int:
    """'90', '90m', '2h', '1,5h' → Minuten (ohne Einheit: Minuten)."""
    m = _EST.match(text)
    if not m:
        raise ValueError(f"Aufwand nicht lesbar: {text!r} (Beispiele: 90, 45m, 2h, 1,5h)")
    value = float(m.group(1).replace(",", "."))
    minutes = round(value * 60) if (m.group(2) or "").lower() in {"h", "std"} else round(value)
    if minutes <= 0:
        raise ValueError("Aufwand muss > 0 sein")
    return minutes


def parse_deadline(text: str, *, today: date | None = None) -> str:
    """Nimmt '2026-10-20', '2026-10-20 18:00', '20.10.' oder '20.10.2026 18:00'.

    Ergebnis ist der gespeicherte String: reines Datum (gilt bis Tagesende) oder ISO mit Offset.
    """
    text = text.strip()
    today = today or datetime.now(TZ).date()
    m = _DE_DATE.match(text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        d = date(year, month, day)
        if not m.group(3) and d < today:   # ohne Jahr: nächstes Vorkommen
            d = date(year + 1, month, day)
        if m.group(4):
            return datetime.combine(d, time(int(m.group(4)), int(m.group(5))), TZ).isoformat(timespec="minutes")
        return d.isoformat()
    try:
        if len(text) == 10:
            return date.fromisoformat(text).isoformat()
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"Deadline nicht lesbar: {text!r} (Beispiele: 2026-10-20, 20.10., 20.10. 18:00)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.isoformat(timespec="minutes")


def deadline_to_dt(value: str) -> datetime:
    """Gespeicherte Deadline → aware datetime. Reines Datum = Ende dieses Tages (23:59)."""
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time(23, 59), TZ)
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
