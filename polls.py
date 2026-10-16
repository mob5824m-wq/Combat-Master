"""Reaction polls: parsing, the poll embed, and storage.

A poll is a message with one number emoji per option. Each member votes by
adding a reaction. The bot adds one reaction of each kind itself, so a count
of ``n`` means ``n - 1`` votes. The tally is read from Discord when the poll is
closed, either by hand or when its ``ends_at`` time passes, and the final
counts are kept in the database.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import discord

import store
from announcements import iso_utc

MAX_QUESTION_LENGTH = 256
MAX_OPTION_LENGTH = 100
MIN_OPTIONS = 2
MAX_OPTIONS = 10

# Keycap digits 1-9, then the keycap-ten emoji.
EMOJIS = [f"{n}\ufe0f\N{COMBINING ENCLOSING KEYCAP}" for n in range(1, 10)] + ["\N{KEYCAP TEN}"]
OPEN_COLOR = discord.Color.gold()
CLOSED_COLOR = discord.Color.green()


def clean_question(text: Optional[str]) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("A poll needs a question.")
    if len(cleaned) > MAX_QUESTION_LENGTH:
        raise ValueError(f"Keep the question under {MAX_QUESTION_LENGTH} characters.")
    return cleaned


def parse_options(options: list[str] | str) -> list[str]:
    """Accept a list, or text split on ``|`` or new lines. Returns cleaned options."""
    if isinstance(options, str):
        parts = [p for chunk in options.splitlines() for p in chunk.split("|")]
    else:
        parts = list(options)
    cleaned = [str(p).strip() for p in parts]
    cleaned = [p for p in cleaned if p]
    if len(cleaned) < MIN_OPTIONS:
        raise ValueError(f"A poll needs at least {MIN_OPTIONS} options.")
    if len(cleaned) > MAX_OPTIONS:
        raise ValueError(f"A poll can have at most {MAX_OPTIONS} options.")
    for option in cleaned:
        if len(option) > MAX_OPTION_LENGTH:
            raise ValueError(f"Keep each option under {MAX_OPTION_LENGTH} characters.")
    return cleaned


def votes_from_reactions(raw_counts: list[int]) -> list[int]:
    """Remove the bot's own starting reaction from each count."""
    return [max(0, int(count) - 1) for count in raw_counts]


def build_poll_embed(
    *,
    question: str,
    options: list[str],
    votes: Optional[list[int]] = None,
    ends_at: Optional[datetime] = None,
    author_name: str = "",
) -> discord.Embed:
    closed = votes is not None
    lines = []
    total = sum(votes) if votes else 0
    for index, option in enumerate(options):
        if votes is None:
            lines.append(f"{EMOJIS[index]} {option}")
        else:
            count = votes[index]
            share = round(100 * count / total) if total else 0
            bar = "\N{BLACK SQUARE}" * (share // 10)
            lines.append(f"{EMOJIS[index]} **{option}**: {count} ({share}%) {bar}".rstrip())
    embed = discord.Embed(
        title=f"Poll: {question}" if not closed else f"Poll closed: {question}",
        description="\n".join(lines),
        color=CLOSED_COLOR if closed else OPEN_COLOR,
    )
    if closed:
        embed.set_footer(text=f"{total} vote(s) in total")
    elif ends_at is not None:
        embed.set_footer(text="Vote by adding a reaction. Closes automatically.")
    else:
        embed.set_footer(text=f"Vote by adding a reaction. Started by {author_name}".strip())
    if not closed and ends_at is not None:
        embed.add_field(name="Closes", value=f"<t:{int(ends_at.timestamp())}:R>", inline=False)
    return embed


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def create_poll(
    *,
    guild_id: int,
    channel_id: int,
    author_id: int,
    question: str,
    options: list[str],
    ends_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> int:
    return store.db_insert(
        "INSERT INTO polls (guild_id, channel_id, question, options, author_id, created_at, ends_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
        (
            int(guild_id),
            int(channel_id),
            question,
            json.dumps(options),
            int(author_id),
            iso_utc(now or datetime.now(timezone.utc)),
            iso_utc(ends_at) if ends_at else None,
        ),
    )


def attach_message(poll_id: int, message_id: int) -> None:
    store.db_execute("UPDATE polls SET message_id = ? WHERE id = ?", (int(message_id), int(poll_id)))


def _decode(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    row["options"] = json.loads(row["options"])
    row["results"] = json.loads(row["results"]) if row["results"] else None
    return row


def get_poll(guild_id: int, poll_id: int) -> Optional[dict[str, Any]]:
    return _decode(
        store.db_fetchone(
            "SELECT * FROM polls WHERE id = ? AND guild_id = ?", (int(poll_id), int(guild_id))
        )
    )


def list_polls(guild_id: int, *, open_only: bool = False, limit: int = 25) -> list[dict[str, Any]]:
    query = "SELECT * FROM polls WHERE guild_id = ?"
    if open_only:
        query += " AND status = 'open'"
    query += " ORDER BY id DESC LIMIT ?"
    return [_decode(row) for row in store.db_fetchall(query, (int(guild_id), int(limit)))]


def list_due_polls(now: datetime) -> list[dict[str, Any]]:
    rows = store.db_fetchall(
        "SELECT * FROM polls WHERE status = 'open' AND ends_at IS NOT NULL AND ends_at <= ? ORDER BY ends_at",
        (iso_utc(now),),
    )
    return [_decode(row) for row in rows]


def mark_closed(poll_id: int, results: list[int], now: Optional[datetime] = None) -> bool:
    changed = store.db_execute(
        "UPDATE polls SET status = 'closed', results = ?, closed_at = ? WHERE id = ? AND status = 'open'",
        (json.dumps(results), iso_utc(now or datetime.now(timezone.utc)), int(poll_id)),
    )
    return changed > 0
