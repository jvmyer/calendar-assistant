"""Sprachnachrichten → Aufgabe, komplett lokal: Whisper (faster-whisper, CPU) macht Text daraus,
das lokale Modell liest Titel, Frist-Stelle und Bereich heraus. Uhrzeit, Dauer und Priorität bestimmt der Code
aus dem Wortlaut (das kleine Modell machte aus „zehn Minuten“ 600 und fand fast alles „hoch“, Test 2026-10-30).

Der Wortlaut liegt nur bis zur Entscheidung in der lokalen Datenbank.
Bei Sprache wird **nichts ohne Bestätigung** angelegt (Hörfehler!): der Bot zeigt, was er verstanden hat,
und legt die Aufgabe erst auf „Anlegen“ an. Ist das Modell nicht erreichbar, wird der Text wie eine
getippte Nachricht gelesen (`parse_quick`).
"""

from __future__ import annotations

import io
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import areas, tasks
from .changelog import now_iso
from .deadlines import resolve_phrase
from .feedback import PRIO_BOOST, PRIO_ICON
from .llm import LlmError, SpokenTask
from .rules import TZ

log = logging.getLogger("planner.voice")
MAX_SECONDS = 300   # längere Aufnahmen nicht transkribieren (CPU)
AWAIT_TITLE = "await:voice_title"   # Sprachnachricht, deren Titel als Nächstes getippt wird

Buttons = list[list[tuple[str, str]]]


class Transcriber:
    """Whisper lokal auf der CPU. Das Modell wird beim ersten Mal nach `download_root` geladen."""

    def __init__(self, model_size: str, download_root: Path):
        self._size, self._root = model_size, download_root
        self._model = None

    def transcribe(self, audio: bytes) -> str:
        from faster_whisper import WhisperModel

        if self._model is None:
            self._root.mkdir(parents=True, exist_ok=True)
            self._model = WhisperModel(self._size, device="cpu", compute_type="int8",
                                       download_root=str(self._root))
        # beam 5 statt 1 und kein Weiterreichen des vorigen Abschnitts: weniger Hörfehler und keine Wiederholschleifen.
        # Kein initial_prompt: Uhrzeiten darin („16:30“) machten aus „halb fünf“ „05“ (Test 2026-10-30).
        segments, _info = self._model.transcribe(io.BytesIO(audio), language="de", vad_filter=True, beam_size=5,
                                                 condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()


def _when(task: SpokenTask, transcript: str, now: datetime) -> tuple[str | None, str | None]:
    """(Frist, frühester Tag). Erst die Regeln der Textnachricht (kennen „morgen“ ohne „bis“), dann die der
    Mails; hat das Modell keine Stelle kopiert, wird der Wortlaut selbst durchsucht (Nutzer 2026-10-28:
    „morgen“ wurde für heute eingeplant)."""
    from .quickadd import parse_when

    today = now.astimezone(TZ).date()
    copied = task.deadline_text if _squash(task.deadline_text) in _squash(transcript) else ""   # erfundene Stellen
    for text in (copied, spoken_clock(transcript)):   # (Modell: „bis Freitag“ bei „morgen“) zählen nicht
        if not text:
            continue
        deadline, not_before, _spans = parse_when(text, today=today)
        if deadline:
            return deadline, not_before
        d = resolve_phrase(text, posted=now, now=now)
        if d is not None:
            return (d.when.date().isoformat() if d.date_only else d.when.isoformat(timespec="minutes")), None
    return None, None


def _squash(text: str) -> str:
    return re.sub(r"[^\w:]+", " ", text.lower()).strip()


_NUMBERS = {"eins": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9,
            "zehn": 10, "elf": 11, "zwölf": 12}
_H = r"(\d{1,2}|" + "|".join(_NUMBERS) + r")"
_QUARTER = re.compile(r"(?<![\d.:])\b(halb|viertel nach|viertel vor|dreiviertel)\s+" + _H + r"\b(?![:.]\d)(?:\s+uhr\b)?",
                      re.IGNORECASE)
_FULL = re.compile(r"(?<![\d.:])\b" + _H + r"\s+uhr(?:\s+(\d{1,2})\b)?", re.IGNORECASE)   # „5 Uhr“, „5 Uhr 30“
_AT = re.compile(r"\b(um|von|bis|ab)\s+" + _H + r"\b(?![:.]\d|\s*(?:uhr|h|std|min|stunde|minute|tag|woche|\.))",
                 re.IGNORECASE)   # „um 9“, „von 4 bis 6“
_MORNING = re.compile(r"\b(morgens|früh|vormittags?|in der früh)\b", re.IGNORECASE)


def spoken_clock(text: str) -> str:
    """Gesprochene Uhrzeiten → „16:30“: „halb fünf“, „viertel nach 3“, „um 9“, „5 Uhr 30“. Gesprochen meint
    „um 4“ fast immer nachmittags: 1–6 Uhr zählen als 13–18 Uhr, außer es fällt „morgens“/„früh“."""
    morning = bool(_MORNING.search(text))

    def hour(raw: str) -> int:
        return int(raw) if raw.isdigit() else _NUMBERS[raw.lower()]

    def clock(h: int, m: int) -> str:
        h = 12 if h == 0 else h
        if 1 <= h <= 6 and not morning:
            h += 12
        return f"{h:02d}:{m:02d}" if h < 24 and m < 60 else f"{h}"

    def quarter(mt: re.Match) -> str:
        h, kind = hour(mt.group(2)), mt.group(1).lower()
        h, m = {"halb": (h - 1, 30), "viertel nach": (h, 15), "viertel vor": (h - 1, 45),
                "dreiviertel": (h - 1, 45)}[kind]
        return clock(h, m)

    text = _QUARTER.sub(quarter, text)
    text = _FULL.sub(lambda mt: clock(hour(mt.group(1)), int(mt.group(2) or 0)) + " Uhr", text)
    for _ in range(2):   # „von 4 bis 6“: beide Zahlen
        text = _AT.sub(lambda mt: f"{mt.group(1)} {clock(hour(mt.group(2)), 0)}" if hour(mt.group(2)) <= 23
                       else mt.group(0), text)
    return text


_DURATION_WORDS = {"ein": 1, "eine": 1, "einer": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6,
                   "zehn": 10, "fünfzehn": 15, "zwanzig": 20, "dreißig": 30, "vierzig": 40, "fünfundvierzig": 45,
                   "fünfzig": 50, "neunzig": 90}
_DURATION = re.compile(
    r"\b(?P<fix>halbe stunde|halben stunde|viertelstunde|dreiviertel stunde|dreiviertelstunde|anderthalb stunden"
    r"|eineinhalb stunden|ein paar minuten)\b"
    r"|\b(?P<n>\d+(?:[.,]\d+)?|" + "|".join(sorted(_DURATION_WORDS, key=len, reverse=True)) + r")"
    r"(?P<half>\s+(?:und\s+)?(?:einhalb|halb))?\s*(?P<unit>stunden?|std|h|minuten?|min)\b",
    re.IGNORECASE)
_FIXED_DURATIONS = {"halbe stunde": 30, "halben stunde": 30, "viertelstunde": 15, "dreiviertel stunde": 45,
                    "dreiviertelstunde": 45, "anderthalb stunden": 90, "eineinhalb stunden": 90, "ein paar minuten": 10}


def spoken_minutes(text: str) -> int | None:
    """Genannte Dauer in Minuten („zehn Minuten“, „zwei Stunden“, „eine halbe Stunde“), sonst None."""
    m = _DURATION.search(text)
    if m is None:
        return None
    if m.group("fix"):
        return _FIXED_DURATIONS[m.group("fix").lower()]
    raw = m.group("n").lower()
    n = float(raw.replace(",", ".")) if raw[0].isdigit() else _DURATION_WORDS[raw]
    n += 0.5 if m.group("half") else 0
    minutes = round(n * 60) if m.group("unit").lower().startswith(("s", "h")) else round(n)
    return minutes if 0 < minutes <= 24 * 60 else None


_LOW = re.compile(r"\b(?:nicht\s+(?:so\s+|sehr\s+|besonders\s+|ganz\s+)?(?:wichtig|dringend|eilig)|irgendwann|"
                  r"kein(?:en)?\s+stress|keine\s+eile|hat\s+zeit|wenn\s+(?:ich\s+)?zeit\s+(?:ist|habe)|unwichtig)",
                  re.IGNORECASE)
_HIGH = re.compile(r"\b(?:wichtig|dringend|eilig|unbedingt|sofort|asap|superwichtig|sehr wichtig)\b", re.IGNORECASE)


def spoken_priority(text: str) -> str:
    """„wichtig“/„dringend“ = hoch, „irgendwann“/„nicht so wichtig“ = niedrig, sonst mittel."""
    if _LOW.search(text):
        return "niedrig"
    return "hoch" if _HIGH.search(text) else "mittel"


_TITLE_NOISE = [
    re.compile(r"\bfür\s+(?:die\s+|den\s+|das\s+|meine\s+|meinen\s+)?(?:uni|universität|hochschule|arbeit|job)\b",
               re.IGNORECASE),
    re.compile(r"\b(?:privat|wichtig|dringend|unbedingt|bitte|ähm?|ehm|okay|ok)\b", re.IGNORECASE),
    re.compile(r"\b(?:anlegen|eintragen|einplanen|einspeichern|notieren|aufschreiben|hinzufügen|einstellen)\b\W*$",
               re.IGNORECASE),
    re.compile(r"^\W*(?:neue\s+)?(?:aufgabe|erinnerung|termin|todo)\s*[:\-–]\s*", re.IGNORECASE),
    re.compile(r"^\W*(?:also|ja|so|und|ich\s+(?:muss|sollte|will|möchte)(?:\s+noch)?)\b\W*", re.IGNORECASE),
]
# Ohne Modell ist der Titel der ganze Satz: Anreden an den Planer, Dauer- und Wichtigkeits-Nachsätze weg
_SENTENCE_NOISE = [
    re.compile(r"^\W*(?:(?:ja\s+)?hallo|hey|okay|ok|also|ähm?|ehm|so)\b\W*", re.IGNORECASE),
    re.compile(r"^\W*(?:trag(?:e)?\s+(?:mal\s+|bitte\s+)?ein|erinner(?:e)?\s+mich(?:\s+daran)?|notier(?:e)?"
               r"(?:\s+mal)?|neue\s+aufgabe|ich\s+(?:hab|habe)\s+vergessen|ich\s+glaub(?:e)?)\b\W*(?:dass\s+)?(?:ich\s+)?",
               re.IGNORECASE),
    re.compile(r"^\W*(?:(?:ich|wir)\s+)?(?:muss|müssen|sollte|sollten|will|möchte)\s+(?:noch\s+|mal\s+)*(?:mich\s+)?",
               re.IGNORECASE),
    re.compile(r",?\s*(?:das\s+)?(?:dauert|braucht|brauche)\b[^,.]*", re.IGNORECASE),
    re.compile(r",?\s*\b(?:das\s+)?ist\b(?:\s+aber)?(?:\s+nicht)?(?:\s+(?:so|sehr))?(?:\s+(?:wichtig|dringend|eilig))?"
               r"\s*(?=,|$)", re.IGNORECASE),
    re.compile(r",?\s*(?:frist\s+ist|sonst|das\s+muss)\b.*$", re.IGNORECASE),
    re.compile(r"\b(?:nächste|kommende|diese)[nrms]?\s+woche\b", re.IGNORECASE),
    re.compile(r"^\W*(?:die|der|das|den|zum|zur)\s+", re.IGNORECASE),
    re.compile(r",?\s*(?:kannst|könntest)\s+du\b.*$", re.IGNORECASE),
    re.compile(r"\b(?:muss|sollte|zu)\s*$|\b(?:noch|mal|irgendwann)\b", re.IGNORECASE),
    re.compile(r"\b(?:für|am|um|bis|ab|von)\s*(?=,|$)", re.IGNORECASE),   # Reste nach gelöschtem Datum
    re.compile(r"^\W*termin\s+(?:für|am|beim|bei)?\W*", re.IGNORECASE),
]


def clean_title(title: str, *, today) -> str:
    """Titel ohne Datum, Uhrzeit, Bereich und Wörter an den Planer („Kaffee mit Familie anlegen“ → „Kaffee mit
    Familie“, Nutzer 2026-10-30). Bleibt nichts übrig, gilt der Titel des Modells."""
    from .quickadd import _ESTIMATE, _blank, parse_slot, parse_when

    text = spoken_clock(title)
    try:
        slot = parse_slot(text, today=today)
    except ValueError:
        slot = None
    text = _blank(text, slot[2] if slot else parse_when(text, today=today)[2])
    text = _DURATION.sub(" ", _ESTIMATE.sub(" ", text))
    for pattern in _TITLE_NOISE:
        text = pattern.sub(" ", text)
    for _ in range(2):   # Reihenfolge: erst Anrede, dann „ich muss“
        for pattern in _SENTENCE_NOISE:
            text = re.sub(r"\s+", " ", pattern.sub(" ", text)).strip(" ,;:.!?-–")
    text = re.sub(r"\s*,\s*(?:,\s*)*", ", ", text)
    text = re.sub(r"\bzu\s+(\w+)$", r"\1", text)   # „Bücher zu verlängern“ → „Bücher verlängern“
    text = re.sub(r"\s+", " ", text).strip(" ,;:.!?-–")
    if not text:
        return title.strip()
    return text[0].upper() + text[1:]


_SPOKEN_AREAS = {   # oberster Bereich → Wörter, mit denen er ausdrücklich genannt wird („Hausarbeit“ zählt nicht)
    "Uni": re.compile(r"#uni\b|\b(?:uni|universität|hochschule)\b", re.IGNORECASE),
    "Arbeit": re.compile(r"#arbeit\b|\b(?:arbeit|job)\b", re.IGNORECASE),
    "Privat": re.compile(r"#privat\b|\bprivat\w*", re.IGNORECASE),
}


def explicit_area(transcript: str, guess: str | None) -> str | None:
    """Bereich nur, wenn er im Wortlaut genannt wird (Nutzer 2026-10-28: „Kaffee mit Familie“ landete per
    Modell-Raten unter Uni). Den Unterbereich des Modells („Uni/Statistik“) behalten wir, wenn er dazu passt."""
    named = [root for root, pattern in _SPOKEN_AREAS.items() if pattern.search(transcript)]
    if guess and guess.split("/", 1)[0].lower() in {r.lower() for r in named}:
        return guess
    return named[0] if named else None


def understand(llm, text: str, conn: sqlite3.Connection, now: datetime) -> dict:
    """Transkript → Felder der Aufgabe (title, deadline, estimate_min, priority, area, fixed_start/fixed_end).
    Eine genannte Uhrzeit ohne „bis“ bzw. eine Spanne („von 16:30 bis 17:30“) ist ein fester Zeitblock, keine Frist."""
    today = now.astimezone(TZ).date()
    fields = _understand(llm, text, conn, now)
    fields["title"] = clean_title(fields["title"], today=today)
    fields["area"] = explicit_area(text, fields["area"])
    fields["priority"] = spoken_priority(text)
    fields["estimate_min"] = spoken_minutes(text) or fields["estimate_min"]
    from .quickadd import parse_slot

    try:
        slot = parse_slot(spoken_clock(text), today=today)
    except ValueError:
        slot = None
    fields["fixed_start"] = fields["fixed_end"] = None
    if slot:
        start, end, _spans = slot
        end = end or start + timedelta(minutes=fields["estimate_min"] or 60)
        fields.update(deadline=None, not_before=None, estimate_min=int((end - start).total_seconds() // 60),
                      fixed_start=start.isoformat(timespec="minutes"), fixed_end=end.isoformat(timespec="minutes"))
    return fields


def _understand(llm, text: str, conn: sqlite3.Connection, now: datetime) -> dict:
    known = [areas.path_of(conn, i) for _d, i, _n, _w in areas.tree(conn)]
    try:
        t = llm.extract_spoken_task(text, today=now.astimezone(TZ).date(), areas=known)
        deadline, not_before = _when(t, text, now)
        return {"title": t.title, "deadline": deadline, "not_before": not_before, "estimate_min": t.effort_min,
                "area": t.area or None}
    except LlmError as exc:
        log.warning("Modell nicht verfügbar (%s) – lese die Sprachnachricht wie Text", exc)
        from .quickadd import parse_quick

        try:
            q = parse_quick(spoken_clock(text), today=now.astimezone(TZ).date())
        except ValueError:
            return {"title": text[:120], "deadline": None, "not_before": None, "estimate_min": None, "area": None}
        return {"title": q.title, "deadline": q.deadline, "not_before": q.not_before, "estimate_min": q.estimate_min,
                "area": q.area}


def save_pending(conn: sqlite3.Connection, transcript: str, fields: dict, message_id: int | None) -> int:
    cur = conn.execute(
        "INSERT INTO voice_notes(message_id, transcript, title, deadline, not_before, fixed_start, fixed_end, "
        "estimate_min, priority, area, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (message_id, transcript, fields["title"], fields["deadline"], fields.get("not_before"),
         fields.get("fixed_start"), fields.get("fixed_end"), fields["estimate_min"], fields["priority"], fields["area"],
         now_iso()))
    conn.commit()
    return int(cur.lastrowid)


def preview(conn: sqlite3.Connection, note_id: int) -> tuple[str, Buttons]:
    from .bot_logic import fmt_minutes, fmt_slot, fmt_when

    n = conn.execute("SELECT * FROM voice_notes WHERE id = ?", (note_id,)).fetchone()
    when = fmt_slot(n["fixed_start"], n["fixed_end"]) if n["fixed_start"] else fmt_when(n["deadline"], n["not_before"])
    effort = fmt_minutes(n["estimate_min"]) if n["estimate_min"] else "?"
    lines = [f"🎙️ „{n['transcript']}“", "",
             f"➕ {n['title']}",
             f"{"Zeit" if n["fixed_start"] else "Frist"}: {when} · Aufwand: ~{effort} · Prio: {PRIO_ICON[n['priority']]} {n['priority']}"
             + (f" · #{n['area']}" if n["area"] else ""),
             "Passt das? Falscher Titel → ✏️, sonst tipp die Aufgabe einfach."]
    return "\n".join(lines), [[("✅ Anlegen", f"vo:{note_id}:ok"), ("✏️ Titel", f"vo:{note_id}:edit"),
                               ("❌ Verwerfen", f"vo:{note_id}:no")]]


def ask_title(conn: sqlite3.Connection, note_id: int) -> tuple[str, Buttons]:
    """„✏️ Titel“: die nächste getippte Nachricht wird der Titel."""
    from .db import set_meta

    n = conn.execute("SELECT state FROM voice_notes WHERE id = ?", (note_id,)).fetchone()
    if n is None or n["state"] != "pending":
        return "Diese Sprachnachricht ist schon entschieden.", []
    set_meta(conn, AWAIT_TITLE, str(note_id))
    text, buttons = preview(conn, note_id)
    return text + "\n\n✏️ Schick mir jetzt den richtigen Titel als Nachricht.", buttons


def receive_title(conn: sqlite3.Connection, text: str) -> tuple[str, Buttons] | None:
    """Antwort auf „✏️ Titel“. None = es wird gerade kein Titel erwartet."""
    from .db import get_meta, set_meta

    raw = get_meta(conn, AWAIT_TITLE)
    if not raw or not raw.isdigit():
        return None
    set_meta(conn, AWAIT_TITLE, "")
    title = re.sub(r"\s+", " ", text).strip()[:120]
    if conn.execute("UPDATE voice_notes SET title = ? WHERE id = ? AND state = 'pending'",
                    (title, int(raw))).rowcount == 0 or not title:
        return None
    conn.commit()
    return preview(conn, int(raw))


def handle_voice(conn: sqlite3.Connection, transcriber, llm, audio: bytes, *, duration: int | None,
                 message_id: int | None, now: datetime | None = None) -> tuple[str, Buttons]:
    """Sprachnachricht → Vorschau mit Buttons (legt noch nichts an)."""
    if duration and duration > MAX_SECONDS:
        return f"🎙️ Die Nachricht ist zu lang (max. {MAX_SECONDS // 60} Min.). Bitte kürzer oder als Text.", []
    try:
        text = transcriber.transcribe(audio)
    except Exception as exc:  # noqa: BLE001 – defekte Datei/Modell darf den Bot nicht stoppen
        log.warning("Transkription fehlgeschlagen: %s", type(exc).__name__)
        return "🎙️ Konnte die Sprachnachricht nicht verstehen (technischer Fehler). Bitte als Text schicken.", []
    if not text:
        return "🎙️ Ich habe nichts verstanden – war die Nachricht leer?", []
    fields = understand(llm, text, conn, now or datetime.now(TZ))
    return preview(conn, save_pending(conn, text, fields, message_id))


def decide(conn: sqlite3.Connection, note_id: int, accept: bool) -> tuple[str, Buttons, bool]:
    """Button unter der Vorschau. Rückgabe: (Text, Buttons, Planer neu starten?)."""
    n = conn.execute("SELECT * FROM voice_notes WHERE id = ?", (note_id,)).fetchone()
    if n is None:
        return "Diese Sprachnachricht gibt es nicht mehr.", [], False
    if n["state"] != "pending":
        return f"Schon erledigt ({'angelegt' if n['state'] == 'added' else 'verworfen'}).", [], False
    if not accept:
        conn.execute("UPDATE voice_notes SET state = 'dropped', transcript = NULL WHERE id = ?", (note_id,))
        conn.commit()
        return f"❌ Verworfen: „{n['transcript']}“", [], False
    area_id = areas.ensure_path(conn, n["area"], actor="user") if n["area"] else None
    task_id = tasks.add_task(conn, title=n["title"], source="voice", source_ref=f"voice:{note_id}", actor="user",
                             area_id=area_id, deadline=n["deadline"], not_before=n["not_before"],
                             fixed_start=n["fixed_start"], fixed_end=n["fixed_end"], estimate_min=n["estimate_min"])
    if task_id is None:
        return "Dazu gibt es schon eine Aufgabe – oder der Titel ist gesperrt (Blacklist).", [], False
    if n["priority"] != "mittel":
        tasks.set_priority(conn, task_id, PRIO_BOOST[n["priority"]], actor="user")
    text, _ = preview(conn, note_id)
    head = text.split("\n")[2:4]
    conn.execute("UPDATE voice_notes SET state = 'added', task_id = ?, transcript = NULL WHERE id = ?",
                 (task_id, note_id))   # Wortlaut nach der Entscheidung nicht aufbewahren
    conn.commit()
    from .bot_logic import area_buttons, calendar_line

    return "\n".join([f"✅ Aufgabe #{task_id} angelegt", *head, "Ich plane sie ein.", calendar_line(conn, task_id)]), \
        [[(f"✅ #{task_id} erledigt", f"done:{task_id}"), (f"🗑 #{task_id} verwerfen", f"drop:{task_id}")],
         area_buttons(conn, task_id)], True
