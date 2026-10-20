"""Support tickets: a private channel opened by pressing a button on a panel.

Each member can have one open ticket at a time. Opening one creates a text
channel that only the member, the staff role and the bot can see. Closing it
deletes the channel and records the close in the database and the staff log.
There is no transcript: a deleted channel's messages are gone.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

import discord

import store
from announcements import iso_utc

TICKET_OPEN_ID = "cm:ticket:open"
TICKET_CLOSE_ID = "cm:ticket:close"
MAX_CHANNEL_SLUG = 40


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
            "Press **Close ticket** when you are finished. This deletes the channel."
        ),
        color=discord.Color.blurple(),
    )


def panel_buttons() -> list[dict[str, Any]]:
    return [{"custom_id": TICKET_OPEN_ID, "label": "Open a ticket", "emoji": "\N{TICKET}", "style": "primary"}]


def close_buttons() -> list[dict[str, Any]]:
    return [{"custom_id": TICKET_CLOSE_ID, "label": "Close ticket", "emoji": "\N{LOCK}", "style": "danger"}]


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def create(guild_id: int, channel_id: int, opener_id: int, opener_name: str, now: Optional[datetime] = None) -> int:
    return store.db_insert(
        "INSERT INTO tickets (guild_id, channel_id, opener_id, opener_name, status, created_at) "
        "VALUES (?, ?, ?, ?, 'open', ?)",
        (
            int(guild_id),
            int(channel_id),
            int(opener_id),
            opener_name,
            iso_utc(now or datetime.now(timezone.utc)),
        ),
    )


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
