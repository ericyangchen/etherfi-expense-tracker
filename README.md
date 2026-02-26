# Ether.fi Local Expense Tracker

Automated expense tracking for Ether.fi Cash cards, running entirely on your Mac.

## What It Does

### Automated Data Fetching

Headless Playwright scraper logs into your Ether.fi Cash account, iterates through all your cards, and pulls transaction history. Browser session is saved locally so you only need to sign in with your wallet once (~90 day session, auto-refreshed on each fetch).

### PostgreSQL Storage with Deduplication

All transactions are stored in a local PostgreSQL database (Docker). A SHA256-based dedup key (`timestamp | amount | merchant`) prevents duplicate records. Status transitions (PENDING → CLEARED, PENDING → CANCELLED) are automatically synced on every fetch.

### Flexible Card Categories (Many-to-Many)

Every card is implicitly in **All**. Create custom categories (Business, Travel, Personal, etc.) and assign cards to them — a card can belong to multiple categories. Each card can have a nickname for readable reports.

### Configuration Dashboard (Streamlit)

Web-based GUI for managing everything — launch with `python main.py gui`:

- **Dashboard** — session expiry countdown, last fetch time, card/transaction counts, fetch/login buttons, recent transactions table (local timezone)
- **Cards** — inline nickname editing, category tags, delete
- **Categories** — create/delete categories, multi-select card assignment
- **Config** — fetch interval, daily/monthly report schedules (with enable/disable toggle), notification settings
- **Reports** — generate latest/daily/monthly reports in-app with spend-by-card bar charts

### Smart Reporting

Three report types, all using server local timezone:

- **`report latest`** — unreported transactions grouped by card, marks them as reported
- **`report daily`** — daily transactions grouped by card (optional date: year/month/day; omit for today)
- **`report monthly`** — grand total, per-category subtotals, per-card breakdowns with top merchants (default top 10; optional `top` arg). Cards in multiple categories show "(also in: ...)" annotations.

```
Ether.fi Monthly Summary - 2026/02
==================================
Grand Total: $2,929.74

Personal ($2,929.74):
  Personal Virtual (7867): $3,272.80 (40 txns)
    Top:
      - BKG*BOOKING.COM FLIGHT: $1,539.05
      - WALMART.COM 8009626278: $468.15
      - Trip.com: $441.57
  Ticket (9145): $306.76 (1 txns)
    Top:
      - Trip.com: $306.76

Top Merchants (All):
  BKG*BOOKING.COM FLIGHT: $1,539.05
  Trip.com: $748.33
  WALMART.COM 8009626278: $468.15
```

### Discord Bot

Long-running bot (Docker) with slash commands and scheduled tasks:

- **Slash commands** — type `/` in Discord:
  - `/report_latest` — scrape + report unreported transactions
  - `/report_daily` — scrape + report daily transactions (optional `year`, `month`, `day`; omit for today)
  - `/report_monthly` — month summary (optional `year`, `month`; optional `top` for # of top merchants, default 10)
- **Scheduled tasks** (all using server local time):
  - Auto-fetch every `fetch_interval_hours` (default: 24)
  - Daily report at `daily_report_hour` (default midnight): scrapes + reports yesterday's transactions
  - Monthly report on `monthly_report_day` (default: 1st, `-1` to disable)
- **Session expiry warning** — alerts in Discord when Ether.fi session ≤7 days from expiring

### CSV Import

Bulk-import Ether.fi CSV exports for backfilling. Same dedup and status-sync logic as the scraper. Cards are auto-discovered.

## CLI Reference

| Command | Description |
|---------|-------------|
| `python main.py login` | Open browser for wallet login, save session |
| `python main.py scrape` | Headless scrape + store data |
| `python main.py import <file>` | Import Ether.fi CSV export |
| `python main.py report latest` | Report unreported transactions (terminal only) |
| `python main.py report daily` | Report today's transactions (terminal only) |
| `python main.py report monthly` | Current month summary (terminal only) |
| `python main.py report monthly --year Y --month M` | Specific month |
| `python main.py bot` | Start Discord bot (long-running) |
| `python main.py gui` | Launch Streamlit configuration dashboard |
| `python main.py card list` | List all cards with nicknames/categories |
| `python main.py card set CARD -n NAME` | Set card nickname |
| `python main.py card remove CARD` | Remove card |
| `python main.py config list` | Show all config values |
| `python main.py config get KEY` | Get a config value |
| `python main.py config set KEY VALUE` | Update a config value |

CLI report commands print to the terminal. Discord notifications are handled by the bot.

### Config Keys

| Key | Default | Description |
|-----|---------|-------------|
| `fetch_interval_hours` | `24` | Auto-fetch interval (`-1` to disable) |
| `daily_report_hour` | `0` | Hour (server local) for daily report (`-1` to disable) |
| `monthly_report_day` | `1` | Day of month for monthly report (`-1` to disable) |
| `etherfi_url` | `https://www.ether.fi/app/cash/safe` | Ether.fi Cash app URL |
| `last_fetch_at` | epoch | Last successful scrape timestamp (auto-managed) |

## Documentation

- [System Setup](docs/SYSTEM_SETUP.md) — full installation and configuration guide
- [Discord Setup](docs/DISCORD_SETUP.md) — Discord bot creation and configuration

## Project Structure

```
├── main.py              # CLI entry point
├── config.py            # Bootstrap: DATABASE_URL, AUTH_STATE_PATH, Discord env vars
├── db.py                # PostgreSQL schema, upsert, card/category CRUD, queries
├── scraper.py           # Playwright scraper + session management
├── csv_import.py        # CSV parser + bulk upsert
├── analytics.py         # Category-aware analytics + report formatters
├── notify.py            # Notification dispatcher (extensible for future channels)
├── bot.py               # Discord bot (slash commands + scheduled tasks)
├── gui.py               # Streamlit configuration dashboard
├── docker-compose.yml   # PostgreSQL + bot services
├── Dockerfile           # Bot container image (includes Playwright + Chromium)
├── .dockerignore        # Excludes .venv, data/, docs/ from Docker build
├── .env.example         # Template for Discord credentials
└── docs/
    ├── SYSTEM_SETUP.md  # Installation guide
    └── DISCORD_SETUP.md # Discord bot setup
```
