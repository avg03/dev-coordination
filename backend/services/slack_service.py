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
