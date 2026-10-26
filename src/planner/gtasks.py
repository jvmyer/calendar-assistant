"""Google-Tasks-Zugriff (dünner Client).

Ganztägige Tasks legt der Planer seit 2026-10-26 nicht mehr an (Nutzer: keine Ganztags-Tasks) – das
Abhak-Kästchen steht jetzt als „Aufgabe mit Zeitraum“ aus dem Vorrat direkt im Zeitfenster, siehe pool.py.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from googleapiclient.discovery import build

from .db import get_meta, set_meta


class TasksClient:
    def __init__(self, creds, conn: sqlite3.Connection):
        self._svc = build("tasks", "v1", credentials=creds, cache_discovery=False)
        self._conn = conn

    def ensure_list(self, title: str, meta_key: str) -> str:
        cached = get_meta(self._conn, meta_key)
        if cached:
            return cached
        for item in self.all_lists():
            if item.get("title") == title:
                set_meta(self._conn, meta_key, item["id"])
                return item["id"]
        created = self.create_list(title)
        set_meta(self._conn, meta_key, created)
        return created

    def all_lists(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = None
        while True:
            res = self._svc.tasklists().list(maxResults=100, pageToken=page).execute()
            out += res.get("items", [])
            page = res.get("nextPageToken")
            if not page:
                return out

    def create_list(self, title: str) -> str:
        return self._svc.tasklists().insert(body={"title": title}).execute()["id"]

    def move(self, list_id: str, task_id: str, dest_list_id: str) -> dict[str, Any]:
        return self._svc.tasks().move(tasklist=list_id, task=task_id, destinationTasklist=dest_list_id).execute()

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

    def patch(self, list_id: str, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._svc.tasks().patch(tasklist=list_id, task=task_id, body=body).execute()

    def delete(self, list_id: str, task_id: str) -> None:
        from googleapiclient.errors import HttpError

        try:
            self._svc.tasks().delete(tasklist=list_id, task=task_id).execute()
        except HttpError as exc:
            if exc.resp.status not in (404, 410):
                raise
