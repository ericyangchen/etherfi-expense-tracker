# System Setup

Full setup guide for the Ether.fi Expense Tracker.

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| macOS | any recent | Required for Playwright scraper |
| Python | 3.11+ | `python3 --version` to check |
| Docker Desktop | any | [Download](https://www.docker.com/products/docker-desktop/) |

## Step 1: Docker Desktop

Install Docker Desktop. Configure auto-start:

**Docker Desktop** > **Settings** > **General** > Check **"Start Docker Desktop when you sign in"**

## Step 2: Start Services

```bash
cd /path/to/personal-expense

# First time: create .env from template
cp .env.example .env
# Edit .env with your Discord bot token and channel ID (see docs/DISCORD_SETUP.md)

# Start PostgreSQL + Bot
docker compose up -d
```

## Step 3: Python Environment (for CLI + GUI)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install "psycopg[binary]" playwright "discord.py" streamlit
playwright install chromium
```

## Step 4: Initialize Database

```bash
python main.py config list
```

This creates tables and seeds default config values. The schema auto-migrates if upgrading from an older version (single-category → many-to-many categories).

## Step 5: Import Existing Data (optional)

```bash
python main.py import transaction-history-2026-02-25.csv
```

Cards are auto-discovered from transaction data — no manual registration needed. Safe to run multiple times.

## Step 6: Authenticate with Ether.fi

```bash
python main.py login
```

Opens a browser. Connect wallet, sign in, press Enter. Session saved to `data/auth_state.json`.

## Step 7: Launch the Dashboard

```bash
python main.py gui
```

This opens the Streamlit configuration dashboard in your browser. From here you can:

- **Dashboard** — check auth status, fetch transactions, view recent activity
- **Cards** — set nicknames for your cards
- **Categories** — create categories (Business, Travel, etc.) and assign cards to them. A card can belong to multiple categories; every card is always in the virtual "All" group.
- **Config** — set fetch interval, report schedules, notification webhooks
- **Reports** — generate and view latest/daily/monthly reports with charts

All settings can also be managed via CLI if preferred (see below).

## Step 8: Configure (CLI alternative)

```bash
# Card nicknames (also doable from the GUI Cards tab)
python main.py card set 7867 --nickname "Main"
python main.py card set 6109 --nickname "Business"
python main.py card list

# Notification webhook (for CLI report commands)
python main.py config set notify_channels '[{"type": "discord", "webhook_url": "https://discord.com/api/webhooks/..."}]'

# Auto-fetch interval (default: 24 hours; -1 to disable)
python main.py config set fetch_interval_hours 24

# Daily report hour in server local time (default: 0 = midnight, reports yesterday)
python main.py config set daily_report_hour 0

# Monthly report day (default: 1st of month)
python main.py config set monthly_report_day 1
```

Category management (create categories, assign cards) is easiest through the GUI: `python main.py gui` → **Categories** tab.

## Step 9: Verify

```bash
# Monthly report
python main.py report monthly --no-send

# Check bot is running
docker compose logs bot --tail 20
```

In Discord, type `report monthly` to test the bot.

## Auto-Resume After Reboot

| Component | How it resumes |
|-----------|---------------|
| PostgreSQL | Docker `restart: unless-stopped` + Docker Desktop starts on login |
| Discord Bot | Docker `restart: unless-stopped` alongside PostgreSQL |
| Scraper session | Persisted to `data/auth_state.json` on disk |

No cron needed. The bot handles all scheduling.

## DataGrip / DB Access

| Field | Value |
|-------|-------|
| Host | `localhost` |
| Port | `5432` |
| Database | `etherfi` |
| User | `etherfi` |
| Password | `etherfi_local` |

## Uninstall

```bash
docker compose down -v    # stop containers + delete data volume
rm -rf /path/to/personal-expense
```
