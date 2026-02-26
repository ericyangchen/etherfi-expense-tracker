"""Ether.fi Expense Tracker — Configuration Dashboard (Streamlit)"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import streamlit as st
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import config
import db

# ── Page config ────────────────────────────────────────────────────────────

st.set_page_config(page_title="Ether.fi Tracker", page_icon="⬡", layout="wide")

st.markdown(
    """<style>
    .block-container {padding-top:1.5rem; max-width:1100px;}
    div[data-testid="stMetric"] {
        background: color-mix(in srgb, var(--text-color) 8%, transparent);
        padding:0.75rem 1rem;
        border-radius:0.5rem;
        border:1px solid color-mix(in srgb, var(--text-color) 15%, transparent);
    }
    </style>""",
    unsafe_allow_html=True,
)

# ── Boot ───────────────────────────────────────────────────────────────────


@st.cache_resource
def _boot():
    db.init_db()
    db.migrate_seed_cards()
    return True


try:
    _boot()
except Exception as exc:
    st.error(f"**Cannot connect to PostgreSQL** — {exc}")
    st.info("Start the database first: `docker compose up -d postgres`")
    st.stop()


# ── Helpers ────────────────────────────────────────────────────────────────


def _to_local(dt) -> str | None:
    """Convert datetime from DB (UTC) to system local time string."""
    if dt is None:
        return None
    # Normalize to UTC (DB stores TIMESTAMPTZ as UTC; naive = assume UTC)
    try:
        import pandas as _pd
        if isinstance(dt, _pd.Timestamp):
            ts = dt
            if ts.tzinfo is None:
                ts = ts.tz_localize(timezone.utc)
            else:
                ts = ts.tz_convert(timezone.utc)
            local_ts = ts.tz_convert(datetime.now().astimezone().tzinfo)
            return local_ts.strftime("%b %d, %H:%M")
    except Exception:
        pass
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%b %d, %H:%M")
    return str(dt)


def _relative_time(dt: datetime) -> str:
    now = datetime.now().astimezone()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_local = dt.astimezone()
    secs = (now - dt_local).total_seconds()
    if secs < 0:
        return "just now"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _fmt_usd(val) -> str:
    try:
        return f"${float(val):,.2f}"
    except (TypeError, ValueError):
        return str(val)


def _get_session_expiry() -> tuple[str, int]:
    """Read auth_state.json and find the session cookie expiry.

    Returns (label, days_remaining). Negative days = expired.
    """
    try:
        import json as _json

        with open(config.AUTH_STATE_PATH) as f:
            state = _json.load(f)
        now_ts = datetime.now().timestamp()
        session_exp = None
        for cookie in state.get("cookies", []):
            name = cookie.get("name", "")
            exp = cookie.get("expires", -1)
            if name.startswith("session_") and exp > 0:
                session_exp = exp
                break
        if session_exp is None:
            return "Unknown", 0
        days = int((session_exp - now_ts) / 86400)
        if days < 0:
            return "Expired", days
        if days <= 7:
            return f"⚠️ {days}d left", days
        return f"✅ {days}d left", days
    except Exception:
        return "N/A", 0


# ── Dashboard ──────────────────────────────────────────────────────────────


def _page_dashboard():
    cards = db.get_all_cards()
    tx_count = db.get_transaction_count()
    last_fetch = db.get_last_fetch_at()
    auth_ok = os.path.isfile(config.AUTH_STATE_PATH)

    if auth_ok:
        session_label, session_days = _get_session_expiry()
    else:
        session_label, session_days = "❌ Missing", -1

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cards", len(cards))
    c2.metric("Transactions", f"{tx_count:,}")
    c3.metric("Last Fetch", _relative_time(last_fetch))
    c4.metric("Session", session_label)

    if session_days < 0 and auth_ok:
        st.error("Session has expired. Please re-login.")
    elif 0 <= session_days <= 7:
        st.warning(
            f"Session expires in {session_days} day(s). Consider re-logging in soon."
        )

    st.divider()

    a1, a2, _ = st.columns([1, 1, 3])
    with a1:
        if st.button("🔄 Fetch Now", type="primary", width="stretch"):
            with st.spinner("Scraping …"):
                r = subprocess.run(
                    [sys.executable, "main.py", "scrape"],
                    capture_output=True,
                    text=True,
                    cwd=str(ROOT),
                )
            if r.returncode == 0:
                st.success(r.stdout.strip() or "Fetch complete")
                st.cache_resource.clear()
                st.rerun()
            else:
                err = r.stderr.strip() or r.stdout.strip() or "Fetch failed"
                st.error(err)
                if (
                    "session expired" in err.lower()
                    or "no saved session" in err.lower()
                ):
                    st.warning("Please run the login command first (see below).")

    with a2:
        if st.button("🔑 Login", width="stretch"):
            st.info(
                "Run this in your terminal to open the login browser:\n\n"
                f"```bash\ncd {ROOT}\npython main.py login\n```\n\n"
                "Complete the wallet connection, press Enter in the terminal, "
                "then come back here and click **Fetch Now**."
            )

    st.divider()

    st.subheader("Recent Transactions")
    txns = db.get_recent_transactions(25)
    if not txns:
        st.info("No transactions yet. Import a CSV or run a fetch.")
        return

    df = pd.DataFrame(txns)
    if "timestamp" in df.columns:
        df = df.copy()
        df["timestamp"] = df["timestamp"].apply(_to_local)
    display_cols = ["timestamp", "card", "description", "amount_usd", "status"]
    existing = [c for c in display_cols if c in df.columns]
    st.dataframe(
        df[existing],
        column_config={
            "timestamp": "Time",
            "card": "Card",
            "description": "Merchant",
            "amount_usd": st.column_config.NumberColumn("Amount (USD)", format="$%.2f"),
            "status": "Status",
        },
        hide_index=True,
        width="stretch",
    )


# ── Cards ──────────────────────────────────────────────────────────────────


def _page_cards():
    st.subheader("Registered Cards")
    st.caption(
        "Cards are auto-discovered when you import or fetch transactions. "
        "Set nicknames here; manage category assignments on the **Categories** tab."
    )

    cards = db.get_all_cards()
    if not cards:
        st.info("No cards yet.")
        return

    # Header row
    hdr = st.columns([1.2, 2.5, 3, 0.5])
    hdr[0].markdown("**Card**")
    hdr[1].markdown("**Nickname**")
    hdr[2].markdown("**Categories**")
    hdr[3].markdown("")

    for card in cards:
        cols = st.columns([1.2, 2.5, 3, 0.5])
        with cols[0]:
            st.code(card["card"], language=None)
        with cols[1]:
            cur_nick = card.get("nickname") or ""
            new_nick = st.text_input(
                "Nickname",
                value=cur_nick,
                key=f"nick_{card['card']}",
                label_visibility="collapsed",
                placeholder="Set nickname …",
            )
            if new_nick != cur_nick:
                db.upsert_card(card["card"], new_nick or None)
                st.toast(f"Nickname updated for {card['card']}")
                st.rerun()
        with cols[2]:
            cats = db.get_card_categories(card["card"])
            if cats:
                st.markdown(" ".join(f"`{c}`" for c in cats))
            else:
                st.caption("—")
        with cols[3]:
            if st.button("✕", key=f"del_card_{card['card']}"):
                db.delete_card(card["card"])
                st.toast(f"Removed {card['card']}")
                st.rerun()


# ── Categories ─────────────────────────────────────────────────────────────


def _page_categories():
    st.subheader("Categories")
    st.caption(
        "Every card is implicitly in **All**. "
        "Create custom categories to group cards for filtering and reporting."
    )

    # Create form
    with st.form("new_cat", clear_on_submit=True):
        fc1, fc2 = st.columns([3, 1])
        with fc1:
            name = st.text_input(
                "New category",
                label_visibility="collapsed",
                placeholder="Category name …",
            )
        with fc2:
            submitted = st.form_submit_button("➕ Create", width="stretch")
        if submitted and name.strip():
            db.create_category(name.strip())
            st.toast(f"Created **{name.strip()}**")
            st.rerun()

    st.divider()

    categories = db.get_all_categories()
    all_cards = db.get_all_cards()
    all_ids = [c["card"] for c in all_cards]

    if not categories:
        st.info("No categories yet — create one above to start grouping your cards.")
        return

    for cat in categories:
        with st.container(border=True):
            h1, h2 = st.columns([5, 1])
            with h1:
                st.markdown(f"**{cat['name']}**")
            with h2:
                if st.button("🗑️ Delete", key=f"delcat_{cat['name']}"):
                    db.delete_category(cat["name"])
                    st.toast(f"Deleted **{cat['name']}**")
                    st.rerun()

            current = [c["card"] for c in db.get_cards_in_category(cat["name"])]

            selected = st.multiselect(
                "Assigned cards",
                options=all_ids,
                default=current,
                format_func=lambda x: db.get_card_display(x),
                key=f"catcards_{cat['name']}",
                label_visibility="collapsed",
            )

            if set(selected) != set(current):
                if st.button(
                    "💾 Save Changes", key=f"savecat_{cat['name']}", type="primary"
                ):
                    db.set_category_cards(cat["name"], selected)
                    st.toast(f"Updated **{cat['name']}**")
                    st.rerun()


# ── Config ─────────────────────────────────────────────────────────────────


def _page_config():
    st.subheader("Configuration")
    st.caption("All settings are stored in the database and take effect immediately.")

    rows = db.get_all_config()

    _DISABLEABLE = {"fetch_interval_hours", "daily_report_hour", "monthly_report_day"}

    with st.form("cfg"):
        vals: dict[str, str] = {}
        for row in rows:
            k, v = row["key"], row["value"]

            if k in _DISABLEABLE:
                cur_val = int(float(v))
                disabled = cur_val < 0
                defaults = {"fetch_interval_hours": 24, "daily_report_hour": 0, "monthly_report_day": 1}
                limits = {
                    "fetch_interval_hours": (1, 168),
                    "daily_report_hour": (0, 23),
                    "monthly_report_day": (1, 31),
                }
                lo, hi = limits.get(k, (0, 999))
                c_en, c_num = st.columns([1, 3])
                with c_en:
                    enabled = st.checkbox(
                        "Enabled",
                        value=not disabled,
                        key=f"c_{k}_en",
                    )
                with c_num:
                    num = int(
                        st.number_input(
                            k,
                            value=max(lo, min(cur_val, hi)) if cur_val >= 0 else defaults[k],
                            step=1,
                            min_value=lo,
                            max_value=hi,
                            disabled=not enabled,
                            key=f"c_{k}",
                        )
                    )
                vals[k] = str(num) if enabled else "-1"

            elif k == "last_fetch_at":
                st.text_input(k, value=v, disabled=True, key=f"c_{k}")
                vals[k] = v
            elif k == "notify_channels":
                vals[k] = st.text_area(
                    k,
                    value=v,
                    key=f"c_{k}",
                    help="Reserved for future channels. Discord uses the bot.",
                )
            else:
                vals[k] = st.text_input(k, value=v, key=f"c_{k}")

        if st.form_submit_button("💾 Save All", type="primary"):
            n = 0
            for row in rows:
                old = row["value"]
                new = vals.get(row["key"], old)
                if str(new) != str(old):
                    db.set_config(row["key"], str(new))
                    n += 1
            st.toast(f"Saved ({n} changed)" if n else "No changes")


# ── Reports ────────────────────────────────────────────────────────────────


def _page_reports():
    import analytics

    st.subheader("Reports")

    rtype = st.radio("Type", ["Latest", "Daily", "Monthly"], horizontal=True)

    now = datetime.now()
    year, month = now.year, now.month
    if rtype == "Monthly":
        rc1, rc2 = st.columns(2)
        with rc1:
            year = int(
                st.number_input(
                    "Year", value=year, step=1, min_value=2020, max_value=2030
                )
            )
        with rc2:
            month = int(
                st.number_input("Month", value=month, min_value=1, max_value=12, step=1)
            )

    if st.button("📊 Generate", type="primary"):
        with st.spinner("Generating …"):
            if rtype == "Latest":
                txns = db.get_unreported_transactions()
                txt = analytics.format_daily_report(txns, title="Latest Transactions")
                st.session_state["_report_summary"] = None
            elif rtype == "Daily":
                txns = db.get_today_transactions()
                txt = analytics.format_daily_report(
                    txns, title=f"Daily — {datetime.now():%Y/%m/%d}"
                )
                st.session_state["_report_summary"] = None
            else:
                summary = analytics.get_monthly_summary(year, month)
                txt = analytics.format_monthly_report(summary)
                st.session_state["_report_summary"] = summary

            st.session_state["_report"] = txt

    if "_report" in st.session_state and st.session_state["_report"]:
        st.code(st.session_state["_report"], language=None)

        summary = st.session_state.get("_report_summary")
        if summary:
            _monthly_chart(summary)


def _monthly_chart(summary):
    data: list[dict] = []
    seen: set[str] = set()
    for cat in summary.categories:
        for cs in cat.cards:
            if cs.card not in seen:
                label = cs.display_name if cs.display_name != cs.card else cs.card
                data.append({"Card": label, "Spend (USD)": float(cs.total)})
                seen.add(cs.card)
    for cs in summary.uncategorized_cards:
        if cs.card not in seen:
            label = cs.display_name if cs.display_name != cs.card else cs.card
            data.append({"Card": label, "Spend (USD)": float(cs.total)})
            seen.add(cs.card)

    if data:
        st.divider()
        st.caption("Spend by Card")
        df = pd.DataFrame(data)
        st.bar_chart(df, x="Card", y="Spend (USD)")


# ── Main ───────────────────────────────────────────────────────────────────

st.title("⬡ Ether.fi Tracker")

t_dash, t_cards, t_cats, t_cfg, t_reports = st.tabs(
    ["📊 Dashboard", "💳 Cards", "🏷️ Categories", "⚙️ Config", "📋 Reports"]
)

with t_dash:
    _page_dashboard()
with t_cards:
    _page_cards()
with t_cats:
    _page_categories()
with t_cfg:
    _page_config()
with t_reports:
    _page_reports()
