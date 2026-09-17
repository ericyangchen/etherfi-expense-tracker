"""Popup dismissal on the transaction-history page.

Ether.fi's promotional modals are radix dialogs. Since 2026-09-15 the
"Pre-order an iPhone 18" promo renders with NO close button at all — only an
<a> call-to-action — so every button-based dismiss selector misses, the
full-screen overlay intercepts the download click, and the scrape times out.
Radix closes on Escape; that is the fallback these tests pin.
"""
import scraper


class FakeKeyboard:
    def __init__(self, page):
        self._page = page
        self.pressed: list[str] = []

    def press(self, key):
        self.pressed.append(key)
        if key == "Escape":
            self._page.dialog_open = False


class FakeDialog:
    def is_visible(self):
        return True


class FakePage:
    """A page showing a radix dialog that has no dismiss button."""

    def __init__(self, dialog_open: bool = True):
        self.dialog_open = dialog_open
        self.keyboard = FakeKeyboard(self)

    def wait_for_timeout(self, _ms):
        pass

    def query_selector(self, selector):
        if selector == scraper._OPEN_DIALOG_SELECTOR and self.dialog_open:
            return FakeDialog()
        return None


def test_dialog_without_close_button_is_dismissed_with_escape():
    """No selector matches a button-less promo; Escape must close it."""
    page = FakePage(dialog_open=True)

    scraper._dismiss_popups(page)

    assert page.dialog_open is False
    assert "Escape" in page.keyboard.pressed
