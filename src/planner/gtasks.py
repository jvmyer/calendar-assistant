"""Google Tasks: jede offene Aufgabe steht immer als Task im Kalender.

Regel: Gibt es Zeit, steht zusätzlich ein Blocker im Kalender „Planer“ (Notiz der Task nennt ihn).
Gibt es keine Zeit, bleibt nur die Task – sichtbar am Fälligkeitstag, nie stillschweigend weg.

Der Mensch gewinnt: abgehakte Tasks werden im Planer als erledigt übernommen, gelöschte als verworfen.
Titel, Notiz und Fälligkeit werden dagegen vom Planer vorgegeben (Deadline ändert man im Planer).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from googleapiclient.discovery import build

from . import changelog, tasks
from .db import get_meta, set_meta
from .parsing import deadline_to_dt
from .rules import TZ

LIST_TITLE = "Planer"
META_LIST = "tasklist_id:planer"
MAX_SHOWN_BLOCKS = 3


class TasksClient:
    def __init__(self, creds, conn: sqlite3.Connection):
        self._svc = build("tasks", "v1", credentials=creds, cache_discovery=False)
        self._conn = conn

    def ensure_list(self, title: str, meta_key: str) -> str:
        cached = get_meta(self._conn, meta_key)
        if cached:
            return cached
        page = None
        while True:
            res = self._svc.tasklists().list(maxResults=100, pageToken=page).execute()
            for item in res.get("items", []):
                if item.get("title") == title:
                    set_meta(self._conn, meta_key, item["id"])
                    return item["id"]
            page = res.get("nextPageToken")
            if not page:
                break
        created = self._svc.tasklists().insert(body={"title": title}).execute()
        set_meta(self._conn, meta_key, created["id"])
        return created["id"]

    def list_tasks(self, list_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = None
        while True:
            res = self._svc.tasks().list(
                tasklist=list_id, showCompleted=True, showHidden=True, maxResults=100, pageToken=page
            ).execute()
            out += res.get("items", [])
            page = res.get("nextPageToken")
            if not page:
                return out

    def insert(self, list_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._svc.tasks().insert(tasklist=list_id, body=body).execute()

    def patch(self, list_id: str, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._svc.tasks().patch(tasklist=list_id, task=task_id, body=body).execute()

    def delete(self, list_id: str, task_id: str) -> None:
        from googleapiclient.errors import HttpError

        try:
            self._svc.tasks().delete(tasklist=list_id, task=task_id).execute()
        except HttpError as exc:
            if exc.resp.status not in (404, 410):
                raise


@dataclass
class TasksReport:
    created: int = 0
    updated: int = 0
    completed: int = 0
    removed: int = 0
    user_done: list[str] = field(default_factory=list)
    user_dropped: list[str] = field(default_factory=list)


def _load_remote(conn: sqlite3.Connection, tc) -> tuple[str, dict[str, dict], bool]:
    """(Listen-ID, Tasks nach ID, Liste_neu_angelegt).

    Ist die ganze Liste gelöscht (404), wird sie neu angelegt: das ist kein Verwerfen der Aufgaben,
    sondern wird beim Nachziehen (`push`) komplett wiederhergestellt.
    """
    from googleapiclient.errors import HttpError

    list_id = tc.ensure_list(LIST_TITLE, META_LIST)
    try:
        return list_id, {t["id"]: t for t in tc.list_tasks(list_id)}, False
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
    set_meta(conn, META_LIST, "")
    new_id = tc.ensure_list(LIST_TITLE, META_LIST)
    changelog.log(conn, actor="system", action="gtasks.list_recreated", entity="gtasks",
                  reason="Task-Liste „Planer“ war gelöscht – wird neu angelegt, Aufgaben bleiben erhalten")
    return new_id, {}, True


def pull_changes(conn: sqlite3.Connection, tc) -> TasksReport:
    """Übernimmt, was der Nutzer in Google Tasks getan hat: abgehakt → erledigt, gelöscht → verworfen."""
    report = TasksReport()
    list_id, remote, fresh = _load_remote(conn, tc)
    if fresh:
        return report
    rows = conn.execute(
        "SELECT g.gtask_id, t.id, t.title FROM gtasks g JOIN tasks t ON t.id = g.task_id "
        "WHERE t.status = 'open' AND g.tasklist_id = ?", (list_id,)
    ).fetchall()
    for row in rows:
        r = remote.get(row["gtask_id"])
        if r is None:
            tasks.set_status(conn, row["id"], "dropped", actor="user", reason="Task in Google Tasks gelöscht")
            report.user_dropped.append(row["title"])
        elif r.get("status") == "completed":
            tasks.set_status(conn, row["id"], "done", actor="user", reason="Task in Google Tasks abgehakt")
            report.user_done.append(row["title"])
    return report


def utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _fmt(start_iso: str, end_iso: str) -> str:
    s, e = datetime.fromisoformat(start_iso).astimezone(TZ), datetime.fromisoformat(end_iso).astimezone(TZ)
    return f"{s:%a %d.%m. %H:%M}–{e:%H:%M}"


def _notes(task_id: int, future: list[sqlite3.Row], has_past: bool, unplaced_min: int | None) -> str:
    lines: list[str] = []
    if future:
        shown = [_fmt(b["start"], b["end"]) for b in future[:MAX_SHOWN_BLOCKS]]
        more = len(future) - MAX_SHOWN_BLOCKS
        lines.append("🗓 Eingeplant: " + ", ".join(shown) + (f" (+{more} weitere)" if more > 0 else ""))
    if unplaced_min:
        lines.append(f"⚠️ {unplaced_min} Min. ohne freien Slot – bitte selbst Zeit schaffen.")
    elif not future:
        lines.append("Geplante Zeit ist vorbei – bitte abhaken oder neu planen." if has_past
                     else "Noch nicht eingeplant.")
    lines.append(f"Aufgabe #{task_id} (Planer)")
    return "\n".join(lines)


def _due_date(task: sqlite3.Row, future: list[sqlite3.Row], remote_due: str | None, now: datetime) -> str:
    if task["deadline"]:
        return deadline_to_dt(task["deadline"]).astimezone(TZ).date().isoformat()
    if future:
        return datetime.fromisoformat(future[0]["start"]).astimezone(TZ).date().isoformat()
    return remote_due[:10] if remote_due else now.astimezone(TZ).date().isoformat()   # nie ohne Datum


def push(
    conn: sqlite3.Connection, tc, *, now: datetime | None = None, unplaced: dict[int, int] | None = None
) -> TasksReport:
    """Schreibt den Planer-Stand nach Google Tasks: jede offene Aufgabe hat eine Task."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    unplaced = unplaced or {}
    report = TasksReport()
    list_id, remote, _ = _load_remote(conn, tc)
    mapping = {r["task_id"]: r for r in conn.execute("SELECT * FROM gtasks")}
    future_by_task: dict[int, list[sqlite3.Row]] = {}
    for b in conn.execute("SELECT * FROM blocks WHERE state = 'active' AND end > ? ORDER BY start", (utc(now),)):
        future_by_task.setdefault(b["task_id"], []).append(b)
    past_tasks = {r["task_id"] for r in conn.execute(
        "SELECT DISTINCT task_id FROM blocks WHERE state = 'active' AND end <= ?", (utc(now),))}

    for t in tasks.open_tasks(conn):
        tid = int(t["id"])
        future = future_by_task.get(tid, [])
        m = mapping.get(tid)
        remote_task = remote.get(m["gtask_id"]) if m and m["tasklist_id"] == list_id else None
        desired = {
            "title": t["title"],
            "notes": _notes(tid, future, tid in past_tasks, unplaced.get(tid)),
            "due": _due_date(t, future, (remote_task or {}).get("due"), now),
        }
        body = {**desired, "due": f"{desired['due']}T00:00:00.000Z"}
        if remote_task is None:
            created = tc.insert(list_id, body)
            conn.execute(
                "INSERT INTO gtasks(task_id, tasklist_id, gtask_id) VALUES (?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET tasklist_id = excluded.tasklist_id, gtask_id = excluded.gtask_id",
                (tid, list_id, created["id"]),
            )
            conn.commit()
            changelog.log(conn, actor="planner", action="gtask.created", entity="task", entity_id=tid,
                          detail={"due": desired["due"]}, reason="Jede Aufgabe steht als Task im Kalender")
            report.created += 1
            continue
        diff = {}
        if (remote_task.get("title") or "") != desired["title"]:
            diff["title"] = desired["title"]
        if (remote_task.get("notes") or "").strip() != desired["notes"].strip():
            diff["notes"] = desired["notes"]
        if (remote_task.get("due") or "")[:10] != desired["due"]:
            diff["due"] = body["due"]
        if diff:
            tc.patch(list_id, remote_task["id"], diff)
            report.updated += 1

    # Erledigt/verworfen im Planer → in Google nachziehen
    rows = conn.execute(
        "SELECT g.*, t.status FROM gtasks g JOIN tasks t ON t.id = g.task_id WHERE t.status != 'open'"
    ).fetchall()
    for row in rows:
        r = remote.get(row["gtask_id"])
        if row["status"] == "done" and r is not None and r.get("status") != "completed":
            tc.patch(row["tasklist_id"], row["gtask_id"], {"status": "completed"})
            report.completed += 1
        elif row["status"] == "dropped":
            if r is not None:
                tc.delete(row["tasklist_id"], row["gtask_id"])
            conn.execute("DELETE FROM gtasks WHERE task_id = ?", (row["task_id"],))
            conn.commit()
            report.removed += 1
    return report
