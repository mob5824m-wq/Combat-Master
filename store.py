"""SQLite access shared by :mod:`bot` and :mod:`announcements`.

Deliberately small: one connection per statement, opened ``with
closing(...)`` so it is always released, rows returned as plain dicts. The
database path is read from :mod:`paths` on every call, so a test that sets
``COMBAT_MASTER_DATA`` before importing is honoured.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any, Optional

import paths

SCHEMA = """
-- Every announcement that was actually posted, whether sent right away or
-- from the schedule. This is what `/manage history` reads.
CREATE TABLE IF NOT EXISTS announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER,
    author_id       INTEGER NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    mention_role_id INTEGER,
    source          TEXT NOT NULL,          -- 'now' or 'scheduled'
    created_at      TEXT NOT NULL           -- UTC, ISO 8601
);
CREATE INDEX IF NOT EXISTS idx_announcements_guild
    ON announcements (guild_id, created_at DESC);

-- Announcements waiting for their send time. The row is the record of truth:
-- it survives a restart, and `status` tells you what happened to it.
CREATE TABLE IF NOT EXISTS scheduled_announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    author_id       INTEGER NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    mention_role_id INTEGER,
    send_at         TEXT NOT NULL,          -- UTC, ISO 8601
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|failed|cancelled
    error           TEXT,
    created_at      TEXT NOT NULL,
    sent_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_scheduled_due
    ON scheduled_announcements (status, send_at);
"""


def db_path() -> str:
    return str(paths.db_path())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the tables if they do not exist yet."""
    with closing(_connect()) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def db_execute(query: str, params: tuple = ()) -> int:
    """Run one write statement and commit. Returns the affected row count."""
    with closing(_connect()) as conn:
        cur = conn.execute(query, params)
        conn.commit()
        return cur.rowcount


def db_insert(query: str, params: tuple = ()) -> int:
    """Run one INSERT and commit. Returns the new row id."""
    with closing(_connect()) as conn:
        cur = conn.execute(query, params)
        conn.commit()
        return int(cur.lastrowid)


def db_fetchall(query: str, params: tuple = ()) -> list[dict[str, Any]]:
    with closing(_connect()) as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def db_fetchone(query: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    with closing(_connect()) as conn:
        row = conn.execute(query, params).fetchone()
        return dict(row) if row is not None else None
