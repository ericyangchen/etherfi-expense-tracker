"""CSV export through the "Documents & Statements" modal.

Since ~2026-10-09 the arrow-down button on transaction-history no longer
downloads anything: it is a radix dialog trigger. The modal makes you pick a
document type and a period, then "Download CSV", and closes itself after each
download. Periods are calendar-bounded: "Month to date" is the only one that
includes today, and "Last month" is the only way to catch rows from the
previous month that cleared after the 1st. The scraper exports both.

These tests drive a stripped-down copy of that modal in a real browser so the
selectors are exercised, not mocked.
"""
import pytest

import scraper

playwright_api = pytest.importorskip("playwright.sync_api")

HEADER = (
    "timestamp,type,description,status,amount,currency,card,card holder name,"
    "original amount,original currency,cashback earned,cashback currency,"
    "category,spending mode"
)
ROWS = {
    "Last month": [
        "2026-09-30 17:35:41 UTC,card_spend,UBER EATS,CLEARED,23.19,USD,8732,A B,23.19,USD,0.70,USD,Eating,Direct Pay",
        # Straddles the boundary in the site's export; must not come back twice.
        "2026-09-30 23:59:00 UTC,card_spend,BOUNDARY CAFE,CLEARED,4.00,USD,8732,A B,4.00,USD,0.12,USD,Eating,Direct Pay",
    ],
    "Month to date": [
        "2026-10-13 00:30:57 UTC,card_spend,BAREBURGER,PENDING,22.97,USD,8732,A B,22.97,USD,0.69,USD,Eating,Direct Pay",
        "2026-09-30 23:59:00 UTC,card_spend,BOUNDARY CAFE,CLEARED,4.00,USD,8732,A B,4.00,USD,0.12,USD,Eating,Direct Pay",
    ],
}

# Mirrors the live markup captured 2026-10-12: Last month is preselected, and
# the modal unmounts after the download fires.
PAGE = """
<button id="trigger" aria-haspopup="dialog" data-slot="dialog-trigger" type="button">
  <svg class="lucide lucide-arrow-down-to-line"></svg>Export
</button>
<div id="dlg"></div>
<script>
const ROWS = %(rows)s;
const HEADER = %(header)s;
let period = null, docType = null;
document.getElementById('trigger').onclick = () => {
  period = 'Last month'; docType = null;
  document.getElementById('dlg').innerHTML = `
    <div role="dialog" data-state="open">
      <h2>Documents &amp; Statements</h2>
      <button data-doc>Transaction History<span>Export your account and card transactions</span></button>
      <button data-doc>Account statement<span>Balances and Account Activity</span></button>
      <p id="period">Period: ${period}</p>
      <button data-period>Month to date</button>
      <button data-period>Last month</button>
      <button data-period>3 months</button>
      <button id="dl">Download CSV</button>
    </div>`;
  document.querySelectorAll('[data-doc]').forEach(b => b.onclick = () => {
    docType = b.firstChild.textContent;
  });
  document.querySelectorAll('[data-period]').forEach(b => b.onclick = () => {
    period = b.textContent;
    document.getElementById('period').textContent = 'Period: ' + period;
  });
  document.getElementById('dl').onclick = () => {
    if (docType !== 'Transaction History') return;
    const csv = [HEADER, ...ROWS[period]].join('\\n');
    const a = document.createElement('a');
    a.href = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csv);
    a.download = 'transaction-history.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => { document.getElementById('dlg').innerHTML = ''; }, 300);
  };
};
</script>
"""


@pytest.fixture()
def page():
    import json

    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:  # no chromium binary in this environment
            pytest.skip(f"chromium unavailable: {e}")
        pg = browser.new_context(accept_downloads=True).new_page()
        pg.set_content(PAGE % {"rows": json.dumps(ROWS), "header": json.dumps(HEADER)})
        yield pg
        browser.close()


def test_export_downloads_last_month_and_month_to_date(page):
    trigger = page.wait_for_selector("button:has(svg.lucide-arrow-down-to-line)")

    txns = scraper._export_transactions(page, trigger)

    descriptions = sorted(t["description"] for t in txns)
    assert descriptions == ["BAREBURGER", "BOUNDARY CAFE", "UBER EATS"]
