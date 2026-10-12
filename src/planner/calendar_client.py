"""Google Calendar: eigener Nebenkalender „Planer“ für Blocker.

Der Hauptkalender wird nur gelesen. Eigene Events sind per `extendedProperties.private`
markiert, damit das Tool nie fremde Termine verändert.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from googleapiclient.discovery import build

from .db import get_meta, set_meta

PLANNER_CALENDAR_NAME = "Planer"
OWNER_KEY = "planner_owned"
META_CALENDAR_ID = "planner_calendar_id"


class CalendarClient:
    def __init__(self, creds, conn: sqlite3.Connection):
        self._svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self._conn = conn

    # -- Nebenkalender -------------------------------------------------
    def ensure_calendar(self, name: str, meta_key: str, description: str = "") -> str:
        """Eigenen Nebenkalender finden oder anlegen (ID wird in `meta` gemerkt)."""
        cached = get_meta(self._conn, meta_key)
        if cached:
            return cached
        for item in self.list_calendars():
            if item.get("summary") == name and item.get("accessRole") == "owner":
                set_meta(self._conn, meta_key, item["id"])
                return item["id"]
        created = self._svc.calendars().insert(body={"summary": name, "description": description}).execute()
        set_meta(self._conn, meta_key, created["id"])
        return created["id"]

    def ensure_planner_calendar(self) -> str:
        return self.ensure_calendar(PLANNER_CALENDAR_NAME, META_CALENDAR_ID, "Automatisch geplante Blocker")

    # -- Beliebige Events in eigenen Kalendern -----------------------
    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._svc.events().insert(calendarId=calendar_id, body=body).execute()

    def update_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._svc.events().update(calendarId=calendar_id, eventId=event_id, body=body).execute()

    def remove_event(self, calendar_id: str, event_id: str) -> None:
        from googleapiclient.errors import HttpError

        try:
            self._svc.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            if exc.resp.status not in (404, 410):  # schon weg ist okay
                raise

    # -- Lesen (alle Kalender, nur lesend) ---------------------------
    def list_calendars(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = None
        while True:
            res = self._svc.calendarList().list(pageToken=page).execute()
            items += res.get("items", [])
            page = res.get("nextPageToken")
            if not page:
                return items

    def list_events(self, calendar_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        page = None
        while True:
            res = (
                self._svc.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    pageToken=page,
                )
                .execute()
            )
            events += res.get("items", [])
            page = res.get("nextPageToken")
            if not page:
                return events

    # -- Schreiben (nur Planer-Kalender) -----------------------------
    def create_block(
        self, title: str, start: datetime, end: datetime, *, task_id: int, description: str = ""
    ) -> dict[str, Any]:
        body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "extendedProperties": {"private": {OWNER_KEY: "1", "task_id": str(task_id)}},
        }
        return self._svc.events().insert(calendarId=self.ensure_planner_calendar(), body=body).execute()

    def delete_block(self, event_id: str) -> None:
        self._svc.events().delete(calendarId=self.ensure_planner_calendar(), eventId=event_id).execute()
