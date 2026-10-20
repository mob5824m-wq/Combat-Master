"""SQLite access shared by :mod:`bot`, :mod:`operations` and :mod:`dashboard`.

Deliberately small: one connection per statement, opened ``with
closing(...)`` so it is always released, rows returned as plain dicts. The
database path is read from :mod:`paths` on every call, so a test that sets
``COMBAT_MASTER_DATA`` before importing is honoured.

Schema changes are additive. Tables are created with ``IF NOT EXISTS`` and
columns added later are added by :func:`_ensure_columns`, so an existing
``bot.db`` upgrades in place.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any, Optional

import paths

SCHEMA = """
-- Every announcement that was actually posted, whether sent right away or
-- from the schedule. This is what history and the dashboard read.
CREATE TABLE IF NOT EXISTS announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER,
    author_id       INTEGER NOT NULL,
    author_name     TEXT,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    mention_role_id INTEGER,
    source          TEXT NOT NULL,          -- now | scheduled | repeat
    created_at      TEXT NOT NULL           -- UTC, ISO 8601
);
CREATE INDEX IF NOT EXISTS idx_announcements_guild
    ON announcements (guild_id, created_at DESC);

-- Announcements waiting to be posted. interval_seconds is NULL for a one-off
-- and set for a repeating announcement, which stays 'pending' until cancelled.
CREATE TABLE IF NOT EXISTS scheduled_announcements (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    author_id        INTEGER NOT NULL,
    author_name      TEXT,
    title            TEXT NOT NULL,
    body             TEXT NOT NULL,
    mention_role_id  INTEGER,
    publish          INTEGER NOT NULL DEFAULT 0,
    send_at          TEXT NOT NULL,          -- UTC, the next time it goes out
    interval_seconds INTEGER,
    runs             INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|failed|cancelled
    error            TEXT,
    created_at       TEXT NOT NULL,
    sent_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_scheduled_due
    ON scheduled_announcements (status, send_at);

-- Reusable announcement text. Names are unique per server, ignoring case.
CREATE TABLE IF NOT EXISTS announcement_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL COLLATE NOCASE,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_by  INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (guild_id, name)
);

-- Reaction polls. Votes are reactions on the poll message, so the tally is
-- read from Discord when the poll closes; `results` keeps the final counts.
CREATE TABLE IF NOT EXISTS polls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    question    TEXT NOT NULL,
    options     TEXT NOT NULL,              -- JSON list of strings
    author_id   INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    ends_at     TEXT,                       -- UTC; NULL = closed by hand only
    status      TEXT NOT NULL DEFAULT 'open',  -- open | closed
    results     TEXT,                       -- JSON list of vote counts
    closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_polls_guild ON polls (guild_id, status);

-- Events with an RSVP reaction. Reminders are posted once, before start.
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    starts_at       TEXT NOT NULL,          -- UTC
    remind_minutes  INTEGER,                -- NULL = no reminder
    reminder_at     TEXT,                   -- UTC, derived from starts_at
    reminded        INTEGER NOT NULL DEFAULT 0,
    author_id       INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'   -- open | cancelled
);
CREATE INDEX IF NOT EXISTS idx_events_guild ON events (guild_id, status, starts_at);

-- Who changed what. Written by every staff action, from Discord or the
-- dashboard, and by the scheduler when it posts or closes something.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    actor_id    INTEGER NOT NULL,
    actor_name  TEXT NOT NULL,
    source      TEXT NOT NULL,              -- discord | dashboard | system
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_guild ON audit_log (guild_id, created_at DESC);

-- Announcements waiting for a second staff member. payload is the JSON the
-- operation needs to run once approved. The requester and the decider are
-- recorded as (source, id) pairs, so the same person can not approve their own.
CREATE TABLE IF NOT EXISTS approval_requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    kind              TEXT NOT NULL,            -- announce | schedule
    payload           TEXT NOT NULL,            -- JSON
    summary           TEXT NOT NULL,
    requester_id      INTEGER NOT NULL,
    requester_source  TEXT NOT NULL,
    requester_name    TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    decided_id        INTEGER,
    decided_source    TEXT,
    decided_name      TEXT,
    decided_at        TEXT,
    created_at        TEXT NOT NULL,
    reminded_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_guild ON approval_requests (guild_id, status);

-- Announcements pinned for a while. The scheduler unpins them when unpin_at passes.
CREATE TABLE IF NOT EXISTS pinned_posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    unpin_at    TEXT NOT NULL,                -- UTC
    status      TEXT NOT NULL DEFAULT 'pinned',  -- pinned | unpinned | failed
    error       TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pins_due ON pinned_posts (status, unpin_at);
-- One row per vote. A single-choice poll has at most one row per person; a
-- multiple-choice poll can have one per option they picked.
CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    voter_name    TEXT NOT NULL DEFAULT '',
    option_index  INTEGER NOT NULL,
    voted_at      TEXT NOT NULL,            -- UTC
    PRIMARY KEY (poll_id, user_id, option_index)
);
-- Support tickets: each opens a private channel. channel_id is unique, so a
-- channel maps to exactly one ticket.
CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL UNIQUE,
    opener_id    INTEGER NOT NULL,
    opener_name  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open',  -- open | closed
    created_at   TEXT NOT NULL,              -- UTC
    closed_at    TEXT,
    closed_by    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_guild ON tickets (guild_id, status);
-- Give a role when a member adds an emoji to a message, and take it away when
-- they remove it. The emoji is stored in the normalised form from reaction_roles.
CREATE TABLE IF NOT EXISTS reaction_roles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    emoji        TEXT NOT NULL,
    role_id      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (message_id, emoji)
);
"""

# Columns added after a first release. (table, column, declaration)
_ADDED_COLUMNS = [
    ("approval_requests", "reminded_at", "TEXT"),
    ("scheduled_announcements", "publish", "INTEGER NOT NULL DEFAULT 0"),
    ("scheduled_announcements", "interval_seconds", "INTEGER"),
    ("scheduled_announcements", "runs", "INTEGER NOT NULL DEFAULT 0"),
    ("scheduled_announcements", "author_name", "TEXT"),
    ("announcements", "author_name", "TEXT"),
    ("announcements", "edited_at", "TEXT"),
    ("announcements", "deleted_at", "TEXT"),
    ("scheduled_announcements", "pin_seconds", "INTEGER"),
    # 'reactions' is the default so polls made before buttons existed keep their old behaviour.
    ("polls", "mode", "TEXT NOT NULL DEFAULT 'reactions'"),  # buttons | reactions
    ("polls", "multiple", "INTEGER NOT NULL DEFAULT 0"),
]


def db_path() -> str:
    return str(paths.db_path())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    """Create the tables if they do not exist yet, and add any new columns."""
    with closing(_connect()) as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
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


def db_transaction(work):
    """Run ``work(conn)`` as one transaction: commit if it returns, roll back if it raises."""
    with closing(_connect()) as conn:
        try:
            result = work(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def db_fetchall(query: str, params: tuple = ()) -> list[dict[str, Any]]:
    with closing(_connect()) as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def db_fetchone(query: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    with closing(_connect()) as conn:
        row = conn.execute(query, params).fetchone()
        return dict(row) if row is not None else None
