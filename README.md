# Anonim Chat Bot

Anonymous Telegram messaging bot with personal links, anonymous replies, reaction sync, and PostgreSQL persistence.

## Overview

This bot gives each user a personal Telegram link like `https://t.me/<bot>?start=<token>`. Anyone opening that link can send an anonymous message to the owner, and the owner can reply without revealing identity.

The project is built around persistent user links, private message relay, reply threading, and a small engagement loop for repeat usage.

## Core features

- Personal anonymous link for every user
- Anonymous inbox actions:
  - `💬 Reply anonymously`
  - `🚫 Block sender`
  - `🔍 Guess who`
- Anonymous reply flow between linked users
- Voice message relay
- Reaction mirroring between paired messages
- Daily Question of the Day prompt
- Profile statistics with `/stats`
- PostgreSQL storage
- One-time legacy migration from `users.json` and `messages.json` when the database is empty

## Stack

- Python 3.10+
- `python-telegram-bot==22.6`
- `python-dotenv==1.0.1`
- `psycopg[binary]==3.1.18`

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then set your bot token and PostgreSQL connection string in `.env`.

## Configuration

Required environment variables:

```bash
BOT_TOKEN="<your-bot-token>"
DATABASE_URL="postgresql://username:password@localhost:5432/anonim_bot"
```

Core optional variables:

```bash
MIGRATE_JSON_ON_START=0
ENABLE_REACTION_UPDATES=1
ENABLE_DAILY_QUESTION=1
```

- `MIGRATE_JSON_ON_START=0`: keep disabled for fresh installs; set to `1` only for a one-time local JSON migration when the database is empty
- `ENABLE_REACTION_UPDATES=1`: include reaction updates in polling
- `ENABLE_DAILY_QUESTION=1`: enable scheduled Question of the Day prompts

Optional local Telegram Bot API server settings:

```bash
TELEGRAM_BASE_URL="http://127.0.0.1:8081/bot"
TELEGRAM_BASE_FILE_URL="http://127.0.0.1:8081/file/bot"
TELEGRAM_LOCAL_MODE=1
TELEGRAM_HEALTHCHECK_URL="http://127.0.0.1:8081/"
CHECK_URL=
```

## Bot commands

- `/start`: create or show a personal anonymous link, or connect through a start payload
- `/newlink`: rotate the user's personal link token
- `/stats`: show profile statistics

## Run locally

```bash
source .venv/bin/activate
python main.py
```

Service startup prefers `.venv/bin/python`; if unavailable it falls back to `venv312/bin/python`.

## Run with systemd

1. Make sure `.env` is configured.
2. Copy the systemd template and replace `YOUR_USER` and `/path/to/anonim-bot`.
3. Reload systemd and start the service.

```bash
sudo cp systemd/anonim-bot.service /etc/systemd/system/anonim-bot.service
sudoedit /etc/systemd/system/anonim-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now anonim-bot.service
```

Monitor the service with:

```bash
systemctl status anonim-bot.service
journalctl -u anonim-bot.service -f
```

Health-check URL priority in `scripts/run_bot_service.sh`:

1. `CHECK_URL`
2. `TELEGRAM_HEALTHCHECK_URL`
3. `TELEGRAM_BASE_URL`
4. `https://api.telegram.org`

## Project structure

- `main.py`: main bot logic, schema management, handlers, message relay, stats, and jobs
- `requirements.txt`: pinned Python dependencies
- `.env.example`: safe example environment file
- `scripts/run_bot_service.sh`: service startup wrapper with network health check
- `systemd/anonim-bot.service`: portable systemd template

Local runtime files such as `.env`, virtual environments, caches, `users.json`, and `messages.json` are ignored by Git.

## Data model summary

- `users`: user profile, personal token, relay state, and daily marker
- `message_pairs`: bidirectional message mapping for replies and reactions
- `owner_blocks`: per-owner blocked sender list
- `user_daily_stats`: aggregated daily counters for `/stats`

## Notes

- If `copyMessage` fails, related relay state is cleaned up automatically
- Reaction mirroring still follows Telegram API limitations
- Rotate the bot token immediately if it was ever exposed in logs, screenshots, or shared output

## License

This repository includes an MIT license in [`LICENSE`](LICENSE).
