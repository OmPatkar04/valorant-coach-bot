# Valorant Discord Coaching Bot

A self-hosted Discord bot that watches your competitive Valorant matches (via HenrikDev's
Unofficial Valorant API, v3 matches endpoint) and posts a coaching report to a channel after
each game: stats, rank-scaled benchmarks, diagnoses, playstyle and agent fit, and drills.

## Project layout

```
.env.example            all environment variables
requirements.txt        discord.py, aiohttp, python-dotenv
config/settings.py      env loader + validation
services/valorant_api.py  async HenrikDev client (retries, rate-limit safety)
engine/benchmarks.py    rank targets, mechanics advice, drills, agent classes (edit me!)
engine/analyzer.py      pure analysis functions (no I/O)
utils/embed_builder.py  Discord embeds (green win / red loss / gold draw)
main.py                 entry point + background poller
```

## 1. Prerequisites

* Python 3.10 or newer
* A Discord bot token and a HenrikDev API key
* A Discord server where you can add the bot

## 2. Create the Discord bot (once)

1. Go to <https://discord.com/developers/applications> -> **New Application**.
2. **Bot** tab -> **Reset Token** -> copy it (this is `DISCORD_BOT_TOKEN`). No privileged intents are needed.
3. **OAuth2 -> URL Generator**: scopes `bot` **and** `applications.commands` (needed for the slash commands); permissions **View Channel**, **Send Messages**, **Embed Links**.
4. Open the generated URL and add the bot to your server.
5. In Discord enable **Settings -> Advanced -> Developer Mode**, right-click the channel the reports should go to, and **Copy Channel ID**.

## 3. Install

Linux / macOS:

```bash
cd valorant-coach-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell (run one line at a time; PowerShell 5 does not support `&&`):

```powershell
cd valorant-coach-bot
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If activation is blocked, run `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` once, or skip activation and use `.venv\Scripts\python.exe` instead of `python`.

## 4. Configure

Linux / macOS: `cp .env.example .env`. Windows: `copy .env.example .env`. Then edit `.env`:

Edit `.env`:

| Variable | Meaning |
| --- | --- |
| `DISCORD_BOT_TOKEN` | Bot token from step 2 |
| `DISCORD_CHANNEL_ID` | Channel ID from step 2 |
| `HENRIK_API_KEY` | Your HenrikDev key |
| `VALORANT_REGION` | `na`, `eu`, `ap`, `kr`, `latam` or `br` (India -> `ap`) |
| `VALORANT_NAME` / `VALORANT_TAG` | Your Riot ID `Name#TAG` split in two, without the `#` |
| `POLL_INTERVAL_SECONDS` | Optional, default 180, minimum 30 |
| `MATCH_MODE` | Optional, default `competitive` |
| `HISTORY_SIZE` | Optional, default 10 (matches used for playstyle) |
| `LOG_LEVEL` | Optional, default `INFO` |

## 5. Launch

```bash
python main.py
```

On the first poll the bot **only remembers** your latest matches (it posts nothing). Finish a
competitive game and the report appears within one poll interval.

## Slash commands (ask the coach any time)

These appear in the server of your report channel when the bot starts. Type `/` in Discord.

| Command | What it does |
| --- | --- |
| `/lastmatch` | Full report (stats, diagnosis, agent fit, drills) for your newest competitive match |
| `/recent count:2-5` | One line per recent match, combined stats vs your rank targets, and trend coaching |
| `/coach topic:...` | Focused advice on one area: overall, aim, opening duels, deaths and damage, agent fit, or drills |

Notes: `/coach` is a menu of topics, not free-form chat. It answers from your stats using the built-in
rules in `engine/`, so it is fast and free, but it can't answer arbitrary questions. Each command has a
15 second per-user cooldown to protect your API key's rate limit. If the commands don't show up, re-invite
the bot with the `applications.commands` scope (step 2.3) and restart it.

To keep it running 24/7, use a process manager, e.g. a systemd unit:

```ini
[Unit]
Description=Valorant Coaching Bot
After=network-online.target

[Service]
WorkingDirectory=/path/to/valorant-coach-bot
ExecStart=/path/to/valorant-coach-bot/.venv/bin/python main.py
Restart=on-failure
User=youruser

[Install]
WantedBy=multi-user.target
```

## How it works

* **Polling:** a `discord.ext.tasks` loop calls the API every `POLL_INTERVAL_SECONDS` without blocking the Discord connection.
* **No spam:** the first poll seeds the cache silently. Afterwards only matches whose IDs have not been seen are posted (at most 3 per poll, oldest first). If Discord rejects a post, the match is retried next cycle.
* **Metrics:** HS% = headshots / total hits; ADR = damage / rounds; K/D = kills / deaths (with zero deaths it equals your kills). First bloods and first deaths come from the earliest kill in each round. "First-kill conversion" is the share of rounds your team won after getting the first kill.
* **Benchmarks:** `engine/benchmarks.py` has one target set per rank (Iron to Immortal; Radiant uses Immortal). They are rough climbing targets, not official Riot numbers. Tune them.
* **Playstyle and agent fit:** judged on your last `HISTORY_SIZE` matches, because one game is too noisy. New agents Riot adds are reported as "unknown" until you add them to `AGENT_CLASSES`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Invalid configuration: ...` on start | The listed variables are missing or invalid in `.env` |
| `API key rejected (HTTP 401/403)` | Check `HENRIK_API_KEY`; the bot stops polling until restarted |
| `Not found (HTTP 404)` | Check region, name and tag (the bot keeps retrying) |
| HTTP 400 mentioning mode | Set `MATCH_MODE=Competitive` |
| Rate limited (HTTP 429) | Raise `POLL_INTERVAL_SECONDS`; the client already honours `Retry-After` |
| `No access to channel` / nothing posted | Give the bot View Channel, Send Messages and Embed Links in that channel |
| Rank shows as assumed | The API reported no rank (unrated); Gold benchmarks are used |
