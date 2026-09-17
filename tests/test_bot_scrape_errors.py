"""How the bot reports a failed scrape to Discord.

Playwright timeouts carry their whole retry log in the message — tens of KB.
Measured 2026-09-15..17: posting it verbatim got "400 Bad Request: Must be
4000 or fewer in length", the HTTPException escaped _run_scrape's except
block, the slash command died after defer() with no followup (Discord shows
"The application did not respond"), and the midnight task posted nothing.
"""
import asyncio
from types import SimpleNamespace

import discord
import pytest

import bot as bot_module
import scraper

DISCORD_MESSAGE_LIMIT = 2000

LONG_PLAYWRIGHT_ERROR = (
    "ElementHandle.click: Timeout 30000ms exceeded.\nCall log:\n"
    + "  - retrying click action\n    - waiting 500ms\n" * 1500
)


class RecordingChannel:
    def __init__(self, reject_everything: bool = False):
        self.sent: list[str] = []
        self._reject = reject_everything

    async def send(self, content):
        if self._reject:
            response = SimpleNamespace(status=400, reason="Bad Request")
            raise discord.HTTPException(response, "Invalid Form Body")
        self.sent.append(content)


@pytest.fixture()
def failing_scrape(monkeypatch):
    def _boom():
        raise RuntimeError(LONG_PLAYWRIGHT_ERROR)

    monkeypatch.setattr(scraper, "scrape", _boom)


def _make_bot(channel):
    b = bot_module.EtherfiBot()
    b.channel = channel
    return b


def test_scrape_failure_notice_fits_in_a_discord_message(failing_scrape):
    channel = RecordingChannel()
    b = _make_bot(channel)

    ok = asyncio.run(b._run_scrape())

    assert ok is False
    assert len(channel.sent) == 1
    assert len(channel.sent[0]) <= DISCORD_MESSAGE_LIMIT
    assert "Timeout 30000ms exceeded" in channel.sent[0]


def test_run_scrape_returns_false_when_discord_rejects_the_error_notice(failing_scrape):
    """A notice Discord refuses must not escape and kill the caller."""
    channel = RecordingChannel(reject_everything=True)
    b = _make_bot(channel)

    ok = asyncio.run(b._run_scrape())

    assert ok is False
