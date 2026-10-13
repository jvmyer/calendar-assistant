"""SQLite-Datenbank: Schema und Verbindung."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS areas (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    parent_id INTEGER REFERENCES areas(id),
    weight    REAL NOT NULL DEFAULT 1.0,
    UNIQUE (parent_id, name)
);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    area_id       INTEGER REFERENCES areas(id),
    source        TEXT NOT NULL,            -- telegram | mail | ilias | manual ...
    source_ref    TEXT,                     -- z. B. Mail-ID, zur Deduplizierung
    deadline      TEXT,                     -- ISO 8601
    estimate_min  INTEGER,                  -- geschätzter Aufwand (Minuten)
    actual_min    INTEGER,                  -- tatsächlicher Aufwand (fürs Lernen)
    priority_boost REAL NOT NULL DEFAULT 1.0,
    status        TEXT NOT NULL DEFAULT 'open',  -- open | done | dropped
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (source, source_ref)
);

-- ILIAS-Neuigkeiten (Auslöser für Aufgaben aus Kursen); Erstimport gilt als „seen“
CREATE TABLE IF NOT EXISTS ilias_news (
    news_id    TEXT PRIMARY KEY,
    ts         TEXT NOT NULL,            -- ISO 8601
    author     TEXT,
    uid        TEXT,
    headline   TEXT,
    obj_type   TEXT,
    ref_id     TEXT,
    title      TEXT,
    body       TEXT,
    first_seen TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'new',  -- new | seen
    attempts   INTEGER NOT NULL DEFAULT 0    -- fehlgeschlagene Verarbeitungsversuche
);

-- Vorschläge („nice to know“): nichts davon landet ohne Bestätigung im Kalender
CREATE TABLE IF NOT EXISTS proposals (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,                    -- task | event
    title         TEXT NOT NULL,
    note          TEXT,
    source        TEXT NOT NULL,
    source_ref    TEXT,
    start         TEXT,                             -- nur event: ISO 8601 mit Offset
    end           TEXT,
    deadline      TEXT,                             -- nur task
    estimate_min  INTEGER,
    area_id       INTEGER REFERENCES areas(id),
    state         TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | rejected
    notified      INTEGER NOT NULL DEFAULT 0,
    gcal_event_id TEXT,                             -- gesetzt, sobald das Event im Kalender steht
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (source, source_ref)
);

-- Zuordnung Aufgabe → Google Task (jede offene Aufgabe hat immer eine)
CREATE TABLE IF NOT EXISTS gtasks (
    task_id     INTEGER PRIMARY KEY REFERENCES tasks(id),
    tasklist_id TEXT NOT NULL,
    gtask_id    TEXT NOT NULL
);

-- Vom Planer eingetragene Blocker (Events im Nebenkalender „Planer“)
CREATE TABLE IF NOT EXISTS blocks (
    id            INTEGER PRIMARY KEY,
    task_id       INTEGER NOT NULL REFERENCES tasks(id),
    start         TEXT NOT NULL,            -- ISO 8601, UTC
    end           TEXT NOT NULL,
    gcal_event_id TEXT NOT NULL,
    calendar_id   TEXT NOT NULL,
    pinned        INTEGER NOT NULL DEFAULT 0,       -- 1 = vom Nutzer verschoben, Planer fasst nicht an
    state         TEXT NOT NULL DEFAULT 'active',   -- active | removed (vom Nutzer gelöscht) | released
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_task ON blocks(task_id);

-- Termine aus externen Quellen (Stundenplan etc.); Diff gegen den letzten Abruf
CREATE TABLE IF NOT EXISTS external_events (
    source      TEXT NOT NULL,
    uid         TEXT NOT NULL,
    title       TEXT NOT NULL,
    start       TEXT NOT NULL,          -- ISO 8601 (mit Offset) bzw. Datum bei ganztägig
    end         TEXT NOT NULL,
    all_day     INTEGER NOT NULL DEFAULT 0,
    location    TEXT,
    description TEXT,
    state       TEXT NOT NULL DEFAULT 'active',   -- active | removed
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (source, uid)
);

-- Zuordnung externer Termine zu gespiegelten Google-Events
CREATE TABLE IF NOT EXISTS mirror (
    source       TEXT NOT NULL,
    uid          TEXT NOT NULL,
    calendar_id  TEXT NOT NULL,
    gcal_event_id TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    PRIMARY KEY (source, uid)
);

-- Append-only: Einträge werden nie geändert oder gelöscht.
CREATE TABLE IF NOT EXISTS changelog (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,   -- system | user | <connector>
    action    TEXT NOT NULL,   -- z. B. task.created, block.moved
    entity    TEXT NOT NULL,
    entity_id TEXT,
    detail    TEXT,            -- JSON
    reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_changelog_ts ON changelog(ts);

CREATE TRIGGER IF NOT EXISTS changelog_no_update
BEFORE UPDATE ON changelog BEGIN SELECT RAISE(ABORT, 'changelog is append-only'); END;
CREATE TRIGGER IF NOT EXISTS changelog_no_delete
BEFORE DELETE ON changelog BEGIN SELECT RAISE(ABORT, 'changelog is append-only'); END;
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Spalten nachrüsten, die in älteren Datenbanken fehlen (CREATE IF NOT EXISTS ändert nichts)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ilias_news)")}
    if "attempts" not in cols:
        conn.execute("ALTER TABLE ilias_news ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
