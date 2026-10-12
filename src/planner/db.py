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
    return conn


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
