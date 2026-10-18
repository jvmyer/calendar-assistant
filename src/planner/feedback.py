"""Einschätzung des Nutzers zu Aufgaben aus Mails – und daraus Lernbeispiele fürs lokale Modell.

Unter jeder Mail-Aufgabe zeigt der Bot eine Karte: Art (Aufgabe / nur Vorschlag / nichts), Priorität,
Aufwand, Frist. Jede Eingabe wirkt sofort auf die Aufgabe (Planer plant neu) und wird gespeichert.
Bei neuen Mails bekommt das Modell die letzten Einschätzungen als Beispiele mit (Few-Shot),
bevorzugt solche vom selben Absender.

Gespeichert werden Absender, Betreff, ein kurzer Auszug (≤ 400 Zeichen, ohne Zitate und Signatur),
das Urteil des Modells und deine Angaben – nur in der lokalen Datenbank auf dem Server. Auszüge
unbewerteter Mails werden nach 30 Tagen gelöscht.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import tasks
from .changelog import now_iso

PRIOS = ("hoch", "mittel", "niedrig")
PRIO_ICON = {"hoch": "🔴", "mittel": "🟠", "niedrig": "🟢"}
PRIO_BOOST = {"hoch": 1.5, "mittel": 1.0, "niedrig": 0.6}   # Faktor im Planer (tasks.priority_boost)
EFFORTS = (15, 30, 60, 120, 240)
MAX_EXAMPLES = 4
SAME_SENDER = 2
AWAIT_KEY = "await_deadline"   # meta: Aufgabe, deren Frist als nächste Textnachricht kommt
EXCERPT_LEN = 400
EXCERPT_KEEP = timedelta(days=30)   # Auszüge unbewerteter Mails
_RATED = ("(done = 1 OR user_kind IS NOT NULL OR user_prio IS NOT NULL OR user_effort IS NOT NULL "
          "OR user_deadline_ok IS NOT NULL)")
# Ab hier beginnt Zitiertes oder die Signatur
_CUT = re.compile(r"^(?:-- ?$|_{5,}|-{5,}|>|Von: |From: |Am .{5,80} schrieb|On .{5,80} wrote|"
                  r"Mit freundlichen Grüßen|Viele Grüße|Beste Grüße|Liebe Grüße|Best regards|Kind regards)",
                  re.IGNORECASE)

Buttons = list[list[tuple[str, str]]]


@dataclass(frozen=True)
class Example:
    sender: str
    subject: str
    answer: dict   # so hätte das Modell antworten sollen
    excerpt: str = ""


def make_excerpt(body: str, limit: int = EXCERPT_LEN) -> str:
    """Anfang der Mail ohne Zitate und Signatur, Leerraum zusammengezogen."""
    kept: list[str] = []
    for line in body.replace("\r", "").split("\n"):
        if _CUT.match(line.strip()):
            break
        kept.append(line.strip())
    text = re.sub(r"\s+", " ", " ".join(kept)).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def record(conn: sqlite3.Connection, task_id: int, *, item_id: str, sender: str, subject: str, received: str,
           kind: str, title: str, deadline_text: str, deadline: str | None, effort: int | None,
           prio: str | None, body: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO mail_feedback(task_id, item_id, sender, subject, received, excerpt, model_kind, "
        "model_title, model_deadline_text, model_deadline, model_effort, model_prio, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, item_id, sender, subject, received, make_excerpt(body), kind, title, deadline_text, deadline,
         effort, prio, now_iso()))
    conn.commit()
    prune(conn)


def prune(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Löscht Auszüge unbewerteter Mails nach 30 Tagen (Bewertete bleiben als Lernbeispiele)."""
    cutoff = ((now or datetime.now(UTC)) - EXCERPT_KEEP).isoformat()
    cur = conn.execute(f"UPDATE mail_feedback SET excerpt = NULL WHERE excerpt IS NOT NULL AND NOT {_RATED} "
                       "AND updated_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def _get(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT f.*, t.title AS t_title, t.deadline AS t_deadline, t.estimate_min AS t_effort, t.status AS t_status "
        "FROM mail_feedback f JOIN tasks t ON t.id = f.task_id WHERE f.task_id = ?", (task_id,)).fetchone()


def _set(conn: sqlite3.Connection, task_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE mail_feedback SET {cols}, updated_at = ? WHERE task_id = ?",
                 (*fields.values(), now_iso(), task_id))
    conn.commit()


# -- Karte ---------------------------------------------------------------------------------------------
def _fmt_effort(m: int | None) -> str:
    if not m:
        return "?"
    return f"{m} Min." if m < 60 else (f"{m // 60} h" if m % 60 == 0 else f"{m // 60} h {m % 60} Min.")


def _btn_effort(m: int) -> str:
    return f"{m}m" if m < 60 else f"{m // 60}h"


def card(conn: sqlite3.Connection, task_id: int, *, header: str = "", note: str = "") -> tuple[str, Buttons]:
    from .bot_logic import fmt_deadline

    f = _get(conn, task_id)
    if f is None:
        return "Dazu gibt es keine Einschätzung (mehr).", []
    prio = f["user_prio"] or f["model_prio"] or "mittel"
    deadline = f"bis {fmt_deadline(f['t_deadline'])}" if f["t_deadline"] else "ohne Frist"
    mark = {1: " ✓", 0: " (von dir geändert)"}.get(f["user_deadline_ok"], "")
    lines = [header] if header else []
    lines.append(f"#{task_id} {f['t_title']}")
    lines.append(f"Frist: {deadline}{mark} · Aufwand: ~{_fmt_effort(f['t_effort'])} · Prio: {PRIO_ICON[prio]} {prio}")
    if f["user_kind"] in ("vorschlag", "nichts"):
        word = "nur ein Vorschlag" if f["user_kind"] == "vorschlag" else "keine Aufgabe"
        lines.append(f"🗑 Verworfen – laut dir {word}. Danke, das merkt sich das Modell.")
        return "\n".join(lines), []
    if f["done"]:
        lines.append("✅ Gespeichert – danke, das merkt sich das Modell.")
        return "\n".join(lines), []
    if note:
        lines.append(note)
    else:
        lines.append("Wie schätzt du es ein? (Alles optional, „Fertig“ übernimmt den Rest.)")
    p = f"fb:{task_id}"
    sel = lambda on, text: f"• {text}" if on else text
    return "\n".join(lines), [
        [("❌ Keine Aufgabe", f"{p}:k:nichts"), ("💡 Nur Vorschlag", f"{p}:k:vorschlag")],
        [(sel(prio == q, f"{PRIO_ICON[q]} {q}"), f"{p}:p:{q}") for q in PRIOS],
        [(sel(f["t_effort"] == m, _btn_effort(m)), f"{p}:e:{m}") for m in EFFORTS],
        [(sel(f["user_deadline_ok"] == 1, "📅 Frist stimmt"), f"{p}:d:ok"), ("✏️ Frist ändern", f"{p}:d:edit")],
        [("✅ Fertig", f"{p}:x:ok")],
    ]


# -- Eingaben --------------------------------------------------------------------------------------------
def apply(conn: sqlite3.Connection, task_id: int, field: str, value: str) -> tuple[str, Buttons, bool]:
    """Ein Button-Druck. Rückgabe: (Text, Buttons, Planer neu starten?)."""
    from .db import set_meta

    f = _get(conn, task_id)
    if f is None:
        return "Dazu gibt es keine Einschätzung (mehr).", [], False
    changed = False
    if field == "k" and value in ("nichts", "vorschlag"):
        _set(conn, task_id, user_kind=value, done=1)
        if f["t_status"] == "open":
            tasks.set_status(conn, task_id, "dropped", actor="user", reason=f"Mail laut Nutzer: {value}")
        changed = True
    elif field == "p" and value in PRIOS:
        _set(conn, task_id, user_prio=value)
        tasks.set_priority(conn, task_id, PRIO_BOOST[value], actor="user")
        changed = True
    elif field == "e" and value.isdigit():
        _set(conn, task_id, user_effort=int(value))
        tasks.set_estimate(conn, task_id, int(value), actor="user")
        changed = True
    elif field == "d" and value == "ok":
        _set(conn, task_id, user_deadline_ok=1, user_deadline=f["t_deadline"])
    elif field == "d" and value == "edit":
        set_meta(conn, AWAIT_KEY, str(task_id))
        text, buttons = card(conn, task_id, note="✏️ Schick mir die richtige Frist als Nachricht, "
                                                 "z. B. „20.10.“, „20.10. 18:00“ oder „keine“.")
        return text, buttons, False
    elif field == "x":
        _set(conn, task_id, done=1, user_kind=f["user_kind"] or "aufgabe",
             user_prio=f["user_prio"] or f["model_prio"] or "mittel",
             user_deadline_ok=1 if f["user_deadline_ok"] is None else f["user_deadline_ok"],
             user_deadline=f["user_deadline"] if f["user_deadline_ok"] == 0 else f["t_deadline"],
             user_effort=f["user_effort"] or f["t_effort"])
    text, buttons = card(conn, task_id)
    return text, buttons, changed


def awaiting(conn: sqlite3.Connection) -> int | None:
    from .db import get_meta

    raw = get_meta(conn, AWAIT_KEY)
    return int(raw) if raw and raw.isdigit() else None


def receive_deadline(conn: sqlite3.Connection, task_id: int, text: str) -> tuple[str, Buttons] | None:
    """Antwort auf „Frist ändern“. None = kein Datum erkannt (Nachricht ist dann etwas anderes)."""
    from .db import set_meta
    from .parsing import parse_deadline

    set_meta(conn, AWAIT_KEY, "")
    value = text.strip()
    if value.lower() in {"keine", "-", "weg", "ohne"}:
        deadline = None
    else:
        try:
            deadline = parse_deadline(value)
        except ValueError:
            return None
    tasks.set_deadline(conn, task_id, deadline, actor="user")
    _set(conn, task_id, user_deadline_ok=0, user_deadline=deadline)
    return card(conn, task_id, header="📅 Frist übernommen.")


# -- Lernbeispiele -------------------------------------------------------------------------------------------
def _answer(f: sqlite3.Row) -> dict:
    kind = f["user_kind"] or f["model_kind"]
    if f["user_deadline_ok"] == 0:
        dl = f"bis {f['user_deadline'][8:10]}.{f['user_deadline'][5:7]}.{f['user_deadline'][:4]}" \
            if f["user_deadline"] else ""
    else:
        dl = f["model_deadline_text"] or ""
    return {   # gleiche Reihenfolge wie llm.SCHEMA
        "handlung": f["model_title"] if kind != "nichts" else "",
        "pflicht": kind == "aufgabe",
        "kind": kind,
        "title": f["model_title"] if kind != "nichts" else "",
        "deadline_text": dl if kind != "nichts" else "",
        "effort_min": (f["user_effort"] or f["model_effort"] or 0) if kind != "nichts" else 0,
        "priority": f["user_prio"] or f["model_prio"] or "mittel",
    }


def examples(conn: sqlite3.Connection, sender: str, *, limit: int = MAX_EXAMPLES) -> list[Example]:
    """Letzte bewertete Mails: zuerst vom selben Absender, dann die neuesten übrigen."""
    rated = _RATED
    same = conn.execute(f"SELECT * FROM mail_feedback WHERE {rated} AND sender = ? ORDER BY updated_at DESC LIMIT ?",
                        (sender, SAME_SENDER)).fetchall()
    ids = [r["task_id"] for r in same] or [-1]
    rest = conn.execute(
        f"SELECT * FROM mail_feedback WHERE {rated} AND task_id NOT IN ({','.join('?' * len(ids))}) "
        "ORDER BY updated_at DESC LIMIT ?", (*ids, limit - len(same))).fetchall()
    return [Example(r["sender"] or "", r["subject"] or "", _answer(r), r["excerpt"] or "") for r in [*same, *rest]]


def example_messages(exs: list[Example]) -> list[dict]:
    """Beispiele als Gesprächsverlauf (Nutzer: Kopf der Mail → Modell: richtige Antwort)."""
    out: list[dict] = []
    for ex in reversed(exs):   # älteste zuerst, die passendsten stehen direkt vor der neuen Mail
        text = ex.excerpt or "(Text nicht gespeichert)"
        out.append({"role": "user", "content": f"Von: {ex.sender}\nBetreff: {ex.subject}\n\n{text}"})
        out.append({"role": "assistant", "content": json.dumps(ex.answer, ensure_ascii=False)})
    return out
