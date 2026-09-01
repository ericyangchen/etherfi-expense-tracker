"""Session-freshness math read from the saved Playwright storage state.

This is the early-warning signal, and it failed silently in Aug 2026: the
countdown truncated toward zero, so a session that died 12 hours ago reported
"0 days left" and rendered as a healthy warning instead of an expiry.
"""
import json
import math

import pytest

import session_status

DAY = 86400
NOW = 1_772_000_000.0  # fixed reference point; real clock never enters these tests


def _state(tmp_path, cookies):
    p = tmp_path / "auth_state.json"
    p.write_text(json.dumps({"cookies": cookies}))
    return str(p)


def _session_cookie(expires):
    return {"name": "session_71dcbf81-ba46-46c3-9b7d-d4b24a04005e", "expires": expires}


def test_missing_state_file_reads_as_unknown(tmp_path):
    assert session_status.session_days_left(str(tmp_path / "nope.json"), NOW) is None


def test_unreadable_state_file_reads_as_unknown(tmp_path):
    p = tmp_path / "auth_state.json"
    p.write_text("{not json")
    assert session_status.session_days_left(str(p), NOW) is None


def test_reports_days_until_the_session_cookie_expires(tmp_path):
    path = _state(tmp_path, [_session_cookie(NOW + 90 * DAY)])
    assert session_status.session_days_left(path, NOW) == 90


def test_session_dead_for_half_a_day_is_negative_not_zero(tmp_path):
    """The Aug 2026 bug: int() truncated -0.5 to 0, so an expired session
    displayed as '0d left' and never tripped the expired branch."""
    path = _state(tmp_path, [_session_cookie(NOW - DAY // 2)])

    days = session_status.session_days_left(path, NOW)

    assert days is not None and days < 0


def test_session_expiring_within_the_hour_is_still_positive_zero(tmp_path):
    """Not yet expired must not read as expired — the boundary matters."""
    path = _state(tmp_path, [_session_cookie(NOW + 3600)])

    assert session_status.session_days_left(path, NOW) == 0


def test_ignores_rolling_and_unrelated_cookies(tmp_path):
    """sync_sid is a 30-minute rolling cookie; sync_id outlives the session."""
    path = _state(tmp_path, [
        {"name": "sync_sid", "expires": NOW + 1800},
        {"name": "sync_id", "expires": NOW + 365 * DAY},
        _session_cookie(NOW + 10 * DAY),
    ])

    assert session_status.session_days_left(path, NOW) == 10


def test_session_cookie_without_an_expiry_is_not_a_countdown(tmp_path):
    path = _state(tmp_path, [_session_cookie(-1)])
    assert session_status.session_days_left(path, NOW) is None


def test_seconds_left_is_exposed_for_finer_grained_callers(tmp_path):
    path = _state(tmp_path, [_session_cookie(NOW + 2.5 * DAY)])
    assert session_status.session_seconds_left(path, NOW) == pytest.approx(2.5 * DAY)
