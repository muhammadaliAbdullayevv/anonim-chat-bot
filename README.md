# Anonim Relay Bot (PTB)

A Telegram anonymous chat bot with permanent personal links, daily engagement loops, reaction sync, and PostgreSQL persistence.

## Features
- Personal anonymous link per user: `https://t.me/<bot>?start=<token>`
- Anonymous confession inbox with controls:
  - `💬 Reply anonymously`
  - `🚫 Block sender`
  - `🔍 Guess who`
- Profile statistics (`/stats`)
- Daily viral loop (Question of the Day)
- Voice message support (anonymous voice relay)
- Reaction mirroring between paired messages
- Legacy one-time migration from `users.json`/`messages.json` when DB is empty

## Stack
- Python 3.10+
- `python-telegram-bot==22.6`
- `python-dotenv==1.0.1`
- `psycopg[binary]==3.1.18`

## Project structure
- `main.py`: bot logic, schema management, handlers, relay, stats, jobs
- `requirements.txt`: pinned dependencies
- `.env.example`: safe starter config
- `scripts/run_bot_service.sh`: service startup wrapper
- `systemd/anonim-bot.service`: systemd template
- `users.json`, `messages.json`: optional local migration source, ignored by Git

## Setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create local config from the example file:
```bash
cp .env.example .env
```

Service startup prefers `.venv/bin/python`; if unavailable it falls back to `venv312/bin/python`.

## Environment variables
Required:
```bash
BOT_TOKEN="<your-bot-token>"
DATABASE_URL="postgresql://username:password@localhost:5432/anonim_bot"
```

Core optional:
```bash
MIGRATE_JSON_ON_START=0
ENABLE_REACTION_UPDATES=1
ENABLE_DAILY_QUESTION=1
```
- `MIGRATE_JSON_ON_START=0`: keep disabled for fresh installs; set to `1` only to migrate local legacy JSON when the DB is empty.
- `ENABLE_REACTION_UPDATES=1`: include reaction updates in polling.
- `ENABLE_DAILY_QUESTION=1`: enable scheduled Question of the Day pushes.

Local Telegram Bot API server optional:
```bash
TELEGRAM_BASE_URL="http://127.0.0.1:8081/bot"
TELEGRAM_BASE_FILE_URL="http://127.0.0.1:8081/file/bot"
TELEGRAM_LOCAL_MODE=1
TELEGRAM_HEALTHCHECK_URL="http://127.0.0.1:8081/"
```

## Commands
- `/start`: create/show personal anonymous link, connect by payload token
- `/newlink`: rotate personal link token
- `/stats`: show profile stats

## Run manually
```bash
source .venv/bin/activate
python main.py
```

## Run with systemd
1. Ensure `.env` is set.
2. Copy the template and replace `YOUR_USER` and `/path/to/anonim-bot`.
3. Install and start service:
```bash
sudo cp systemd/anonim-bot.service /etc/systemd/system/anonim-bot.service
sudoedit /etc/systemd/system/anonim-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now anonim-bot.service
```
4. Monitor:
```bash
systemctl status anonim-bot.service
journalctl -u anonim-bot.service -f
```

Health-check URL priority in `scripts/run_bot_service.sh`:
1. `CHECK_URL` (service env override)
2. `TELEGRAM_HEALTHCHECK_URL`
3. `TELEGRAM_BASE_URL`
4. `https://api.telegram.org`

## Data model summary
- `users`: user profile + relay state + daily marker
- `message_pairs`: bidirectional message-id mapping for replies/reactions
- `owner_blocks`: per-owner blocked sender list
- `user_daily_stats`: aggregated daily counters for `/stats`

## Notes
- Local-only files such as `.env`, virtual environments, caches, and legacy JSON are ignored by `.gitignore`.
- If `copyMessage` fails (blocked bot, invalid destination, etc.), related state is cleaned.
- Reaction mirroring uses bot-side API rules; multi-reaction limits may still apply.
- Rotate bot token if it was ever exposed in logs or shared output.
