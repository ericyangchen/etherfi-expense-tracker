"""Browser launch options for the manual login flow.

Google refuses to sign you in from a browser that advertises automation:
Playwright's bundled Chromium reports navigator.webdriver=true behind a
"HeadlessChrome/145" UA, and the sign-in page answers "This browser or app
may not be secure." Measured on 2026-09-02:

    bundled Chromium, default args : webdriver=True   ua=HeadlessChrome/145...
    real Chrome, switches stripped : webdriver=False  ua=Chrome/152.0.0.0

These tests pin the launch options that produce the second row.
"""
import pytest

import scraper


class FakePlaywright:
    """Stand-in for sync_playwright()'s object; records how launch was called."""

    def __init__(self, fail_channels=()):
        self.calls = []
        self._fail_channels = fail_channels
        self.chromium = self

    def launch(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("channel") in self._fail_channels:
            raise Exception(
                f"BrowserType.launch: Chromium distribution "
                f"'{kwargs['channel']}' is not found at /Applications/..."
            )
        return f"browser<{kwargs.get('channel', 'bundled')}>"


def test_login_launches_real_chrome_not_bundled_chromium():
    """The bundled build is a dead giveaway; the real Chrome binary is not."""
    p = FakePlaywright()
    scraper._launch_login_browser(p)

    assert p.calls[0]["channel"] == "chrome"


def test_login_strips_the_switches_that_set_navigator_webdriver():
    """--enable-automation is what makes navigator.webdriver true."""
    p = FakePlaywright()
    scraper._launch_login_browser(p)

    kwargs = p.calls[0]
    assert "--enable-automation" in kwargs["ignore_default_args"]
    assert "--disable-blink-features=AutomationControlled" in kwargs["args"]


def test_login_browser_is_headed():
    """A headless UA says HeadlessChrome; the user also has to click through."""
    p = FakePlaywright()
    scraper._launch_login_browser(p)

    assert p.calls[0]["headless"] is False


def test_falls_back_to_bundled_chromium_when_chrome_is_not_installed():
    """No Chrome on the machine must degrade, not crash — sign-in may still work."""
    p = FakePlaywright(fail_channels=("chrome",))
    browser = scraper._launch_login_browser(p)

    assert len(p.calls) == 2
    assert "channel" not in p.calls[1]
    assert p.calls[1]["headless"] is False
    assert browser == "browser<bundled>"


def test_fallback_warns_that_sign_in_may_be_blocked(caplog):
    """The user needs to know why Google might reject them."""
    p = FakePlaywright(fail_channels=("chrome",))
    with caplog.at_level("WARNING", logger="scraper"):
        scraper._launch_login_browser(p)

    assert any("chrome" in r.message.lower() for r in caplog.records)
