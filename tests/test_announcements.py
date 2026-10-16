"""
Tests for announcement parsing, posting, history and the scheduler
(announcements.py). Database tests run against a temporary data directory.

Run with either:
    python -m pytest tests/test_announcements.py
    python tests/test_announcements.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


import announcements as ann  # noqa: E402
import store  # noqa: E402

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


class DataDirTestCase(unittest.TestCase):
    """Gives each test its own empty data directory and database."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("COMBAT_MASTER_DATA")
        os.environ["COMBAT_MASTER_DATA"] = self._tmp.name
        store.init_db()

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("COMBAT_MASTER_DATA", None)
        else:
            os.environ["COMBAT_MASTER_DATA"] = self._old
        self._tmp.cleanup()


class ParseDurationTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(ann.parse_duration("30m"), 1800)
        self.assertEqual(ann.parse_duration("2h"), 7200)
        self.assertEqual(ann.parse_duration("1d12h"), 86400 + 43200)
        self.assertEqual(ann.parse_duration("45 seconds"), 45)
        self.assertEqual(ann.parse_duration("1 week"), 604800)

    def test_bare_number_means_minutes(self):
        self.assertEqual(ann.parse_duration("15"), 900)

    def test_rejects_non_durations(self):
        for text in ["", "   ", "soon", "2026-10-20 18:00", "1 2", "10x", "0", "5 m 3"]:
            self.assertIsNone(ann.parse_duration(text), text)


class ParseSendTimeTests(unittest.TestCase):
    def test_delay_is_relative_to_now(self):
        at = ann.parse_send_time("2h", NOW, "America/Toronto")
        self.assertEqual(at, NOW + timedelta(hours=2))

    def test_absolute_time_uses_server_timezone(self):
        # 18:00 on 20 Oct 2026 in Toronto (EDT, UTC-4) is 22:00 UTC.
        at = ann.parse_send_time("2026-10-20 18:00", NOW, "America/Toronto")
        self.assertEqual(at, datetime(2026, 10, 20, 22, 0, tzinfo=timezone.utc))

    def test_absolute_time_accepts_t_separator(self):
        at = ann.parse_send_time("2026-10-20T18:00", NOW, "America/Toronto")
        self.assertEqual(at.hour, 22)

    def test_unknown_timezone_falls_back_to_utc(self):
        at = ann.parse_send_time("2026-10-20 18:00", NOW, "Not/AZone")
        self.assertEqual(at.hour, 18)

    def test_refuses_the_past_and_too_soon(self):
        with self.assertRaisesRegex(ValueError, "at least one minute"):
            ann.parse_send_time("30s", NOW, "UTC")
        with self.assertRaisesRegex(ValueError, "at least one minute"):
            ann.parse_send_time("2026-10-16 11:00", NOW, "UTC")

    def test_refuses_more_than_a_year_ahead(self):
        with self.assertRaisesRegex(ValueError, "365 days"):
            ann.parse_send_time("400d", NOW, "UTC")

    def test_refuses_nonsense_and_impossible_dates(self):
        with self.assertRaisesRegex(ValueError, "could not read"):
            ann.parse_send_time("next tuesday", NOW, "UTC")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            ann.parse_send_time("2026-02-30 10:00", NOW, "UTC")


class ValidationTests(unittest.TestCase):
    def test_validate_timezone(self):
        self.assertEqual(ann.validate_timezone("Europe/London"), "Europe/London")
        with self.assertRaises(ValueError):
            ann.validate_timezone("Mars/Olympus")

    def test_clean_title_defaults(self):
        self.assertEqual(ann.clean_title(None), "Announcement")
        self.assertEqual(ann.clean_title("   "), "Announcement")
        self.assertEqual(ann.clean_title("  Patch 1.2  "), "Patch 1.2")
        with self.assertRaises(ValueError):
            ann.clean_title("x" * (ann.MAX_TITLE_LENGTH + 1))

    def test_clean_body(self):
        self.assertEqual(ann.clean_body("  hi  "), "hi")
        with self.assertRaises(ValueError):
            ann.clean_body("   ")
        with self.assertRaises(ValueError):
            ann.clean_body("x" * (ann.MAX_BODY_LENGTH + 1))


class EmbedTests(unittest.TestCase):
    def test_announcement_embed_carries_text_and_author(self):
        embed = ann.build_announcement_embed(title="Raid night", body="Be online at 8", author_name="Ana")
        self.assertEqual(embed.title, "Raid night")
        self.assertEqual(embed.description, "Be online at 8")
        self.assertEqual(embed.footer.text, "Posted by Ana")

    def test_staff_log_preview_is_truncated(self):
        embed = ann.build_staff_log_embed(
            heading="Announcement posted",
            channel_mention="#news",
            author_mention="@Ana",
            title="T",
            body="a" * 1000,
            mention=None,
            source="Now",
        )
        message_field = [f for f in embed.fields if f.name == "Message"][0]
        self.assertLessEqual(len(message_field.value), 300)
        self.assertNotIn("Pinged", [f.name for f in embed.fields])


class FakeChannel:
    def __init__(self):
        self.calls: list[dict] = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=555)


class PostTests(unittest.TestCase):
    def test_only_the_chosen_role_can_ping(self):
        channel = FakeChannel()
        role = SimpleNamespace(mention="<@&42>", id=42)
        asyncio.run(
            ann.post_announcement(
                channel,
                title="Hi",
                body="@everyone read this",
                author_name="Ana",
                mention_role=role,
            )
        )
        call = channel.calls[0]
        self.assertEqual(call["content"], "<@&42>")
        allowed = call["allowed_mentions"]
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.users)
        self.assertEqual([r.id for r in allowed.roles], [42])

    def test_no_role_means_no_ping_at_all(self):
        channel = FakeChannel()
        asyncio.run(ann.post_announcement(channel, title="Hi", body="x", author_name="Ana"))
        call = channel.calls[0]
        self.assertIsNone(call["content"])
        allowed = call["allowed_mentions"]
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.users)
        self.assertFalse(allowed.roles)


class HistoryTests(DataDirTestCase):
    def _record(self, guild_id: int, title: str, when: datetime, source: str = "now") -> int:
        return ann.record_announcement(
            guild_id=guild_id,
            channel_id=10,
            message_id=99,
            author_id=1,
            title=title,
            body="body",
            mention_role_id=None,
            source=source,
            when=when,
        )

    def test_history_is_newest_first_and_per_guild(self):
        self._record(1, "old", NOW - timedelta(days=1))
        self._record(1, "new", NOW)
        self._record(2, "other server", NOW)
        titles = [r["title"] for r in ann.list_history(1)]
        self.assertEqual(titles, ["new", "old"])

    def test_history_limit_is_clamped(self):
        for i in range(30):
            self._record(1, f"t{i}", NOW + timedelta(minutes=i))
        self.assertEqual(len(ann.list_history(1, 500)), ann.MAX_HISTORY_LIMIT)
        self.assertEqual(len(ann.list_history(1, 0)), 1)


class ScheduleTests(DataDirTestCase):
    def _add(self, guild_id: int = 1, send_at: datetime = NOW + timedelta(hours=1), **overrides) -> int:
        fields = dict(
            guild_id=guild_id,
            channel_id=10,
            author_id=7,
            title="Scheduled",
            body="body",
            mention_role_id=None,
            send_at=send_at,
            now=NOW,
        )
        fields.update(overrides)
        return ann.add_scheduled(**fields)

    def test_pending_list_and_cancel(self):
        first = self._add(send_at=NOW + timedelta(hours=2))
        second = self._add(send_at=NOW + timedelta(hours=1))
        self.assertEqual([r["id"] for r in ann.list_pending(1)], [second, first])

        self.assertTrue(ann.cancel_scheduled(1, first))
        self.assertEqual([r["id"] for r in ann.list_pending(1)], [second])
        # Cancelling twice, or from another server, does nothing.
        self.assertFalse(ann.cancel_scheduled(1, first))
        self.assertFalse(ann.cancel_scheduled(2, second))

    def test_run_due_sends_only_what_is_due(self):
        due = self._add(send_at=NOW - timedelta(minutes=1))
        later = self._add(send_at=NOW + timedelta(hours=1))
        delivered: list[int] = []

        async def deliver(row):
            delivered.append(row["id"])
            return 1000 + row["id"]

        count = asyncio.run(ann.run_due_scheduled(NOW, deliver))
        self.assertEqual(count, 1)
        self.assertEqual(delivered, [due])
        self.assertEqual([r["id"] for r in ann.list_pending(1)], [later])

        # Running again does not re-send the one that already went out.
        self.assertEqual(asyncio.run(ann.run_due_scheduled(NOW, deliver)), 0)
        self.assertEqual(delivered, [due])

    def test_failed_delivery_is_marked_and_does_not_stop_others(self):
        bad = self._add(send_at=NOW - timedelta(minutes=2))
        good = self._add(send_at=NOW - timedelta(minutes=1))
        seen: list[int] = []

        async def deliver(row):
            seen.append(row["id"])
            if row["id"] == bad:
                raise PermissionError("I can't send messages there.")
            return 1

        asyncio.run(ann.run_due_scheduled(NOW, deliver))
        self.assertEqual(seen, [bad, good])
        row = store.db_fetchone("SELECT * FROM scheduled_announcements WHERE id = ?", (bad,))
        self.assertEqual(row["status"], "failed")
        self.assertIn("can't send", row["error"])
        row = store.db_fetchone("SELECT * FROM scheduled_announcements WHERE id = ?", (good,))
        self.assertEqual(row["status"], "sent")

    def test_storage_format_sorts_as_text(self):
        self.assertEqual(
            ann.iso_utc(datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=timezone(timedelta(hours=-5)))),
            "2026-01-02T08:04:05+00:00",
        )


if __name__ == "__main__":
    unittest.main()
