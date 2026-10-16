"""Demo mode: the dashboard and scheduler with simulated servers and no token.

    python bot.py --demo

It runs the same operations as the live bot, against :class:`DemoGateway`,
which keeps posted messages and reactions in memory. Nothing is sent to
Discord. The database is a temporary directory unless ``COMBAT_MASTER_DATA``
is set, so a demo never touches a real install.

:class:`DemoGateway` is also what the test suite uses as its fake Discord.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import secrets
import tempfile
from datetime import datetime, timezone
from typing import Any, Optional

import discord

import settings
import store
from operations import OperationError
from discord_gateway import _norm

logger = logging.getLogger("combat_master.demo")

DEMO_GUILDS = [
    {
        "id": 100000000000000001,
        "name": "Combat Arena",
        "member_count": 1284,
        "channels": [
            (100000000000000101, "announcements", True),
            (100000000000000102, "general", False),
            (100000000000000103, "events", False),
            (100000000000000104, "polls", False),
            (100000000000000105, "staff-log", False),
        ],
        "roles": [
            (100000000000000201, "Staff", True),
            (100000000000000202, "Member", True),
            (100000000000000203, "Event Pings", False),
        ],
    },
    {
        "id": 100000000000000002,
        "name": "Lofi Study Hall",
        "member_count": 312,
        "channels": [
            (100000000000000301, "news", True),
            (100000000000000302, "chat", False),
            (100000000000000303, "mod-log", False),
        ],
        "roles": [
            (100000000000000401, "Moderators", True),
            (100000000000000402, "Regulars", True),
        ],
    },
]

DEMO_MEMBER_IDS = [100000000000001000 + i for i in range(60)]


class DemoGateway:
    """An in-memory Discord. Votes are simulated unless a test sets them."""

    def __init__(self, guilds: Optional[list[dict[str, Any]]] = None) -> None:
        self._guilds = copy.deepcopy(guilds if guilds is not None else DEMO_GUILDS)
        self._channel_index: dict[int, tuple[int, str]] = {}
        for guild in self._guilds:
            for cid, name, *_ in guild["channels"]:
                self._channel_index[cid] = (guild["id"], name)
        self._next_message = 900000000000000000
        self.messages: dict[tuple[int, int], dict[str, Any]] = {}
        # Explicit vote counts for tests: (message_id, emoji) -> raw count.
        self.counts: dict[tuple[int, str], int] = {}
        self.posts: list[dict[str, Any]] = []
        self.fail_posts_in: set[int] = set()

    # ---- lookups ------------------------------------------------------ #
    def _find(self, guild_id: int) -> Optional[dict[str, Any]]:
        return next((g for g in self._guilds if g["id"] == int(guild_id)), None)

    def guilds(self) -> list[dict[str, Any]]:
        return [{"id": g["id"], "name": g["name"], "member_count": g["member_count"]} for g in self._guilds]

    def snapshot(self, guild_id: int) -> Optional[dict[str, Any]]:
        guild = self._find(guild_id)
        if guild is None:
            return None
        return {
            "id": guild["id"],
            "name": guild["name"],
            "member_count": guild["member_count"],
            "channels": [{"id": cid, "name": name, "news": news} for cid, name, news in guild["channels"]],
            "roles": [{"id": rid, "name": name, "mentionable": m} for rid, name, m in guild["roles"]],
        }

    def channel_problem(self, guild_id: int, channel_id: int) -> Optional[str]:
        if self._find(guild_id) is None:
            return "I'm not in that server any more."
        entry = self._channel_index.get(int(channel_id))
        if entry is None or entry[0] != int(guild_id):
            return "That channel does not exist in this server, or it is not a text channel."
        if int(channel_id) in self.fail_posts_in:
            return f"I can't send messages in #{entry[1]}. Grant me View Channel and Send Messages there."
        return None

    def ping_warning(self, guild_id: int, role_id: int) -> Optional[str]:
        guild = self._find(guild_id)
        if guild is None:
            return None
        for rid, name, mentionable in guild["roles"]:
            if rid == int(role_id) and not mentionable:
                return f"Note: @{name} is not mentionable, so it will show but not notify."
        return None

    # ---- posting ------------------------------------------------------ #
    def _check(self, guild_id: int, channel_id: int) -> None:
        problem = self.channel_problem(guild_id, channel_id)
        if problem:
            raise OperationError(problem)

    def _new_id(self) -> int:
        self._next_message += 1
        return self._next_message

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
    ) -> dict[str, Any]:
        self._check(guild_id, channel_id)
        message_id = self._new_id()
        published: Optional[bool] = None
        if publish:
            published = any(
                cid == int(channel_id) and is_news for cid, _, is_news in self._guild_channels(guild_id)
            )
        self.messages[(int(channel_id), message_id)] = {
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "embed": embed,
            "content": content,
            "role_id": role_id,
            "user_ids": list(user_ids),
        }
        self.posts.append({"channel_id": int(channel_id), "message_id": message_id, "content": content, "embed": embed})
        return {
            "message_id": message_id,
            "jump_url": f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}",
            "published": published,
        }

    def _guild_channels(self, guild_id: int):
        guild = self._find(guild_id)
        return guild["channels"] if guild else []

    async def edit_embed(self, guild_id: int, channel_id: int, message_id: int, embed: discord.Embed) -> None:
        record = self.messages.get((int(channel_id), int(message_id)))
        if record is None:
            raise OperationError("Could not find that message on Discord.")
        record["embed"] = embed

    def _simulated(self, message_id: int, emoji: str, index: int) -> int:
        key = (int(message_id), _norm(emoji))
        if key in self.counts:
            return self.counts[key]
        # Deterministic demo votes: the bot's own reaction plus a few members.
        return 1 + ((int(message_id) + index * 7) % 6)

    async def reaction_counts(self, guild_id: int, channel_id: int, message_id: int, emojis: list[str]) -> list[int]:
        if (int(channel_id), int(message_id)) not in self.messages:
            raise OperationError("Could not find that message on Discord.")
        return [self._simulated(message_id, e, i) for i, e in enumerate(emojis)]

    async def reactors(self, guild_id: int, channel_id: int, message_id: int, emoji: str, limit: int) -> list[int]:
        if (int(channel_id), int(message_id)) not in self.messages:
            raise OperationError("Could not find that message on Discord.")
        count = max(0, self._simulated(message_id, emoji, 0) - 1)
        return DEMO_MEMBER_IDS[: min(count, limit)]

    async def add_reactions(self, guild_id: int, channel_id: int, message_id: int, emojis: list[str]) -> None:
        if (int(channel_id), int(message_id)) not in self.messages:
            raise OperationError("Could not find that message on Discord.")


# --------------------------------------------------------------------------- #
# Running the demo
# --------------------------------------------------------------------------- #
def demo_config(guilds: list[dict[str, Any]]) -> dict[str, Any]:
    """A config where every demo server already has its roles and channels set."""
    cfg = copy.deepcopy(settings.DEFAULT_CONFIG)
    cfg["dashboard_token"] = secrets.token_urlsafe(18)
    for guild in guilds:
        names = {name: cid for cid, name, _ in guild["channels"]}
        roles = {name: rid for rid, name, _ in guild["roles"]}
        staff_role = roles.get("Staff") or roles.get("Moderators")
        announce = next((cid for cid, _, news in guild["channels"] if news), None)
        entry = {
            "staff_role_id": staff_role,
            "announcement_channel_id": announce,
            "staff_channel_id": names.get("staff-log") or names.get("mod-log"),
            "timezone": "America/Toronto",
            "welcome_channel_id": names.get("general") or names.get("chat"),
            "welcome_message": None,
        }
        cfg["guilds"][str(guild["id"])] = entry
    return cfg


async def run_demo(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the dashboard over simulated servers until interrupted."""
    import paths
    from dashboard import DashboardServer
    from operations import run_scheduler

    if "COMBAT_MASTER_DATA" not in os.environ:
        os.environ["COMBAT_MASTER_DATA"] = tempfile.mkdtemp(prefix="combat-master-demo-")
    store.init_db()

    gateway = DemoGateway()
    cfg = demo_config(DEMO_GUILDS)
    dashboard = DashboardServer(
        gateway,
        cfg,
        token=cfg["dashboard_token"],
        host=host,
        port=port,
        allowed_hosts=["*"],
        secure_cookie=False,
        demo=True,
    )
    await dashboard.start()
    print(f"Demo dashboard: http://{host if host != '0.0.0.0' else 'localhost'}:{port}  (no sign-in needed)", flush=True)
    print(f"Data directory: {paths.data_dir()} (temporary)", flush=True)
    try:
        while True:
            try:
                counts = await run_scheduler(gateway, cfg, now=datetime.now(timezone.utc))
                if any(counts.values()):
                    logger.info("Demo scheduler: %s", counts)
            except Exception:
                logger.exception("Demo scheduler tick failed.")
            await asyncio.sleep(15)
    finally:
        await dashboard.close()
