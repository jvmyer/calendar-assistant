"""Bot-Logik ohne Telegram-Abhängigkeit: Nachricht rein → Antwort (Text + Buttons) raus.

So bleibt alles testbar; `telegram_bot.py` ist nur der dünne Anschluss an die Telegram-API.
Jede Änderung geht über dieselben Module wie die CLI und landet damit im Change-Log
(Akteur `user`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import areas, blacklist, changelog, feedback, proposals, tasks, voice
from .quickadd import parse_quick
from .rules import TZ, Rules

Buttons = list[list[tuple[str, str]]]   # Zeilen aus (Beschriftung, callback_data)

HELP = (
    "Schreib mir einfach eine Aufgabe, z. B.:\n"
    "  Bericht schreiben bis 20.10. 2h #Uni\n"
    "(bis <Datum|morgen|Freitag> [Uhrzeit] · Aufwand wie 2h/45min · #Bereich)\n\n"
    "/heute – Termine, Blocker und Fälliges von heute\n"
    "/aufgaben – offene Aufgaben (mit Buttons)\n"
    "/vorschlaege – offene Vorschläge\n"
    "/erledigt <Nr> · /verwerfen <Nr>\n"
    "/frist <Nr> <Datum|keine> – z. B. /frist 3 20.10. 18:00\n"
    "/aufwand <Nr> <2h|45min>\n"
    "/bereich <Nr> <Uni|Arbeit|Privat|keiner> – bestimmt den Kalender (oder Buttons unter der Aufgabe)\n"
    "/sperren <Nr> – Titel auf die Blacklist: wird nie wieder automatisch angelegt\n"
    "/sperrliste – zeigt die Blacklist (mit Buttons zum Entfernen)\n"
    "/ilias – neueste ILIAS-Neuigkeiten\n"
    "/log [n] – letzte Änderungen\n\n"
    "🎙️ Sprachnachrichten gehen auch: ich zeige dir, was ich verstanden habe, und lege erst nach „Anlegen“ an."
)


@dataclass
class Reply:
    text: str
    buttons: Buttons = field(default_factory=list)
    changed: bool = False   # True → Planer soll sofort neu laufen


def fmt_minutes(minutes: int) -> str:
    if minutes % 60 == 0:
        return f"{minutes // 60} h"
    return f"{minutes} Min." if minutes < 60 else f"{minutes // 60} h {minutes % 60} Min."


def fmt_deadline(value: str) -> str:
    if len(value) == 10:
        return datetime.fromisoformat(value).strftime("%d.%m.")
    return datetime.fromisoformat(value).astimezone(TZ).strftime("%d.%m. %H:%M")


def fmt_when(deadline: str | None, not_before: str | None) -> str:
    """„am 29.10.“ (an dem Tag erledigen), „bis 30.10. 14:00“ (Frist) oder „ohne Frist“."""
    if not_before and deadline and deadline[:10] == not_before:
        return f"am {fmt_deadline(deadline)}"
    parts = [f"ab {fmt_deadline(not_before)}"] if not_before else []
    parts += [f"bis {fmt_deadline(deadline)}"] if deadline else []
    return " ".join(parts) or "ohne Frist"


def fmt_slot(start: str, end: str) -> str:
    """„fest Do 29.10. 16:30–17:30“"""
    s, e = (datetime.fromisoformat(x).astimezone(TZ) for x in (start, end))
    return f"fest {('Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So')[s.weekday()]} {s:%d.%m. %H:%M}–{e:%H:%M}"


def _task_line(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    bits = [f"#{row['id']} {row['title']}"]
    if row["fixed_start"]:
        bits.append(fmt_slot(row["fixed_start"], row["fixed_end"]))
    elif row["deadline"] or row["not_before"]:
        bits.append(fmt_when(row["deadline"], row["not_before"]))
    if row["estimate_min"] and not row["fixed_start"]:
        bits.append(fmt_minutes(row["estimate_min"]))
    area = areas.path_of(conn, row["area_id"])
    if area:
        bits.append(f"#{area}")
    return " · ".join(bits)


AREA_CHOICES = {"uni": ("🎓 Uni", "Uni"), "arbeit": ("💼 Arbeit", "Arbeit"), "privat": ("🌿 Privat", "Privat"),
                "keiner": ("🔵 Planer", None)}


def _area_path(conn: sqlite3.Connection, task_id: int) -> str:
    row = conn.execute("SELECT area_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return areas.path_of(conn, row["area_id"]) if row else ""


def area_buttons(conn: sqlite3.Connection, task_id: int) -> list[tuple[str, str]]:
    """Kalender mit einem Tipp wählen (Uni/Arbeit/Leisure Time/Planer); die aktuelle Wahl trägt ein ✓."""
    root = _area_path(conn, task_id).split("/", 1)[0].lower() or "keiner"
    return [(("✓ " if key == root else "") + label, f"ar:{task_id}:{key}") for key, (label, _p) in AREA_CHOICES.items()]


def calendar_line(conn: sqlite3.Connection, task_id: int) -> str:
    name = Rules().topic_calendar(_area_path(conn, task_id)) or "Planer"
    return f"📅 Kalender: {name} – unten tippen, um ihn zu ändern"


def _set_area(conn: sqlite3.Connection, arg: str) -> Reply:
    """/bereich <Nr> <Bereich|keiner> bzw. Button „ar:<Nr>:<uni|arbeit|privat>“."""
    num, _, value = arg.strip().partition(" ")
    value = value.strip().lstrip("#")
    if not num.isdigit() or not value:
        return Reply("So geht's: /bereich 3 Privat  (oder Uni, Arbeit, keiner)")
    row = conn.execute("SELECT title FROM tasks WHERE id = ?", (int(num),)).fetchone()
    if row is None:
        return Reply(f"Aufgabe #{num} gibt es nicht.")
    path = AREA_CHOICES.get(value.lower(), (None, value))[1]
    none = value.lower() in {"keiner", "kein", "keine", "-"}
    area_id = None if none else areas.ensure_path(conn, path, actor="user")
    tasks.set_area(conn, int(num), area_id, actor="user")
    shown = "ohne Bereich" if none else f"#{areas.path_of(conn, area_id)}"
    return Reply(f"#{num} „{row['title']}“: {shown} ✅\n{calendar_line(conn, int(num))}",
                 [area_buttons(conn, int(num))], changed=True)


def _task_buttons(task_id: int) -> list[tuple[str, str]]:
    return [(f"✅ #{task_id} erledigt", f"done:{task_id}"), (f"🗑 #{task_id} verwerfen", f"drop:{task_id}"),
            (f"🚫 #{task_id} nie wieder", f"blk:{task_id}")]


def _title(conn: sqlite3.Connection, table: str, row_id: int) -> str:
    row = conn.execute(f"SELECT title FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return row["title"] if row else f"#{row_id}"


# -- Kommandos ------------------------------------------------------------------------------
def today_overview(conn: sqlite3.Connection, now: datetime | None = None) -> str:
    now = (now or datetime.now(TZ)).astimezone(TZ)
    today = now.date()
    lines: list[str] = []

    events = [
        r for r in conn.execute(
            "SELECT * FROM external_events WHERE state = 'active' AND all_day = 0 ORDER BY start")
        if datetime.fromisoformat(r["start"]).astimezone(TZ).date() == today
    ]
    if events:
        lines.append("📅 Termine")
        for r in events:
            s, e = (datetime.fromisoformat(r[k]).astimezone(TZ) for k in ("start", "end"))
            room = f" ({r['location']})" if r["location"] else ""
            lines.append(f"  {s:%H:%M}–{e:%H:%M} {r['title']}{room}")

    blocks = [
        r for r in conn.execute(
            "SELECT b.start, b.end, t.title FROM blocks b JOIN tasks t ON t.id = b.task_id "
            "WHERE b.state = 'active' AND t.status = 'open' ORDER BY b.start")
        if datetime.fromisoformat(r["start"]).astimezone(TZ).date() == today
    ]
    if blocks:
        lines.append("🧱 Geplant")
        for r in blocks:
            s, e = (datetime.fromisoformat(r[k]).astimezone(TZ) for k in ("start", "end"))
            lines.append(f"  {s:%H:%M}–{e:%H:%M} {r['title']}")

    due = []
    for r in tasks.open_tasks(conn):
        if r["deadline"] and tasks_deadline_date(r["deadline"]) <= today:
            overdue = tasks_deadline_date(r["deadline"]) < today
            due.append(f"  {'⏰ überfällig: ' if overdue else ''}{_task_line(conn, r)}")
    if due:
        lines.append("❗ Fällig")
        lines += due

    return "\n".join(lines) if lines else "Heute ist nichts geplant."


def tasks_deadline_date(value: str):
    from .parsing import deadline_to_dt

    return deadline_to_dt(value).astimezone(TZ).date()


def open_tasks_reply(conn: sqlite3.Connection, limit: int = 10) -> Reply:
    rows = tasks.open_tasks(conn)
    if not rows:
        return Reply("Keine offenen Aufgaben. 🎉")
    shown = rows[:limit]
    text = "\n".join(_task_line(conn, r) for r in shown)
    if len(rows) > limit:
        text += f"\n… und {len(rows) - limit} weitere"
    return Reply(text, [_task_buttons(int(r["id"])) for r in shown])


def proposals_reply(conn: sqlite3.Connection) -> Reply:
    rows = proposals.pending(conn)
    if not rows:
        return Reply("Keine offenen Vorschläge.")
    return Reply("\n".join(proposals.describe(r) for r in rows),
                 [[(f"✅ #{r['id']} ja", f"acc:{r['id']}"), (f"❌ #{r['id']} nein", f"rej:{r['id']}")] for r in rows])


def ilias_overview(conn: sqlite3.Connection, days: int = 14, limit: int = 8) -> str:
    """Neueste ILIAS-Neuigkeiten aus der lokalen Datenbank (kein Netzwerkzugriff)."""
    cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat()
    rows = [r for r in conn.execute("SELECT * FROM ilias_news ORDER BY ts DESC LIMIT 200")
            if datetime.fromisoformat(r["ts"]) >= datetime.fromisoformat(cutoff)][:limit]
    if not rows:
        return f"Keine ILIAS-Neuigkeiten in den letzten {days} Tagen."
    lines = []
    for r in rows:
        ts = datetime.fromisoformat(r["ts"]).astimezone(TZ)
        text = f" – {r['body'][:60]}" if r["body"] else ""
        lines.append(f"{ts:%d.%m.} {r['title'][:60]}{text}  ({r['author'].split(',')[0][:18]})")
    return "\n".join(lines)


def log_reply(conn: sqlite3.Connection, n: int = 8) -> str:
    rows = changelog.recent(conn, max(1, min(n, 20)))
    if not rows:
        return "Noch keine Einträge."
    out = []
    for r in reversed(rows):
        ts = datetime.fromisoformat(r["ts"]).astimezone(TZ).strftime("%d.%m. %H:%M")
        why = f" – {r['reason']}" if r["reason"] else ""
        out.append(f"{ts} {r['action']} {r['entity']}:{r['entity_id'] or '-'}{why}")
    return "\n".join(out)


def _set_field(conn: sqlite3.Connection, arg: str, what: str) -> Reply:
    """/frist <Nr> <Datum|keine> bzw. /aufwand <Nr> <Dauer>."""
    from .parsing import parse_deadline, parse_estimate

    num, _, value = arg.strip().partition(" ")
    value = value.strip()
    example = "/frist 3 20.10. 18:00" if what == "frist" else "/aufwand 3 2h"
    if not num.lstrip("#").isdigit() or not value:
        return Reply(f"So geht’s: {example}")
    task_id = int(num.lstrip("#"))
    try:
        title = _title(conn, "tasks", task_id)
        if what == "frist":
            clear = value.lower() in {"keine", "-", "weg"}
            tasks.set_deadline(conn, task_id, None if clear else parse_deadline(value), actor="user")
            shown = "entfernt" if clear else f"bis {fmt_deadline(parse_deadline(value))}"
        else:
            minutes = parse_estimate(value)
            tasks.set_estimate(conn, task_id, minutes, actor="user")
            shown = fmt_minutes(minutes)
    except KeyError:
        return Reply(f"Aufgabe #{task_id} gibt es nicht.")
    except ValueError as exc:
        return Reply(str(exc))
    return Reply(f"#{task_id} „{title}“: {shown} ✅", changed=True)


def _ilias_to_task(conn: sqlite3.Connection, news_id: str) -> Reply:
    """Button unter einer ILIAS-Meldung: erst auf Klick wird daraus eine Aufgabe."""
    row = conn.execute("SELECT * FROM ilias_news WHERE news_id = ?", (news_id,)).fetchone()
    if row is None:
        return Reply("Den ILIAS-Eintrag gibt es nicht mehr.")
    task_id = tasks.add_task(conn, title=f"ILIAS: {row['title']}"[:200], source="ilias", actor="user",
                             source_ref=news_id, area_id=areas.ensure_path(conn, "Uni", actor="user"))
    if task_id is None:
        return Reply("Dazu gibt es schon eine Aufgabe – oder der Titel ist gesperrt (Blacklist).")
    return Reply(f"✅ Aufgabe #{task_id} angelegt: ILIAS: {row['title']}\n"
                 f"Frist ergänzen: /frist {task_id} 20.10.  ·  Aufwand: /aufwand {task_id} 2h",
                 [_task_buttons(task_id)], changed=True)


def _set_status(conn: sqlite3.Connection, arg: str, status: str) -> Reply:
    try:
        task_id = int(arg.lstrip("#"))
        title = _title(conn, "tasks", task_id)
        tasks.set_status(conn, task_id, status, actor="user")
    except (ValueError, KeyError):
        return Reply("Welche Aufgabe? Beispiel: /erledigt 3")
    verb = "erledigt ✅" if status == "done" else "verworfen 🗑"
    return Reply(f"#{task_id} „{title}“ {verb}.", changed=True)


def _blacklist_task(conn: sqlite3.Connection, arg: str) -> Reply:
    """Setzt den Titel der Aufgabe dauerhaft auf die Blacklist und verwirft die aktuelle Instanz."""
    try:
        task_id = int(arg.lstrip("#"))
        row = conn.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        title = row["title"]
        tasks.set_status(conn, task_id, "dropped", actor="user", reason="Auf Blacklist gesetzt")
    except (ValueError, KeyError):
        return Reply("Welche Aufgabe? Beispiel: /sperren 3")
    note = ("stand schon auf der Blacklist" if blacklist.add(conn, title, actor="user") is None
            else "auf die Blacklist gesetzt – wird nie wieder automatisch angelegt")
    return Reply(f"🚫 „{title}“ {note}. #{task_id} verworfen.\nMit /sperrliste siehst du alle Einträge.",
                 changed=True)


def blacklist_reply(conn: sqlite3.Connection) -> Reply:
    rows = blacklist.list_all(conn)
    if not rows:
        return Reply("Die Blacklist ist leer.")
    text = "🚫 Blacklist:\n" + "\n".join(f"#{r['id']} „{r['title']}“" for r in rows)
    return Reply(text, [[(f"🗑 #{r['id']} entfernen", f"unblk:{r['id']}")] for r in rows])


# -- Einstiegspunkte ----------------------------------------------------------------------
def handle_message(
    conn: sqlite3.Connection, text: str, *, message_id: int | None = None, now: datetime | None = None
) -> Reply:
    text = (text or "").strip()
    if not text:
        return Reply(HELP)
    if text.startswith("/"):
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd in {"/start", "/hilfe", "/help"}:
            return Reply(HELP)
        if cmd == "/heute":
            return Reply(today_overview(conn, now))
        if cmd == "/aufgaben":
            return open_tasks_reply(conn)
        if cmd in {"/vorschlaege", "/vorschläge"}:
            return proposals_reply(conn)
        if cmd == "/ilias":
            return Reply(ilias_overview(conn))
        if cmd == "/log":
            return Reply(log_reply(conn, int(arg) if arg.isdigit() else 8))
        if cmd == "/bereich":
            return _set_area(conn, arg)
        if cmd in {"/frist", "/aufwand"}:
            return _set_field(conn, arg, cmd[1:])
        if cmd == "/erledigt":
            return _set_status(conn, arg, "done")
        if cmd == "/verwerfen":
            return _set_status(conn, arg, "dropped")
        if cmd == "/sperren":
            return _blacklist_task(conn, arg)
        if cmd in {"/sperrliste", "/blacklist"}:
            return blacklist_reply(conn)
        return Reply("Das Kommando kenne ich nicht.\n\n" + HELP)
    titled = voice.receive_title(conn, text)   # Antwort auf „✏️ Titel“ unter einer Sprachnachricht?
    if titled is not None:
        return Reply(*titled)
    waiting = feedback.awaiting(conn)   # Antwort auf „✏️ Frist ändern“?
    if waiting is not None:
        got = feedback.receive_deadline(conn, waiting, text)
        if got is not None:
            return Reply(got[0], got[1], changed=True)
    return quick_add(conn, text, message_id=message_id, now=now)


def quick_add(
    conn: sqlite3.Connection, text: str, *, message_id: int | None = None, now: datetime | None = None
) -> Reply:
    today = (now or datetime.now(TZ)).astimezone(TZ).date()
    try:
        q = parse_quick(text, today=today)
    except ValueError as exc:
        return Reply(str(exc))
    area_id = areas.ensure_path(conn, q.area, actor="user") if q.area else None
    task_id = tasks.add_task(
        conn, title=q.title, source="telegram", actor="user", area_id=area_id, deadline=q.deadline,
        not_before=q.not_before, fixed_start=q.start, fixed_end=q.end, estimate_min=q.estimate_min,
        source_ref=f"msg:{message_id}" if message_id else None)
    if task_id is None:
        return Reply("Diese Nachricht hatte ich schon als Aufgabe angelegt – oder der Titel ist gesperrt (Blacklist).")
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    plan = "Ich trage sie genau dann ein." if q.start else "Ich plane sie ein."
    return Reply(f"✅ Aufgabe angelegt:\n{_task_line(conn, row)}\n{plan}\n{calendar_line(conn, task_id)}",
                 [_task_buttons(task_id), area_buttons(conn, task_id)], changed=True)


def handle_callback(conn: sqlite3.Connection, data: str) -> Reply:
    action, _, raw = (data or "").partition(":")
    if action == "ilt":
        return _ilias_to_task(conn, raw)
    if action == "ar":   # Bereich per Button: ar:<Nr>:<uni|arbeit|privat>
        num, _, key = raw.partition(":")
        return _set_area(conn, f"{num} {key}")
    if action == "vo":   # Sprachnachricht: vo:<Nr>:ok|edit|no
        num, _, choice = raw.partition(":")
        if not num.isdigit():
            return Reply("Unbekannte Aktion.")
        if choice == "edit":
            return Reply(*voice.ask_title(conn, int(num)))
        text, buttons, changed = voice.decide(conn, int(num), choice == "ok")
        return Reply(text, buttons, changed=changed)
    if action == "fb":   # Einschätzung zu einer Mail-Aufgabe: fb:<Nr>:<Feld>:<Wert>
        num, _, rest = raw.partition(":")
        fld, _, value = rest.partition(":")
        if not num.isdigit():
            return Reply("Unbekannte Aktion.")
        text, buttons, changed = feedback.apply(conn, int(num), fld, value)
        return Reply(text, buttons, changed=changed)
    try:
        item_id = int(raw)
    except ValueError:
        return Reply("Unbekannte Aktion.")
    try:
        if action == "acc":
            prop = conn.execute("SELECT title, kind FROM proposals WHERE id = ?", (item_id,)).fetchone()
            title = prop["title"] if prop else _title(conn, "proposals", item_id)
            task_id = proposals.accept(conn, item_id, actor="user")
            if prop is not None and prop["kind"] == "task" and task_id is None:
                return Reply(f"🚫 „{title}“ nicht angelegt – der Titel ist gesperrt (Blacklist).", changed=True)
            what = f"als Aufgabe #{task_id} übernommen" if task_id else "wird in den Kalender eingetragen"
            return Reply(f"✅ „{title}“ {what}.", changed=True)
        if action == "rej":
            title = _title(conn, "proposals", item_id)
            proposals.reject(conn, item_id, actor="user")
            return Reply(f"❌ „{title}“ verworfen – es landet nichts im Kalender.")
        if action in {"done", "drop"}:
            return _set_status(conn, str(item_id), "done" if action == "done" else "dropped")
        if action == "blk":
            return _blacklist_task(conn, str(item_id))
        if action == "unblk":
            title = blacklist.remove(conn, item_id, actor="user")
            return Reply(f"✅ „{title}“ von der Blacklist entfernt.")
    except KeyError:
        return Reply("Das gibt es nicht mehr.")
    except ValueError as exc:
        return Reply(str(exc))   # z. B. „Vorschlag #3 ist schon accepted“
    return Reply("Unbekannte Aktion.")
