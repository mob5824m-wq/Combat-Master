"""Tickets: opening, the one-per-member rule, who may close, and what gets logged.

Run with either:
    python -m pytest tests/test_tickets.py
    python tests/test_tickets.py
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import operations as ops  # noqa: E402
import settings  # noqa: E402
import store  # noqa: E402
import tickets  # noqa: E402
from audit import Actor  # noqa: E402
from demo import DemoGateway  # noqa: E402
from operations import OperationError  # noqa: E402

GUILD = 100000000000000001
ANNOUNCE = 100000000000000101
STAFF_LOG = 100000000000000105
STAFF_ROLE = 100000000000000201
SUPPORT_CATEGORY = 100000000000000901
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
STAFF = Actor(42, "Ada", "discord")
MEMBER = Actor(1001, "Alice", "discord")
OTHER = Actor(1002, "Bob", "discord")


def run(coro):
    return asyncio.run(coro)


class TicketTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("COMBAT_MASTER_DATA")
        os.environ["COMBAT_MASTER_DATA"] = self._tmp.name
        store.init_db()
        self.gw = DemoGateway()
        self.cfg = copy.deepcopy(settings.DEFAULT_CONFIG)
        settings.set_guild_value(self.cfg, GUILD, "announcement_channel_id", ANNOUNCE)
        settings.set_guild_value(self.cfg, GUILD, "staff_channel_id", STAFF_LOG)
        settings.set_guild_value(self.cfg, GUILD, "staff_role_id", STAFF_ROLE)
        settings.set_guild_value(self.cfg, GUILD, "timezone", "America/Toronto")

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("COMBAT_MASTER_DATA", None)
        else:
            os.environ["COMBAT_MASTER_DATA"] = self._old
        self._tmp.cleanup()

    def open(self, actor=MEMBER):
        return run(ops.open_ticket(self.gw, self.cfg, GUILD, actor, now=NOW))

    def close(self, ticket_id, actor, *, staff=False):
        return run(ops.close_ticket(self.gw, self.cfg, GUILD, actor, ticket_id, actor_is_staff=staff, now=NOW))

    def staff_log_posts(self):
        return [p for p in self.gw.posts if p["channel_id"] == STAFF_LOG]


class OpenTicketTests(TicketTestCase):
    def test_opening_creates_a_private_channel_and_a_greeting(self):
        result = self.open()
        channel = self.gw.created_channels[-1]
        self.assertEqual(int(result["channel_id"]), channel["id"])
        self.assertEqual(channel["name"], "ticket-alice")
        self.assertEqual(channel["opener_id"], MEMBER.id)
        self.assertEqual(channel["staff_role_id"], STAFF_ROLE)
        self.assertIsNone(channel["category_id"])
        greeting = self.gw.messages[(channel["id"], next(
            mid for (cid, mid) in self.gw.messages if cid == channel["id"]
        ))]
        self.assertEqual([b["custom_id"] for b in greeting["view"]], [tickets.TICKET_CLOSE_ID])
        self.assertIn(f"<@{MEMBER.id}>", greeting["content"])
        self.assertIn(f"<@&{STAFF_ROLE}>", greeting["content"])
        row = tickets.get(GUILD, result["id"])
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["opener_id"], MEMBER.id)

    def test_the_channel_goes_in_the_ticket_category_when_one_is_set(self):
        settings.set_guild_value(self.cfg, GUILD, "ticket_category_id", SUPPORT_CATEGORY)
        self.open()
        self.assertEqual(self.gw.created_channels[-1]["category_id"], SUPPORT_CATEGORY)

    def test_a_category_that_has_gone_is_refused_clearly(self):
        settings.set_guild_value(self.cfg, GUILD, "ticket_category_id", 999)
        with self.assertRaises(OperationError) as caught:
            self.open()
        self.assertIn("ticket category", str(caught.exception))
        self.assertEqual(ops.list_tickets(GUILD), [])

    def test_one_open_ticket_per_member(self):
        first = self.open()
        with self.assertRaises(OperationError) as caught:
            self.open()
        self.assertIn(f"#{first['id']}", str(caught.exception))
        self.assertEqual(len(self.gw.created_channels), 1)
        # Someone else can still open one.
        self.open(OTHER)
        self.assertEqual(len(ops.list_tickets(GUILD, open_only=True)), 2)

    def test_missing_manage_channels_leaves_no_ticket_behind(self):
        self.gw.fail_ticket_creation = True
        with self.assertRaises(OperationError):
            self.open()
        self.assertEqual(ops.list_tickets(GUILD), [])

    def test_opening_is_logged_to_the_staff_channel(self):
        self.open()
        self.assertEqual(len(self.staff_log_posts()), 1)
        self.assertIn("Ticket #1 opened", self.staff_log_posts()[0]["embed"].title)

    def test_a_failed_greeting_does_not_undo_the_ticket(self):
        original = self.gw.post

        async def failing_post(guild_id, channel_id, **kwargs):
            if kwargs.get("view"):  # the greeting carries the close button
                raise OperationError("Discord refused the message")
            return await original(guild_id, channel_id, **kwargs)

        self.gw.post = failing_post
        result = self.open()
        self.assertEqual(tickets.get(GUILD, result["id"])["status"], "open")


class CloseTicketTests(TicketTestCase):
    def test_the_opener_can_close_their_own_ticket(self):
        ticket = self.open()
        channel_id = int(ticket["channel_id"])
        result = self.close(ticket["id"], MEMBER)
        self.assertEqual(result["id"], ticket["id"])
        self.assertIn(channel_id, self.gw.deleted_channels)
        row = tickets.get(GUILD, ticket["id"])
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["closed_by"], "Alice")

    def test_another_member_can_not_close_it(self):
        ticket = self.open()
        with self.assertRaises(OperationError):
            self.close(ticket["id"], OTHER)
        self.assertEqual(tickets.get(GUILD, ticket["id"])["status"], "open")
        self.assertEqual(self.gw.deleted_channels, [])

    def test_staff_can_close_any_ticket(self):
        ticket = self.open()
        self.close(ticket["id"], STAFF, staff=True)
        self.assertEqual(tickets.get(GUILD, ticket["id"])["closed_by"], "Ada")

    def test_a_closed_ticket_can_not_be_closed_again(self):
        ticket = self.open()
        self.close(ticket["id"], MEMBER)
        with self.assertRaises(OperationError):
            self.close(ticket["id"], MEMBER)

    def test_after_closing_the_member_can_open_another(self):
        ticket = self.open()
        self.close(ticket["id"], MEMBER)
        again = self.open()
        self.assertNotEqual(again["id"], ticket["id"])

    def test_a_channel_that_is_already_gone_counts_as_closed(self):
        ticket = self.open()
        del self.gw._channel_index[int(ticket["channel_id"])]  # someone deleted it by hand
        self.close(ticket["id"], STAFF, staff=True)
        self.assertEqual(tickets.get(GUILD, ticket["id"])["status"], "closed")

    def test_closing_is_logged_and_audited(self):
        ticket = self.open()
        self.close(ticket["id"], STAFF, staff=True)
        self.assertIn("Ticket #1 closed", self.staff_log_posts()[-1]["embed"].title)

    def test_a_missing_ticket_is_refused(self):
        with self.assertRaises(OperationError):
            self.close(999, STAFF, staff=True)


class LookupTests(TicketTestCase):
    def test_find_ticket_by_channel_only_returns_open_tickets(self):
        ticket = self.open()
        channel_id = int(ticket["channel_id"])
        self.assertEqual(ops.find_ticket_by_channel(GUILD, channel_id)["id"], ticket["id"])
        self.close(ticket["id"], STAFF, staff=True)
        self.assertIsNone(ops.find_ticket_by_channel(GUILD, channel_id))
        self.assertIsNone(ops.find_ticket_by_channel(GUILD, ANNOUNCE))

    def test_list_can_show_only_open_tickets(self):
        first = self.open()
        self.open(OTHER)
        self.close(first["id"], STAFF, staff=True)
        self.assertEqual(len(ops.list_tickets(GUILD)), 2)
        self.assertEqual(len(ops.list_tickets(GUILD, open_only=True)), 1)

    def test_open_ticket_count_is_on_the_overview(self):
        self.open()
        self.assertEqual(ops.overview(self.cfg, GUILD, now=NOW)["tickets_open"], 1)


class PanelTests(TicketTestCase):
    def test_the_panel_has_an_open_button_and_is_audited(self):
        result = run(ops.post_ticket_panel(self.gw, self.cfg, GUILD, STAFF, channel_id=None))
        self.assertEqual(result["channel_id"], ANNOUNCE)
        view = self.gw.messages[(ANNOUNCE, int(result["message_id"]))]["view"]
        self.assertEqual([b["custom_id"] for b in view], [tickets.TICKET_OPEN_ID])

    def test_the_panel_needs_a_channel_it_can_post_in(self):
        with self.assertRaises(OperationError):
            run(ops.post_ticket_panel(self.gw, self.cfg, GUILD, STAFF, channel_id=999))


class ChannelNameTests(unittest.TestCase):
    def test_names_are_lower_case_ascii_slugs(self):
        self.assertEqual(tickets.channel_name("Ada Lovelace!"), "ticket-ada-lovelace")
        self.assertEqual(tickets.channel_name("  "), "ticket-member")
        self.assertEqual(tickets.channel_name("名前"), "ticket-member")

    def test_long_names_are_cut(self):
        name = tickets.channel_name("x" * 200)
        self.assertLessEqual(len(name), 100)
        self.assertTrue(name.startswith("ticket-"))


if __name__ == "__main__":
    unittest.main()
