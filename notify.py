"""Notification dispatcher — extensible channel system. Bot handles Discord; add handlers here for other channels (e.g. email, Telegram)."""

from __future__ import annotations

import json

import db


# ---------------------------------------------------------------------------
# Channel implementations (Discord is handled by the bot directly)
# ---------------------------------------------------------------------------

_CHANNELS: dict = {
    # Future: "telegram": _send_telegram, "email": _send_email, etc.
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def send(message: str) -> bool:
    """
    Send a notification through all enabled channels.
    Config is read from DB key 'notify_channels'.
    Returns True if at least one channel succeeded.
    """
    try:
        raw = db.get_config("notify_channels")
        channels = json.loads(raw)
    except (KeyError, json.JSONDecodeError):
        print("[notify] No notify_channels configured, skipping.")
        return False

    if not channels:
        print("[notify] notify_channels is empty, skipping.")
        return False

    success = False
    for ch in channels:
        channel_type = ch.get("type", "")
        handler = _CHANNELS.get(channel_type)
        if handler is None:
            print(f"[notify] Unknown channel type: {channel_type}")
            continue
        if handler(message, ch):
            success = True

    return success
