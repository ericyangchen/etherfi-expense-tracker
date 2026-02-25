# Discord Bot Setup

The tracker uses a Discord bot for slash commands, scheduled reports, and session expiry warnings.

## How It Works

```
Discord Bot (bot.py) — runs in Docker
  ├── Slash commands: /report_latest, /report_daily, /report_monthly
  ├── Scheduled tasks: auto-fetch, daily report, monthly report
  ├── Session expiry warning (≤7 days)
  └── All times use server local timezone
```

## Setup

### 1. Create a Discord Application

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application**, name it (e.g. "Ether.fi Tracker")
3. Go to **Bot** tab
4. Click **Reset Token** and copy the **bot token** — save it

### 2. Invite the Bot to Your Server

1. Go to **OAuth2** tab
2. Under **OAuth2 URL Generator**, select scopes: **`bot`** and **`applications.commands`** (both required)
3. Under **Bot Permissions**, select: `Send Messages`, `Read Messages/View Channels`, `Read Message History`
4. Copy the generated URL, open it in your browser, and add the bot to your server

> If the bot was previously added without `applications.commands`, kick it from Server Settings → Integrations and re-invite with the new URL.

### 3. Get Your Channel ID

1. In Discord, go to **User Settings** > **Advanced** > Enable **Developer Mode**
2. Right-click the channel where you want reports > **Copy Channel ID**

### 4. Configure `.env`

```bash
cp .env.example .env
```

Edit `.env`:

```
DISCORD_BOT_TOKEN=your_bot_token_here
DISCORD_CHANNEL_ID=your_channel_id_here
```

### 5. Start the Bot

**Docker (recommended):**

```bash
docker compose up -d --build
```

The bot runs alongside PostgreSQL. Both auto-restart after reboot.

After code changes, rebuild: `docker compose down && docker compose up -d --build`

**Manual (for development):**

```bash
source .venv/bin/activate
python main.py bot
```

### 6. Verify

Check bot logs for successful sync:

```bash
docker compose logs bot --tail 10
```

You should see:

```
Synced 3 slash command(s) to guild (instant)
```

Then type `/` in your Discord channel — you should see `report_latest`, `report_daily`, `report_monthly`.

## Slash Commands

Type `/` in the configured Discord channel:

| Command | What it does |
|---------|-------------|
| `/report_latest` | Scrape + report all unreported transactions, mark as reported |
| `/report_daily` | Scrape + report daily transactions (omit year/month/day for today) |
| `/report_daily year:2026 month:2 day:24` | Report for a specific date |
| `/report_monthly` | Current month's summary (top 10 merchants by default) |
| `/report_monthly year:2026 month:1` | Specific month's summary |
| `/report_monthly top:5` | Current month with top 5 merchants only |

Commands only work in the channel specified by `DISCORD_CHANNEL_ID`. In other channels, the bot responds with an ephemeral "use the configured channel" message.

## Scheduled Tasks

The bot automatically runs these (no cron needed):

| Task | Schedule | Description |
|------|----------|-------------|
| Auto-fetch | Every `fetch_interval_hours` (default 24h) | Scrapes new transaction data |
| Daily report | At `daily_report_hour` local time (default midnight) | Reports unreported transactions, skips if none. Set to `-1` to disable. |
| Monthly report | On `monthly_report_day` (default 1st) at midnight | Previous month's full summary. Set to `-1` to disable. |
| Session check | After each auto-fetch | Warns if Ether.fi session expires in ≤7 days |

Change schedules via CLI or the GUI (**Config** tab):

```bash
python main.py config set fetch_interval_hours 12
python main.py config set daily_report_hour 8
python main.py config set monthly_report_day 1
# Disable daily report:
python main.py config set daily_report_hour -1
```

Or launch `python main.py gui` and use the toggle + number input on the **Config** tab.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Slash commands don't appear | Ensure bot was invited with `applications.commands` scope. Check logs for "Synced N slash command(s)". Kick and re-invite if needed. |
| "Synced 0 slash command(s)" | Code is stale. Rebuild: `docker compose down && docker compose up -d --build` |
| "Channel not found" in logs | Verify `DISCORD_CHANNEL_ID` in `.env`. Bot must be in the server. |
| Bot can't send messages | Check bot has Send Messages permission in the channel |
| Playwright error in logs | Rebuild image: `docker compose down && docker compose up -d --build` |
| Scrape errors | Session may be expired. Run `python main.py login` to refresh |
