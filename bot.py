"""
Combat Master - a discord.py bot for staff announcements.

Everything is reached through one slash-command group, ``/manage``:

    /manage announce     post an announcement now (staff)
    /manage schedule     post an announcement later (staff)
    /manage scheduled    list announcements waiting to be posted (staff)
    /manage cancel       cancel a scheduled announcement (staff)
    /manage history      the most recent announcements (staff)
    /manage status       this server's settings and what is pending (staff)
    /manage setup        staff role, channels and timezone (administrators)
    /manage fixcommands  remove duplicated slash commands and re-sync (admins)

Staff means: holders of the server's configured staff role, plus
administrators. Discord's *Manage Server* permission is only the fallback
until a staff role is set. The check runs again on every command, so a
mis-configured Discord integration can not expose the commands to others.

Announcements are posted as embeds. The bot never pings @everyone, @here or
individual users; the only ping possible is the one role a staff member picks.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import announcements
import command_tree
import paths
import store
from announcements import (
    MAX_BODY_LENGTH,
    MAX_TITLE_LENGTH,
    add_scheduled,
    build_staff_log_embed,
    cancel_scheduled,
    clean_body,
    clean_title,
    discord_time,
    list_history,
    list_pending,
    parse_send_time,
    post_announcement,
    record_announcement,
    run_due_scheduled,
    validate_timezone,
)
from command_tree import is_administrator
from settings import (
    get_announcement_channel_id,
    get_guild_config,
    get_staff_channel_id,
    get_staff_role_id,
    get_timezone_name,
    load_config,
    resolve_token,
    save_config,
    set_guild_value,
    staff_denial,
)

SCHEDULER_INTERVAL_SECONDS = 30

# --------------------------------------------------------------------------- #
# Logging: stdout (journal / console) and data/bot.log
# --------------------------------------------------------------------------- #
logger = logging.getLogger("combat_master")
logger.setLevel(logging.INFO)
_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_stream = logging.StreamHandler(sys.stdout)
_stream.setFormatter(_formatter)
logger.addHandler(_stream)
try:
    _file = logging.FileHandler(paths.log_path(), encoding="utf-8")
    _file.setFormatter(_formatter)
    logger.addHandler(_file)
except OSError as exc:  # a broken log file must not stop the bot
    logger.warning("File logging disabled (%s)", exc)

intents = discord.Intents.default()


class CombatMasterBot(commands.Bot):
    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(
            command_prefix="!",  # unused: slash commands only
            intents=intents,
            help_command=None,
        )
        self.config = config
        self._guild_commands_synced: set[int] = set()
        self._global_registry_cleared = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def setup_hook(self) -> None:
        await _register_cog(self)
        if not self.scheduler_tick.is_running():
            self.scheduler_tick.start()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        if not self._global_registry_cleared:
            await self._clear_global_registry()
        for guild in self.guilds:
            await self.sync_guild_commands(guild.id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.sync_guild_commands(guild.id)

    async def close(self) -> None:
        if self.scheduler_tick.is_running():
            self.scheduler_tick.cancel()
        await super().close()

    # ------------------------------------------------------------------ #
    # Slash command sync
    # ------------------------------------------------------------------ #
    async def _clear_global_registry(self) -> None:
        """Empty Discord's *global* command list.

        The commands are published per server only. A command that lives in
        both the global and a server registry is listed twice by the Discord
        client, so the global copy is removed.
        """
        try:
            await self.http.bulk_upsert_global_commands(self.application_id, payload=[])
        except discord.HTTPException as exc:
            logger.warning("Could not clear global commands (%s); will retry on next ready.", exc)
            return
        self._global_registry_cleared = True
        logger.info("Cleared duplicate global command registry.")

    async def sync_guild_commands(self, guild_id: int, *, force: bool = False) -> int:
        """Publish the command tree to one server. Returns how many were synced."""
        guild_id = int(guild_id)
        if not force and guild_id in self._guild_commands_synced:
            return 0
        guild_obj = discord.Object(id=guild_id)
        try:
            self.tree.copy_global_to(guild=guild_obj)
            synced = await self.tree.sync(guild=guild_obj)
        except discord.HTTPException as exc:
            logger.warning("Command sync to guild %s failed (%s).", guild_id, exc)
            return 0
        self._guild_commands_synced.add(guild_id)
        logger.info("Synced %d command(s) to guild %s.", len(synced), guild_id)
        return len(synced)

    # ------------------------------------------------------------------ #
    # Scheduler
    # ------------------------------------------------------------------ #
    @tasks.loop(seconds=SCHEDULER_INTERVAL_SECONDS)
    async def scheduler_tick(self) -> None:
        try:
            await run_due_scheduled(datetime.now(timezone.utc), self._deliver_scheduled)
        except Exception:  # the loop must survive any single bad tick
            logger.exception("Scheduler tick failed.")

    @scheduler_tick.before_loop
    async def _wait_until_ready(self) -> None:
        await self.wait_until_ready()

    async def _deliver_scheduled(self, row: dict[str, Any]) -> int:
        """Post one scheduled announcement. Raises if it can not be posted.

        Every failure is also written to the staff log, so a missed
        announcement is visible to staff without anyone reading bot.log.
        """
        guild = self.get_guild(row["guild_id"])
        if guild is None:
            raise LookupError("I am no longer in that server.")
        try:
            return await self._post_scheduled(guild, row)
        except Exception as exc:
            await self._send_staff_log(
                guild, _failure_embed(row, str(exc) or type(exc).__name__)
            )
            raise

    async def _post_scheduled(self, guild: discord.Guild, row: dict[str, Any]) -> int:
        channel = await _resolve_text_channel(guild, row["channel_id"])
        if channel is None:
            raise LookupError("The announcement channel no longer exists.")
        problem = _channel_problem(channel, guild)
        if problem:
            raise PermissionError(problem)

        role = guild.get_role(row["mention_role_id"]) if row["mention_role_id"] else None
        message = await post_announcement(
            channel,
            title=row["title"],
            body=row["body"],
            author_name=_member_name(guild, row["author_id"]),
            mention_role=role,
        )
        record_announcement(
            guild_id=guild.id,
            channel_id=channel.id,
            message_id=message.id,
            author_id=row["author_id"],
            title=row["title"],
            body=row["body"],
            mention_role_id=row["mention_role_id"],
            source="scheduled",
        )
        await self._send_staff_log(
            guild,
            build_staff_log_embed(
                heading="Scheduled announcement posted",
                channel_mention=channel.mention,
                author_mention=f"<@{row['author_id']}>",
                title=row["title"],
                body=row["body"],
                mention=role.mention if role else None,
                source=f"Scheduled #{row['id']}",
            ),
        )
        return message.id

    # ------------------------------------------------------------------ #
    # Staff log
    # ------------------------------------------------------------------ #
    async def _send_staff_log(self, guild: discord.Guild, embed: discord.Embed) -> None:
        """Post in the server's staff log channel. Never raises."""
        channel_id = get_staff_channel_id(self.config, guild.id)
        if not channel_id:
            return
        channel = await _resolve_text_channel(guild, channel_id)
        if channel is None:
            return
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            logger.warning("Could not write to the staff log in %s: %s", guild.id, exc)


# --------------------------------------------------------------------------- #
# Helpers shared by the commands and the scheduler
# --------------------------------------------------------------------------- #
async def _resolve_text_channel(
    guild: discord.Guild, channel_id: int
) -> Optional[discord.TextChannel]:
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await guild.fetch_channel(int(channel_id))
        except discord.HTTPException:
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


def _channel_problem(channel: discord.TextChannel, guild: discord.Guild) -> Optional[str]:
    """Why the bot can not post in ``channel``, or ``None`` if it can."""
    me = guild.me
    perms = channel.permissions_for(me)
    if not (perms.view_channel and perms.send_messages):
        return (
            f"I can't send messages in {channel.mention}. Grant me View Channel "
            "and Send Messages there."
        )
    if not perms.embed_links:
        return f"I need Embed Links in {channel.mention} to post announcements."
    return None


def _member_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(int(user_id))
    return member.display_name if member is not None else f"user {user_id}"


def _failure_embed(row: dict[str, Any], reason: str) -> discord.Embed:
    embed = discord.Embed(
        title="Scheduled announcement failed",
        description=reason[:500],
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Channel", value=f"<#{row['channel_id']}>", inline=True)
    embed.add_field(name="Title", value=row["title"][:1024], inline=False)
    embed.add_field(name="Source", value=f"Scheduled #{row['id']}", inline=True)
    return embed


def _channel_warnings(channel: discord.TextChannel, guild: discord.Guild, role: Optional[discord.Role]) -> list[str]:
    notes: list[str] = []
    if role is not None and not role.mentionable and not channel.permissions_for(guild.me).mention_everyone:
        notes.append(
            f"Note: {role.mention} is not mentionable, so it will show but not notify. "
            "Make the role mentionable or grant me Mention All Roles."
        )
    return notes


# --------------------------------------------------------------------------- #
# The shared /manage cog
# --------------------------------------------------------------------------- #
class CombatMasterCog(commands.Cog):
    """Every staff command, under the shared ``/manage`` group."""

    def __init__(self, bot_: CombatMasterBot) -> None:
        self.bot = bot_

    # The group lives in command_tree.py. Naming it here is what makes this
    # cog the one that registers /manage with Discord.
    manage_group = command_tree.manage_group

    # NOTE: Discord rejects the whole command list (HTTP 400, code 50035) if
    # any command or option description is over 100 characters.
    # tests/test_commands.py checks every description against that limit.

    # ---- /manage announce --------------------------------------------- #
    @manage_group.command(name="announce", description="Post an announcement now.")
    @app_commands.describe(
        message="What to announce. Shown inside an embed.",
        title="Optional heading above the message.",
        channel="Where to post. Defaults to the server's announcement channel.",
        mention="Optional role to ping with the announcement.",
    )
    async def manage_announce(
        self,
        interaction: discord.Interaction,
        message: app_commands.Range[str, 1, MAX_BODY_LENGTH],
        title: Optional[app_commands.Range[str, 1, MAX_TITLE_LENGTH]] = None,
        channel: Optional[discord.TextChannel] = None,
        mention: Optional[discord.Role] = None,
    ) -> None:
        if not await self._begin(interaction):
            return
        if not await self._require_staff(interaction):
            return
        guild = interaction.guild
        assert guild is not None

        target = channel or await self._configured_channel(guild)
        if target is None:
            await self._reply(
                interaction,
                "No channel given and no announcement channel is set. Pass `channel:` "
                "or run `/manage setup announcement_channel:#channel`.",
            )
            return
        problem = _channel_problem(target, guild)
        if problem:
            await self._reply(interaction, problem)
            return
        try:
            cleaned_title = clean_title(title)
            cleaned_body = clean_body(message)
        except ValueError as exc:
            await self._reply(interaction, str(exc))
            return

        try:
            sent = await post_announcement(
                target,
                title=cleaned_title,
                body=cleaned_body,
                author_name=interaction.user.display_name,
                mention_role=mention,
            )
        except discord.HTTPException as exc:
            logger.warning("Announcement in %s failed: %s", target.id, exc)
            await self._reply(interaction, f"Discord refused the announcement: {exc}")
            return

        record_announcement(
            guild_id=guild.id,
            channel_id=target.id,
            message_id=sent.id,
            author_id=interaction.user.id,
            title=cleaned_title,
            body=cleaned_body,
            mention_role_id=mention.id if mention else None,
            source="now",
        )
        await self.bot._send_staff_log(
            guild,
            build_staff_log_embed(
                heading="Announcement posted",
                channel_mention=target.mention,
                author_mention=interaction.user.mention,
                title=cleaned_title,
                body=cleaned_body,
                mention=mention.mention if mention else None,
                source="Now",
            ),
        )
        lines = [f"Posted in {target.mention}: {sent.jump_url}"]
        lines += _channel_warnings(target, guild, mention)
        await self._reply(interaction, "\n".join(lines))

    # ---- /manage schedule --------------------------------------------- #
    @manage_group.command(name="schedule", description="Post an announcement at a later time.")
    @app_commands.describe(
        when="A delay like 2h or 1d12h, or a time like 2026-10-20 18:00 (server time).",
        message="What to announce. Shown inside an embed.",
        title="Optional heading above the message.",
        channel="Where to post. Defaults to the server's announcement channel.",
        mention="Optional role to ping when it is posted.",
    )
    async def manage_schedule(
        self,
        interaction: discord.Interaction,
        when: app_commands.Range[str, 1, 40],
        message: app_commands.Range[str, 1, MAX_BODY_LENGTH],
        title: Optional[app_commands.Range[str, 1, MAX_TITLE_LENGTH]] = None,
        channel: Optional[discord.TextChannel] = None,
        mention: Optional[discord.Role] = None,
    ) -> None:
        if not await self._begin(interaction):
            return
        if not await self._require_staff(interaction):
            return
        guild = interaction.guild
        assert guild is not None

        target = channel or await self._configured_channel(guild)
        if target is None:
            await self._reply(
                interaction,
                "No channel given and no announcement channel is set. Pass `channel:` "
                "or run `/manage setup announcement_channel:#channel`.",
            )
            return
        problem = _channel_problem(target, guild)
        if problem:
            await self._reply(interaction, problem)
            return
        try:
            cleaned_title = clean_title(title)
            cleaned_body = clean_body(message)
            send_at = parse_send_time(
                when,
                datetime.now(timezone.utc),
                get_timezone_name(self.bot.config, guild.id),
            )
        except ValueError as exc:
            await self._reply(interaction, str(exc))
            return

        schedule_id = add_scheduled(
            guild_id=guild.id,
            channel_id=target.id,
            author_id=interaction.user.id,
            title=cleaned_title,
            body=cleaned_body,
            mention_role_id=mention.id if mention else None,
            send_at=send_at,
        )
        lines = [
            f"Scheduled **#{schedule_id}** for {discord_time(send_at)} in {target.mention}.",
            "Use `/manage cancel` with that number to stop it before then.",
        ]
        lines += _channel_warnings(target, guild, mention)
        await self._reply(interaction, "\n".join(lines))

    # ---- /manage scheduled -------------------------------------------- #
    @manage_group.command(name="scheduled", description="List announcements waiting to be posted.")
    async def manage_scheduled(self, interaction: discord.Interaction) -> None:
        if not await self._begin(interaction):
            return
        if not await self._require_staff(interaction):
            return
        guild = interaction.guild
        assert guild is not None

        pending = list_pending(guild.id)
        if not pending:
            await self._reply(interaction, "Nothing is scheduled.")
            return
        lines = []
        for row in pending[:25]:
            when = datetime.fromisoformat(row["send_at"])
            lines.append(
                f"**#{row['id']}** · {discord_time(when)} · <#{row['channel_id']}> · {row['title']}"
            )
        if len(pending) > 25:
            lines.append(f"...and {len(pending) - 25} more.")
        embed = discord.Embed(
            title="Scheduled announcements",
            description="\n".join(lines),
            color=announcements.EMBED_COLOR,
        )
        await self._reply(interaction, embed=embed)

    # ---- /manage cancel ----------------------------------------------- #
    @manage_group.command(name="cancel", description="Cancel a scheduled announcement.")
    @app_commands.describe(schedule_id="The number shown by /manage scheduled.")
    async def manage_cancel(
        self,
        interaction: discord.Interaction,
        schedule_id: app_commands.Range[int, 1, 2_000_000_000],
    ) -> None:
        if not await self._begin(interaction):
            return
        if not await self._require_staff(interaction):
            return
        guild = interaction.guild
        assert guild is not None

        if cancel_scheduled(guild.id, schedule_id):
            await self._reply(interaction, f"Cancelled scheduled announcement **#{schedule_id}**.")
        else:
            await self._reply(
                interaction,
                f"No pending scheduled announcement **#{schedule_id}** in this server. "
                "Check `/manage scheduled`.",
            )

    # ---- /manage history ---------------------------------------------- #
    @manage_group.command(name="history", description="Show the most recent announcements.")
    @app_commands.describe(limit="How many to show (1-25, default 10).")
    async def manage_history(
        self,
        interaction: discord.Interaction,
        limit: Optional[app_commands.Range[int, 1, 25]] = None,
    ) -> None:
        if not await self._begin(interaction):
            return
        if not await self._require_staff(interaction):
            return
        guild = interaction.guild
        assert guild is not None

        rows = list_history(guild.id, limit or announcements.DEFAULT_HISTORY_LIMIT)
        if not rows:
            await self._reply(interaction, "No announcements have been posted yet.")
            return
        lines = []
        for row in rows:
            when = datetime.fromisoformat(row["created_at"])
            how = "scheduled" if row["source"] == "scheduled" else "posted"
            jump = ""
            if row["message_id"]:
                jump = f" · [jump](https://discord.com/channels/{guild.id}/{row['channel_id']}/{row['message_id']})"
            lines.append(
                f"{discord_time(when)} · {how} in <#{row['channel_id']}> by <@{row['author_id']}> · "
                f"**{row['title']}**{jump}"
            )
        embed = discord.Embed(
            title="Recent announcements",
            description="\n".join(lines),
            color=announcements.EMBED_COLOR,
        )
        await self._reply(interaction, embed=embed)

    # ---- /manage status ----------------------------------------------- #
    @manage_group.command(name="status", description="Show this server's settings and what is pending.")
    async def manage_status(self, interaction: discord.Interaction) -> None:
        if not await self._begin(interaction):
            return
        if not await self._require_staff(interaction):
            return
        guild = interaction.guild
        assert guild is not None

        cfg = get_guild_config(self.bot.config, guild.id)
        staff_role_id = get_staff_role_id(self.bot.config, guild.id)
        pending = list_pending(guild.id)
        last = list_history(guild.id, 1)

        embed = discord.Embed(title="Combat Master status", color=announcements.EMBED_COLOR)
        embed.add_field(
            name="Staff role",
            value=f"<@&{staff_role_id}>" if staff_role_id else "Not set (Manage Server is used)",
            inline=False,
        )
        embed.add_field(
            name="Announcement channel",
            value=_or_unset(cfg["announcement_channel_id"]),
            inline=True,
        )
        embed.add_field(name="Staff log", value=_or_unset(cfg["staff_channel_id"]), inline=True)
        embed.add_field(name="Timezone", value=cfg["timezone"], inline=True)
        embed.add_field(name="Scheduled", value=str(len(pending)), inline=True)
        embed.add_field(
            name="Last announcement",
            value=(
                discord_time(datetime.fromisoformat(last[0]["created_at"]))
                if last
                else "None yet"
            ),
            inline=True,
        )
        await self._reply(interaction, embed=embed)

    # ---- /manage setup (administrators) ------------------------------- #
    @manage_group.command(name="setup", description="Set the staff role, channels and timezone.")
    @app_commands.describe(
        staff_role="Members with this role can run /manage commands.",
        announcement_channel="Default channel for /manage announce and schedule.",
        staff_channel="Channel that logs everything that is posted.",
        timezone_name="IANA timezone for typed times, e.g. America/Toronto.",
    )
    @app_commands.rename(timezone_name="timezone")
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        staff_role: Optional[discord.Role] = None,
        announcement_channel: Optional[discord.TextChannel] = None,
        staff_channel: Optional[discord.TextChannel] = None,
        timezone_name: Optional[app_commands.Range[str, 1, 64]] = None,
    ) -> None:
        if not await self._begin(interaction):
            return
        if not is_administrator(interaction):
            await self._reply(interaction, "Only administrators can change the setup.")
            return
        guild = interaction.guild
        assert guild is not None

        try:
            tz_value = validate_timezone(timezone_name) if timezone_name else None
        except ValueError as exc:
            await self._reply(interaction, str(exc))
            return

        changes = {
            "staff_role_id": staff_role.id if staff_role else None,
            "announcement_channel_id": announcement_channel.id if announcement_channel else None,
            "staff_channel_id": staff_channel.id if staff_channel else None,
            "timezone": tz_value,
        }
        for key, value in changes.items():
            if value is not None:
                set_guild_value(self.bot.config, guild.id, key, value)
        try:
            save_config(self.bot.config)
        except OSError as exc:
            await self._reply(interaction, f"Could not save the configuration: {exc}")
            return

        lines = ["Setup saved."]
        if staff_role:
            lines.append(f"Staff role: {staff_role.mention}")
        if announcement_channel:
            lines.append(f"Announcement channel: {announcement_channel.mention}")
        if staff_channel:
            lines.append(f"Staff log: {staff_channel.mention}")
        if tz_value:
            lines.append(f"Timezone: {tz_value}")
        if len(lines) == 1:
            lines.append("Nothing changed. Pass at least one option to update it.")
        await self._reply(interaction, "\n".join(lines))

    # ---- /manage fixcommands (administrators) ------------------------- #
    @manage_group.command(name="fixcommands", description="Re-sync this server's /manage commands.")
    async def fixcommands_cmd(self, interaction: discord.Interaction) -> None:
        if not await self._begin(interaction):
            return
        if not is_administrator(interaction):
            await self._reply(interaction, "Only administrators can re-sync commands.")
            return
        guild = interaction.guild
        assert guild is not None

        count = await self.bot.sync_guild_commands(guild.id, force=True)
        await self._reply(
            interaction,
            f"Re-synced {count} command(s). If Discord still shows duplicates, "
            "restart the Discord client (Ctrl+R).",
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _begin(self, interaction: discord.Interaction) -> bool:
        """Defer the reply (ephemeral) and refuse anything outside a server."""
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            return False
        if interaction.guild is None or interaction.guild_id is None:
            await self._reply(interaction, "Combat Master commands only work inside a server.")
            return False
        return True

    async def _require_staff(self, interaction: discord.Interaction) -> bool:
        denial = staff_denial(
            interaction.user,
            self.bot.config,
            interaction.guild_id,
            guild=interaction.guild,
        )
        if denial is None:
            return True
        await self._reply(interaction, denial)
        return False

    async def _configured_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        channel_id = get_announcement_channel_id(self.bot.config, guild.id)
        if not channel_id:
            return None
        return await _resolve_text_channel(guild, channel_id)

    async def _reply(
        self,
        interaction: discord.Interaction,
        content: Optional[str] = None,
        *,
        embed: Optional[discord.Embed] = None,
    ) -> None:
        """Ephemeral answer to the command. Never raises."""
        try:
            await interaction.followup.send(content, embed=embed, ephemeral=True)
        except discord.HTTPException as exc:
            logger.warning("Could not reply to /manage command: %s", exc)


def _or_unset(value: Any) -> str:
    return f"<#{value}>" if value else "Not set"


async def _register_cog(bot_: CombatMasterBot) -> None:
    if bot_.get_cog("CombatMasterCog") is None:
        await bot_.add_cog(CombatMasterCog(bot_))


# --------------------------------------------------------------------------- #
# Module-level bot (created at import so the command tree can be inspected)
# --------------------------------------------------------------------------- #
bot = CombatMasterBot(load_config())


# --------------------------------------------------------------------------- #
# Slash command error handling
# --------------------------------------------------------------------------- #
@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.CheckFailure):
        msg = "You don't have permission to run that command."
    elif isinstance(error, app_commands.CommandNotFound):
        return
    elif isinstance(error, app_commands.MissingPermissions):
        msg = f"You're missing permissions: {', '.join(error.missing_permissions)}."
    elif isinstance(error, app_commands.BotMissingPermissions):
        msg = f"I'm missing permissions: {', '.join(error.missing_permissions)}."
    elif isinstance(error, app_commands.TransformerError):
        msg = f"Invalid value for `{error.param.name}`."
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = "This command can only be used in a server."
    else:
        logger.exception("Unhandled slash command error: %s", error)
        msg = "Something went wrong while running that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException as exc:
        logger.warning("Failed to deliver error message: %s", exc)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--paths" in args:
        print(paths.describe())
        return 0
    if "--version" in args:
        print(paths.app_version())
        return 0
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0

    logger.info("Combat Master %s", paths.app_version())
    logger.info("Data directory: %s", paths.data_dir())
    store.init_db()
    token = resolve_token(bot.config)
    if not token:
        logger.error(
            "No Discord token. Set DISCORD_TOKEN, or put it in %s under 'bot_token'.",
            paths.config_path(),
        )
        return 1
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        logger.error("Login failed - is the token valid?")
        return 1
    except OSError as exc:  # aiohttp connection errors are OSError subclasses
        logger.error("Could not reach Discord (%s). Check the network connection.", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
