"""Staff operations: the one place where things happen.

Slash commands, the dashboard and the scheduler all call these functions. Each
one checks its inputs, talks to Discord through a :class:`Gateway`, writes the
database row, records an audit entry and (where it applies) posts to the staff
log. Failures a staff member should see are raised as :class:`OperationError`
with a message written for them.

Nothing here imports discord.py's client. The live bot passes a
``DiscordGateway``; the dashboard demo and the tests pass a fake one. That is
why the behaviour can be tested without a Discord connection.
"""

from __future__ import annotations

import functools
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

import discord

import announcements as ann
import approvals
import audit
import events
import pins
import polls
import quiet
import reaction_roles
import settings
import tickets
import welcome
from audit import Actor, SCHEDULER_ACTOR

logger = logging.getLogger("combat_master.operations")

POLL_MIN_DURATION = timedelta(minutes=1)
POLL_MAX_DURATION = timedelta(days=30)


class OperationError(Exception):
    """A problem to show the staff member, worded for them."""


class Gateway(Protocol):
    """What the operations need from Discord. Methods raise OperationError on failure."""

    def guilds(self) -> list[dict[str, Any]]:
        """``[{"id", "name", "member_count"}]`` for every server the bot is in."""

    def snapshot(self, guild_id: int) -> Optional[dict[str, Any]]:
        """Channels and roles for dropdowns, or ``None`` if the bot is not there."""

    def channel_problem(self, guild_id: int, channel_id: int) -> Optional[str]:
        """Why the bot can not post in the channel, or ``None``."""

    def ping_warning(self, guild_id: int, role_id: int) -> Optional[str]:
        """A note if the role is set so that it would not notify anyone, or ``None``."""

    def role_problem(self, guild_id: int, role_id: int) -> Optional[str]:
        """Why the bot can not give this role to members (missing, managed, or above the bot), or ``None``."""

    async def post(
        self,
        guild_id: int,
        channel_id: int,
        *,
        embed: Optional[discord.Embed] = None,
        content: Optional[str] = None,
        role_id: Optional[int] = None,
        user_ids: tuple[int, ...] = (),
        publish: bool = False,
        view: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Send a message. Returns ``{"message_id", "jump_url", "published"}``.

        ``view`` is a list of button specs (``{"custom_id", "label", "emoji", "style"}``)
        whose buttons keep working after a restart.
        """

    async def edit_embed(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        embed: discord.Embed,
        *,
        clear_view: bool = False,
    ) -> None:
        """Replace the embed. ``clear_view`` also removes the buttons."""

    async def create_ticket_channel(
        self,
        guild_id: int,
        *,
        name: str,
        opener_id: int,
        category_id: Optional[int],
        staff_role_id: Optional[int],
        reason: str,
    ) -> int:
        """A private text channel for a ticket. Returns its id."""

    async def delete_channel(self, guild_id: int, channel_id: int, *, reason: str) -> None:
        """Delete a channel. A channel that is already gone counts as deleted."""

    async def set_member_role(self, guild_id: int, user_id: int, role_id: int, *, add: bool) -> None:
        """Give or take a role from one member."""

    async def reaction_counts(
        self, guild_id: int, channel_id: int, message_id: int, emojis: list[str]
    ) -> list[int]:
        """Raw reaction counts, one per emoji, including the bot's own."""

    async def reactors(
        self, guild_id: int, channel_id: int, message_id: int, emoji: str, limit: int
    ) -> list[int]:
        """User ids who added the emoji, bots excluded."""

    async def add_reactions(self, guild_id: int, channel_id: int, message_id: int, emojis: list[str]) -> None:
        ...

    async def delete_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        """Delete a message. A message that is already gone counts as deleted."""

    async def pin_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        ...

    async def unpin_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        ...


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _user_errors(fn):
    """Turn a ValueError from input parsing into an OperationError."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ValueError as exc:
            raise OperationError(str(exc)) from None

    return wrapper


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _actor_mention(actor: Actor) -> str:
    return f"<@{actor.id}>" if actor.source == "discord" else actor.name


def _require_channel(gw: Gateway, cfg: dict, guild_id: int, channel_id: Optional[int]) -> int:
    target = int(channel_id) if channel_id else settings.get_announcement_channel_id(cfg, guild_id)
    if not target:
        raise OperationError(
            "No channel was given and no announcement channel is set. Choose a channel, "
            "or set one with `/manage setup announcement_channel:#channel`."
        )
    problem = gw.channel_problem(guild_id, int(target))
    if problem:
        raise OperationError(problem)
    return int(target)


def _warnings(gw: Gateway, guild_id: int, role_id: Optional[int], published: Optional[bool] = None) -> list[str]:
    notes = []
    if role_id:
        warning = gw.ping_warning(guild_id, int(role_id))
        if warning:
            notes.append(warning)
    if published is False:
        notes.append("Could not publish it to followers. Check that the channel is an announcement channel.")
    return notes


async def _log(gw: Gateway, cfg: dict, guild_id: int, embed: discord.Embed) -> None:
    """Write to the staff log. Never raises: a broken log must not break the action."""
    channel_id = settings.get_staff_channel_id(cfg, guild_id)
    if not channel_id:
        return
    try:
        await gw.post(guild_id, channel_id, embed=embed)
    except Exception as exc:
        logger.warning("Could not write to the staff log in %s: %s", guild_id, exc)


def _failure_embed(title: str, reason: str, channel_id: int, reference: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=reason[:500],
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Channel", value=f"<#{channel_id}>", inline=True)
    embed.add_field(name="Source", value=reference, inline=True)
    return embed


def _persist(cfg: dict) -> None:
    try:
        settings.save_config(cfg)
    except OSError as exc:
        raise OperationError(f"Could not save the settings: {exc}") from None


# --------------------------------------------------------------------------- #
# Announcements
# --------------------------------------------------------------------------- #
def _message_id(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise OperationError("That is not a message ID. Copy the ID of the announcement message.") from None


def _same_person(a: Actor, b: Actor) -> bool:
    return a.source == b.source and int(a.id) == int(b.id)


async def _pin(
    gw: Gateway, guild_id: int, channel_id: int, message_id: int, seconds: int, now: datetime
) -> tuple[Optional[str], list[str]]:
    """Pin a posted message and remember when to unpin it. Returns (unpin time, warnings)."""
    try:
        await gw.pin_message(guild_id, channel_id, message_id)
    except Exception as exc:  # the post itself went out, so this is a warning, not a failure
        return None, [f"Posted, but could not pin it: {(str(exc) or type(exc).__name__)[:300]}"]
    until = now + timedelta(seconds=seconds)
    pins.add(guild_id=guild_id, channel_id=channel_id, message_id=message_id, unpin_at=until, now=now)
    return ann.iso_utc(until), []


async def _post_announcement(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    actor: Actor,
    *,
    message: str,
    title: str,
    channel_id: int,
    mention_role_id: Optional[int],
    publish: bool,
    pin_seconds: Optional[int],
    now: Optional[datetime],
    approved_by: Optional[Actor] = None,
) -> dict[str, Any]:
    """Post an announcement now and record it. Used by staff posts and approved requests."""
    now = _now(now)
    held_until = _manual_hold_until(cfg, guild_id, now)
    if held_until is not None:
        return _hold_manual_post(
            cfg,
            gw,
            guild_id,
            actor,
            held_until,
            message=message,
            title=title,
            channel_id=channel_id,
            mention_role_id=mention_role_id,
            publish=publish,
            pin_seconds=pin_seconds,
            now=now,
            approved_by=approved_by,
        )
    embed = ann.build_announcement_embed(title=title, body=message, author_name=actor.name)
    sent = await gw.post(
        guild_id,
        channel_id,
        embed=embed,
        content=ann.mention_content(mention_role_id),
        role_id=mention_role_id,
        publish=publish,
    )
    ann.record_announcement(
        guild_id=guild_id,
        channel_id=channel_id,
        message_id=sent["message_id"],
        author_id=actor.id,
        author_name=actor.name,
        title=title,
        body=message,
        mention_role_id=mention_role_id,
        source="now",
        when=now,
    )
    warnings = _warnings(gw, guild_id, mention_role_id, sent.get("published") if publish else None)
    pinned_until: Optional[str] = None
    if pin_seconds:
        pinned_until, pin_warnings = await _pin(gw, guild_id, channel_id, sent["message_id"], pin_seconds, now)
        warnings += pin_warnings
    detail = f"#{channel_id}: {title}"
    if approved_by is not None:
        detail += f" (approved by {approved_by.name})"
    audit.record(guild_id, actor, "announce.post", detail)
    await _log(
        gw,
        cfg,
        guild_id,
        ann.build_staff_log_embed(
            heading="Announcement posted",
            channel_mention=f"<#{channel_id}>",
            author_mention=_actor_mention(actor),
            title=title,
            body=message,
            mention=ann.mention_content(mention_role_id),
            source="Now" if approved_by is None else f"Approved by {approved_by.name}",
        ),
    )
    return {
        "message_id": str(sent["message_id"]),
        "jump_url": sent.get("jump_url"),
        "channel_id": channel_id,
        "published": bool(sent.get("published")),
        "pinned_until": pinned_until,
        "warnings": warnings,
    }


def _manual_hold_until(cfg: dict, guild_id: int, now: datetime) -> Optional[datetime]:
    """When a post made by hand must wait until, or None if it can go out now."""
    if not settings.get_quiet_hold_manual(cfg, guild_id):
        return None
    start, end = settings.get_quiet_hours(cfg, guild_id)
    return quiet.window_end(now, settings.get_timezone_name(cfg, guild_id), start, end)


def _hold_manual_post(
    cfg: dict,
    gw: Gateway,
    guild_id: int,
    actor: Actor,
    held_until: datetime,
    *,
    message: str,
    title: str,
    channel_id: int,
    mention_role_id: Optional[int],
    publish: bool,
    pin_seconds: Optional[int],
    now: datetime,
    approved_by: Optional[Actor] = None,
) -> dict[str, Any]:
    """Quiet hours are on and hold-by-hand is set: save the post for when the window ends."""
    details: dict[str, Any] = {
        "send_at": ann.iso_utc(held_until),
        "message": message,
        "title": title,
        "channel_id": channel_id,
        "mention_role_id": mention_role_id,
        "publish": bool(publish),
        "interval_seconds": None,
        "pin_seconds": pin_seconds,
    }
    created = _create_schedule(gw, guild_id, actor, details, now=now, approved_by=approved_by)
    audit.record(guild_id, actor, "quiet.hold", f"#{created['id']} held until {created['send_at']}")
    return {
        "held": True,
        "schedule_id": created["id"],
        "send_at": created["send_at"],
        "channel_id": channel_id,
        "message_id": None,
        "jump_url": None,
        "published": False,
        "pinned_until": None,
        "warnings": created["warnings"],
    }


def _approval_summary(text: str, channel_id: int, when: str = "") -> str:
    """One line for the approval queue: what it is, where it goes, and when."""
    preview = " ".join(str(text or "").split())
    if len(preview) > 80:
        preview = preview[:79] + "…"
    prefix = f"{when}, " if when else ""
    return f"{prefix}channel {channel_id}: {preview}"


async def _queue(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    actor: Actor,
    kind: str,
    details: dict[str, Any],
    *,
    summary: str,
    now: datetime,
) -> dict[str, Any]:
    """Hold an announcement for approval and tell the staff log it is waiting."""
    request_id = approvals.create(
        guild_id=guild_id, kind=kind, payload=details, summary=summary, requester=actor, now=now
    )
    audit.record(guild_id, actor, "approval.request", f"#{request_id} ({kind}) {summary}")
    await _log(
        gw,
        cfg,
        guild_id,
        ann.build_staff_log_embed(
            heading="Announcement waiting for approval",
            channel_mention=f"<#{details['channel_id']}>",
            author_mention=_actor_mention(actor),
            title=details.get("title") or "",
            body=details["message"],
            mention=ann.mention_content(details.get("mention_role_id")),
            source=f"Request #{request_id}: approve it with /manage approve",
        ),
    )
    return {"queued": True, "approval_id": request_id, "warnings": []}


@_user_errors
async def announce_now(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    actor: Actor,
    *,
    message: str,
    title: Optional[str] = None,
    channel_id: Optional[int] = None,
    mention_role_id: Optional[int] = None,
    publish: bool = False,
    pin_for: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Post an announcement now, or hold it for approval when the server requires that."""
    now = _now(now)
    details: dict[str, Any] = {
        "message": ann.clean_body(message),
        "title": ann.clean_title(title),
        "channel_id": _require_channel(gw, cfg, guild_id, channel_id),
        "mention_role_id": int(mention_role_id) if mention_role_id else None,
        "publish": bool(publish),
        "pin_seconds": ann.parse_pin(pin_for),
    }
    if settings.get_require_approval(cfg, guild_id):
        return await _queue(
            gw,
            cfg,
            guild_id,
            actor,
            approvals.KIND_ANNOUNCE,
            details,
            summary=_approval_summary(details.get("title") or details["message"], details["channel_id"]),
            now=now,
        )
    return await _post_announcement(gw, cfg, guild_id, actor, now=now, **details)


def _create_schedule(
    gw: Gateway,
    guild_id: int,
    actor: Actor,
    details: dict[str, Any],
    *,
    now: datetime,
    approved_by: Optional[Actor] = None,
) -> dict[str, Any]:
    send_at = ann.parse_stored(details["send_at"])
    interval = details.get("interval_seconds")
    target = int(details["channel_id"])
    schedule_id = ann.add_scheduled(
        guild_id=guild_id,
        channel_id=target,
        author_id=actor.id,
        author_name=actor.name,
        title=details["title"],
        body=details["message"],
        mention_role_id=details.get("mention_role_id"),
        send_at=send_at,
        publish=bool(details.get("publish")),
        interval_seconds=interval,
        pin_seconds=details.get("pin_seconds"),
        now=now,
    )
    kind = "repeat.create" if interval else "schedule.create"
    detail = f"#{schedule_id} for {ann.iso_utc(send_at)} in #{target}"
    if approved_by is not None:
        detail += f" (approved by {approved_by.name})"
    audit.record(guild_id, actor, kind, detail)
    return {
        "id": schedule_id,
        "send_at": ann.iso_utc(send_at),
        "repeat_seconds": interval,
        "channel_id": target,
        "pinned": bool(details.get("pin_seconds")),
        "warnings": _warnings(gw, guild_id, details.get("mention_role_id")),
    }


@_user_errors
async def schedule_announcement(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    actor: Actor,
    *,
    when: str,
    message: str,
    title: Optional[str] = None,
    channel_id: Optional[int] = None,
    mention_role_id: Optional[int] = None,
    publish: bool = False,
    repeat_every: Optional[str] = None,
    pin_for: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Schedule one announcement, or a repeating one when ``repeat_every`` is set."""
    now = _now(now)
    body = ann.clean_body(message)
    heading = ann.clean_title(title)
    send_at = ann.parse_send_time(when, now, settings.get_timezone_name(cfg, guild_id))
    interval = ann.parse_interval(repeat_every) if repeat_every else None
    pin_seconds = ann.parse_pin(pin_for)
    if pin_seconds and interval and pin_seconds >= interval:
        raise ValueError(
            "A repeating post's pin must be shorter than the time between posts, so only one copy "
            "is pinned at a time. Pin it for less than the repeat interval."
        )
    target = _require_channel(gw, cfg, guild_id, channel_id)
    details: dict[str, Any] = {
        # Stored as an absolute time, so a request that waits in the queue keeps its meaning.
        "send_at": ann.iso_utc(send_at),
        "message": body,
        "title": heading,
        "channel_id": target,
        "mention_role_id": int(mention_role_id) if mention_role_id else None,
        "publish": bool(publish),
        "interval_seconds": interval,
        "pin_seconds": pin_seconds,
    }
    if settings.get_require_approval(cfg, guild_id):
        return await _queue(
            gw,
            cfg,
            guild_id,
            actor,
            approvals.KIND_SCHEDULE,
            details,
            summary=_approval_summary(
                heading or details["message"],
                target,
                when=f"{'repeats' if interval else 'once'} at {ann.iso_utc(send_at)}",
            ),
            now=now,
        )
    return _create_schedule(gw, guild_id, actor, details, now=now)


# --------------------------------------------------------------------------- #
# Audit export
# --------------------------------------------------------------------------- #
def export_audit_csv(guild_id: int) -> str:
    """The whole audit log for one server as CSV text, oldest first."""
    return audit.to_csv(guild_id)


# --------------------------------------------------------------------------- #
# Approval queue
# --------------------------------------------------------------------------- #
def list_approvals(guild_id: int) -> list[dict[str, Any]]:
    return approvals.list_pending(guild_id)


@_user_errors
async def approve_request(
    gw: Gateway, cfg: dict, guild_id: int, approver: Actor, request_id: int, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Approve a waiting announcement. It goes out under the requester's name."""
    now = _now(now)
    row = approvals.get(guild_id, request_id)
    if row is None or row["status"] != approvals.PENDING:
        raise OperationError(f"There is no pending request #{request_id} in this server. Check the approval queue.")
    requester = approvals.requester_of(row)
    if _same_person(requester, approver):
        raise OperationError("You can't approve your own request. Ask another staff member to approve it.")
    payload = row["payload"]
    if row["kind"] == approvals.KIND_SCHEDULE and ann.parse_stored(payload["send_at"]) - now < ann.MIN_SCHEDULE_AHEAD:
        raise OperationError("That time has already passed. Reject this request and schedule it again.")
    if not approvals.decide(guild_id, request_id, approvals.APPROVED, approver, now=now):
        raise OperationError(f"Request #{request_id} was just decided by someone else.")
    try:
        if row["kind"] == approvals.KIND_ANNOUNCE:
            result = await _post_announcement(
                gw, cfg, guild_id, requester, now=now, approved_by=approver, **payload
            )
        else:
            result = _create_schedule(gw, guild_id, requester, payload, now=now, approved_by=approver)
    except Exception:
        approvals.reopen(guild_id, request_id)  # nothing went out, so it stays in the queue
        raise
    audit.record(guild_id, approver, "approval.approve", f"#{request_id} ({row['kind']}) by {requester.name}")
    result["request_id"] = request_id
    return result


@_user_errors
async def reject_request(guild_id: int, actor: Actor, request_id: int, *, now: Optional[datetime] = None) -> None:
    row = approvals.get(guild_id, request_id)
    if row is None or row["status"] != approvals.PENDING:
        raise OperationError(f"There is no pending request #{request_id} in this server. Check the approval queue.")
    if not approvals.decide(guild_id, request_id, approvals.REJECTED, actor, now=now):
        raise OperationError(f"Request #{request_id} was just decided by someone else.")
    audit.record(guild_id, actor, "approval.reject", f"#{request_id} ({row['kind']}) by {row['requester_name']}")


# --------------------------------------------------------------------------- #
# Editing and deleting what was posted
# --------------------------------------------------------------------------- #
def _recorded(guild_id: int, message_id: int) -> dict[str, Any]:
    row = ann.get_announcement(guild_id, message_id)
    if row is None:
        raise OperationError(
            "That message is not an announcement the bot recorded in this server, so it can not be changed here."
        )
    return row


@_user_errors
async def edit_announcement(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    actor: Actor,
    *,
    message_id: Any,
    message: Optional[str] = None,
    title: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Change the text or heading of an announcement the bot posted. Leave one out to keep it."""
    if message is None and title is None:
        raise ValueError("Give a new message, a new heading, or both.")
    mid = _message_id(message_id)
    row = _recorded(guild_id, mid)
    body = ann.clean_body(message) if message is not None else row["body"]
    heading = ann.clean_title(title) if title is not None else row["title"]
    embed = ann.build_announcement_embed(title=heading, body=body, author_name=row["author_name"] or "staff")
    await gw.edit_embed(guild_id, int(row["channel_id"]), mid, embed)
    ann.update_announcement(row["id"], title=heading, body=body, now=_now(now))
    audit.record(guild_id, actor, "announce.edit", f"message {mid} in #{row['channel_id']}: {heading}")
    return {"message_id": str(mid), "channel_id": int(row["channel_id"]), "title": heading, "body": body}


@_user_errors
async def delete_announcement(
    gw: Gateway, cfg: dict, guild_id: int, actor: Actor, *, message_id: Any, now: Optional[datetime] = None
) -> None:
    mid = _message_id(message_id)
    row = _recorded(guild_id, mid)
    await gw.delete_message(guild_id, int(row["channel_id"]), mid)  # already gone counts as deleted
    ann.mark_announcement_deleted(row["id"], _now(now))
    pins.close_for_message(guild_id, mid)
    audit.record(guild_id, actor, "announce.delete", f"message {mid} in #{row['channel_id']}: {row['title']}")


def cancel_schedule(guild_id: int, actor: Actor, schedule_id: int) -> None:
    if not ann.cancel_scheduled(guild_id, schedule_id):
        raise OperationError(
            f"There is no pending scheduled announcement #{schedule_id} in this server. "
            "Check the scheduled list."
        )
    audit.record(guild_id, actor, "schedule.cancel", f"#{schedule_id}")


def list_schedule(guild_id: int) -> list[dict[str, Any]]:
    return ann.list_pending(guild_id)


def list_pins(guild_id: int) -> list[dict[str, Any]]:
    """Posts that are pinned right now, soonest to unpin first."""
    return pins.list_active(guild_id)


def list_history(guild_id: int, limit: int = ann.DEFAULT_HISTORY_LIMIT) -> list[dict[str, Any]]:
    return ann.list_history(guild_id, limit)


@_user_errors
async def save_template(
    guild_id: int, actor: Actor, *, name: str, title: Optional[str], message: str
) -> dict[str, Any]:
    clean_name = ann.clean_template_name(name)
    body = ann.clean_body(message)
    heading = ann.clean_title(title)
    updated = ann.save_template(guild_id, clean_name, heading, body, actor.id)
    audit.record(guild_id, actor, "template.save", clean_name)
    return {"name": clean_name, "updated": updated}


def delete_template(guild_id: int, actor: Actor, name: str) -> None:
    if not ann.delete_template(guild_id, name):
        raise OperationError(f"There is no template called `{name}`.")
    audit.record(guild_id, actor, "template.delete", name)


def list_templates(guild_id: int) -> list[dict[str, Any]]:
    return ann.list_templates(guild_id)


@_user_errors
async def use_template(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    actor: Actor,
    *,
    name: str,
    channel_id: Optional[int] = None,
    mention_role_id: Optional[int] = None,
    publish: bool = False,
) -> dict[str, Any]:
    template = ann.get_template(guild_id, name)
    if template is None:
        raise OperationError(f"There is no template called `{name}`.")
    return await announce_now(
        gw,
        cfg,
        guild_id,
        actor,
        message=template["body"],
        title=template["title"],
        channel_id=channel_id,
        mention_role_id=mention_role_id,
        publish=publish,
    )


# --------------------------------------------------------------------------- #
# Polls
# --------------------------------------------------------------------------- #
@_user_errors
async def create_poll(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    actor: Actor,
    *,
    question: str,
    options: Any,
    channel_id: Optional[int] = None,
    duration: Optional[str] = None,
    multiple: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = _now(now)
    text = polls.clean_question(question)
    choices = polls.parse_options(options)
    ends_at = None
    if duration:
        seconds = ann.parse_duration(duration)
        if seconds is None:
            raise ValueError("I could not read that duration. Use something like `1h` or `2d`.")
        ends_at = now + timedelta(seconds=seconds)
        if ends_at - now < POLL_MIN_DURATION or ends_at - now > POLL_MAX_DURATION:
            raise ValueError("A poll must run between 1 minute and 30 days.")
    target = _require_channel(gw, cfg, guild_id, channel_id)
    # The row is written first: the button ids contain the poll id.
    poll_id = polls.create_poll(
        guild_id=guild_id,
        channel_id=target,
        author_id=actor.id,
        question=text,
        options=choices,
        ends_at=ends_at,
        now=now,
        mode=polls.MODE_BUTTONS,
        multiple=bool(multiple),
    )
    embed = polls.build_poll_embed(
        question=text,
        options=choices,
        votes=[0] * len(choices),
        voters=0,
        ends_at=ends_at,
        author_name=actor.name,
        mode=polls.MODE_BUTTONS,
        multiple=bool(multiple),
    )
    try:
        sent = await gw.post(guild_id, target, embed=embed, view=polls.button_specs(poll_id, choices))
    except Exception:
        polls.delete_poll(poll_id)
        raise
    polls.attach_message(poll_id, sent["message_id"])
    audit.record(guild_id, actor, "poll.create", f"#{poll_id}: {text}")
    return {
        "id": poll_id,
        "channel_id": target,
        "message_id": str(sent["message_id"]),
        "ends_at": ann.iso_utc(ends_at) if ends_at else None,
        "options": choices,
        "multiple": bool(multiple),
    }


@_user_errors
async def cast_vote(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    poll_id: int,
    actor: Actor,
    index: int,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Record one press of a vote button and refresh the live results on the poll message."""
    now = _now(now)
    poll = polls.get_poll_by_id(poll_id)
    if poll is None or int(poll["guild_id"]) != int(guild_id):
        raise OperationError("That poll no longer exists.")
    if poll["mode"] != polls.MODE_BUTTONS:
        raise OperationError("This is an older poll. Vote by adding its reaction.")
    closes = ann.parse_stored(poll["ends_at"]) if poll["ends_at"] else None
    if poll["status"] != "open" or (closes is not None and closes <= now):
        raise OperationError(f"Poll #{poll['id']} is closed.")
    options = poll["options"]
    if not 0 <= int(index) < len(options):
        raise OperationError("That option no longer exists.")
    index = int(index)
    before, after = polls.cast_vote(
        poll_id=poll["id"],
        user_id=actor.id,
        voter_name=actor.name,
        index=index,
        multiple=poll["multiple"],
        now=now,
    )
    if before != after:
        counts, voters = polls.tally(poll["id"], len(options))
        embed = polls.build_poll_embed(
            question=poll["question"],
            options=options,
            votes=counts,
            voters=voters,
            ends_at=closes,
            mode=polls.MODE_BUTTONS,
            multiple=poll["multiple"],
        )
        try:
            await gw.edit_embed(guild_id, poll["channel_id"], poll["message_id"], embed)
        except OperationError as exc:  # the vote is kept even if the message can not be edited
            logger.warning("Could not refresh poll #%s results: %s", poll["id"], exc)
    return {
        "poll_id": poll["id"],
        "question": poll["question"],
        "option": options[index],
        "index": index,
        "multiple": poll["multiple"],
        "unchanged": before == after,
        "added": index in after,
        "choices": [options[i] for i in after],
    }


async def _finish_poll(gw: Gateway, cfg: dict, poll: dict[str, Any], actor: Actor, now: datetime) -> dict[str, Any]:
    guild_id, channel_id = poll["guild_id"], poll["channel_id"]
    options = poll["options"]
    voters: Optional[int] = None
    if poll["mode"] == polls.MODE_BUTTONS:
        votes, voters = polls.tally(poll["id"], len(options))
        embed = polls.build_poll_embed(
            question=poll["question"],
            options=options,
            votes=votes,
            voters=voters,
            closed=True,
            mode=polls.MODE_BUTTONS,
            multiple=poll["multiple"],
        )
        clear_view = True
    else:
        # A poll from before buttons: the votes are the reactions on its message.
        emojis = polls.EMOJIS[: len(options)]
        try:
            raw = await gw.reaction_counts(guild_id, channel_id, poll["message_id"], emojis)
        except OperationError as exc:
            # The poll message was deleted. Close the poll with no votes rather than retry forever.
            logger.warning("Could not read poll #%s votes (%s); closing it with zero votes.", poll["id"], exc)
            raw = [1] * len(emojis)
        votes = polls.votes_from_reactions(raw)
        embed = polls.build_poll_embed(question=poll["question"], options=options, votes=votes, closed=True)
        clear_view = False
    try:
        await gw.edit_embed(guild_id, channel_id, poll["message_id"], embed, clear_view=clear_view)
    except OperationError as exc:
        logger.warning("Could not update poll #%s message: %s", poll["id"], exc)
    polls.mark_closed(poll["id"], votes, now)
    audit.record(guild_id, actor, "poll.close", f"#{poll['id']}: {poll['question']}")
    return {
        "id": poll["id"],
        "question": poll["question"],
        "options": options,
        "votes": votes,
        "voters": voters,
    }


@_user_errors
async def close_poll(gw: Gateway, cfg: dict, guild_id: int, actor: Actor, poll_id: int, *, now: Optional[datetime] = None) -> dict[str, Any]:
    poll = polls.get_poll(guild_id, poll_id)
    if poll is None:
        raise OperationError(f"There is no poll #{poll_id} in this server.")
    if poll["status"] != "open":
        raise OperationError(f"Poll #{poll_id} is already closed.")
    return await _finish_poll(gw, cfg, poll, actor, _now(now))


def list_polls(guild_id: int, *, open_only: bool = False) -> list[dict[str, Any]]:
    return polls.list_polls(guild_id, open_only=open_only)


def poll_details(guild_id: int, poll_id: int) -> dict[str, Any]:
    """A poll with its current counts and, for button polls, who voted for what."""
    poll = polls.get_poll(guild_id, poll_id)
    if poll is None:
        raise OperationError(f"There is no poll #{poll_id} in this server.")
    if poll["mode"] == polls.MODE_BUTTONS:
        counts, voters_count = polls.tally(poll["id"], len(poll["options"]))
        people = polls.voters(poll["id"])
        for person in people:
            person["choices"] = [poll["options"][i] for i in person["choices"] if 0 <= i < len(poll["options"])]
    else:
        counts = poll["results"] or []
        voters_count = None
        people = []
    return {**poll, "counts": counts, "voters_count": voters_count, "voters": people}


# --------------------------------------------------------------------------- #
# Tickets
# --------------------------------------------------------------------------- #
@_user_errors
async def post_ticket_panel(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    actor: Actor,
    *,
    channel_id: Optional[int] = None,
) -> dict[str, Any]:
    target = _require_channel(gw, cfg, guild_id, channel_id)
    sent = await gw.post(guild_id, target, embed=tickets.panel_embed(), view=tickets.panel_buttons())
    audit.record(guild_id, actor, "ticket.panel", f"channel {target}")
    return {"channel_id": target, "message_id": str(sent["message_id"]), "jump_url": sent["jump_url"]}


def find_ticket_by_channel(guild_id: int, channel_id: int) -> Optional[dict[str, Any]]:
    """The open ticket that uses this channel, or ``None``."""
    row = tickets.get_by_channel(guild_id, channel_id)
    if row is None or row["status"] != "open":
        return None
    return row


async def open_ticket(gw: Gateway, cfg: dict, guild_id: int, actor: Actor, *, now: Optional[datetime] = None) -> dict[str, Any]:
    now = _now(now)
    existing = tickets.open_for_user(guild_id, actor.id)
    if existing is not None:
        raise OperationError(f"You already have ticket #{existing['id']} open: <#{existing['channel_id']}>.")
    category_id = settings.get_ticket_category_id(cfg, guild_id)
    staff_role_id = settings.get_staff_role_id(cfg, guild_id)
    channel_id = await gw.create_ticket_channel(
        guild_id,
        name=tickets.channel_name(actor.name),
        opener_id=actor.id,
        category_id=category_id,
        staff_role_id=staff_role_id,
        reason=f"Ticket opened by {actor.name}",
    )
    ticket_id = tickets.create(guild_id, channel_id, actor.id, actor.name, now)
    mentions = f"<@{actor.id}>" + (f" <@&{staff_role_id}>" if staff_role_id else "")
    try:
        await gw.post(
            guild_id,
            channel_id,
            embed=tickets.welcome_embed(actor.name, ticket_id),
            content=mentions,
            role_id=staff_role_id,
            user_ids=(actor.id,),
            view=tickets.close_buttons(),
        )
    except Exception as exc:  # the ticket exists even if its greeting did not post
        logger.warning("Ticket #%s opened without its greeting: %s", ticket_id, exc)
    embed = discord.Embed(
        title=f"Ticket #{ticket_id} opened",
        description=f"{actor.name} opened <#{channel_id}>.",
        color=discord.Color.blurple(),
        timestamp=now,
    )
    await _log(gw, cfg, guild_id, embed)
    audit.record(guild_id, actor, "ticket.open", f"#{ticket_id} in channel {channel_id}")
    return {"id": ticket_id, "channel_id": str(channel_id)}


async def close_ticket(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    actor: Actor,
    ticket_id: int,
    *,
    actor_is_staff: bool,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Delete the ticket's channel and record the close. Only staff or the opener may close it."""
    now = _now(now)
    ticket = tickets.get(guild_id, ticket_id)
    if ticket is None:
        raise OperationError(f"There is no ticket #{ticket_id} in this server.")
    if ticket["status"] != "open":
        raise OperationError(f"Ticket #{ticket_id} is already closed.")
    if not actor_is_staff and int(actor.id) != int(ticket["opener_id"]):
        raise OperationError("Only the person who opened this ticket, or staff, can close it.")
    await gw.delete_channel(guild_id, ticket["channel_id"], reason=f"Ticket #{ticket_id} closed by {actor.name}")
    tickets.mark_closed(ticket_id, actor.name, now)
    embed = discord.Embed(
        title=f"Ticket #{ticket_id} closed",
        description=f"Opened by {ticket['opener_name'] or 'a member'}, closed by {actor.name}.",
        color=discord.Color.greyple(),
        timestamp=now,
    )
    await _log(gw, cfg, guild_id, embed)
    audit.record(guild_id, actor, "ticket.close", f"#{ticket_id}")
    return {"id": ticket_id, "channel_id": str(ticket["channel_id"]), "opener_name": ticket["opener_name"]}


def list_tickets(guild_id: int, *, open_only: bool = False) -> list[dict[str, Any]]:
    return tickets.list_tickets(guild_id, open_only=open_only)


# --------------------------------------------------------------------------- #
# Reaction roles
# --------------------------------------------------------------------------- #
@_user_errors
async def add_reaction_role(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    actor: Actor,
    *,
    message: str,
    emoji: str,
    role_id: Any,
    channel_id: Optional[int] = None,
) -> dict[str, Any]:
    ref_guild, ref_channel, message_id = reaction_roles.parse_message_ref(message)
    if ref_guild is not None and int(ref_guild) != int(guild_id):
        raise ValueError("That message link is from another server.")
    target = ref_channel or (int(channel_id) if channel_id else settings.get_announcement_channel_id(cfg, guild_id))
    if not target:
        raise ValueError("Paste a message link, or choose the channel the message is in.")
    target = int(target)
    problem = gw.channel_problem(guild_id, target)
    if problem:
        raise OperationError(problem)
    emoji_text = reaction_roles.parse_emoji(emoji)
    role = int(role_id)
    problem = gw.role_problem(guild_id, role)
    if problem:
        raise OperationError(problem)
    if reaction_roles.find(message_id, emoji_text):
        raise ValueError("That emoji already gives a role on that message. Remove it first.")
    # The bot reacts too, so members have something to click. This also fails if the message is missing.
    await gw.add_reactions(guild_id, target, message_id, [emoji_text])
    try:
        rule_id = reaction_roles.add(
            guild_id=guild_id, channel_id=target, message_id=message_id, emoji=emoji_text, role_id=role
        )
    except sqlite3.IntegrityError:
        raise ValueError("That emoji already gives a role on that message. Remove it first.") from None
    audit.record(guild_id, actor, "reactionrole.add", f"#{rule_id}: {emoji_text} gives role {role} on {message_id}")
    return {
        "id": rule_id,
        "channel_id": str(target),
        "message_id": str(message_id),
        "emoji": emoji_text,
        "role_id": str(role),
    }


def list_reaction_roles(guild_id: int) -> list[dict[str, Any]]:
    return reaction_roles.list_rules(guild_id)


def remove_reaction_role(guild_id: int, actor: Actor, rule_id: int) -> None:
    """Stop the emoji giving a role. Roles members already have are kept."""
    if reaction_roles.get(guild_id, rule_id) is None:
        raise OperationError(f"There is no reaction role #{rule_id} in this server.")
    reaction_roles.remove(guild_id, rule_id)
    audit.record(guild_id, actor, "reactionrole.remove", f"#{rule_id}")


async def handle_reaction(
    gw: Gateway,
    guild_id: int,
    channel_id: int,
    message_id: int,
    user_id: int,
    emoji: str,
    *,
    added: bool,
) -> Optional[int]:
    """Give or take the role for one reaction. Returns the role id, or ``None`` if nothing applied.

    Never raises: a reaction event is not a command, so a failure is only logged.
    """
    rule = reaction_roles.find(message_id, emoji)
    if rule is None or int(rule["guild_id"]) != int(guild_id):
        return None
    try:
        await gw.set_member_role(guild_id, user_id, int(rule["role_id"]), add=added)
    except OperationError as exc:
        logger.warning("Reaction role #%s could not change a role for %s: %s", rule["id"], user_id, exc)
        return None
    except Exception:
        logger.exception("Reaction role #%s failed for %s", rule["id"], user_id)
        return None
    return int(rule["role_id"])


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
@_user_errors
async def create_event(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    actor: Actor,
    *,
    title: str,
    starts_when: str,
    description: str = "",
    channel_id: Optional[int] = None,
    remind_minutes: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = _now(now)
    heading = events.clean_title(title)
    details = events.clean_description(description)
    starts_at = ann.parse_send_time(starts_when, now, settings.get_timezone_name(cfg, guild_id))
    events.check_start_time(starts_at, now)
    remind = events.clean_remind_minutes(remind_minutes)
    target = _require_channel(gw, cfg, guild_id, channel_id)
    embed = events.build_event_embed(
        title=heading, description=details, starts_at=starts_at, author_name=actor.name, going=0
    )
    sent = await gw.post(guild_id, target, embed=embed)
    event_id = events.create_event(
        guild_id=guild_id,
        channel_id=target,
        author_id=actor.id,
        title=heading,
        description=details,
        starts_at=starts_at,
        remind_minutes=remind,
        now=now,
    )
    events.attach_message(event_id, sent["message_id"])
    try:
        await gw.add_reactions(guild_id, target, sent["message_id"], [events.RSVP_EMOJI])
    except OperationError as exc:
        logger.warning("Event #%s posted without its RSVP reaction: %s", event_id, exc)
    audit.record(guild_id, actor, "event.create", f"#{event_id}: {heading}")
    return {
        "id": event_id,
        "channel_id": target,
        "message_id": str(sent["message_id"]),
        "starts_at": ann.iso_utc(starts_at),
        "reminder_at": ann.iso_utc(events.reminder_time(starts_at, remind)) if remind else None,
    }


@_user_errors
async def cancel_event(gw: Gateway, cfg: dict, guild_id: int, actor: Actor, event_id: int) -> None:
    event = events.get_event(guild_id, event_id)
    if event is None or event["status"] != "open":
        raise OperationError(f"There is no open event #{event_id} in this server.")
    events.cancel_event(guild_id, event_id)
    embed = events.build_event_embed(
        title=event["title"],
        description=event["description"],
        starts_at=ann.parse_stored(event["starts_at"]),
        author_name=actor.name,
        cancelled=True,
    )
    if event["message_id"]:
        try:
            await gw.edit_embed(guild_id, event["channel_id"], event["message_id"], embed)
        except OperationError as exc:
            logger.warning("Could not mark event #%s cancelled on Discord: %s", event_id, exc)
    audit.record(guild_id, actor, "event.cancel", f"#{event_id}: {event['title']}")


async def list_events(gw: Gateway, guild_id: int, *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Upcoming events, each with its live RSVP count (``None`` if unreadable)."""
    rows = events.list_upcoming(guild_id, _now(now))
    for row in rows:
        row["going"] = None
        if row["message_id"]:
            try:
                raw = await gw.reaction_counts(guild_id, row["channel_id"], row["message_id"], [events.RSVP_EMOJI])
                row["going"] = polls.votes_from_reactions(raw)[0]
            except OperationError:
                pass
    return rows


# --------------------------------------------------------------------------- #
# Welcome messages
# --------------------------------------------------------------------------- #
@_user_errors
async def set_welcome(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    actor: Actor,
    *,
    channel_id: int,
    message: Optional[str] = None,
) -> dict[str, Any]:
    problem = gw.channel_problem(guild_id, int(channel_id))
    if problem:
        raise OperationError(problem)
    settings.set_guild_value(cfg, guild_id, "welcome_channel_id", int(channel_id))
    if message is not None:
        settings.set_guild_value(cfg, guild_id, "welcome_message", welcome.clean_template(message))
    _persist(cfg)
    audit.record(guild_id, actor, "welcome.set", f"#{channel_id}")
    return {"channel_id": int(channel_id), "message": welcome_template(cfg, guild_id)}


def disable_welcome(cfg: dict, guild_id: int, actor: Actor) -> None:
    settings.set_guild_value(cfg, guild_id, "welcome_channel_id", None)
    _persist(cfg)
    audit.record(guild_id, actor, "welcome.off", "")


def welcome_template(cfg: dict, guild_id: int) -> str:
    return settings.get_guild_config(cfg, guild_id)["welcome_message"] or welcome.DEFAULT_WELCOME_MESSAGE


def render_for(cfg: dict, guild_id: int, *, mention: str, name: str, server: str, members: int) -> str:
    return welcome.render_welcome(
        welcome_template(cfg, guild_id), mention=mention, name=name, server=server, members=members
    )


@_user_errors
async def send_test_welcome(gw: Gateway, cfg: dict, guild_id: int, actor: Actor, *, server: str, members: int) -> dict[str, Any]:
    channel_id = settings.get_welcome_channel_id(cfg, guild_id)
    if not channel_id:
        raise OperationError("Welcome messages are off. Set a channel with `/manage welcome set`.")
    problem = gw.channel_problem(guild_id, channel_id)
    if problem:
        raise OperationError(problem)
    text = render_for(
        cfg, guild_id, mention=f"<@{actor.id}>", name=actor.name, server=server, members=members
    )
    sent = await gw.post(guild_id, channel_id, content=text, user_ids=(actor.id,))
    audit.record(guild_id, actor, "welcome.test", f"#{channel_id}")
    return {"message_id": str(sent["message_id"]), "channel_id": channel_id, "text": text}


async def send_welcome(
    gw: Gateway,
    cfg: dict,
    guild_id: int,
    *,
    user_id: int,
    user_name: str,
    server: str,
    members: int,
) -> Optional[dict[str, Any]]:
    """Welcome a new member. Returns ``None`` when welcomes are off. Never raises."""
    channel_id = settings.get_welcome_channel_id(cfg, guild_id)
    if not channel_id:
        return None
    text = render_for(
        cfg, guild_id, mention=f"<@{user_id}>", name=user_name, server=server, members=members
    )
    try:
        return await gw.post(guild_id, channel_id, content=text, user_ids=(user_id,))
    except OperationError as exc:
        logger.warning("Could not send the welcome in %s: %s", guild_id, exc)
        return None


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
SETTING_FIELDS = (
    "staff_role_id",
    "announcement_channel_id",
    "staff_channel_id",
    "timezone",
    "welcome_channel_id",
    "welcome_message",
    "require_approval",
    "quiet_start",
    "quiet_end",
    "quiet_hold_manual",
    "ticket_category_id",
)


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return value.strip() == ""
    return isinstance(value, int) and value == 0


def _as_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _clean_setting(gw: Gateway, guild_id: int, key: str, value: Any) -> Any:
    if key in ("require_approval", "quiet_hold_manual"):
        return _as_flag(value)
    if _is_blank(value):
        value = None
    if key == "timezone" and value:
        return ann.validate_timezone(str(value))
    if key == "welcome_message" and value:
        return welcome.clean_template(str(value))
    if key in ("quiet_start", "quiet_end") and value:
        return quiet.parse_clock(str(value))
    if key.endswith("_id") and value is not None:
        value = int(value)
        if key in ("announcement_channel_id", "staff_channel_id", "welcome_channel_id"):
            problem = gw.channel_problem(guild_id, value)
            if problem:
                raise OperationError(problem)
    return value


@_user_errors
async def update_settings(gw: Gateway, cfg: dict, guild_id: int, actor: Actor, changes: dict[str, Any]) -> dict[str, Any]:
    """Apply the given settings for one server. ``None`` clears a value."""
    cleaned: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in SETTING_FIELDS:
            raise ValueError(f"Unknown setting `{key}`.")
        cleaned[key] = _clean_setting(gw, guild_id, key, value)
    if "quiet_start" in cleaned or "quiet_end" in cleaned:
        current_start, current_end = settings.get_quiet_hours(cfg, guild_id)
        start = cleaned.get("quiet_start", current_start)
        end = cleaned.get("quiet_end", current_end)
        cleaned["quiet_start"], cleaned["quiet_end"] = quiet.validate_pair(start, end)
    for key, value in cleaned.items():
        settings.set_guild_value(cfg, guild_id, key, value)
    _persist(cfg)
    audit.record(guild_id, actor, "settings.update", ", ".join(sorted(cleaned)))
    return cleaned


# --------------------------------------------------------------------------- #
# The scheduler
# --------------------------------------------------------------------------- #
async def run_scheduler(gw: Gateway, cfg: dict, *, now: Optional[datetime] = None) -> dict[str, int]:
    """Do everything that is due: scheduled posts, unpins, poll closes, event reminders."""
    now = _now(now)
    counts = {
        "posted": 0,
        "failed": 0,
        "deferred": 0,
        "unpinned": 0,
        "polls_closed": 0,
        "reminders": 0,
        "approval_reminders": 0,
    }

    for row in ann.list_due_scheduled(now):
        await _run_scheduled(gw, cfg, row, now, counts)

    for pin in pins.list_due(now):
        await _unpin(gw, pin, counts)

    for poll in polls.list_due_polls(now):
        try:
            await _finish_poll(gw, cfg, poll, SCHEDULER_ACTOR, now)
            counts["polls_closed"] += 1
        except Exception:
            logger.exception("Could not close poll #%s", poll["id"])

    for event in events.list_due_reminders(now):
        await _send_reminder(gw, cfg, event, counts)

    await _remind_waiting_approvals(gw, cfg, now, counts)

    return counts


async def _remind_waiting_approvals(gw: Gateway, cfg: dict, now: datetime, counts: dict[str, int]) -> None:
    """Remind the staff log about requests that have waited a day. One message per server."""
    by_guild: dict[int, list[dict[str, Any]]] = {}
    for row in approvals.list_due_reminders(now):
        by_guild.setdefault(int(row["guild_id"]), []).append(row)
    for guild_id, waiting in by_guild.items():
        # Without a staff log nobody would see the reminder, so the request stays due until one is set.
        if not settings.get_staff_channel_id(cfg, guild_id):
            continue
        try:
            lines = [
                f"**#{row['id']}** · {row['kind']} · requested by {row['requester_name']} · "
                f"since {ann.discord_time(ann.parse_stored(row['created_at']))}"
                for row in waiting[:25]
            ]
            if len(waiting) > 25:
                lines.append(f"...and {len(waiting) - 25} more.")
            embed = discord.Embed(
                title=f"{len(waiting)} request(s) still waiting for approval",
                description="\n".join(lines)[:4000],
                color=ann.EMBED_COLOR,
            )
            embed.set_footer(text="Approve with /manage approve, or turn one down with /manage reject.")
            await _log(gw, cfg, guild_id, embed)
            approvals.mark_reminded([int(row["id"]) for row in waiting], now)
            counts["approval_reminders"] += len(waiting)
            audit.record(guild_id, SCHEDULER_ACTOR, "approval.remind", f"{len(waiting)} waiting")
        except Exception:
            logger.exception("Could not send the approval reminder for server %s", guild_id)


async def _unpin(gw: Gateway, pin: dict[str, Any], counts: dict[str, int]) -> None:
    guild_id, channel_id, message_id = int(pin["guild_id"]), int(pin["channel_id"]), int(pin["message_id"])
    try:
        await gw.unpin_message(guild_id, channel_id, message_id)
    except Exception as exc:
        reason = (str(exc) or type(exc).__name__)[:500]
        pins.mark(pin["id"], pins.FAILED, reason)
        logger.warning("Could not unpin message %s in %s: %s", message_id, channel_id, reason)
        return
    pins.mark(pin["id"], pins.UNPINNED)
    counts["unpinned"] += 1
    audit.record(guild_id, SCHEDULER_ACTOR, "pin.expire", f"message {message_id} in #{channel_id}")


async def _run_scheduled(gw: Gateway, cfg: dict, row: dict[str, Any], now: datetime, counts: dict[str, int]) -> None:
    guild_id = int(row["guild_id"])
    channel_id = int(row["channel_id"])
    repeating = row["interval_seconds"] is not None
    source = "repeat" if repeating else "scheduled"

    # Quiet hours: an announcement due inside the window waits for it to end.
    held_until = quiet.window_end(now, settings.get_timezone_name(cfg, guild_id), *settings.get_quiet_hours(cfg, guild_id))
    if held_until is not None:
        ann.set_send_at(row["id"], held_until)
        counts["deferred"] += 1
        audit.record(guild_id, SCHEDULER_ACTOR, "schedule.hold", f"#{row['id']} until {ann.iso_utc(held_until)}")
        return

    author = row["author_name"] or "staff"
    error: Optional[str] = None
    try:
        problem = gw.channel_problem(guild_id, channel_id)
        if problem:
            raise OperationError(problem)
        embed = ann.build_announcement_embed(title=row["title"], body=row["body"], author_name=author)
        sent = await gw.post(
            guild_id,
            channel_id,
            embed=embed,
            content=ann.mention_content(row["mention_role_id"]),
            role_id=row["mention_role_id"],
            publish=bool(row["publish"]),
        )
        ann.record_announcement(
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=sent["message_id"],
            author_id=row["author_id"],
            author_name=author,
            title=row["title"],
            body=row["body"],
            mention_role_id=row["mention_role_id"],
            source=source,
            when=now,
        )
        counts["posted"] += 1
        audit.record(guild_id, SCHEDULER_ACTOR, "schedule.post", f"#{row['id']} ({source}) in #{channel_id}")
        await _log(
            gw,
            cfg,
            guild_id,
            ann.build_staff_log_embed(
                heading="Scheduled announcement posted" if not repeating else "Repeating announcement posted",
                channel_mention=f"<#{channel_id}>",
                author_mention=author,
                title=row["title"],
                body=row["body"],
                mention=ann.mention_content(row["mention_role_id"]),
                source=f"Schedule #{row['id']}",
            ),
        )
        if row.get("pin_seconds"):
            _, pin_warnings = await _pin(gw, guild_id, channel_id, sent["message_id"], int(row["pin_seconds"]), now)
            for note in pin_warnings:
                await _log(
                    gw,
                    cfg,
                    guild_id,
                    _failure_embed("Could not pin a scheduled announcement", note, channel_id, f"Schedule #{row['id']}"),
                )
    except Exception as exc:
        error = (str(exc) or type(exc).__name__)[: ann.ERROR_TEXT_LIMIT]
        counts["failed"] += 1
        logger.warning("Scheduled announcement #%s failed: %s", row["id"], error)
        await _log(
            gw,
            cfg,
            guild_id,
            _failure_embed(
                "Scheduled announcement failed", error, channel_id, f"Schedule #{row['id']}"
            ),
        )

    if repeating:
        ann.advance_repeat(row, now, error)
    else:
        ann.finish_one_off(row["id"], "failed" if error else "sent", error, now)


async def _send_reminder(gw: Gateway, cfg: dict, event: dict[str, Any], counts: dict[str, int]) -> None:
    guild_id, channel_id = int(event["guild_id"]), int(event["channel_id"])
    try:
        going: list[int] = []
        if event["message_id"]:
            going = await gw.reactors(
                guild_id, channel_id, event["message_id"], events.RSVP_EMOJI, events.MAX_REMINDER_MENTIONS
            )
        mentions = " ".join(f"<@{uid}>" for uid in going)
        starts = int(ann.parse_stored(event["starts_at"]).timestamp())
        text = f"**{event['title']}** starts <t:{starts}:R>. {len(going)} going."
        if mentions:
            text = f"{text}\n{mentions}"
        await gw.post(guild_id, channel_id, content=text, user_ids=tuple(going))
        counts["reminders"] += 1
        audit.record(guild_id, SCHEDULER_ACTOR, "event.remind", f"#{event['id']}")
    except Exception as exc:
        counts["failed"] += 1
        logger.warning("Could not send the reminder for event #%s: %s", event["id"], exc)
    finally:
        # Mark it either way, so a broken event does not produce a reminder every tick.
        events.mark_reminded(event["id"])


# --------------------------------------------------------------------------- #
# Overview for the dashboard and /manage status
# --------------------------------------------------------------------------- #
def overview(cfg: dict, guild_id: int, *, now: Optional[datetime] = None) -> dict[str, Any]:
    pending = ann.list_pending(guild_id)
    return {
        "scheduled": sum(1 for r in pending if r["interval_seconds"] is None),
        "repeats": sum(1 for r in pending if r["interval_seconds"] is not None),
        "templates": len(ann.list_templates(guild_id)),
        "polls_open": len(polls.list_polls(guild_id, open_only=True)),
        "events_upcoming": len(events.list_upcoming(guild_id, _now(now), limit=100)),
        "welcome_enabled": settings.get_welcome_channel_id(cfg, guild_id) is not None,
        "approvals_pending": len(approvals.list_pending(guild_id)),
        "pins_active": len(pins.list_active(guild_id)),
        "tickets_open": len(tickets.list_tickets(guild_id, open_only=True)),
        "reaction_roles": len(reaction_roles.list_rules(guild_id)),
        "recent": ann.list_history(guild_id, 5),
    }
