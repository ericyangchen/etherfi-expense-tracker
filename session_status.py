"""Freshness of the saved Ether.fi auth session, read from the storage state.

The `session_*` cookie is an UPPER BOUND on session life, not a promise. The
session saved on 2026-08-12 carried a cookie good until 2026-08-22 but stopped
authenticating on 2026-08-20 — so treat this as an early heads-up only. The
authoritative signal is a scrape failing with "Session expired", which
scraper._is_session_expired derives from a 401 on the cash API.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime

# Ether.fi names its session cookie session_<account-uuid>.
_SESSION_COOKIE_PREFIX = "session_"

WARN_WITHIN_DAYS = 7


def session_seconds_left(state_path: str, now_ts: float | None = None) -> float | None:
    """Seconds until the saved session's cookie expires.

    Returns None when there is nothing to read: no state file, unreadable file,
    or no session cookie carrying an expiry. Negative means already expired.
    """
    if not os.path.isfile(state_path):
        return None
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None

    now = datetime.now().timestamp() if now_ts is None else now_ts
    for cookie in state.get("cookies", []):
        expires = cookie.get("expires", -1)
        if cookie.get("name", "").startswith(_SESSION_COOKIE_PREFIX) and expires > 0:
            return expires - now
    return None


def session_days_left(state_path: str, now_ts: float | None = None) -> int | None:
    """Whole days until expiry, or None if unknown. Negative once expired.

    Floors rather than truncates: a session that died 12 hours ago must read as
    -1, not 0, or callers testing `days < 0` treat a dead session as healthy.
    """
    seconds = session_seconds_left(state_path, now_ts)
    if seconds is None:
        return None
    return math.floor(seconds / 86400)
