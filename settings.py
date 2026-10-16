"""Per-server configuration and the staff permission rule.

Config shape (``config.json``)::

    {
      "bot_token": "...",
      "default_timezone": "America/Toronto",
      "guilds": {
        "<guild id>": {
          "staff_role_id": 123,            # may run every /manage command
          "announcement_channel_id": 456,  # default target of /manage announce
          "staff_channel_id": 789,         # log of everything that was posted
          "timezone": "America/Toronto"    # how typed times are read
        }
      }
    }

Each server is configured on its own with ``/manage setup``. Nothing here
touches Discord or the database, which is what makes it easy to test.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import discord

import paths

logger = logging.getLogger("combat_master.settings")

DEFAULT_TIMEZONE = "America/Toronto"

DEFAULT_CONFIG: dict[str, Any] = {
    "bot_token": "",
    "default_timezone": DEFAULT_TIMEZONE,
    "guilds": {},
}

DEFAULT_GUILD: dict[str, Any] = {
    "staff_role_id": None,
    "announcement_channel_id": None,
    "staff_channel_id": None,
    "timezone": None,  # None -> the config-wide default_timezone
}


# --------------------------------------------------------------------------- #
# Loading and saving
# --------------------------------------------------------------------------- #
def load_config() -> dict[str, Any]:
    """Read the live config, falling back to the repository template.

    A missing or corrupt file is logged and treated as "not configured yet",
    so the bot starts and says what is missing instead of crashing on import.
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    source = paths.config_path()
    if not source.is_file():
        source = paths.TEMPLATE_CONFIG_PATH
    if source.is_file():
        try:
            loaded = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except (OSError, ValueError) as exc:
            logger.error("Ignoring unreadable config %s (%s).", source, exc)
    if not isinstance(cfg.get("guilds"), dict):
        cfg["guilds"] = {}
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    """Write the live config to the data directory (never the repository)."""
    target: Path = paths.config_path()
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # the file holds the bot token
    except OSError:
        pass
    os.replace(tmp, target)


def resolve_token(cfg: dict[str, Any]) -> Optional[str]:
    """``DISCORD_TOKEN`` wins over ``bot_token`` in the config file."""
    return os.environ.get("DISCORD_TOKEN") or cfg.get("bot_token") or None


# --------------------------------------------------------------------------- #
# Per-server lookups
# --------------------------------------------------------------------------- #
def get_guild_config(cfg: dict[str, Any], guild_id: int) -> dict[str, Any]:
    """Settings for one server, with defaults filled in. Never ``None``."""
    stored = (cfg.get("guilds") or {}).get(str(int(guild_id)))
    result = dict(DEFAULT_GUILD)
    if isinstance(stored, dict):
        result.update({k: v for k, v in stored.items() if k in DEFAULT_GUILD})
    if not result.get("timezone"):
        result["timezone"] = cfg.get("default_timezone") or DEFAULT_TIMEZONE
    return result


def _as_id(value: Any) -> Optional[int]:
    return int(value) if value else None


def get_staff_role_id(cfg: dict[str, Any], guild_id: int) -> Optional[int]:
    return _as_id(get_guild_config(cfg, guild_id)["staff_role_id"])


def get_announcement_channel_id(cfg: dict[str, Any], guild_id: int) -> Optional[int]:
    return _as_id(get_guild_config(cfg, guild_id)["announcement_channel_id"])


def get_staff_channel_id(cfg: dict[str, Any], guild_id: int) -> Optional[int]:
    return _as_id(get_guild_config(cfg, guild_id)["staff_channel_id"])


def get_timezone_name(cfg: dict[str, Any], guild_id: int) -> str:
    return str(get_guild_config(cfg, guild_id)["timezone"])


def set_guild_value(cfg: dict[str, Any], guild_id: int, key: str, value: Any) -> None:
    """Set one key for a server in memory. Call :func:`save_config` after."""
    if key not in DEFAULT_GUILD:
        raise KeyError(key)
    guilds = cfg.setdefault("guilds", {})
    entry = guilds.setdefault(str(int(guild_id)), dict(DEFAULT_GUILD))
    entry[key] = value


# --------------------------------------------------------------------------- #
# Who may use /manage
# --------------------------------------------------------------------------- #
def staff_denial(
    member: Any,
    cfg: dict[str, Any],
    guild_id: int,
    *,
    guild: Optional[discord.Guild] = None,
) -> Optional[str]:
    """Why ``member`` may not run staff commands, or ``None`` if they may.

    * Administrators always pass.
    * Once a staff role is configured, *only* holders of that role pass.
      Discord's *Manage Server* permission is deliberately not enough on its
      own: a server can hand that permission to anyone.
    * A server with no staff role yet falls back to *Manage Server*, so a
      fresh install keeps working until ``/manage setup`` is run.

    The returned text is written for the member who hit the wall.
    """
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and getattr(perms, "administrator", False):
        return None

    staff_role_id = get_staff_role_id(cfg, guild_id)
    if staff_role_id is None:
        if perms is not None and getattr(perms, "manage_guild", False):
            return None
        return (
            "Only administrators can use this until a staff role is set. "
            "An administrator can run `/manage setup staff_role:@Staff`."
        )

    roles = getattr(member, "roles", None) or []
    if any(getattr(role, "id", None) == staff_role_id for role in roles):
        return None
    mention = f"<@&{staff_role_id}>"
    if guild is not None and hasattr(guild, "get_role"):
        role = guild.get_role(staff_role_id)
        if role is not None:
            mention = role.mention
    return f"Only members with the {mention} role can use this command."
