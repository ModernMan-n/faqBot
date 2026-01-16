# FAQ Telegram Bot (Python)

## Setup

1) Create a bot with BotFather and copy the token.
2) Set env vars (or copy .env.example -> .env and export them):

```bash
export BOT_TOKEN="..."
export ADMIN_CHAT_ID="123456789"
```

`ADMIN_CHAT_ID` can be your user ID or a group ID where the bot is added.

Optional env vars:

```bash
export BOT_LOG_PATH="bot.log"
export ANALYTICS_DB_PATH="analytics.sqlite"
export SUPPORT_REMINDER_SECONDS="600"
export SUPPORT_REMINDER_MAX="3"
```

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Media

Put images/videos into `media/` and update paths in `main.py`.
If a media file is missing, the bot will send text only.

## Logs and analytics

- Logs are written to `bot.log` with rotation.
- Analytics are stored in `analytics.sqlite`.
- Use `/stats` in the admin chat to get a 7-day summary.
- Support reminder is sent every `SUPPORT_REMINDER_SECONDS` of inactivity,
  up to `SUPPORT_REMINDER_MAX` times.
