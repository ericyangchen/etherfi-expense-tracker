"""Session-expiry detection for the Ether.fi scraper.

Ether.fi is a SPA: when the saved session dies it does NOT redirect and does
NOT render a "Connect"/"Sign in" button. It 401s on the cash API and paints the
public shell. These tests pin the states captured from the live site on
2026-08-20 with an expired `sync_sid` cookie.
"""
import pytest

import scraper


class FakeResponse:
    def __init__(self, status: int, url: str):
        self.status = status
        self.url = url


class FakePage:
    """Minimal stand-in for playwright's Page for the checks scraper makes."""

    def __init__(self, url: str, body: str, connect_button: bool = False,
                 responses: list[FakeResponse] | None = None):
        self.url = url
        self._body = body
        self._connect_button = connect_button
        self._responses = responses or []
        self._handlers: dict[str, list] = {}

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def load(self):
        """Replay the network traffic a real navigation would produce."""
        for r in self._responses:
            for h in self._handlers.get("response", []):
                h(r)

    def inner_text(self, _selector):
        return self._body

    def query_selector(self, _selector):
        if self._connect_button:
            return type("El", (), {"is_visible": lambda self: True})()
        return None


# Body text captured from the live logged-out shell.
LOGGED_OUT_BODY = (
    "Membership\nHome\nPortfolio\nCards\nEarn\nMarkets\nBorrow\nTransactions\n"
    "Promotions\nTravel\nRefer & Earn\nGet Started\nTransactions\n"
    "No transactions yet\nBecome a Member"
)

# Body text for a live session: real rows and the export control are present.
LOGGED_IN_BODY = (
    "Membership\nHome\nPortfolio\nCards\nEarn\nMarkets\nBorrow\nTransactions\n"
    "Promotions\nTravel\nRefer & Earn\nTransactions\n"
    "Jul 14, 2026\nUEP*CHI CHICKEN\n-$187.16\nCard ending 8732"
)

URL = "https://www.ether.fi/app/cash/transaction-history"


def test_expired_session_detected_from_api_401():
    """The definitive signal: /api/v2/users/me returns 401 for a dead session."""
    page = FakePage(URL, LOGGED_OUT_BODY, responses=[
        FakeResponse(200, "https://www.ether.fi/app/cash/transaction-history"),
        FakeResponse(401, "https://www.ether.fi/app/cash/api/v2/users/me"),
    ])
    failures = scraper._watch_auth_failures(page)
    page.load()

    assert scraper._is_session_expired(page, failures) is True


def test_expired_session_detected_from_logged_out_shell_without_network():
    """Fallback: no API traffic observed, but the public shell is on screen."""
    page = FakePage(URL, LOGGED_OUT_BODY)
    failures = scraper._watch_auth_failures(page)
    page.load()

    assert scraper._is_session_expired(page, failures) is True


def test_live_session_not_flagged():
    """A working session must never be reported as expired."""
    page = FakePage(URL, LOGGED_IN_BODY, responses=[
        FakeResponse(200, "https://www.ether.fi/app/cash/api/v2/users/me"),
        FakeResponse(200, "https://www.ether.fi/app/cash/api/v2/transactions"),
    ])
    failures = scraper._watch_auth_failures(page)
    page.load()

    assert scraper._is_session_expired(page, failures) is False


def test_non_auth_error_does_not_trip_detection():
    """A 500 on an unrelated endpoint is not an authentication failure."""
    page = FakePage(URL, LOGGED_IN_BODY, responses=[
        FakeResponse(500, "https://www.ether.fi/app/cash/api/v2/promotions"),
        FakeResponse(401, "https://widget.intercom.io/api/ping"),
    ])
    failures = scraper._watch_auth_failures(page)
    page.load()

    assert scraper._is_session_expired(page, failures) is False


def test_redirect_to_login_still_detected():
    """The original heuristic must keep working if ether.fi ever redirects."""
    page = FakePage("https://www.ether.fi/login", "", connect_button=True)
    failures = scraper._watch_auth_failures(page)

    assert scraper._is_session_expired(page, failures) is True


def test_merchant_name_does_not_look_like_a_logged_out_shell():
    """"sign in" is a substring of "DESIGN INC" — markers must not false-positive."""
    body = LOGGED_IN_BODY + "\nJul 15, 2026\nDESIGN INC\n-$42.00\nConnect Four Cafe"
    page = FakePage(URL, body, responses=[
        FakeResponse(200, "https://www.ether.fi/app/cash/api/v2/users/me"),
    ])
    failures = scraper._watch_auth_failures(page)
    page.load()

    assert scraper._is_session_expired(page, failures) is False
