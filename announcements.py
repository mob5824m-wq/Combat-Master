"""Announcement logic: parsing, embeds, posting, history and the scheduler.

Nothing in this module depends on a live Discord connection, except where a
``discord`` object is handed in by the caller, so the parsing and the
scheduler can be tested with plain values.

Times
    A send time is typed as either a delay (``30m``, ``2h``, ``1d12h``; a bare
    number means minutes) or an absolute local time (``2026-10-20 18:00``)
    read in the server's timezone. Both are stored as UTC.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

import store

logger = logging.getLogger("combat_master.announcements")

MAX_TITLE_LENGTH = 256
MAX_BODY_LENGTH = 4000
MAX_SCHEDULE_AHEAD = timedelta(days=365)
MIN_SCHEDULE_AHEAD = timedelta(minutes=1)
DEFAULT_HISTORY_LIMIT = 10
MAX_HISTORY_LIMIT = 25
ERROR_TEXT_LIMIT = 500

EMBED_COLOR = discord.Color.blurple()
STAFF_LOG_COLOR = discord.Color.dark_grey()

DEFAULT_TITLE = "Announcement"

# --------------------------------------------------------------------------- #
# Durations and send times
# --------------------------------------------------------------------------- #
_UNITS = {
    "": 60,  # bare number = minutes, only when it is the whole input
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}
_DURATION_TOKEN = re.compile(r"\s*(\d+)\s*([a-z]*)\s*")
_ABSOLUTE = re.compile(
    r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})\s*$"
)


def parse_duration(text: str) -> Optional[int]:
    """``'1d12h'`` -> seconds. Returns ``None`` if the text is not a duration."""
    s = (text or "").strip().lower()
    if not s:
        return None
    total = 0
    pos = 0
    tokens = 0
    while pos < len(s):
        match = _DURATION_TOKEN.match(s, pos)
        if match is None or match.end() == pos:
            return None
        amount, unit = int(match.group(1)), match.group(2)
        if unit not in _UNITS:
            return None
        if unit == "" and (tokens > 0 or match.end() != len(s)):
            return None  # a bare number only makes sense on its own
        total += amount * _UNITS[unit]
        tokens += 1
        pos = match.end()
    return total or None


def parse_send_time(text: str, now: datetime, tz_name: str) -> datetime:
    """Turn what a moderator typed into a UTC send time.

    Raises :class:`ValueError` with a message fit to show the user.
    """
    now = now.astimezone(timezone.utc)
    seconds = parse_duration(text)
    if seconds is not None:
        target = now + timedelta(seconds=seconds)
    else:
        match = _ABSOLUTE.match(text or "")
        if match is None:
            raise ValueError(
                "I could not read that time. Use a delay such as `30m`, `2h` or "
                "`1d12h`, or a date and time such as `2026-10-20 18:00`."
            )
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            tz = ZoneInfo("UTC")
        year, month, day, hour, minute = (int(g) for g in match.groups())
        try:
            local = datetime(year, month, day, hour, minute, tzinfo=tz)
        except ValueError as exc:
            raise ValueError(f"That date does not exist ({exc}).") from None
        target = local.astimezone(timezone.utc)

    if target - now < MIN_SCHEDULE_AHEAD:
        raise ValueError("Scheduled announcements must be at least one minute away.")
    if target - now > MAX_SCHEDULE_AHEAD:
        raise ValueError("Scheduled announcements can be at most 365 days away.")
    return target


def validate_timezone(name: str) -> str:
    """Return ``name`` if it is an IANA timezone, else raise :class:`ValueError`."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            f"`{name}` is not a timezone. Use an IANA name such as "
            "`America/Toronto` or `Europe/London`."
        ) from None
    return name


def iso_utc(moment: datetime) -> str:
    """The storage format: second precision, always UTC, sorts as text."""
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def discord_time(moment: datetime) -> str:
    """A Discord timestamp tag, rendered in each viewer's own timezone."""
    return f"<t:{int(moment.timestamp())}:f>"


# --------------------------------------------------------------------------- #
# Validation of the text a moderator enters
# --------------------------------------------------------------------------- #
def clean_title(title: Optional[str]) -> str:
    cleaned = (title or "").strip()
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise ValueError(f"Keep the title under {MAX_TITLE_LENGTH} characters.")
    return cleaned or DEFAULT_TITLE


def clean_body(body: Optional[str]) -> str:
    cleaned = (body or "").strip()
    if not cleaned:
        raise ValueError("The announcement message can not be empty.")
    if len(cleaned) > MAX_BODY_LENGTH:
        raise ValueError(f"Keep the message under {MAX_BODY_LENGTH} characters.")
    return cleaned


# --------------------------------------------------------------------------- #
# Embeds
# --------------------------------------------------------------------------- #
def build_announcement_embed(
    *, title: str, body: str, author_name: str
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=body,
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Posted by {author_name}")
    return embed


def build_staff_log_embed(
    *,
    heading: str,
    channel_mention: str,
    author_mention: str,
    title: str,
    body: str,
    mention: Optional[str],
    source: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=heading,
        color=STAFF_LOG_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Channel", value=channel_mention, inline=True)
    embed.add_field(name="By", value=author_mention, inline=True)
    embed.add_field(name="Source", value=source, inline=True)
    embed.add_field(name="Title", value=title[:1024], inline=False)
    if mention:
        embed.add_field(name="Pinged", value=mention, inline=True)
    preview = body if len(body) <= 300 else body[:297] + "..."
    embed.add_field(name="Message", value=preview or "-", inline=False)
    return embed


# --------------------------------------------------------------------------- #
# Posting
# --------------------------------------------------------------------------- #
async def post_announcement(
    channel: discord.abc.Messageable,
    *,
    title: str,
    body: str,
    author_name: str,
    mention_role: Optional[discord.Role] = None,
) -> discord.Message:
    """Post the announcement embed, pinging ``mention_role`` only if given.

    ``@everyone`` and ``@here`` are never pinged, even if they appear in the
    text, and users are never pinged. Only the one role the moderator picked
    can notify anyone.
    """
    allowed = discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[mention_role] if mention_role is not None else False,
    )
    return await channel.send(
        content=mention_role.mention if mention_role is not None else None,
        embed=build_announcement_embed(
            title=title, body=body, author_name=author_name
        ),
        allowed_mentions=allowed,
    )


def record_announcement(
    *,
    guild_id: int,
    channel_id: int,
    message_id: Optional[int],
    author_id: int,
    title: str,
    body: str,
    mention_role_id: Optional[int],
    source: str,
    when: Optional[datetime] = None,
) -> int:
    return store.db_insert(
        "INSERT INTO announcements (guild_id, channel_id, message_id, author_id, "
        "title, body, mention_role_id, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id,
            channel_id,
            message_id,
            author_id,
            title,
            body,
            mention_role_id,
            source,
            iso_utc(when or datetime.now(timezone.utc)),
        ),
    )


def list_history(guild_id: int, limit: int = DEFAULT_HISTORY_LIMIT) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), MAX_HISTORY_LIMIT))
    return store.db_fetchall(
        "SELECT * FROM announcements WHERE guild_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (guild_id, limit),
    )


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
def add_scheduled(
    *,
    guild_id: int,
    channel_id: int,
    author_id: int,
    title: str,
    body: str,
    mention_role_id: Optional[int],
    send_at: datetime,
    now: Optional[datetime] = None,
) -> int:
    return store.db_insert(
        "INSERT INTO scheduled_announcements (guild_id, channel_id, author_id, "
        "title, body, mention_role_id, send_at, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (
            guild_id,
            channel_id,
            author_id,
            title,
            body,
            mention_role_id,
            iso_utc(send_at),
            iso_utc(now or datetime.now(timezone.utc)),
        ),
    )


def list_pending(guild_id: int) -> list[dict[str, Any]]:
    return store.db_fetchall(
        "SELECT * FROM scheduled_announcements WHERE guild_id = ? AND status = 'pending' "
        "ORDER BY send_at, id",
        (guild_id,),
    )


def cancel_scheduled(guild_id: int, schedule_id: int) -> bool:
    """Cancel a pending announcement. ``False`` if it is not pending here."""
    changed = store.db_execute(
        "UPDATE scheduled_announcements SET status = 'cancelled' "
        "WHERE id = ? AND guild_id = ? AND status = 'pending'",
        (schedule_id, guild_id),
    )
    return changed > 0


def _finish_scheduled(schedule_id: int, status: str, error: Optional[str], now: datetime) -> None:
    store.db_execute(
        "UPDATE scheduled_announcements SET status = ?, error = ?, sent_at = ? WHERE id = ?",
        (status, error, iso_utc(now), schedule_id),
    )


async def run_due_scheduled(
    now: datetime,
    deliver: Callable[[dict[str, Any]], Awaitable[Optional[int]]],
) -> int:
    """Send every pending announcement whose time has come.

    ``deliver`` posts one row and returns the new message id. A failure marks
    that row ``failed`` with the reason and the rest still go out. Returns the
    number of rows attempted.
    """
    due = store.db_fetchall(
        "SELECT * FROM scheduled_announcements "
        "WHERE status = 'pending' AND send_at <= ? ORDER BY send_at, id",
        (iso_utc(now),),
    )
    for row in due:
        try:
            await deliver(row)
        except Exception as exc:  # one bad row must not stop the others
            logger.warning("Scheduled announcement #%s failed: %s", row["id"], exc)
            _finish_scheduled(
                row["id"], "failed", str(exc)[:ERROR_TEXT_LIMIT] or type(exc).__name__, now
            )
        else:
            _finish_scheduled(row["id"], "sent", None, now)
    return len(due)
