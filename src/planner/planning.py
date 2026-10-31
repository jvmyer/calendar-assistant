"""Planungslauf: Kalender lesen → Nutzer-Eingriffe erkennen → planen → Differenz schreiben.

Grundsätze (siehe docs/KONZEPT.md):
- Der Mensch gewinnt: verschobene Blocker werden gepinnt, gelöschte Blocker nicht an
  derselben Stelle neu angelegt.
- Heute und morgen bleiben stabil: bestehende Blocker dort werden nur gelöst, wenn sie mit
  einem neuen Termin kollidieren oder ihre Aufgabe erledigt ist.
- Jede Änderung geht mit Begründung ins Change-Log.
- Aufgaben mit festem Zeitblock („von 16:30 bis 17:30“) bekommen genau diesen Blocker, gepinnt – nie umgeplant.
- Blocker stehen im Kalender ihres Themas (`Rules.topic_calendars`), sonst im Kalender „Planer“.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from . import changelog
from .areas import path_of, weight_of
from .db import get_meta, set_meta
from .parsing import deadline_to_dt
from .rules import TZ, Rules
from .scheduler import Interval, Placement, TaskInput, free_windows, minutes, schedule
from .tasks import open_tasks, set_status

MAX_LINES = 8


@dataclass
class Change:
    kind: str                       # created | moved | deleted
    task_id: int
    title: str
    start: datetime | None = None
    end: datetime | None = None
    old_start: datetime | None = None


@dataclass
class PlanReport:
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)   # nur neue (noch nicht gemeldete)
    unplaced: dict[int, int] = field(default_factory=dict)   # task_id -> Minuten ohne Slot
    overdue: set[int] = field(default_factory=set)
    pinned: int = 0
    removed_by_user: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TZ)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%a %d.%m. %H:%M")


def _event_interval(ev: dict) -> Interval | None:
    """Belegtes Intervall eines Google-Events; None für Ganztägiges, Freies, Abgesagtes."""
    if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
        return None
    start, end = ev.get("start", {}).get("dateTime"), ev.get("end", {}).get("dateTime")
    if not start or not end:
        return None
    if any(a.get("self") and a.get("responseStatus") == "declined" for a in ev.get("attendees", [])):
        return None
    return _dt(start), _dt(end)


def _skip_calendar(cal: dict, rules: Rules) -> bool:
    cid = cal.get("id", "")
    # Feiertage/Geburtstage sind keine Termine. Abonnierte ICS-Kalender (@import…) dagegen schon
    # (z. B. Uni-Kalender) und zählen als belegt.
    if "#holiday@" in cid or "#contacts@" in cid:
        return True
    ignored = {n.lower() for n in rules.ignore_calendars}
    return (cal.get("summaryOverride") or cal.get("summary") or "").lower() in ignored


def _collect_busy(cal, rules: Rules, now: datetime, horizon_end: datetime, own: set[str]) -> list[Interval]:
    """Alles außer unseren eigenen Blockern (`own`) zählt als belegt – auch Termine im Planer- und in den Themen-Kalendern."""
    busy: list[Interval] = []
    for c in cal.list_calendars():
        if _skip_calendar(c, rules):
            continue
        for ev in cal.list_events(c["id"], now, horizon_end):
            iv = _event_interval(ev)
            if iv and ev.get("id") not in own:
                busy.append(iv)
    return busy


def _reconcile(conn: sqlite3.Connection, events: dict[str, dict], now: datetime, report: PlanReport) -> None:
    """Vergleicht gespeicherte Blocker mit dem Kalender: verschoben → gepinnt, gelöscht → gemerkt.

    War das der letzte aktive Block einer Aufgabe, gilt die Aufgabe als verworfen (sonst würde der
    nächste Planungslauf einfach eine neue Zeit dafür suchen – der Mensch gewinnt, auch beim Löschen).
    """
    rows = conn.execute(
        "SELECT * FROM blocks WHERE state = 'active' AND end > ?", (_iso(now),)
    ).fetchall()
    for row in rows:
        ev = events.get(row["gcal_event_id"])
        if ev is None or ev.get("status") == "cancelled":
            conn.execute("UPDATE blocks SET state = 'removed' WHERE id = ?", (row["id"],))
            conn.commit()
            changelog.log(conn, actor="user", action="block.removed", entity="block", entity_id=row["id"],
                          detail={"task_id": row["task_id"], "start": row["start"]},
                          reason="Im Kalender gelöscht – Zeitfenster wird nicht erneut belegt")
            report.removed_by_user += 1
            other_active = conn.execute(
                "SELECT 1 FROM blocks WHERE task_id = ? AND state = 'active' AND id != ?",
                (row["task_id"], row["id"])).fetchone()
            if other_active is None:
                set_status(conn, row["task_id"], "dropped", actor="user",
                           reason="Letzter Blocker der Aufgabe im Kalender gelöscht")
            continue
        raw_start, raw_end = ev.get("start", {}).get("dateTime"), ev.get("end", {}).get("dateTime")
        if not raw_start or not raw_end:   # in Ganztägiges umgewandelt: ignorieren
            continue
        new_start, new_end = _dt(raw_start), _dt(raw_end)
        if new_start != _dt(row["start"]) or new_end != _dt(row["end"]):
            conn.execute(
                "UPDATE blocks SET start = ?, end = ?, pinned = 1 WHERE id = ?",
                (_iso(new_start), _iso(new_end), row["id"]),
            )
            conn.commit()
            changelog.log(conn, actor="user", action="block.pinned", entity="block", entity_id=row["id"],
                          detail={"task_id": row["task_id"], "from": row["start"], "to": _iso(new_start)},
                          reason="Im Kalender verschoben – wird nicht mehr automatisch geändert")
            report.pinned += 1


def close_elapsed(conn: sqlite3.Connection, now: datetime, default_estimate_min: int) -> list[str]:
    """Ohne Abhaken im Kalender: ist die geplante Zeit einer Aufgabe komplett verstrichen und nichts mehr
    offen (kein weiterer Blocker), gilt sie als erledigt. Aufgaben mit noch unverplantem Rest bleiben offen.
    """
    now_iso = _iso(now)
    closed: list[str] = []
    for t in conn.execute("SELECT * FROM tasks WHERE status = 'open'").fetchall():
        if conn.execute("SELECT 1 FROM blocks WHERE task_id = ? AND state = 'active' AND end > ?",
                        (t["id"], now_iso)).fetchone():
            continue   # es steht noch ein Blocker an
        worked = sum(minutes(_dt(b["start"]), _dt(b["end"])) for b in conn.execute(
            "SELECT start, end FROM blocks WHERE task_id = ? AND state = 'active' AND end <= ?", (t["id"], now_iso)))
        if worked and worked >= (t["estimate_min"] or default_estimate_min):
            set_status(conn, t["id"], "done", actor="planner", reason="Geplante Zeit verstrichen")
            closed.append(t["title"])
    return closed


def _overlaps(start: datetime, end: datetime, busy: list[Interval]) -> bool:
    return any(bs < end and be > start for bs, be in busy)


def _diff(
    released: list[sqlite3.Row], placements: list[Placement]
) -> tuple[list[tuple[sqlite3.Row, Placement]], list[sqlite3.Row], list[Placement]]:
    """Ordnet gelöste Blocker den neuen Platzierungen zu: (verschoben, zu löschen, neu).

    Identische Blocker (gleiche Aufgabe und Zeit) bleiben unberührt und tauchen nirgends auf.
    """
    old = list(released)
    new = list(placements)
    for row in list(old):   # identische behalten
        for p in new:
            if p.task_id == row["task_id"] and p.start == _dt(row["start"]) and p.end == _dt(row["end"]):
                old.remove(row)
                new.remove(p)
                break
    moved: list[tuple[sqlite3.Row, Placement]] = []
    for row in sorted(old, key=lambda r: r["start"]):
        match = next((p for p in sorted(new, key=lambda p: p.start) if p.task_id == row["task_id"]), None)
        if match is not None:
            moved.append((row, match))
            new.remove(match)
    moved_ids = {r["id"] for r, _ in moved}
    deleted = [r for r in old if r["id"] not in moved_ids]
    return moved, deleted, new


def run_plan(
    conn: sqlite3.Connection,
    cal,
    rules: Rules,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> PlanReport:
    """Ein vollständiger Planungslauf. Mit `dry_run` wird nichts geschrieben (außer Nutzer-Eingriffe zu merken)."""
    now = (now or datetime.now(UTC)).astimezone(TZ)
    horizon_end = now + timedelta(days=rules.horizon_days + 1)
    freeze_end = datetime.combine(now.date() + timedelta(days=rules.freeze_days), time(0), TZ)
    report = PlanReport()

    planner_id = cal.ensure_planner_calendar()
    block_cals = {planner_id} | {r["calendar_id"] for r in conn.execute(
        "SELECT DISTINCT calendar_id FROM blocks WHERE state = 'active' AND end > ?", (_iso(now),))}
    block_events = {e["id"]: e for cid in sorted(block_cals) for e in cal.list_events(cid, now, horizon_end) if "id" in e}
    _reconcile(conn, block_events, now, report)
    if not dry_run:
        close_elapsed(conn, now, rules.default_estimate_min)
    own = {r["gcal_event_id"] for r in conn.execute("SELECT gcal_event_id FROM blocks")}
    busy = _collect_busy(cal, rules, now, horizon_end, own)

    tasks = {int(t["id"]): t for t in open_tasks(conn)}
    all_active = conn.execute("SELECT * FROM blocks WHERE state = 'active'").fetchall()
    now_iso = _iso(now)
    done_min: dict[int, int] = {}
    future: list[sqlite3.Row] = []
    for b in all_active:
        if b["end"] <= now_iso:
            done_min[b["task_id"]] = done_min.get(b["task_id"], 0) + minutes(_dt(b["start"]), _dt(b["end"]))
        else:
            future.append(b)

    kept: list[sqlite3.Row] = []
    released: list[tuple[sqlite3.Row, str]] = []
    for b in future:
        s, e = _dt(b["start"]), _dt(b["end"])
        if b["task_id"] not in tasks:
            released.append((b, "Aufgabe erledigt oder gestrichen"))
        elif b["pinned"]:
            kept.append(b)
        elif s < freeze_end and not _overlaps(s, e, busy):
            kept.append(b)      # heute/morgen stabil
        else:
            released.append((b, "Umplanung"))

    kept_min: dict[int, int] = {}
    for b in kept:
        kept_min[b["task_id"]] = kept_min.get(b["task_id"], 0) + minutes(_dt(b["start"]), _dt(b["end"]))

    # Feste Zeitblöcke: einmal anlegen (gepinnt). Hat der Nutzer ihn schon gelöscht oder verschoben, bleibt es dabei.
    has_block = {r["task_id"] for r in conn.execute("SELECT DISTINCT task_id FROM blocks")}
    fixed_new = [(tid, _dt(t["fixed_start"]), _dt(t["fixed_end"])) for tid, t in tasks.items()
                 if t["fixed_start"] and tid not in has_block]

    inputs: list[TaskInput] = []
    for tid, t in tasks.items():
        if t["fixed_start"]:
            continue
        estimate = t["estimate_min"] or rules.default_estimate_min
        remaining = estimate - done_min.get(tid, 0) - kept_min.get(tid, 0)
        if remaining <= 0:
            continue
        inputs.append(TaskInput(
            id=tid,
            title=t["title"],
            remaining_min=remaining,
            deadline=deadline_to_dt(t["deadline"]) if t["deadline"] else None,
            not_before=datetime.combine(date.fromisoformat(t["not_before"]), time(0), TZ) if t["not_before"] else None,
            weight=weight_of(conn, t["area_id"]),
            boost=float(t["priority_boost"]),
        ))

    rejected = conn.execute("SELECT start, end FROM blocks WHERE state = 'removed' AND end > ?", (now_iso,)).fetchall()
    fixed: list[Interval] = [(_dt(b["start"]), _dt(b["end"])) for b in kept]
    fixed += [(_dt(r["start"]), _dt(r["end"])) for r in rejected]
    fixed += [(s, e) for _tid, s, e in fixed_new]

    free = free_windows(now, rules, busy, fixed)
    result = schedule(inputs, free, rules, now)

    # Nur lösbare Blocker diffen: gelöste Blocker erledigter Aufgaben nie „verschieben“
    live_released = [b for b, _ in released if b["task_id"] in tasks]
    dead_released = [b for b, _ in released if b["task_id"] not in tasks]
    moved, deleted, created = _diff(live_released, result.placements)
    deleted += dead_released
    reasons = {b["id"]: why for b, why in released}

    def title_of(task_id: int) -> str:
        row = conn.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row["title"] if row else f"#{task_id}"

    for row in deleted:
        report.changes.append(Change("deleted", row["task_id"], title_of(row["task_id"]),
                                     _dt(row["start"]), _dt(row["end"])))
    for row, p in moved:
        report.changes.append(Change("moved", p.task_id, title_of(p.task_id), p.start, p.end, _dt(row["start"])))
    for p in created:
        report.changes.append(Change("created", p.task_id, title_of(p.task_id), p.start, p.end))
    for tid, s, e in fixed_new:
        report.changes.append(Change("created", tid, title_of(tid), s, e))

    report.unplaced, report.overdue = dict(result.unplaced), set(result.overdue)

    # Warnungen: neu oder verändert sofort, ungelöste einmal pro Tag erneut – wichtige Aufgaben
    # dürfen nicht untergehen (eine ganztägige Task als Auffangnetz gibt es seit 2026-10-26 nicht mehr).
    today = now.date().isoformat()
    current: dict[str, tuple[str, str]] = {}
    for tid, missing in result.unplaced.items():
        t = tasks[tid]
        current[f"unplaced:{tid}"] = (f"{missing}@{today}", (
            f"⚠️ „{t['title']}“: {missing} Min. passen nicht mehr vor die Deadline – bitte selbst Zeit schaffen (ich erinnere täglich)"
            if t["deadline"] and tid not in result.overdue else
            f"⚠️ „{t['title']}“: {missing} Min. finden im Planungshorizont keinen Platz – ich erinnere täglich, bis Zeit frei wird"))
    for tid in result.overdue:
        current[f"overdue:{tid}"] = (f"1@{today}", f"⏰ „{tasks[tid]['title']}“ ist überfällig – wird so früh wie möglich eingeplant")
    previous = json.loads(get_meta(conn, "plan_warnings") or "{}")
    report.warnings = [text for key, (sig, text) in current.items() if previous.get(key) != sig]

    if dry_run:
        return report

    set_meta(conn, "plan_warnings", json.dumps({k: sig for k, (sig, _) in current.items()}))
    created_at = changelog.now_iso()

    for row in deleted:
        cal.delete_block(row["calendar_id"], row["gcal_event_id"])
        conn.execute("UPDATE blocks SET state = 'released' WHERE id = ?", (row["id"],))
        conn.commit()
        changelog.log(conn, actor="planner", action="block.deleted", entity="block", entity_id=row["id"],
                      detail={"task_id": row["task_id"], "start": row["start"]},
                      reason=reasons.get(row["id"], "Umplanung"))
    for row, p in moved:
        cal.update_block(row["calendar_id"], row["gcal_event_id"], title_of(p.task_id), p.start, p.end, task_id=p.task_id,
                         description=_description(conn, p.task_id))
        conn.execute("UPDATE blocks SET start = ?, end = ? WHERE id = ?", (_iso(p.start), _iso(p.end), row["id"]))
        conn.commit()
        changelog.log(conn, actor="planner", action="block.moved", entity="block", entity_id=row["id"],
                      detail={"task_id": p.task_id, "from": row["start"], "to": _iso(p.start)},
                      reason="Neu geplant (Änderung im Kalender oder neue Aufgabe)")
    topic_ids: dict[str, str] = {}

    def calendar_of(task_id: int) -> str:
        row = conn.execute("SELECT area_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        name = rules.topic_calendar(path_of(conn, row["area_id"])) if row else None
        if name is None:
            return planner_id
        if name not in topic_ids:
            topic_ids[name] = cal.ensure_calendar(name, f"calendar_id:topic:{name.lower()}")
        return topic_ids[name]

    for task_id, start, end, pinned in [*((p.task_id, p.start, p.end, 0) for p in created),
                                         *((tid, s, e, 1) for tid, s, e in fixed_new)]:
        target = calendar_of(task_id)
        ev = cal.create_block(target, title_of(task_id), start, end, task_id=task_id,
                              description=_description(conn, task_id))
        cur = conn.execute(
            "INSERT INTO blocks(task_id, start, end, gcal_event_id, calendar_id, pinned, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, _iso(start), _iso(end), ev["id"], target, pinned, created_at),
        )
        conn.commit()
        changelog.log(conn, actor="planner", action="block.created", entity="block", entity_id=cur.lastrowid,
                      detail={"task_id": task_id, "start": _iso(start), "end": _iso(end)},
                      reason="Fester Zeitblock des Nutzers" if pinned else "Aufgabe eingeplant")

    # Bestehende Blocker (auch gepinnte) in den Kalender ihres Themas umziehen – Zeit bleibt, wie sie ist
    for row in conn.execute("SELECT * FROM blocks WHERE state = 'active' AND end > ?", (now_iso,)).fetchall():
        if row["task_id"] not in tasks or (target := calendar_of(row["task_id"])) == row["calendar_id"]:
            continue
        cal.move_block(row["calendar_id"], row["gcal_event_id"], target)
        conn.execute("UPDATE blocks SET calendar_id = ? WHERE id = ?", (target, row["id"]))
        conn.commit()
        changelog.log(conn, actor="planner", action="block.relocated", entity="block", entity_id=row["id"],
                      detail={"task_id": row["task_id"], "from": row["calendar_id"], "to": target},
                      reason="In den Kalender des Themas verschoben")
    return report


def _description(conn: sqlite3.Connection, task_id: int) -> str:
    row = conn.execute("SELECT deadline, fixed_start FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row and row["fixed_start"]:
        return f"Fester Zeitblock (Aufgabe #{task_id}) – wird nicht automatisch verschoben."
    lines = [f"Automatisch geplant (Aufgabe #{task_id})."]
    if row and row["deadline"]:
        dl = deadline_to_dt(row["deadline"]).astimezone(TZ)
        lines.append("Frist: " + (dl.strftime("%d.%m.%Y") if len(row["deadline"]) == 10 else dl.strftime("%d.%m.%Y %H:%M")))
    lines.append("Verschieben im Kalender pinnt den Blocker (der Planer fasst ihn dann nicht mehr an).")
    return "\n".join(lines)


def describe_plan(report: PlanReport) -> str | None:
    """Kurze Meldung für den Nutzer; None = nichts zu melden."""
    lines: list[str] = []
    for c in report.changes:
        if c.kind == "created":
            lines.append(f"➕ „{c.title}“ {_fmt(c.start)}–{c.end.astimezone(TZ):%H:%M}")
        elif c.kind == "moved":
            lines.append(f"🔁 „{c.title}“ {_fmt(c.old_start)} → {_fmt(c.start)}")
        else:
            lines.append(f"➖ „{c.title}“ {_fmt(c.start)} entfernt")
    more = len(lines) - MAX_LINES
    lines = lines[:MAX_LINES] + ([f"… und {more} weitere"] if more > 0 else [])
    lines += report.warnings
    if not lines:
        return None
    return "🗓 Planer:\n" + "\n".join(lines)
