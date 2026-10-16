# Combat Master

A [discord.py](https://discordpy.readthedocs.io/) bot that lets staff post
announcements, schedule them for later, and keep a record of what was posted.
It has the same layout as [Sentinel](https://github.com/mob5824m-wq/Sentinel):
one slash-command group, a staff-role gate, a local SQLite database, and a
staff log channel.

Everything is one command group, `/manage`. Type `/manage` in Discord to see
the whole list:

```
/manage announce     post an announcement now
/manage schedule     post an announcement at a later time
/manage scheduled    list announcements waiting to go out
/manage cancel       cancel a scheduled announcement
/manage history      the most recent announcements
/manage status       this server's settings and what is pending
/manage setup        staff role, channels and timezone          (administrators)
/manage fixcommands  remove duplicated commands and re-sync     (administrators)
```

---

## 1. What it does

* **Announcements are embeds.** The title and message are shown in an embed,
  with the author in the footer. An optional role can be pinged, and that is
  the only ping the bot will ever send. `@everyone`, `@here` and individual
  users are never pinged, even if someone types them.
* **Scheduling.** `/manage schedule` takes a delay (`30m`, `2h`, `1d12h`; a
  bare number means minutes) or a local time (`2026-10-20 18:00`, read in the
  server's timezone). The bot checks every 30 seconds and posts whatever is
  due. Schedules are stored in the database, so they survive a restart.
* **Failures are not silent.** If a scheduled post can not go out (channel
  deleted, missing permission), it is marked `failed` with the reason and the
  staff log says so.
* **Staff log.** Every post, and every failed scheduled post, is written to the
  staff log channel, if one is set.
* **History.** Every announcement that was posted is kept, and `/manage
  history` reads from that table.

## 2. Setup

### Requirements

* Python **3.9 or newer** (developed and tested on 3.11).
* A Discord application with a bot user: <https://discord.com/developers/applications>.

### Invite the bot

In the Developer Portal, under *OAuth2 → URL Generator*, select the scopes
**`bot`** and **`applications.commands`**. Give the bot these permissions:

* View Channel, Send Messages, Embed Links (in every channel it will post to)
* Mention All Roles, only if you want to ping roles that are not set to
  *Allow anyone to @mention this role*

Invite it to your server with the generated URL.

### Give it a token

Either export the token:

```bash
export DISCORD_TOKEN="your-bot-token"
```

or put it in `data/config.json` (created on first run; it is git-ignored):

```json
{
  "bot_token": "your-bot-token",
  "default_timezone": "America/Toronto"
}
```

`DISCORD_TOKEN` wins if both are set. The repository's `config.json` is a
template and is never written to, so a token can not get committed by accident.

### Run it

macOS / Linux:

```bash
scripts/run_linux.sh
```

Windows:

```bat
scripts\run_windows.bat
```

Both create a virtual environment, install `requirements.txt`, and start the
bot. To run it by hand, `python -m pip install -r requirements.txt` then
`python bot.py`.

### Configure your server

Run this once, as an administrator:

```
/manage setup staff_role:@Staff announcement_channel:#announcements staff_channel:#staff-log timezone:America/Toronto
```

Every option is optional, and only the ones you pass are changed.
`timezone` takes an IANA name such as `America/Toronto`, `Europe/London` or
`Australia/Sydney`. `/manage status` shows what is set.

## 3. Who can use what

| Command | Who |
|---------|-----|
| `announce`, `schedule`, `scheduled`, `cancel`, `history`, `status` | Holders of the **staff role**, and administrators |
| `setup`, `fixcommands` | Administrators |

The check runs again every time a command is used, so a Discord integration
that shows the commands to more people does not give them access.

**Before a staff role is set,** the staff commands fall back to *Manage
Server*, so a fresh install still works. The refusal message tells you to run
`/manage setup staff_role:` once.

**Holding *Manage Server* alone does not count** once a staff role is set. A
server can grant that permission to anyone, and staff membership is meant to
be explicit.

## 4. Times

| You type | Meaning |
|----------|---------|
| `30m`, `2h`, `1d12h`, `45 seconds` | That long from now |
| `15` | 15 minutes from now (bare numbers are minutes) |
| `2026-10-20 18:00` | 18:00 on that date, in the server's timezone |
| `2026-10-20T18:00` | Same, with a `T` between date and time |

A scheduled time must be at least one minute away and at most 365 days away.
Replies show the time as a Discord timestamp, so each person sees it in their
own timezone.

## 5. Limits

* Title: 256 characters. Message: 4000 characters.
* `/manage history` shows up to 25 entries.
* `/manage scheduled` lists up to 25 pending announcements.

## 6. Repository layout

```
bot.py              entry point, the /manage cog, the scheduler loop
command_tree.py     the /manage group and the administrator check
announcements.py    time and duration parsing, embeds, posting, history, scheduler
settings.py         per-server config and the staff permission rule
store.py            SQLite access and the schema
paths.py            where the database, log and live config are kept
config.json         template config (no token; copied to data/ on first save)
VERSION             the version number reported by --version
requirements.txt    discord.py and tzdata
scripts/            run_linux.sh and run_windows.bat launchers
tests/              unit tests (no Discord connection needed)
.github/workflows/  CI: runs the test suite
```

```bash
python -m pytest -q                       # full test suite
python tests/test_announcements.py        # one file, without pytest
python bot.py --paths                     # where this install keeps its files
python bot.py --version
```

Set `COMBAT_MASTER_DATA=/some/dir` to keep the database, log and config
somewhere else.

## 7. Troubleshooting

* **Commands do not appear.** The bot syncs its commands to each server when it
  starts and when it joins one. Check `data/bot.log` for a `Synced N command(s)`
  line. Run `/manage fixcommands` to re-sync, and restart Discord (Ctrl+R) if the
  list still looks stale.
* **Commands appear twice.** An older copy of the bot may still be running with
  the same token. Stop it, then run `/manage fixcommands`.
* **"Login failed".** The token is wrong or was reset in the Developer Portal.
  Put the new one in `DISCORD_TOKEN` or `data/config.json`.
* **"I can't send messages in #channel" / "I need Embed Links".** Grant the bot
  View Channel, Send Messages and Embed Links in that channel, or in its category.
* **A role shows in the announcement but does not notify.** The role is not
  mentionable. Turn on *Allow anyone to @mention this role* for it, or grant the
  bot *Mention All Roles*.
* **A scheduled announcement is marked failed.** Run `/manage scheduled` or read
  the staff log: the reason is in the entry. Schedule it again once it is fixed.
* **"Only members with the @Staff role can use this command."** Give the role to
  the member, or have an administrator check `/manage status`.

## 8. License

MIT. See [LICENSE](LICENSE).
