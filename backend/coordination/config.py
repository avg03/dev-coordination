"""Configuration for the readiness gate — env-driven with safe defaults.

Follows the project's os.getenv conventions (see github_infrastructure/config.py).
No secrets are read or logged here.
"""

from __future__ import annotations

import os


def pr_wait_timeout_seconds(default: int = 1800) -> int:
    try:
        return max(60, int(os.getenv("PR_WAIT_TIMEOUT_SECONDS", str(default))))
    except ValueError:
        return default


def completion_dedupe_lease_seconds(default: int = 900) -> int:
    try:
        return max(60, int(os.getenv("COMPLETION_DEDUPE_LEASE_SECONDS", str(default))))
    except ValueError:
        return default


def completion_subscription_suffix(default: str = "-completion-sub") -> str:
    return os.getenv("COMPLETION_SUBSCRIPTION_SUFFIX", default)


def pr_dispatch_step_name() -> str:
    return os.getenv("PR_DISPATCH_STEP_NAME", "pr:dispatch")


def completion_dedupe_step_name() -> str:
    return os.getenv("COMPLETION_DEDUPE_STEP_NAME", "commit:completed:dedupe")
