"""Buttons and dropdowns that keep working after a restart: poll votes, ticket open/close, role menus.

Each button is a ``discord.ui.DynamicItem``. Discord only sends the custom id
of a pressed button, and the bot finds the matching class by a regular
expression. Registering the classes once in ``setup_hook`` (see bot.py) is what
makes the buttons on old messages keep working after a restart.

The handlers here only turn a press into an operation and a reply. The rules
(one vote per person, one open ticket per member, who may close a ticket) live
in operations.py.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import discord

import operations as ops
import polls
import role_menus
import settings
import tickets
from audit import Actor
from operations import OperationError

logger = logging.getLogger("combat_master.components")

STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


# --------------------------------------------------------------------------- #
# Replies
# --------------------------------------------------------------------------- #
async def _reply(interaction: discord.Interaction, text: str) -> None:
    """Ephemeral answer. Defers first so a slow operation can not time the press out."""
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(text, ephemeral=True)
    except discord.HTTPException as exc:
        logger.warning("Could not answer a button press: %s", exc)


def _actor(interaction: discord.Interaction) -> Actor:
    return Actor(interaction.user.id, interaction.user.display_name, "discord")


def _bot(interaction: discord.Interaction):
    return interaction.client


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
async def handle_vote(interaction: discord.Interaction, poll_id: int, index: int) -> None:
    if interaction.guild_id is None:
        await _reply(interaction, "Votes only count inside a server.")
        return
    bot = _bot(interaction)
    try:
        result = await ops.cast_vote(bot.gateway, bot.config, interaction.guild_id, poll_id, _actor(interaction), index)
    except OperationError as exc:
        await _reply(interaction, str(exc))
        return
    option = result["option"]
    if not result["multiple"]:
        text = f"You already voted for **{option}**." if result["unchanged"] else f"Your vote is now **{option}**."
    elif result["added"]:
        text = f"Added **{option}**. You picked: {', '.join(result['choices'])}."
    else:
        left = ", ".join(result["choices"]) or "nothing"
        text = f"Removed **{option}**. You picked: {left}."
    await _reply(interaction, text)


async def handle_ticket_open(interaction: discord.Interaction) -> None:
    if interaction.guild_id is None:
        await _reply(interaction, "Tickets can only be opened inside a server.")
        return
    bot = _bot(interaction)
    try:
        result = await ops.open_ticket(bot.gateway, bot.config, interaction.guild_id, _actor(interaction))
    except OperationError as exc:
        await _reply(interaction, str(exc))
        return
    await _reply(interaction, f"Your ticket is ready: <#{result['channel_id']}>")


async def handle_ticket_close(interaction: discord.Interaction) -> None:
    if interaction.guild_id is None or interaction.channel_id is None:
        await _reply(interaction, "This button only works inside a ticket.")
        return
    bot = _bot(interaction)
    ticket = ops.find_ticket_by_channel(interaction.guild_id, interaction.channel_id)
    if ticket is None:
        await _reply(interaction, "This channel is not an open ticket.")
        return
    is_staff = settings.staff_denial(
        interaction.user, bot.config, interaction.guild_id, guild=interaction.guild
    ) is None
    try:
        await ops.close_ticket(
            bot.gateway,
            bot.config,
            interaction.guild_id,
            _actor(interaction),
            ticket["id"],
            actor_is_staff=is_staff,
        )
    except OperationError as exc:
        await _reply(interaction, str(exc))
        return
    # The channel is gone by now, so the reply goes to the interaction, not the channel.
    await _reply(interaction, f"Ticket #{ticket['id']} is closed.")


async def handle_role_menu(interaction: discord.Interaction, menu_id: int, picked: list[str]) -> None:
    if interaction.guild_id is None:
        await _reply(interaction, "Role menus only work inside a server.")
        return
    bot = _bot(interaction)
    held = [role.id for role in getattr(interaction.user, "roles", [])]
    try:
        added, removed = await ops.apply_role_menu(
            bot.gateway,
            interaction.guild_id,
            interaction.user.id,
            held,
            menu_id,
            [int(value) for value in picked if str(value).isdigit()],
        )
    except OperationError as exc:
        await _reply(interaction, str(exc))
        return
    guild = interaction.guild

    def name(role_id: int) -> str:
        role = guild.get_role(role_id) if guild is not None else None
        return f"@{role.name}" if role is not None else f"role {role_id}"

    parts = []
    if added:
        parts.append("Added " + ", ".join(name(r) for r in added))
    if removed:
        parts.append("Removed " + ", ".join(name(r) for r in removed))
    await _reply(interaction, ". ".join(parts) + "." if parts else "No change: your roles already match your choice.")


# --------------------------------------------------------------------------- #
# Dynamic items
# --------------------------------------------------------------------------- #
def _styled(custom_id: str, label: str, emoji: Optional[str], style: str) -> discord.ui.Button:
    return discord.ui.Button(
        label=label or None,
        emoji=emoji,
        style=STYLES.get(style, discord.ButtonStyle.primary),
        custom_id=custom_id,
    )


class PollButton(discord.ui.DynamicItem[discord.ui.Button], template=r"cm:poll:(?P<pid>\d+):(?P<index>\d+)$"):
    def __init__(self, poll_id: int, index: int, label: str = "", emoji: Optional[str] = None, style: str = "primary") -> None:
        self.poll_id = int(poll_id)
        self.index = int(index)
        super().__init__(_styled(polls.button_id(poll_id, index), label, emoji, style))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match) -> "PollButton":
        return cls(int(match["pid"]), int(match["index"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_vote(interaction, self.poll_id, self.index)


class TicketOpenButton(discord.ui.DynamicItem[discord.ui.Button], template=r"cm:ticket:open$"):
    def __init__(self, label: str = "", emoji: Optional[str] = None, style: str = "primary") -> None:
        super().__init__(_styled(tickets.TICKET_OPEN_ID, label, emoji, style))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match) -> "TicketOpenButton":
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_ticket_open(interaction)


class TicketCloseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"cm:ticket:close$"):
    def __init__(self, label: str = "", emoji: Optional[str] = None, style: str = "danger") -> None:
        super().__init__(_styled(tickets.TICKET_CLOSE_ID, label, emoji, style))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match) -> "TicketCloseButton":
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_ticket_close(interaction)


class RoleMenuSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"cm:rolemenu:(?P<mid>\d+)$"):
    def __init__(
        self,
        menu_id: int,
        placeholder: str = role_menus.PLACEHOLDER,
        options: Optional[list[dict[str, Any]]] = None,
        max_values: int = 1,
    ) -> None:
        self.menu_id = int(menu_id)
        select = discord.ui.Select(
            custom_id=role_menus.custom_id(menu_id),
            placeholder=placeholder,
            min_values=0,
            max_values=max(1, max_values),
            options=[
                discord.SelectOption(label=o["label"], value=o["value"], description=o.get("description"))
                for o in options or []
            ],
        )
        super().__init__(select)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match) -> "RoleMenuSelect":
        return cls(int(match["mid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = [str(v) for v in (interaction.data or {}).get("values", [])]
        await handle_role_menu(interaction, self.menu_id, picked)


DYNAMIC_ITEMS = (PollButton, TicketOpenButton, TicketCloseButton, RoleMenuSelect)


def build_view(buttons: list[dict[str, Any]]) -> discord.ui.View:
    """A persistent view from button specs (see polls.button_specs and tickets.panel_buttons).

    Must be called from inside the event loop: discord.py views need one.
    """
    view = discord.ui.View(timeout=None)
    for spec in buttons:
        if spec.get("kind") == "select":
            menu_id = int(spec["custom_id"].rsplit(":", 1)[1])
            view.add_item(
                RoleMenuSelect(
                    menu_id,
                    placeholder=spec.get("placeholder") or role_menus.PLACEHOLDER,
                    options=spec.get("options") or [],
                    max_values=int(spec.get("max") or 1),
                )
            )
            continue
        custom_id = spec["custom_id"]
        label = spec.get("label") or ""
        emoji = spec.get("emoji")
        style = spec.get("style", "primary")
        if custom_id == tickets.TICKET_OPEN_ID:
            item: discord.ui.Item = TicketOpenButton(label, emoji, style)
        elif custom_id == tickets.TICKET_CLOSE_ID:
            item = TicketCloseButton(label, emoji, style)
        else:
            match = PollButton.__discord_ui_compiled_template__.match(custom_id)
            if match is None:
                raise ValueError(f"Unknown button id {custom_id!r}")
            item = PollButton(int(match["pid"]), int(match["index"]), label, emoji, style)
        view.add_item(item)
    return view
