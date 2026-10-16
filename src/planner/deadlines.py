"""Fristen in Texten finden (Deutsch/Englisch), regelbasiert und nachvollziehbar.

Jede Frist trägt die Textstelle (`evidence`), aus der sie stammt. Erkannt werden:

- Absolute Daten in der Nähe eines Abgabe-Hinweises: „Abgabe bis spätestens 12.10. 19 Uhr“,
  „Abgabe 2: Thema (8.10.)“, Tabellenzeilen, „Deadline: 3. November 2026“, „due Oct 12“
- „bis <Datum>“ / „by <date>“ in einem Satz mit einer Aufgabe („Formular bis 12.10. ausfüllen“)
- Relatives: „bis morgen“, „bis Freitag“, „bis zur nächsten Stunde/Vorlesung/Übung“ – die
  nächste Stunde löst der Aufrufer über den Stundenplan auf (`relative == "next_session"`).

Plausibilitätsprüfung: Liegt ein Datum in der Vergangenheit (typisch: Plan aus dem Vorsemester),
ist `past` gesetzt und der Aufrufer trägt es nicht ein.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .rules import TZ

MAX_RESULTS = 12
MONTHS = {
    "januar": 1, "jan": 1, "january": 1, "februar": 2, "feb": 2, "february": 2, "märz": 3, "mär": 3, "maerz": 3,
    "mrz": 3, "march": 3, "mar": 3, "april": 4, "apr": 4, "mai": 5, "may": 5, "juni": 6, "jun": 6, "june": 6,
    "juli": 7, "jul": 7, "july": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10,
    "okt": 10, "october": 10, "oct": 10, "november": 11, "nov": 11, "dezember": 12, "dez": 12, "december": 12,
    "dec": 12,
}
WEEKDAYS = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
_WD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))

# -- Datumsmuster (jeweils mit benannten Gruppen d, m|mon, y) -------------------------------------
_DATE_PATTERNS = [
    re.compile(r"(?<![\d.])(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?!\d)"),
    re.compile(r"(?<![\d.])(?P<d>\d{1,2})\.\s?(?P<m>\d{1,2})\.(?:(?P<y>\d{4}|\d{2})(?!\d))?"),
    re.compile(rf"(?<![\d.])(?P<d>\d{{1,2}})\.?\s*(?P<mon>{_MON})\b\.?(?:\s*(?P<y>\d{{4}}))?", re.IGNORECASE),
    re.compile(rf"\b(?P<mon>{_MON})\b\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s*(?P<y>\d{{4}}))?", re.IGNORECASE),
]
# -- Uhrzeit ---------------------------------------------------------------------------------------
_T = r"(?P<h>[01]?\d|2[0-3])(?:(?::(?P<mi>[0-5]\d))|(?:\.(?P<mi2>[0-5]\d)(?=\s*Uhr)))?\s*(?P<u>Uhr|h\b|am\b|pm\b)?"
_TIME_AFTER = re.compile(rf"^[\s,;:(–-]*(?:um|at|ab|bis|by|gegen)?\s*{_T}", re.IGNORECASE)
_TIME_BEFORE = re.compile(rf"(?:^|[\s,(])(?:bis|um|at|by)?\s*{_T}\s*(?:am|on|,)?\s*$", re.IGNORECASE)

# -- Hinweise ------------------------------------------------------------------------------------------
_CUE = re.compile(
    r"abgabe\w*|abgeb\w*|abzugeben|einreich\w*|hochlad\w*|upload\w*|deadline|submi\w+|hand[\s-]?in\b|"
    r"\bdue\b|fällig\s+(?:bis|am|spätestens)", re.IGNORECASE)   # „fällig“ allein: Anleihen/Rechenaufgaben
_ACTION = re.compile(
    r"ausfüll\w*|ausgefüllt|bearbeit\w*|abgeb\w*|abgabe|einreich\w*|erledig\w*|vorbereit\w*|hochlad\w*|"
    r"\blös\w*|schreib\w*|mitbring\w*|durcharbeit\w*|\blesen\b|\blies\b|fertig\w*|aufgabe|übung|assignment|"
    r"homework|exercise|complete\w*|\bfill\w*|\bread\b|prepare|submit\w*|finish\w*|hand in|solve|\bwrite\b|"
    r"\bbring\b|upload\w*|präsentation|bericht|füll\w*|\bbitte\b|\bsollen?\b|\bsollt\w*|\bmüssen\b|"
    r"\bmuss\b|please|\bmust\b|\bshould\b", re.IGNORECASE)
_DATE_TOKEN = "|".join(p.pattern for p in _DATE_PATTERNS[:2])   # nur zum Erkennen „folgt ein Datum“
_BIS = re.compile(r"\b(?:bis|by|until|before)\s+(?:spätestens\s+|zum\s+|zu\s+|am\s+|einschließlich\s+|the\s+|latest\s+)*", re.IGNORECASE)
_SESSION = r"(?:Stunde|Vorlesung|Übung|Sitzung|Session|Lehrveranstaltung|Veranstaltung|Termin|Mal|Unterricht|Unterrichtsstunde|Seminar|Tutorium|Praktikum|Lecture|Class|Lesson|Meeting|Exercise)"
_REL_SESSION = re.compile(rf"\b(?:bis|by|until|before)\s+(?:spätestens\s+)?(?:(?:zur|zum|zu der|zu dem|the)\s+)?(?:nächsten?|next)\s+{_SESSION}\b", re.IGNORECASE)
_REL_DAY = re.compile(r"\bbis\s+(?:spätestens\s+)?(?P<w>heute|morgen|übermorgen)\b", re.IGNORECASE)
_REL_WEEKDAY = re.compile(   # auch „bis Sonntagabend“, „bis Mittwochmittag“
    rf"\bbis\s+(?:spätestens\s+)?(?:(?:zum|am|nächsten|kommenden)\s+)*(?P<w>{_WD})"
    r"(?:abend|mittag|nachmittag|vormittag|morgen|früh)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class Deadline:
    label: str                    # kurze Beschreibung („Abgabe 2: Thema“), kann leer sein
    when: datetime | None         # None nur bei relative == "next_session" (Aufrufer löst auf)
    date_only: bool = False       # ohne Uhrzeit: gilt bis Tagesende
    relative: str | None = None   # "next_session"
    evidence: str = ""            # Textstelle
    past: bool = False            # liegt (laut Text) in der Vergangenheit
    year_guessed: bool = False    # im Text stand kein Jahr; es wurde aus dem Veröffentlichungsdatum geschätzt


# -- Hilfsfunktionen ----------------------------------------------------------------------------------
def _clean_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace(" ", " ")).strip()


def _to_date(g: dict, ref: datetime) -> tuple[date, bool] | None:
    try:
        day = int(g["d"])
        month = int(g["m"]) if g.get("m") else MONTHS[g["mon"].lower().rstrip(".")]
        if g.get("y"):
            year = int(g["y"])
            year += 2000 if year < 100 else 0
            return date(year, month, day), False
        d = date(ref.year, month, day)
        # ohne Jahr: nächstes Vorkommen ab (Veröffentlichung − 7 Tage), sonst fällt „20.1.“ im Dezember durch
        return (d if d >= ref.date() - timedelta(days=7) else date(ref.year + 1, month, day)), True
    except (ValueError, KeyError):
        return None


def _dates_in(line: str, ref: datetime) -> list[tuple[int, int, date, bool]]:
    """Alle Daten einer Zeile als (Start, Ende, Datum, Jahr_geschätzt), überlappungsfrei nach Position."""
    found: list[tuple[int, int, date, bool]] = []
    for pattern in _DATE_PATTERNS:
        for m in pattern.finditer(line):
            parsed = _to_date(m.groupdict(), ref)
            if parsed and not any(s < m.end() and m.start() < e for s, e, _, _ in found):
                found.append((m.start(), m.end(), parsed[0], parsed[1]))
    return sorted(found)


def _time_near(line: str, start: int, end: int) -> time | None:
    """Uhrzeit direkt nach (oder unmittelbar vor) dem Datum."""
    for m in (_TIME_AFTER.match(line[end:end + 28]), _TIME_BEFORE.search(line[max(0, start - 22):start])):
        if not m:
            continue
        unit = (m.group("u") or "").lower()
        has_minutes = m.group("mi") or m.group("mi2")
        if not (unit or m.group("mi")):   # nackte Zahl ohne Uhr/Minuten ist keine Uhrzeit
            continue
        hour = int(m.group("h"))
        if unit == "pm" and hour < 12:
            hour += 12
        if unit == "am" and hour == 12:
            hour = 0
        minute = int(m.group("mi") or m.group("mi2") or 0) if has_minutes else 0
        if hour <= 24 and minute < 60:
            return time(min(hour, 23), minute if hour < 24 else 59)
    return None


def _combine(d: date, t: time | None) -> tuple[datetime, bool]:
    if t is None:
        return datetime.combine(d, time(23, 59), TZ), True
    return datetime.combine(d, t, TZ), False


def _label_from_line(line: str, ref: datetime) -> str:
    """Kurzbezeichnung aus einer Tabellen-/Listenzeile; Fließtext liefert keine (→ leer)."""
    text = line
    for s, e, _, _ in reversed(_dates_in(line, ref)):
        text = text[:s] + " " + text[e:]
    text = re.sub(r"\b(?:am|bis|spätestens|um|ab|by|until|due)\b\s*(?=[)\]:,.;]|$)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\(\s*\)|\[\s*\]", " ", text)
    text = re.sub(r"^[\s\d•\-–*·.:;,)(]+", "", text)           # führende Zeilennummern/Aufzählungszeichen
    text = _clean_ws(text).strip(" :;,.-–()")
    words = text.split()
    return text[:70] if 0 < len(words) <= 9 else ""


def _snippet(line: str, start: int, end: int) -> str:
    return _clean_ws(line[max(0, start - 50):end + 40])


# -- Hauptfunktion ----------------------------------------------------------------------------------------
def find_deadlines(text: str, *, posted: datetime, now: datetime) -> list[Deadline]:
    """Alle Fristen des Textes. `posted` = Zeitpunkt der Veröffentlichung (Bezug für „morgen“, Jahr …)."""
    posted = posted.astimezone(TZ)
    now = now.astimezone(TZ)
    lines = [_clean_ws(ln) for ln in text.replace("\r", "").split("\n")]
    found: dict[tuple, Deadline] = {}

    def add(label: str, when: datetime | None, date_only: bool, relative: str | None, evidence: str,
            guessed: bool = False) -> None:
        past = when is not None and when < now - timedelta(minutes=5)
        key = (when, relative)
        if key not in found or (label and not found[key].label):
            found[key] = Deadline(label, when, date_only, relative, evidence, past, guessed)

    # A) Abgabe-Hinweis → nächstes Datum (hinter dem Hinweis, sonst in der Folgezeile, sonst davor)
    for i, line in enumerate(lines):
        for cue in _CUE.finditer(line):
            dates = _dates_in(line, posted)
            pick = next((d for d in dates if d[0] >= cue.end()), None)
            src = line
            if pick is None:
                before = [d for d in dates if d[1] <= cue.start()]
                pick = before[-1] if before else None      # Tabelle: „20.10.26  …  Abgabe 3: Plan“
            if pick is None and not dates and i + 1 < len(lines):
                nxt = _dates_in(lines[i + 1], posted)      # Umbruch: „… bis spätestens“ / „12.10. 19 Uhr“
                if nxt and nxt[0][0] <= 60:
                    pick, src = nxt[0], lines[i + 1]
            if pick is None:
                continue
            s, e, d, guessed = pick
            when, date_only = _combine(d, _time_near(src, s, e))
            label = _label_from_line(line[cue.start():], posted) or _label_from_line(line, posted)
            if not label or re.search(r"\b(?:bis|spätestens|uhr|um|until|due|by|in|per|via)\b", label, re.IGNORECASE):
                label = re.sub(r"\d+", "", cue.group(0)).strip().capitalize()   # „Abgabe“, „Deadline“ …
            add(label, when, date_only, None, _snippet(src, s, e), guessed)

    # B) „bis …“ – nur im Zusammenhang mit einer Tätigkeit (sonst: „Vorlesung von 9 bis 11 Uhr“)
    flat = _clean_ws(" ".join(lines))

    def action_nearby(pos: int) -> bool:
        return bool(_ACTION.search(flat[max(0, pos - 140):pos + 140]))

    for m in _BIS.finditer(flat):
        if not action_nearby(m.start()):
            continue
        rest = flat[m.end():m.end() + 40]
        hit = _dates_in(rest, posted)
        if hit and hit[0][0] == 0:
            s, e, d, guessed = hit[0]
            when, date_only = _combine(d, _time_near(rest, s, e))
            add("", when, date_only, None, _snippet(flat, m.start(), m.end() + e), guessed)

    for m in _REL_SESSION.finditer(flat):
        if action_nearby(m.start()):
            add("", None, False, "next_session", _snippet(flat, m.start(), m.end()))

    for m in _REL_DAY.finditer(flat):
        if action_nearby(m.start()):
            offset = {"heute": 0, "morgen": 1, "übermorgen": 2}[m.group("w").lower()]
            t = _time_near(flat, m.start("w"), m.end("w"))
            when, date_only = _combine(posted.date() + timedelta(days=offset), t)
            add("", when, date_only, None, _snippet(flat, m.start(), m.end()))

    for m in _REL_WEEKDAY.finditer(flat):
        if action_nearby(m.start()):
            target = WEEKDAYS[m.group("w").lower()]
            ahead = (target - posted.weekday()) % 7 or 7        # strikt nach dem Veröffentlichungstag
            t = _time_near(flat, m.start("w"), m.end("w"))
            when, date_only = _combine(posted.date() + timedelta(days=ahead), t)
            add("", when, date_only, None, _snippet(flat, m.start(), m.end()))

    ordered = sorted(found.values(), key=lambda d: (d.when is None, d.when or posted))
    return ordered[:MAX_RESULTS]


def resolve_phrase(phrase: str, *, posted: datetime, now: datetime) -> Deadline | None:
    """Frist aus einer kurzen Textstelle („bis Sonntagabend“, „vom 02.11. bis 13.11.2026“).

    Die Stelle stammt aus einem Text, der schon als Aufgabe erkannt wurde (z. B. vom Modell aus
    einer Mail kopiert); der Aufgaben-Zusammenhang wird deshalb vorangestellt. Bei Zeiträumen gilt
    das Ende. Vergangenes und Unauflösbares → None.
    """
    if not phrase.strip():
        return None
    found = [d for d in find_deadlines(f"Abgabe bitte {phrase}", posted=posted, now=now)
             if d.when is not None and not d.past]
    return max(found, key=lambda d: d.when) if found else None
