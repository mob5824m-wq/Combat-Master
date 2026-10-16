"""The live Discord implementation of :class:`operations.Gateway`.

This is the only module that turns Discord objects into the plain values the
operations work with. Discord errors become :class:`operations.OperationError`
so every caller handles one exception type.
"""

from __future__ import annotations

from typing import Any, Optional

import discord

import announcements as ann
from operations import OperationError

# Discord returns some emoji with a variation selector (U+FE0F) and others
# without. Compare them with it removed.
def _norm(emoji: str) -> str:
    return str(emoji).replace("\ufe0f", "")


class DiscordGateway:
    def __init__(self, client: discord.Client) -> None:
        self.client = client

    # ---- lookups ------------------------------------------------------ #
    def _guild(self, guild_id: int) -> Optional[discord.Guild]:
        return self.client.get_guild(int(guild_id))

    def guilds(self) -> list[dict[str, Any]]:
        return [
            {"id": g.id, "name": g.name, "member_count": g.member_count or 0}
            for g in sorted(self.client.guilds, key=lambda g: g.name.lower())
        ]

    def snapshot(self, guild_id: int) -> Optional[dict[str, Any]]:
        guild = self._guild(guild_id)
        if guild is None:
            return None
        channels = [
            {"id": c.id, "name": c.name, "news": bool(c.is_news())}
            for c in sorted(guild.text_channels, key=lambda c: c.position)
        ]
        roles = [
            {"id": r.id, "name": r.name, "mentionable": bool(r.mentionable)}
            for r in sorted(guild.roles, key=lambda r: -r.position)
            if not r.is_default()
        ]
        return {
            "id": guild.id,
            "name": guild.name,
            "member_count": guild.member_count or 0,
            "channels": channels,
            "roles": roles,
        }

    def channel_problem(self, guild_id: int, channel_id: int) -> Optional[str]:
        guild = self._guild(guild_id)
        if guild is None:
            return "I'm not in that server any more."
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return "That channel does not exist in this server, or it is not a text channel."
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            return (
                f"I can't send messages in {channel.mention}. Grant me View Channel "
                "and Send Messages there."
            )
        if not perms.embed_links:
            return f"I need Embed Links in {channel.mention} to post announcements."
        return None

    def ping_warning(self, guild_id: int, role_id: int) -> Optional[str]:
        guild = self._guild(guild_id)
        if guild is None:
            return None
        role = guild.get_role(int(role_id))
        if role is None or role.mentionable:
            return None
        if guild.me.guild_permissions.mention_everyone:
            return None
        return (
            f"Note: {role.mention} is not mentionable, so it will show but not notify. "
            "Make the role mentionable or grant me Mention All Roles."
        )

    async def _channel(self, guild_id: int, channel_id: int) -> discord.TextChannel:
        guild = self._guild(guild_id)
        channel = guild.get_channel(int(channel_id)) if guild else None
        if not isinstance(channel, discord.TextChannel):
            raise OperationError("That channel is no longer available.")
        return channel

    async def _message(self, guild_id: int, channel_id: int, message_id: int) -> discord.Message:
        channel = await self._channel(guild_id, channel_id)
        try:
            return await channel.fetch_message(int(message_id))
        except discord.HTTPException as exc:
            raise OperationError(f"Could not find that message on Discord ({exc}).") from None

    # ---- posting ------------------------------------------------------ #
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
        channel = await self._channel(guild_id, channel_id)
        try:
            message = await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=ann.allowed_mentions_for(role_id, user_ids),
            )
        except discord.HTTPException as exc:
            raise OperationError(f"Discord refused the message: {exc}") from None

        published: Optional[bool] = None
        if publish:
            published = False
            if channel.is_news():
                try:
                    await message.publish()
                    published = True
                except discord.HTTPException:
                    published = False
        return {"message_id": message.id, "jump_url": message.jump_url, "published": published}

    async def edit_embed(self, guild_id: int, channel_id: int, message_id: int, embed: discord.Embed) -> None:
        message = await self._message(guild_id, channel_id, message_id)
        try:
            await message.edit(embed=embed)
        except discord.HTTPException as exc:
            raise OperationError(f"Could not update the message ({exc}).") from None

    async def delete_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        channel = await self._channel(guild_id, channel_id)
        try:
            # A partial message deletes without fetching first, so a message that
            # is already gone (NotFound) simply counts as deleted.
            await channel.get_partial_message(int(message_id)).delete()
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            raise OperationError(f"Could not delete the message ({exc}).") from None

    async def pin_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        message = await self._message(guild_id, channel_id, message_id)
        try:
            await message.pin()
        except discord.HTTPException as exc:
            raise OperationError(
                f"Discord would not pin it ({exc}). Check that I have Manage Messages in that channel."
            ) from None

    async def unpin_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        message = await self._message(guild_id, channel_id, message_id)
        try:
            await message.unpin()
        except discord.HTTPException as exc:
            raise OperationError(f"Discord would not unpin it ({exc}).") from None

    # ---- reactions ---------------------------------------------------- #
    async def reaction_counts(
        self, guild_id: int, channel_id: int, message_id: int, emojis: list[str]
    ) -> list[int]:
        message = await self._message(guild_id, channel_id, message_id)
        seen = {_norm(r.emoji): r.count for r in message.reactions}
        return [int(seen.get(_norm(e), 0)) for e in emojis]

    async def reactors(
        self, guild_id: int, channel_id: int, message_id: int, emoji: str, limit: int
    ) -> list[int]:
        message = await self._message(guild_id, channel_id, message_id)
        for reaction in message.reactions:
            if _norm(reaction.emoji) == _norm(emoji):
                users = []
                async for user in reaction.users(limit=limit):
                    if not user.bot:
                        users.append(user.id)
                return users
        return []

    async def add_reactions(self, guild_id: int, channel_id: int, message_id: int, emojis: list[str]) -> None:
        message = await self._message(guild_id, channel_id, message_id)
        for emoji in emojis:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException as exc:
                raise OperationError(f"Could not add the reaction {emoji} ({exc}).") from None
