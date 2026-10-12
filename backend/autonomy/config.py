"""Autonomy configuration — env-driven, conservative defaults.

Conservative initial values: only mapped awareness messages with high
confidence and multiple evidence items may auto-send; action impacts are
human-only until AUTONOMY_ALLOW_ACTION is deliberately enabled. Thresholds
REQUIRE evaluation against labeled scenarios before relaxing (see
backend/tests/test_autonomy.py).

  AUTONOMY_ENABLED=false       master switch (default off = today's manual flow)
  AUTONOMY_DRY_RUN=false       evaluate + persist + audit, never send
  AUTONOMY_MIN_CONFIDENCE=0.8  minimum impact confidence for AUTO_SEND
  AUTONOMY_MIN_EVIDENCE=2      minimum evidence items for AUTO_SEND
  AUTONOMY_ALLOW_ACTION=false  whether action-impact (non-approval) may auto-send
  AUTONOMY_MAX_AUTO_PER_PR=10  cap on auto-sends per PR (spam guard)
"""

from __future__ import annotations

import os


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return _flag("AUTONOMY_ENABLED", False)


def dry_run() -> bool:
    return _flag("AUTONOMY_DRY_RUN", False)


def min_confidence() -> float:
    return _float("AUTONOMY_MIN_CONFIDENCE", 0.8)


def min_evidence() -> int:
    return _int("AUTONOMY_MIN_EVIDENCE", 2)


def allow_action() -> bool:
    return _flag("AUTONOMY_ALLOW_ACTION", False)


def max_auto_per_pr() -> int:
    return _int("AUTONOMY_MAX_AUTO_PER_PR", 10)


POLICY_VERSION = "v1"

# Case-insensitive substrings forcing HUMAN_REVIEW regardless of confidence.
SECURITY_KEYWORDS = (
    "secur", "auth", "password", "secret", "token", "credential",
    "vuln", "cve", "exploit", "permission", "oauth", "encrypt",
    "private key", "apikey", "api key",
)
