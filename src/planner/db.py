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
    not_before    TEXT,                     -- frühester Tag (ISO-Datum), z. B. „morgen erledigen“
    fixed_start   TEXT,                     -- fester Zeitblock („von 16:30 bis 17:30“), wird nie umgeplant
    fixed_end     TEXT,
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

-- HKA-Mails: nur Kopfdaten (Inhalte werden nie gespeichert); Erstimport gilt als „seen“
CREATE TABLE IF NOT EXISTS mails (
    item_id    TEXT PRIMARY KEY,         -- OWA-ItemId bzw. Gmail-Nachrichten-ID
    source     TEXT NOT NULL DEFAULT 'hka',  -- hka | gmail
    received   TEXT NOT NULL,            -- ISO 8601
    sender     TEXT,
    subject    TEXT,
    thread_id  TEXT,                     -- Konversations-/Thread-ID: Antworten im selben Thread erzeugen keine zweite Aufgabe
    first_seen TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'new',  -- new | seen
    verdict    TEXT,                     -- aufgabe | vorschlag | nichts | antwort (Urteil des lokalen Modells)
    attempts   INTEGER NOT NULL DEFAULT 0
);

-- Einschätzung des Nutzers zu Mail-Aufgaben = Lernbeispiele fürs Modell (kein Mailtext)
CREATE TABLE IF NOT EXISTS mail_feedback (
    task_id             INTEGER PRIMARY KEY REFERENCES tasks(id),
    item_id             TEXT,
    sender              TEXT,
    subject             TEXT,
    received            TEXT,
    excerpt             TEXT,      -- Anfang der Mail (≤ 400 Zeichen, ohne Zitate/Signatur), nur lokal
    model_kind          TEXT,
    model_title         TEXT,
    model_deadline_text TEXT,
    model_deadline      TEXT,
    model_effort        INTEGER,
    model_prio          TEXT,
    user_kind           TEXT,      -- aufgabe | vorschlag | nichts (NULL = nicht angegeben)
    user_prio           TEXT,      -- hoch | mittel | niedrig
    user_deadline_ok    INTEGER,   -- 1 = stimmt, 0 = von dir geändert
    user_deadline       TEXT,
    user_effort         INTEGER,
    done                INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT NOT NULL
);

-- Sprachnachrichten, die auf Bestätigung warten (Wortlaut wird nach der Entscheidung gelöscht)
CREATE TABLE IF NOT EXISTS voice_notes (
    id           INTEGER PRIMARY KEY,
    message_id   INTEGER,
    transcript   TEXT,
    title        TEXT NOT NULL,
    deadline     TEXT,
    not_before   TEXT,
    fixed_start  TEXT,
    fixed_end    TEXT,
    estimate_min INTEGER,
    priority     TEXT NOT NULL DEFAULT 'mittel',
    area         TEXT,
    state        TEXT NOT NULL DEFAULT 'pending',   -- pending | added | dropped
    task_id      INTEGER REFERENCES tasks(id),
    created_at   TEXT NOT NULL
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

-- Altbestand: Aufgabe → ganztägige Google Task (seit 2026-10-26 abgelöst durch `pool`, wird beim
-- nächsten Lauf geleert – siehe pool.retire_legacy_tasks)
CREATE TABLE IF NOT EXISTS gtasks (
    task_id     INTEGER PRIMARY KEY REFERENCES tasks(id),
    tasklist_id TEXT NOT NULL,
    gtask_id    TEXT NOT NULL,
    -- task: Auffangnetz (keine Zeit) – Abhaken/Löschen wirkt auf die Aufgabe.
    -- companion: steht neben einem Blocker, rein zum Abhaken – Abhaken/Löschen wirkt NICHT auf die Aufgabe.
    kind        TEXT NOT NULL DEFAULT 'task'
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

-- Vorrat an „Aufgaben mit Zeitraum“ (Liste „Planer-Vorrat“), siehe pool.py
CREATE TABLE IF NOT EXISTS pool (
    gtask_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,                       -- zugehöriger focusTime-Eintrag im Hauptkalender
    block_id INTEGER UNIQUE REFERENCES blocks(id), -- NULL = frei (geparkt)
    title    TEXT,                                -- zuletzt nach Google geschriebener Stand
    start    TEXT,
    end      TEXT,
    done     INTEGER
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

-- Aufgaben-Titel, die nie wieder automatisch angelegt werden sollen (exakter, normalisierter Treffer)
CREATE TABLE IF NOT EXISTS blacklist (
    id         INTEGER PRIMARY KEY,
    title_norm TEXT NOT NULL UNIQUE,   -- klein, getrimmt, Leerraum zusammengezogen
    title      TEXT NOT NULL,          -- Originalschreibweise, nur zur Anzeige
    created_at TEXT NOT NULL
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
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(gtasks)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE gtasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'task'")
        conn.commit()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(mails)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE mails ADD COLUMN source TEXT NOT NULL DEFAULT 'hka'")
        conn.commit()
    if "thread_id" not in cols:
        conn.execute("ALTER TABLE mails ADD COLUMN thread_id TEXT")
        conn.commit()
    for table in ("tasks", "voice_notes"):
        if "not_before" not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN not_before TEXT")
            conn.commit()
        if "fixed_start" not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN fixed_start TEXT")
            conn.execute(f"ALTER TABLE {table} ADD COLUMN fixed_end TEXT")
            conn.commit()
    if get_meta(conn, "migrated:uni_area") is None:
        # Einmalig (2026-10-28): offene HKA-Mail- und ILIAS-Aufgaben ohne Bereich zählen zu „Uni“,
        # damit ihre Blocker in den Kalender „Uni“ umziehen (vorher bekamen sie keinen Bereich).
        where = ("area_id IS NULL AND status = 'open' AND (source = 'ilias' OR "
                 "(source = 'mail' AND source_ref IN (SELECT item_id FROM mails WHERE source = 'hka')))")
        if conn.execute(f"SELECT 1 FROM tasks WHERE {where}").fetchone():
            uni = conn.execute("SELECT id FROM areas WHERE parent_id IS NULL AND name = 'Uni' COLLATE NOCASE").fetchone()
            uni_id = uni["id"] if uni else conn.execute("INSERT INTO areas(name) VALUES ('Uni')").lastrowid
            conn.execute(f"UPDATE tasks SET area_id = ? WHERE {where}", (uni_id,))
        set_meta(conn, "migrated:uni_area", "1")



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
