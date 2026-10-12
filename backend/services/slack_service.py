from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger("services.slack")

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=ENV_FILE, override=False)


def _read_env(name: str, default: str = "") -> str:
    load_dotenv(dotenv_path=ENV_FILE, override=False)
    return os.getenv(name, default)


SLACK_BOT_TOKEN = _read_env("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = _read_env("SLACK_SIGNING_SECRET", "")


def _get_client():
    token = _read_env("SLACK_BOT_TOKEN", "")
    if not token:
        return None
    try:
        from slack_sdk import WebClient

        return WebClient(token=token)
    except ImportError:
        logger.warning("slack_sdk not installed, falling back to mock")
        return None


def send_slack_message(
    channel: str,
    text: str,
    username: str | None = None,
    icon_emoji: str | None = None,
) -> dict[str, Any]:
    """Send message via Slack API with mock fallback.

    Returns dict with status `sent` (real) or `mock_sent` (fallback).
    """
    if not channel:
        logger.warning("No channel provided, mock fallback")
        return {"status": "mock_sent", "channel": channel, "ok": True, "mock": True}

    client = _get_client()
    if client is None:
        logger.info("[MOCK SLACK] %s: %s", channel, text[:120].replace("\n", " "))
        return {"status": "mock_sent", "channel": channel, "ok": True, "mock": True, "text_preview": text[:120]}

    try:
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if username:
            kwargs["username"] = username
        if icon_emoji:
            kwargs["icon_emoji"] = icon_emoji
        resp = client.chat_postMessage(**kwargs)
        if resp.get("ok"):
            logger.info("Slack message sent to %s ts=%s", channel, resp.get("ts"))
            return {"status": "sent", "channel": channel, "ok": True, "ts": resp.get("ts"), "response": resp.data}
        logger.warning("Slack API returned not ok: %s", resp)
        return {"status": "error", "channel": channel, "ok": False, "error": str(resp)}
    except Exception as exc:
        logger.exception("Slack send failed for %s, falling back to mock: %s", channel, exc)
        return {
            "status": "mock_sent",
            "channel": channel,
            "ok": True,
            "mock": True,
            "fallback_error": str(exc),
            "text_preview": text[:120],
        }


def check_slack_connection() -> dict[str, Any]:
    """Verify Slack token via auth.test. Returns status dict."""
    client = _get_client()
    if client is None:
        return {"ok": False, "error": "No SLACK_BOT_TOKEN or slack_sdk missing", "mock": True}
    try:
        resp = client.auth_test()
        return {"ok": bool(resp.get("ok")), "team": resp.get("team"), "user": resp.get("user"), "response": resp.data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


class ChannelThrottler:
    """Per-channel pacing for Slack sends (chat.postMessage is rate-limited).

    Sequential sends to the SAME channel wait until `delay_seconds` elapsed
    since the previous send; different channels are independent. Uses a
    monotonic clock so system time changes cannot collapse the gap.
    """

    def __init__(self, delay_seconds: float = 1.0) -> None:
        import time

        self._time = time
        try:
            self.delay = max(0.0, float(delay_seconds))
        except (TypeError, ValueError):
            self.delay = 1.0
        self._last_sent: dict[str, float] = {}

    def wait(self, channel: str) -> None:
        if not self.delay or not channel:
            return
        now = self._time.monotonic()
        last = self._last_sent.get(channel)
        if last is not None:
            remaining = self.delay - (now - last)
            if remaining > 0:
                self._time.sleep(remaining)
                now = self._time.monotonic()
        self._last_sent[channel] = now


def send_delay_seconds(default: float = 1.0) -> float:
    try:
        return max(0.0, float(os.getenv("SLACK_SEND_DELAY_SECONDS", str(default))))
    except ValueError:
        return default
