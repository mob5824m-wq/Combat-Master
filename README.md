# Combat Master

A [discord.py](https://discordpy.readthedocs.io/) bot for server staff. It posts
announcements now or later, repeats them on a schedule, runs polls and events,
welcomes new members, and keeps an audit log of what staff did. A web
dashboard in the same process does the same things from a browser.

It follows the layout of [Sentinel](https://github.com/mob5824m-wq/Sentinel):
one slash-command group, a staff-role gate, a local SQLite database, a
`config.json`, a staff log channel, tests, launch scripts and a CI workflow.
It does not copy Sentinel's features or those of Discord-lofi (see
[Not included](#not-included)).

Everything in Discord is one command group, `/manage`. Type `/manage` to see it:

```
/manage announce            post an announcement now
/manage schedule            post an announcement at a later time
/manage repeat              post an announcement again and again
/manage scheduled           list what is waiting to go out
/manage cancel              cancel a scheduled or repeating announcement
/manage history             the most recent announcements
/manage template save|list|delete|use    reusable announcement text
/manage poll create|list|close           reaction polls, with optional auto-close
/manage event create|list|cancel         events with an RSVP reaction and reminders
/manage welcome set|off|test             the message posted when someone joins
/manage status              this server's settings and what is pending
/manage audit               who did what, newest first
/manage setup               staff role, channels and timezone         (administrators)
/manage fixcommands         re-sync the commands                      (administrators)
```

---

## 1. What it does

* **Announcements are embeds.** The heading and message are shown in an embed,
  with the author in the footer. An optional role can be pinged, and that is
  the only ping the bot sends. `@everyone`, `@here` and individual users are
  never pinged, even if someone types them.
* **Scheduling.** `/manage schedule` takes a delay (`30m`, `2h`, `1d12h`; a bare
  number means minutes) or a local time (`2026-10-20 18:00`, read in the
  server's timezone). The scheduler checks every 30 seconds.
* **Repeating announcements.** `/manage repeat` posts again every interval (at
  least 15 minutes, at most 365 days). A repeat that was missed while the bot
  was offline is skipped, not posted in a burst. A repeat stays queued until
  you cancel it.
* **Templates.** Save text once with `/manage template save`, then post it with
  `/manage template use`. Names are unique per server, ignore case, and use
  letters, numbers, spaces, `-` and `_` (up to 40 characters).
* **Polls.** Two to ten options, one reaction each. Votes are the reactions on
  the poll message, minus the bot's own. A poll closes when you run
  `/manage poll close`, or by itself after the duration you set (1 minute to
  30 days). The final counts are kept and posted on the poll.
* **Events.** Members RSVP with a ✅ reaction. The start must be at least five
  minutes away. If you set a reminder (5 minutes to 7 days before the start),
  the bot posts it once and mentions up to 50 people who RSVP'd.
* **Welcome messages.** When someone joins, the bot posts your welcome in the
  channel you choose and mentions the new member. The text can use `{mention}`,
  `{name}`, `{server}` and `{members}`. This feature needs the *Server Members*
  intent (see [Invite the bot](#invite-the-bot)).
* **Publish to followers.** In an announcement (news) channel, tick *publish*
  and the message is also sent to every server that follows the channel.
* **Failures are not silent.** If a scheduled post can not go out (channel
  deleted, missing permission), it is marked `failed` with the reason, and the
  staff log says so. A poll whose message was deleted closes with zero votes.
* **Staff log.** Every announcement post, and every failed scheduled post, is
  also written to the staff log channel, if one is set.
* **Audit log.** Every change staff make, from Discord or from the dashboard,
  is recorded with who did it, when and what. `/manage audit` and the dashboard
  show it. The scheduler's own work is recorded as the *system*.
* **History and overview.** `/manage history` lists posted announcements, and
  `/manage status` (or the dashboard overview) shows counts of what is queued,
  open and upcoming.

## 2. Setup

### Requirements

* Python **3.9 or newer** (developed and tested on 3.11).
* A Discord application with a bot user: <https://discord.com/developers/applications>.
* Node.js is only needed to run the dashboard's JavaScript check in CI. The bot
  itself does not need it.

### Invite the bot

In the Developer Portal, open the bot's page:

* Under *Bot → Privileged Gateway Intents*, switch on **Server Members Intent**.
  Without it, welcome messages do not work. Everything else still does.
* Under *OAuth2 → URL Generator*, select the scopes **`bot`** and
  **`applications.commands`**. Give the bot these permissions in the channels it
  will use:
  * View Channel, Send Messages, Embed Links
  * Read Message History and Add Reactions (polls and events)
  * Publish to followers needs the bot to be able to send in that news channel

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
bot. To run it by hand: `python -m pip install -r requirements.txt`, then
`python bot.py`.

### Configure your server

Run this once, as an administrator:

```
/manage setup staff_role:@Staff announcement_channel:#announcements staff_channel:#staff-log timezone:America/Toronto
```

Every option is optional, and only the ones you pass are changed.
`timezone` takes an IANA name such as `America/Toronto`, `Europe/London` or
`Australia/Sydney`. Welcome messages are set with `/manage welcome set`.
`/manage status` shows what is set.

## 3. Who can use what

| Command | Who |
|---------|-----|
| Everything except `setup` and `fixcommands` (announce, schedule, repeat, scheduled, cancel, history, template, poll, event, welcome, status, audit) | Holders of the **staff role**, and administrators |
| `setup`, `fixcommands` | Administrators |
| The dashboard | Anyone with the dashboard token (see [section 4](#4-the-dashboard)) |

The check runs again every time a command is used, so a Discord integration
that shows the commands to more people does not give them access.

**Before a staff role is set,** the staff commands fall back to *Manage
Server*, so a fresh install still works. The refusal message tells you to run
`/manage setup staff_role:` once.

**Holding *Manage Server* alone does not count** once a staff role is set. A
server can grant that permission to anyone, and staff membership is meant to be
explicit.

## 4. The dashboard

The bot starts a web dashboard in the same process. It lists your servers and
offers the same actions as the commands: announce, schedule and repeat, manage
templates, polls and events, set welcome messages, change settings, and read
the audit log. Every button calls the same code as its command, so the two can
not drift apart.

### Get the token and sign in

```bash
python bot.py --dashboard-token
```

This prints the token, creating one on first use and saving it in
`data/config.json` (the file is written with owner-only permissions). Open
`http://127.0.0.1:8765` on the bot's machine and paste it in. The Discord bot
token does not work on the dashboard, and the dashboard token does not work on
Discord.

### Settings

| Key in `data/config.json` | Default | What it does |
|---------------------------|---------|--------------|
| `dashboard_enabled` | `true` | Set to `false` to run the bot without the dashboard. |
| `dashboard_host` | `127.0.0.1` | Address to listen on. Loopback means only this machine can reach it. |
| `dashboard_port` | `8765` | Port to listen on. |
| `dashboard_token` | created on first use | The sign-in token. Change it by editing the file and restarting. |
| `dashboard_secure_cookie` | `false` | Set to `true` when the dashboard is served over HTTPS, so the session cookie is only sent over HTTPS. |
| `dashboard_allowed_hosts` | `[]` | Extra host names the dashboard answers to, e.g. `["staff.example.com"]`. Loopback names are always allowed. Requests with any other `Host` header get a 400. |
| `dashboard_trusted_proxies` | `[]` | Addresses of reverse proxies, e.g. `["127.0.0.1"]`. Their `X-Forwarded-For` header is used to tell visitors apart for the login throttle. Without this, every visitor looks like the proxy. |

### Security

* The session is an **HttpOnly, SameSite=Strict** cookie. Every change also
  needs an `X-CSRF-Token` header that matches the session.
* Failed sign-ins are throttled per client address (after 8 failures in 10
  minutes, sign-in is refused for a while).
* The `Host` header must be a name you have allowed (loopback by default). This
  blocks DNS-rebinding attacks.
* The page sends a strict Content-Security-Policy, and no inline scripts.
* The default is to listen on `127.0.0.1` only. To reach it from another
  machine, use an SSH tunnel, or put a reverse proxy that terminates HTTPS in
  front of it and list the proxy in `dashboard_trusted_proxies`. Do not open the
  port to the internet directly.

### Demo mode

```bash
python bot.py --demo
```

`--demo` runs the dashboard over two simulated servers, with no token, no
Discord connection and no real data. Posts, polls, reactions and reminders all
happen in memory. The database is a temporary directory unless
`COMBAT_MASTER_DATA` is set. To try it from another machine (for example in a
preview), add `--host 0.0.0.0 --port 8765`. Do not run demo mode on a real
install.

## 5. Times

| You type | Meaning |
|----------|---------|
| `30m`, `2h`, `1d12h`, `45 seconds` | That long from now |
| `15` | 15 minutes from now (bare numbers are minutes) |
| `2026-10-20 18:00` | 18:00 on that date, in the server's timezone |
| `2026-10-20T18:00` | Same, with a `T` between date and time |

A scheduled time must be at least one minute away and at most 365 days away.
Replies show the time as a Discord timestamp, so each person sees it in their
own timezone. The dashboard shows times in the server's timezone.

## 6. Limits

| Item | Limit |
|------|-------|
| Announcement heading | 256 characters |
| Announcement message | 4000 characters |
| Template name | 40 characters |
| Poll question | 256 characters; 2 to 10 options, 100 characters each |
| Poll duration | 1 minute to 30 days |
| Repeat interval | 15 minutes to 365 days |
| Event title | 256 characters; details 1000 characters |
| Event reminder | 5 minutes to 7 days before the start (0 for none); mentions up to 50 people |
| Welcome message | 1000 characters |
| Lists in Discord | up to 25 entries (`history`, `scheduled`, `poll list`, `event list`) |

## 7. Repository layout

```
bot.py              entry point, the /manage cog, the scheduler loop, command-line options
command_tree.py     the /manage group and the administrator check
operations.py       every staff action (commands and dashboard both call these)
announcements.py    time parsing, announcement embeds, posting rules, history, templates
polls.py            poll options, vote counting, poll storage
events.py           event checks, RSVP embeds, reminders, event storage
welcome.py          welcome message templates and placeholders
audit.py            the audit log and the Actor who did each action
discord_gateway.py  the live Discord adapter (discord.py)
demo.py             demo mode: simulated servers and an in-memory Discord
dashboard.py        the dashboard web server and its JSON API
dashboard.html      the dashboard page
static/             dashboard.js and dashboard.css (no build step)
settings.py         per-server config and the staff permission rule
store.py            SQLite access and the schema
paths.py            where the database, log and live config are kept
config.json         template config (no token; copied to data/ on first save)
VERSION             the version number reported by --version
requirements.txt    discord.py, aiohttp and tzdata
scripts/            run_linux.sh and run_windows.bat launchers
tests/              unit tests, including the dashboard API (no Discord connection needed)
.github/workflows/  CI: lint, check the dashboard script, run the tests
```

```bash
python -m pytest -q                       # full test suite
python tests/test_operations.py           # one file, without pytest
node --check static/dashboard.js          # syntax check for the dashboard script
python bot.py --paths                     # where this install keeps its files
python bot.py --version
python bot.py --check                     # check the configuration, do not connect
```

Set `COMBAT_MASTER_DATA=/some/dir` to keep the database, log and config
somewhere else.

## 8. Troubleshooting

* **Commands do not appear.** The bot syncs its commands to each server when it
  starts and when it joins one. Check `data/bot.log` for a `Synced N command(s)`
  line. Run `/manage fixcommands` to re-sync, and restart Discord (Ctrl+R) if the
  list still looks stale.
* **Commands appear twice.** An older copy of the bot may still be running with
  the same token. Stop it, then run `/manage fixcommands`.
* **"Login failed".** The token is wrong or was reset in the Developer Portal.
  Put the new one in `DISCORD_TOKEN` or `data/config.json`.
* **Welcome messages are not sent.** Turn on the *Server Members* intent in the
  Developer Portal and restart the bot.
* **"I can't send messages in #channel" / "I need Embed Links".** Grant the bot
  View Channel, Send Messages and Embed Links in that channel, or in its category.
* **A role shows in the announcement but does not notify.** The role is not
  mentionable. Turn on *Allow anyone to @mention this role* for it, or grant the
  bot *Mention All Roles*.
* **A scheduled announcement is marked failed.** Run `/manage scheduled` or read
  the staff log: the reason is in the entry. Schedule it again once it is fixed.
* **"Only members with the @Staff role can use this command."** Give the role to
  the member, or have an administrator check `/manage status`.
* **The dashboard says "Unrecognized Host header".** You are reaching it under a
  name that is not loopback. Add that name to `dashboard_allowed_hosts`.
* **The dashboard says "The page is out of date".** The session changed in
  another tab. Reload the page.
* **The dashboard does not open.** Check `data/bot.log`. A port that is already
  in use is reported there. Change `dashboard_port`, or set `dashboard_enabled`
  to `false`.

## 9. Not included

Combat Master is a staff tool for announcements and events. It does not include
moderation punishments or warnings, tickets, applications, rules acceptance,
reaction roles, DuckDNS, music, voice, or a streaming dashboard. Those belong to
other bots and are outside this project.

## 10. License

MIT. See [LICENSE](LICENSE).
