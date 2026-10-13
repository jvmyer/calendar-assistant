"""Kurs ↔ Stundenplan: „bis zur nächsten Stunde“ in einen Zeitpunkt umrechnen."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from .rules import TZ

_STOP = {"und", "and", "der", "die", "das", "ws", "ss", "wise", "sose", "kurs", "mit", "für", "fuer", "the",
         "wintersemester", "sommersemester"}
MIN_SCORE = 0.66


def tokens(name: str) -> list[str]:
    """Bedeutungstragende Wörter: klein, ohne Füllwörter/Semesterangaben/Zahlen."""
    words = re.findall(r"[a-zäöüß]+", name.lower().replace("&", " und "))
    return [w for w in words if w not in _STOP and len(w) >= 3]


def _same(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))


def match_score(event_title: str, course_name: str) -> float:
    """Anteil der Wörter des Stundenplan-Titels, die im Kursnamen vorkommen (0…1)."""
    ev, co = tokens(event_title), tokens(course_name)
    if not ev or not co:
        return 0.0
    return sum(1 for e in ev if any(_same(e, c) for c in co)) / len(ev)


def clean_course_name(name: str) -> str:
    """„Projektmanagement und Methodik WS 2026_27“ → ohne Semesterangabe."""
    name = re.sub(r"\b(?:WS|SS|WiSe|SoSe)\s*\d{2,4}(?:[/_-]\d{2,4})?\b", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"\s\d{4}[/_-]\d{2,4}\s*$", " ", name)
    return re.sub(r"\s+", " ", name).strip(" -–")


def next_session(conn: sqlite3.Connection, course_name: str, after: datetime) -> sqlite3.Row | None:
    """Nächster Stundenplan-Termin des Kurses nach `after`; None, wenn der Kurs nicht eindeutig zuzuordnen ist."""
    if not course_name:
        return None
    after = after.astimezone(TZ)
    rows = [r for r in conn.execute(
        "SELECT * FROM external_events WHERE source = 'raumzeit' AND state = 'active' AND all_day = 0")
        if datetime.fromisoformat(r["start"]).astimezone(TZ) > after]
    scored = [(match_score(r["title"], course_name), r) for r in rows]
    best = max((s for s, _ in scored), default=0.0)
    if best < MIN_SCORE:
        return None
    candidates = [r for s, r in scored if s >= best - 1e-9]
    if len({r["title"] for r in candidates}) > 1:
        return None   # mehrdeutig (zwei verschiedene Veranstaltungen passen gleich gut)
    return min(candidates, key=lambda r: datetime.fromisoformat(r["start"]))
