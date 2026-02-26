"""Discord bot — slash commands + scheduled tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks

import config
import db
import analytics

log = logging.getLogger("etherfi.bot")


class EtherfiBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.channel: discord.TextChannel | None = None
        self.tree = app_commands.CommandTree(self)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        """Register slash commands."""
        self.tree.add_command(cmd_report_latest)
        self.tree.add_command(cmd_report_daily)
        self.tree.add_command(cmd_report_monthly)

    async def on_ready(self) -> None:
        log.info(f"Bot ready as {self.user}")
        db.init_db()
        db.migrate_seed_cards()

        ch = self.get_channel(config.DISCORD_CHANNEL_ID)
        if ch and isinstance(ch, discord.TextChannel):
            self.channel = ch
            log.info(f"Bound to #{ch.name} ({ch.id})")
        else:
            log.warning(f"Channel {config.DISCORD_CHANNEL_ID} not found")

        try:
            if self.channel:
                guild = self.channel.guild
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info(f"Synced {len(synced)} slash command(s) to guild (instant)")
            else:
                synced = await self.tree.sync()
                log.info(
                    f"Synced {len(synced)} slash command(s) globally (may take up to 1h)"
                )
        except Exception as e:
            log.error(f"Slash command sync failed: {e}")

        if not self.auto_fetch.is_running():
            self.auto_fetch.start()
        if not self.daily_report_task.is_running():
            self.daily_report_task.start()
        if not self.monthly_report_task.is_running():
            self.monthly_report_task.start()

    # ------------------------------------------------------------------
    # Scheduled tasks
    # ------------------------------------------------------------------

    @tasks.loop(minutes=30)
    async def auto_fetch(self) -> None:
        """Scrape data every fetch_interval_hours. Set to -1 to disable."""
        try:
            interval = db.get_fetch_interval_hours()
            if interval < 0:
                return

            last = db.get_last_fetch_at()
            now = datetime.now(timezone.utc)
            elapsed = (now - last).total_seconds() / 3600

            if elapsed < interval:
                return

            log.info(f"Auto-fetch: {elapsed:.1f}h since last fetch")
            await self._run_scrape()

            await self._check_session_expiry()
        except Exception as e:
            log.error(f"Auto-fetch error: {e}")

    @tasks.loop(minutes=1)
    async def daily_report_task(self) -> None:
        """At midnight (or configured hour), report yesterday's transactions."""
        try:
            report_hour = int(db.get_config("daily_report_hour"))
            if report_hour < 0:
                return

            now = datetime.now()
            if now.hour != report_hour or now.minute != 0:
                return

            if not self.channel:
                return

            # Scrape first to get latest data, then report yesterday
            await self._run_scrape()
            yesterday = now.date() - timedelta(days=1)
            txns = db.get_transactions_for_date(
                yesterday.year, yesterday.month, yesterday.day
            )
            if not txns:
                return

            date_str = yesterday.strftime("%Y/%m/%d")
            log.info(f"Daily report: {len(txns)} transactions for {date_str}")
            report = analytics.format_daily_report(
                txns, title=f"Ether.fi Daily Report - {date_str}"
            )
            await self._send_long(self.channel, report)
        except Exception as e:
            log.error(f"Daily report error: {e}")

    @tasks.loop(hours=1)
    async def monthly_report_task(self) -> None:
        """On 1st at midnight, report previous month's summary."""
        try:
            report_day = int(db.get_config("monthly_report_day"))
            if report_day < 0:
                return

            now = datetime.now()
            if now.day != report_day or now.hour != 0:
                return

            if now.month == 1:
                year, month = now.year - 1, 12
            else:
                year, month = now.year, now.month - 1

            if not self.channel:
                return

            await self._run_scrape()
            log.info(f"Monthly report for {year}/{month:02d}")
            summary = analytics.get_monthly_summary(year, month)
            report = analytics.format_monthly_report(summary)
            await self._send_long(self.channel, report)
        except Exception as e:
            log.error(f"Monthly report error: {e}")

    @auto_fetch.before_loop
    @daily_report_task.before_loop
    @monthly_report_task.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _check_session_expiry(self) -> None:
        """Warn in Discord if the auth session is expiring soon."""
        if not self.channel or not os.path.isfile(config.AUTH_STATE_PATH):
            return
        try:
            with open(config.AUTH_STATE_PATH) as f:
                state = json.load(f)
            now_ts = datetime.now().timestamp()
            for cookie in state.get("cookies", []):
                if (
                    cookie.get("name", "").startswith("session_")
                    and cookie.get("expires", -1) > 0
                ):
                    days_left = int((cookie["expires"] - now_ts) / 86400)
                    if days_left <= 7:
                        await self.channel.send(
                            f"⚠️ Ether.fi session expires in **{days_left} day(s)**. "
                            "Run `python main.py login` to refresh."
                        )
                    return
        except Exception:
            pass

    async def _run_scrape(self) -> bool:
        """Run the scraper in a thread. Returns True on success, False on failure (sends noti)."""
        try:
            import scraper

            txns = await asyncio.to_thread(scraper.scrape)
            if txns:
                affected = db.upsert_transactions(txns)
                log.info(f"Scraped {len(txns)} txns ({affected} new/updated)")
            db.update_last_fetch_at()
            return True
        except Exception as e:
            log.error(f"Scrape failed: {e}")
            if self.channel:
                await self.channel.send(f"Scrape error: {e}")
            return False

    async def _send_long(self, channel: discord.abc.Messageable, text: str) -> None:
        """Send a message, splitting into chunks if needed (Discord 2000 char limit)."""
        wrapped = f"```\n{text}\n```"
        if len(wrapped) <= 2000:
            await channel.send(wrapped)
            return

        chunks = []
        lines = text.split("\n")
        current = ""
        for line in lines:
            if len(current) + len(line) + 10 > 1900:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current.strip():
            chunks.append(current)

        for chunk in chunks:
            await channel.send(f"```\n{chunk}```")

    async def _send_long_followup(
        self, interaction: discord.Interaction, text: str
    ) -> None:
        """Send long text via slash command followup (dismisses 'thinking' state)."""
        wrapped = f"```\n{text}\n```"
        if len(wrapped) <= 2000:
            await interaction.followup.send(wrapped)
            return

        chunks = []
        lines = text.split("\n")
        current = ""
        for line in lines:
            if len(current) + len(line) + 10 > 1900:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current.strip():
            chunks.append(current)

        await interaction.followup.send(f"```\n{chunks[0]}```")
        for chunk in chunks[1:]:
            await interaction.channel.send(f"```\n{chunk}```")


# ---------------------------------------------------------------------------
# Slash command handlers (need bot ref from interaction.client)
# ---------------------------------------------------------------------------


async def _check_channel(interaction: discord.Interaction, bot: EtherfiBot) -> bool:
    """Return False if channel is wrong, after sending ephemeral message."""
    if bot.channel and interaction.channel_id != bot.channel.id:
        await interaction.response.send_message(
            "Use this command in the configured tracker channel.",
            ephemeral=True,
        )
        return False
    return True


@app_commands.command(
    name="report_latest", description="Fetch and report unreported transactions"
)
async def cmd_report_latest(interaction: discord.Interaction) -> None:
    bot = interaction.client
    if not await _check_channel(interaction, bot):
        return
    await interaction.response.defer()

    if not await bot._run_scrape():
        await interaction.followup.send(
            "Scrape failed (see message above). No new transactions reported."
        )
        return
    txns = db.get_unreported_transactions()
    if not txns:
        await interaction.followup.send("No new transactions to report.")
        return

    report = analytics.format_daily_report(txns, title="Ether.fi Latest Report")
    db.mark_as_reported([t["id"] for t in txns])
    await bot._send_long_followup(interaction, report)


@app_commands.command(
    name="report_daily", description="Fetch and report daily transactions"
)
@app_commands.describe(
    year="Year (e.g. 2026). Omit for today.",
    month="Month 1–12. Omit for today.",
    day="Day of month. Omit for today.",
)
async def cmd_report_daily(
    interaction: discord.Interaction,
    year: app_commands.Range[int, 2020, 2030] | None = None,
    month: app_commands.Range[int, 1, 12] | None = None,
    day: app_commands.Range[int, 1, 31] | None = None,
) -> None:
    bot = interaction.client
    if not await _check_channel(interaction, bot):
        return
    await interaction.response.defer()

    if not await bot._run_scrape():
        await interaction.followup.send(
            "Scrape failed (see message above). No daily report."
        )
        return
    if year is not None and month is not None and day is not None:
        txns = db.get_transactions_for_date(year, month, day)
        date_str = f"{year}/{month:02d}/{day:02d}"
    else:
        txns = db.get_today_transactions()
        date_str = datetime.now().strftime("%Y/%m/%d")
    if not txns:
        await interaction.followup.send(
            f"No transactions for {date_str}."
        )
        return

    report = analytics.format_daily_report(
        txns, title=f"Ether.fi Daily Report - {date_str}"
    )
    await bot._send_long_followup(interaction, report)


@app_commands.command(name="report_monthly", description="Monthly expense summary")
@app_commands.describe(
    year="Year (e.g. 2026). Omit for current month.",
    month="Month 1–12. Omit for current month.",
    top="Number of top merchants to show (default 10).",
)
async def cmd_report_monthly(
    interaction: discord.Interaction,
    year: app_commands.Range[int, 2020, 2030] | None = None,
    month: app_commands.Range[int, 1, 12] | None = None,
    top: app_commands.Range[int, 1, 50] = 10,
) -> None:
    bot = interaction.client
    if not await _check_channel(interaction, bot):
        return
    await interaction.response.defer()

    summary = analytics.get_monthly_summary(year, month, top_limit=top)
    report = analytics.format_monthly_report(summary)
    await bot._send_long_followup(interaction, report)


def run_bot() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not config.DISCORD_BOT_TOKEN:
        print("Error: DISCORD_BOT_TOKEN not set. Set it in .env or environment.")
        return
    if not config.DISCORD_CHANNEL_ID:
        print("Error: DISCORD_CHANNEL_ID not set. Set it in .env or environment.")
        return

    bot = EtherfiBot()
    bot.run(config.DISCORD_BOT_TOKEN)
