"""
Tests for the /manage slash commands (bot.py).

Two kinds of check:

* the command tree itself: every name and description is inside Discord's
  limits (a single over-long description makes Discord reject the whole list,
  HTTP 400 / code 50035), and every payload serialises to JSON;
* the command handlers, called directly with fake interactions, so the staff
  gate, the posting path, scheduling and setup are exercised end to end
  without a Discord connection.

Run with either:
    python -m pytest tests/test_commands.py
    python tests/test_commands.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Keep the bot's log file and config out of the real data directory.
_TMP_ROOT = tempfile.TemporaryDirectory()
os.environ["COMBAT_MASTER_DATA"] = _TMP_ROOT.name

import discord  # noqa: E402

import announcements  # noqa: E402
import bot as bot_module  # noqa: E402
import store  # noqa: E402
from command_tree import manage_group  # noqa: E402
from settings import set_guild_value  # noqa: E402

# Discord's limit for the description of a command, group or option.
DESCRIPTION_MIN = 1
DESCRIPTION_MAX = 100

EXPECTED_COMMANDS = {
    "manage announce",
    "manage schedule",
    "manage scheduled",
    "manage cancel",
    "manage history",
    "manage status",
    "manage setup",
    "manage fixcommands",
}

GUILD_ID = 4321
STAFF_ROLE_ID = 5000
ANNOUNCE_CHANNEL_ID = 6000
LOG_CHANNEL_ID = 6001


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self):
        self.deferred = False

    def is_done(self):
        return self.deferred

    async def defer(self, ephemeral=False):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, content=None, *, embed=None, ephemeral=False, **_):
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})
        return SimpleNamespace(id=1)

    def last_text(self) -> str:
        return self.sent[-1]["content"] or ""


class FakeChannel:
    def __init__(self, channel_id: int, *, can_send: bool = True):
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.can_send = can_send
        self.sent: list[dict] = []

    def permissions_for(self, _member):
        return SimpleNamespace(
            view_channel=self.can_send,
            send_messages=self.can_send,
            embed_links=True,
            mention_everyone=False,
        )

    async def send(self, content=None, *, embed=None, allowed_mentions=None, **_):
        self.sent.append(
            {"content": content, "embed": embed, "allowed_mentions": allowed_mentions}
        )
        return SimpleNamespace(id=777, jump_url="https://discord.test/jump/777")


class FakeGuild:
    def __init__(self, roles=None, channels=None):
        self.id = GUILD_ID
        self.me = SimpleNamespace(id=1, display_name="Combat Master")
        self._roles = roles or {}
        self._channels = channels or {}

    def get_role(self, role_id):
        return self._roles.get(role_id)

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_member(self, _user_id):
        return None


def make_member(user_id: int, *, roles=(), admin=False, manage_guild=False):
    return SimpleNamespace(
        id=user_id,
        display_name=f"user{user_id}",
        mention=f"<@{user_id}>",
        guild_permissions=SimpleNamespace(administrator=admin, manage_guild=manage_guild),
        roles=[SimpleNamespace(id=r) for r in roles],
    )


def make_interaction(user, guild):
    return SimpleNamespace(
        user=user,
        guild=guild,
        guild_id=guild.id if guild else None,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


class FakeBot:
    """Stands in for the bot where a handler only needs the config and log."""

    def __init__(self, config):
        self.config = config
        self.logged: list[discord.Embed] = []

    async def _send_staff_log(self, guild, embed):
        self.logged.append(embed)

    async def sync_guild_commands(self, guild_id, force=False):
        return 0


# --------------------------------------------------------------------------- #
# Command tree (registered once: the cog is what Discord sees)
# --------------------------------------------------------------------------- #
_REGISTERED = False


def _register_once() -> None:
    global _REGISTERED
    if not _REGISTERED:
        asyncio.run(bot_module._register_cog(bot_module.bot))
        _REGISTERED = True


class CommandTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _register_once()

    def test_expected_commands_are_registered(self):
        names = {c.qualified_name for c in bot_module.bot.tree.walk_commands()}
        self.assertTrue(EXPECTED_COMMANDS <= names, EXPECTED_COMMANDS - names)

    def test_every_description_is_within_discord_limits(self):
        for command in bot_module.bot.tree.walk_commands():
            self.assertTrue(
                DESCRIPTION_MIN <= len(command.description) <= DESCRIPTION_MAX,
                f"{command.qualified_name}: {len(command.description)} chars",
            )
            for param in getattr(command, "parameters", []):
                self.assertTrue(
                    DESCRIPTION_MIN <= len(param.description) <= DESCRIPTION_MAX,
                    f"{command.qualified_name}.{param.name}",
                )

    def test_group_payload_serialises(self):
        payload = manage_group.to_dict(bot_module.bot.tree)
        json.dumps(payload)  # raises if Discord could not receive it
        self.assertEqual(payload["name"], "manage")
        self.assertIsNone(payload.get("default_member_permissions"))

    def test_setup_timezone_option_is_called_timezone(self):
        setup = bot_module.CombatMasterCog.setup_cmd
        # The Python name is timezone_name; Discord shows it as "timezone".
        shown = [p.display_name for p in setup.parameters]
        self.assertIn("timezone", shown)
        self.assertNotIn("timezone_name", shown)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
class HandlerTestCase(unittest.TestCase):
    def setUp(self):
        self._data = tempfile.TemporaryDirectory()
        self._old = os.environ.get("COMBAT_MASTER_DATA")
        os.environ["COMBAT_MASTER_DATA"] = self._data.name
        store.init_db()
        _register_once()

        self.config = {"bot_token": "", "default_timezone": "America/Toronto", "guilds": {}}
        set_guild_value(self.config, GUILD_ID, "staff_role_id", STAFF_ROLE_ID)
        set_guild_value(self.config, GUILD_ID, "announcement_channel_id", ANNOUNCE_CHANNEL_ID)
        set_guild_value(self.config, GUILD_ID, "staff_channel_id", LOG_CHANNEL_ID)

        self.fake_bot = FakeBot(self.config)
        self.cog = bot_module.CombatMasterCog(self.fake_bot)
        self.announce_channel = FakeChannel(ANNOUNCE_CHANNEL_ID)
        self.guild = FakeGuild(
            roles={STAFF_ROLE_ID: SimpleNamespace(id=STAFF_ROLE_ID, mention=f"<@&{STAFF_ROLE_ID}>", mentionable=True)},
            channels={ANNOUNCE_CHANNEL_ID: self.announce_channel},
        )
        self.staff = make_member(10, roles=[STAFF_ROLE_ID])
        self.outsider = make_member(11)
        self.admin = make_member(12, admin=True)

        # Channels are plain fakes, so resolve them by id instead of through
        # the real isinstance(TextChannel) check.
        resolver = mock.patch.object(
            bot_module,
            "_resolve_text_channel",
            new=mock.AsyncMock(side_effect=lambda guild, cid: guild.get_channel(int(cid))),
        )
        resolver.start()
        self.addCleanup(resolver.stop)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("COMBAT_MASTER_DATA", None)
        else:
            os.environ["COMBAT_MASTER_DATA"] = self._old
        self._data.cleanup()

    def run_cmd(self, handler, interaction, **kwargs):
        return asyncio.run(handler.callback(self.cog, interaction, **kwargs))


class AnnounceTests(HandlerTestCase):
    def announce(self, user, **kwargs):
        interaction = make_interaction(user, self.guild)
        options = {"message": "Raid at 8pm", "title": None, "channel": None, "mention": None}
        options.update(kwargs)
        self.run_cmd(bot_module.CombatMasterCog.manage_announce, interaction, **options)
        return interaction

    def test_outsider_is_refused_and_nothing_is_posted(self):
        interaction = self.announce(self.outsider)
        self.assertIn("can use this command", interaction.followup.last_text())
        self.assertEqual(self.announce_channel.sent, [])

    def test_administrator_passes_the_staff_gate(self):
        interaction = self.announce(self.admin)
        self.assertEqual(len(self.announce_channel.sent), 1)
        self.assertIn("Posted in", interaction.followup.last_text())

    def test_staff_post_is_recorded_and_logged(self):
        self.announce(self.staff, title="Raid night")
        sent = self.announce_channel.sent[0]
        self.assertIsNone(sent["content"])  # no ping without a chosen role
        self.assertEqual(sent["embed"].title, "Raid night")
        history = announcements.list_history(GUILD_ID)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["source"], "now")
        self.assertEqual(history[0]["message_id"], 777)
        self.assertEqual(len(self.fake_bot.logged), 1)

    def test_chosen_role_is_the_only_ping(self):
        role = SimpleNamespace(id=7001, mention="<@&7001>", mentionable=True)
        self.guild._roles[7001] = role
        self.announce(self.staff, mention=role)
        sent = self.announce_channel.sent[0]
        self.assertEqual(sent["content"], "<@&7001>")
        self.assertEqual([r.id for r in sent["allowed_mentions"].roles], [7001])

    def test_whitespace_only_message_is_refused(self):
        interaction = self.announce(self.staff, message="   ")
        self.assertIn("can not be empty", interaction.followup.last_text())
        self.assertEqual(self.announce_channel.sent, [])

    def test_missing_channel_explains_setup(self):
        set_guild_value(self.config, GUILD_ID, "announcement_channel_id", None)
        interaction = self.announce(self.staff)
        self.assertIn("/manage setup", interaction.followup.last_text())

    def test_missing_permissions_are_reported_before_posting(self):
        blocked = FakeChannel(9999, can_send=False)
        interaction = self.announce(self.staff, channel=blocked)
        self.assertIn("Send Messages", interaction.followup.last_text())
        self.assertEqual(blocked.sent, [])


class ScheduleTests(HandlerTestCase):
    def schedule(self, user, when, **kwargs):
        interaction = make_interaction(user, self.guild)
        options = {"when": when, "message": "Doors open", "title": "Event", "channel": None, "mention": None}
        options.update(kwargs)
        self.run_cmd(bot_module.CombatMasterCog.manage_schedule, interaction, **options)
        return interaction

    def test_delay_schedules_for_later(self):
        interaction = self.schedule(self.staff, "2h")
        self.assertIn("Scheduled **#1**", interaction.followup.last_text())
        pending = announcements.list_pending(GUILD_ID)
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.announce_channel.sent, [])  # nothing posted yet

    def test_bad_time_is_refused_and_nothing_is_stored(self):
        interaction = self.schedule(self.staff, "whenever")
        self.assertIn("could not read", interaction.followup.last_text())
        self.assertEqual(announcements.list_pending(GUILD_ID), [])

    def test_outsider_cannot_schedule(self):
        self.schedule(self.outsider, "2h")
        self.assertEqual(announcements.list_pending(GUILD_ID), [])

    def test_cancel_unknown_and_known(self):
        self.schedule(self.staff, "2h")
        interaction = make_interaction(self.staff, self.guild)
        self.run_cmd(bot_module.CombatMasterCog.manage_cancel, interaction, schedule_id=1)
        self.assertIn("Cancelled", interaction.followup.last_text())
        again = make_interaction(self.staff, self.guild)
        self.run_cmd(bot_module.CombatMasterCog.manage_cancel, again, schedule_id=1)
        self.assertIn("No pending", again.followup.last_text())

    def test_scheduled_post_is_delivered_by_the_bot(self):
        self.schedule(self.staff, "2h", mention=None)
        # Make the schedule due now and deliver it through the real bot method.
        store.db_execute("UPDATE scheduled_announcements SET send_at = '2000-01-01T00:00:00+00:00'")
        real_bot = bot_module.bot
        with mock.patch.object(real_bot, "get_guild", return_value=self.guild), mock.patch.object(
            real_bot, "_send_staff_log", new=mock.AsyncMock()
        ) as staff_log, mock.patch.object(bot_module, "_resolve_text_channel", new=mock.AsyncMock(return_value=self.announce_channel)):
            count = asyncio.run(announcements.run_due_scheduled(
                datetime.now(timezone.utc), real_bot._deliver_scheduled
            ))
        self.assertEqual(count, 1)
        self.assertEqual(len(self.announce_channel.sent), 1)
        row = store.db_fetchone("SELECT status FROM scheduled_announcements WHERE id = 1")
        self.assertEqual(row["status"], "sent")
        self.assertEqual(announcements.list_history(GUILD_ID)[0]["source"], "scheduled")
        staff_log.assert_awaited()

    def test_failed_scheduled_post_is_logged_to_staff(self):
        self.schedule(self.staff, "2h")
        store.db_execute("UPDATE scheduled_announcements SET send_at = '2000-01-01T00:00:00+00:00'")
        blocked = FakeChannel(ANNOUNCE_CHANNEL_ID, can_send=False)
        real_bot = bot_module.bot
        with mock.patch.object(real_bot, "get_guild", return_value=self.guild), mock.patch.object(
            real_bot, "_send_staff_log", new=mock.AsyncMock()
        ) as staff_log, mock.patch.object(bot_module, "_resolve_text_channel", new=mock.AsyncMock(return_value=blocked)):
            asyncio.run(announcements.run_due_scheduled(
                datetime.now(timezone.utc), real_bot._deliver_scheduled
            ))
        row = store.db_fetchone("SELECT status, error FROM scheduled_announcements WHERE id = 1")
        self.assertEqual(row["status"], "failed")
        self.assertIn("Send Messages", row["error"])
        staff_log.assert_awaited()
        self.assertEqual(blocked.sent, [])


class SetupTests(HandlerTestCase):
    def test_only_administrators_can_change_setup(self):
        interaction = make_interaction(self.staff, self.guild)
        self.run_cmd(bot_module.CombatMasterCog.setup_cmd, interaction, timezone_name="Europe/London")
        self.assertIn("Only administrators", interaction.followup.last_text())
        self.assertEqual(self.config["guilds"][str(GUILD_ID)]["timezone"], None)

    def test_admin_setup_saves_timezone_and_roles(self):
        interaction = make_interaction(self.admin, self.guild)
        role = SimpleNamespace(id=8000, mention="<@&8000>")
        with mock.patch.object(bot_module, "save_config") as save:
            self.run_cmd(
                bot_module.CombatMasterCog.setup_cmd,
                interaction,
                staff_role=role,
                timezone_name="Europe/London",
            )
        save.assert_called_once()
        entry = self.config["guilds"][str(GUILD_ID)]
        self.assertEqual(entry["staff_role_id"], 8000)
        self.assertEqual(entry["timezone"], "Europe/London")
        self.assertIn("Setup saved", interaction.followup.last_text())

    def test_bad_timezone_is_refused(self):
        interaction = make_interaction(self.admin, self.guild)
        self.run_cmd(bot_module.CombatMasterCog.setup_cmd, interaction, timezone_name="Nowhere/Land")
        self.assertIn("not a timezone", interaction.followup.last_text())

    def test_status_reports_pending_and_staff_role(self):
        interaction = make_interaction(self.staff, self.guild)
        self.run_cmd(bot_module.CombatMasterCog.manage_status, interaction)
        embed = interaction.followup.sent[-1]["embed"]
        fields = {f.name: f.value for f in embed.fields}
        self.assertEqual(fields["Scheduled"], "0")
        self.assertIn(str(STAFF_ROLE_ID), fields["Staff role"])


class HistoryCommandTests(HandlerTestCase):
    def test_history_empty_then_populated(self):
        interaction = make_interaction(self.staff, self.guild)
        self.run_cmd(bot_module.CombatMasterCog.manage_history, interaction, limit=5)
        self.assertIn("No announcements", interaction.followup.last_text())

        announcements.record_announcement(
            guild_id=GUILD_ID, channel_id=ANNOUNCE_CHANNEL_ID, message_id=1,
            author_id=10, title="Patch", body="x", mention_role_id=None, source="now",
        )
        interaction = make_interaction(self.staff, self.guild)
        self.run_cmd(bot_module.CombatMasterCog.manage_history, interaction, limit=5)
        embed = interaction.followup.sent[-1]["embed"]
        self.assertIn("Patch", embed.description)


if __name__ == "__main__":
    unittest.main()
