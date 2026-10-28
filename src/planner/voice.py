"""Sprachnachrichten → Aufgabe, komplett lokal: Whisper (faster-whisper, CPU) macht Text daraus,
das lokale Modell liest Titel, Frist, Aufwand, Priorität und Bereich heraus.

Der Wortlaut liegt nur bis zur Entscheidung in der lokalen Datenbank.
Bei Sprache wird **nichts ohne Bestätigung** angelegt (Hörfehler!): der Bot zeigt, was er verstanden hat,
und legt die Aufgabe erst auf „Anlegen“ an. Ist das Modell nicht erreichbar, wird der Text wie eine
getippte Nachricht gelesen (`parse_quick`).
"""

from __future__ import annotations

import io
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from . import areas, tasks
from .changelog import now_iso
from .deadlines import resolve_phrase
from .feedback import PRIO_BOOST, PRIO_ICON
from .llm import LlmError, SpokenTask
from .rules import TZ

log = logging.getLogger("planner.voice")
MAX_SECONDS = 300   # längere Aufnahmen nicht transkribieren (CPU)

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
        segments, _info = self._model.transcribe(io.BytesIO(audio), language="de", vad_filter=True, beam_size=1)
        return " ".join(s.text.strip() for s in segments).strip()


def _when(task: SpokenTask, transcript: str, now: datetime) -> tuple[str | None, str | None]:
    """(Frist, frühester Tag). Erst die Regeln der Textnachricht (kennen „morgen“ ohne „bis“), dann die der
    Mails; hat das Modell keine Stelle kopiert, wird der Wortlaut selbst durchsucht (Nutzer 2026-10-28:
    „morgen“ wurde für heute eingeplant)."""
    from .quickadd import parse_when

    today = now.astimezone(TZ).date()
    for text in (task.deadline_text, transcript):
        if not text:
            continue
        deadline, not_before, _spans = parse_when(text, today=today)
        if deadline:
            return deadline, not_before
        d = resolve_phrase(text, posted=now, now=now)
        if d is not None:
            return (d.when.date().isoformat() if d.date_only else d.when.isoformat(timespec="minutes")), None
    return None, None


def understand(llm, text: str, conn: sqlite3.Connection, now: datetime) -> dict:
    """Transkript → Felder der Aufgabe (title, deadline, estimate_min, priority, area)."""
    known = [areas.path_of(conn, i) for _d, i, _n, _w in areas.tree(conn)]
    try:
        t = llm.extract_spoken_task(text, today=now.astimezone(TZ).date(), areas=known)
        deadline, not_before = _when(t, text, now)
        return {"title": t.title, "deadline": deadline, "not_before": not_before, "estimate_min": t.effort_min,
                "priority": t.priority, "area": t.area or None}
    except LlmError as exc:
        log.warning("Modell nicht verfügbar (%s) – lese die Sprachnachricht wie Text", exc)
        from .quickadd import parse_quick

        try:
            q = parse_quick(text, today=now.astimezone(TZ).date())
        except ValueError:
            return {"title": text[:120], "deadline": None, "not_before": None, "estimate_min": None,
                    "priority": "mittel", "area": None}
        return {"title": q.title, "deadline": q.deadline, "not_before": q.not_before, "estimate_min": q.estimate_min,
                "priority": "mittel", "area": q.area}


def save_pending(conn: sqlite3.Connection, transcript: str, fields: dict, message_id: int | None) -> int:
    cur = conn.execute(
        "INSERT INTO voice_notes(message_id, transcript, title, deadline, not_before, estimate_min, priority, area, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (message_id, transcript, fields["title"], fields["deadline"], fields.get("not_before"), fields["estimate_min"],
         fields["priority"], fields["area"], now_iso()))
    conn.commit()
    return int(cur.lastrowid)


def preview(conn: sqlite3.Connection, note_id: int) -> tuple[str, Buttons]:
    from .bot_logic import fmt_minutes, fmt_when

    n = conn.execute("SELECT * FROM voice_notes WHERE id = ?", (note_id,)).fetchone()
    when = fmt_when(n["deadline"], n["not_before"])
    effort = fmt_minutes(n["estimate_min"]) if n["estimate_min"] else "?"
    lines = [f"🎙️ „{n['transcript']}“", "",
             f"➕ {n['title']}",
             f"Frist: {when} · Aufwand: ~{effort} · Prio: {PRIO_ICON[n['priority']]} {n['priority']}"
             + (f" · #{n['area']}" if n["area"] else ""),
             "Passt das? (Falsch verstanden? Dann tipp die Aufgabe einfach.)"]
    return "\n".join(lines), [[("✅ Anlegen", f"vo:{note_id}:ok"), ("❌ Verwerfen", f"vo:{note_id}:no")]]


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
                             estimate_min=n["estimate_min"])
    if task_id is None:
        return "Dazu gibt es schon eine Aufgabe – oder der Titel ist gesperrt (Blacklist).", [], False
    if n["priority"] != "mittel":
        tasks.set_priority(conn, task_id, PRIO_BOOST[n["priority"]], actor="user")
    text, _ = preview(conn, note_id)
    head = text.split("\n")[2:4]
    conn.execute("UPDATE voice_notes SET state = 'added', task_id = ?, transcript = NULL WHERE id = ?",
                 (task_id, note_id))   # Wortlaut nach der Entscheidung nicht aufbewahren
    conn.commit()
    return "\n".join([f"✅ Aufgabe #{task_id} angelegt", *head, "Ich plane sie ein."]), \
        [[(f"✅ #{task_id} erledigt", f"done:{task_id}"), (f"🗑 #{task_id} verwerfen", f"drop:{task_id}")]], True
