"""Support tickets: a private channel opened by pressing a button on a panel.

Each member can have one open ticket at a time. Opening one creates a text
channel that only the member, the staff role and the bot can see. Closing it
saves the conversation (a transcript) to the database and the staff log, then
deletes the channel. A ticket that sits idle can be closed automatically after
a number of days (``ticket_auto_close_days``).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

import discord

import store
from announcements import iso_utc, parse_stored

TICKET_OPEN_ID = "cm:ticket:open"
TICKET_CLOSE_ID = "cm:ticket:close"
MAX_CHANNEL_SLUG = 40
# How many messages a transcript keeps, oldest first. Longer tickets keep the first ones.
TRANSCRIPT_LIMIT = 1000


def channel_name(opener_name: str) -> str:
    """A Discord channel name for the ticket: ``ticket-<name>``, lower case, ASCII."""
    slug = re.sub(r"[^a-z0-9]+", "-", (opener_name or "").lower()).strip("-")[:MAX_CHANNEL_SLUG].strip("-")
    return f"ticket-{slug or 'member'}"


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="Need help?",
        description="Press the button below to open a private ticket with the staff team.",
        color=discord.Color.blurple(),
    )


def welcome_embed(opener_name: str, ticket_id: int) -> discord.Embed:
    return discord.Embed(
        title=f"Ticket #{ticket_id}",
        description=(
            f"Hi {opener_name}. Tell us what you need. Staff will reply here.\n"
            "Press **Close ticket** when you are finished. The conversation is saved for staff, "
            "and this channel is deleted."
        ),
        color=discord.Color.blurple(),
    )


def idle_embed(days: int) -> discord.Embed:
    return discord.Embed(
        description=(
            f"No one has written here for {days} day{'s' if days != 1 else ''}, so this ticket is closing. "
            "The conversation is saved for staff."
        ),
        color=discord.Color.greyple(),
    )


def panel_buttons() -> list[dict[str, Any]]:
    return [{"custom_id": TICKET_OPEN_ID, "label": "Open a ticket", "emoji": "\N{TICKET}", "style": "primary"}]


def close_buttons() -> list[dict[str, Any]]:
    return [{"custom_id": TICKET_CLOSE_ID, "label": "Close ticket", "emoji": "\N{LOCK}", "style": "danger"}]


def render_transcript(messages: list[dict[str, Any]]) -> str:
    """Plain text for a transcript. Each message is a dict from the gateway's history read.

    ``created_at`` is a timezone-aware datetime; ``attachments`` is a list of URLs.
    """
    lines: list[str] = []
    for message in messages:
        moment = message["created_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        who = message.get("author") or "unknown"
        if message.get("bot"):
            who += " (bot)"
        text = (message.get("content") or "").strip()
        body = text.replace("\n", "\n    ") if text else ""
        if body:
            lines.append(f"[{moment}] {who}: {body}")
        elif not message.get("attachments"):
            lines.append(f"[{moment}] {who}: (no text)")
        else:
            lines.append(f"[{moment}] {who}:")
        for url in message.get("attachments") or []:
            lines.append(f"    attachment: {url}")
    return "\n".join(lines) + ("\n" if lines else "")


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def create(guild_id: int, channel_id: int, opener_id: int, opener_name: str, now: Optional[datetime] = None) -> int:
    stamp = iso_utc(now or datetime.now(timezone.utc))
    return store.db_insert(
        "INSERT INTO tickets (guild_id, channel_id, opener_id, opener_name, status, created_at, last_activity_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (int(guild_id), int(channel_id), int(opener_id), opener_name, stamp, stamp),
    )


def touch(channel_id: int, now: Optional[datetime] = None) -> bool:
    """Record a message in an open ticket's channel. Returns True if the channel is a ticket."""
    changed = store.db_execute(
        "UPDATE tickets SET last_activity_at = ? WHERE channel_id = ? AND status = 'open'",
        (iso_utc(now or datetime.now(timezone.utc)), int(channel_id)),
    )
    return changed > 0


def get(guild_id: int, ticket_id: int) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM tickets WHERE id = ? AND guild_id = ?", (int(ticket_id), int(guild_id))
    )


def get_by_channel(guild_id: int, channel_id: int) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM tickets WHERE channel_id = ? AND guild_id = ?", (int(channel_id), int(guild_id))
    )


def open_for_user(guild_id: int, user_id: int) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM tickets WHERE guild_id = ? AND opener_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
        (int(guild_id), int(user_id)),
    )


def list_open_all() -> list[dict[str, Any]]:
    """Every open ticket in every server, for the idle check."""
    return store.db_fetchall("SELECT * FROM tickets WHERE status = 'open' ORDER BY id")


def list_tickets(guild_id: int, *, open_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    query = "SELECT * FROM tickets WHERE guild_id = ?"
    if open_only:
        query += " AND status = 'open'"
    query += " ORDER BY id DESC LIMIT ?"
    return store.db_fetchall(query, (int(guild_id), int(limit)))


def mark_closed(ticket_id: int, closed_by: str, now: Optional[datetime] = None) -> bool:
    changed = store.db_execute(
        "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE id = ? AND status = 'open'",
        (iso_utc(now or datetime.now(timezone.utc)), closed_by, int(ticket_id)),
    )
    return changed > 0


def last_activity(row: dict[str, Any]):
    """When the ticket last had a message from someone other than the bot (falls back to opening)."""
    return parse_stored(row["last_activity_at"] or row["created_at"])


def save_transcript(guild_id: int, ticket_id: int, text: str, message_count: int, now: Optional[datetime] = None) -> None:
    """Keep the transcript. Saving again replaces the earlier one (a close that failed part way)."""
    store.db_execute(
        "INSERT OR REPLACE INTO ticket_transcripts (ticket_id, guild_id, text, message_count, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (int(ticket_id), int(guild_id), text, int(message_count), iso_utc(now or datetime.now(timezone.utc))),
    )


def transcript_ids(guild_id: int) -> set[int]:
    rows = store.db_fetchall("SELECT ticket_id FROM ticket_transcripts WHERE guild_id = ?", (int(guild_id),))
    return {int(row["ticket_id"]) for row in rows}


def get_transcript(guild_id: int, ticket_id: int) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM ticket_transcripts WHERE ticket_id = ? AND guild_id = ?", (int(ticket_id), int(guild_id))
    )
