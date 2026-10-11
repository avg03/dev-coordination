"""Pure readiness-gate logic — no Firestore/Pub/Sub clients here.

Keeps the gate unit-testable: callers supply a status store and an
enriched_exists(project_id, sha) callable.
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger("coordination.gate")


def check_readiness(
    status_store: object,
    enriched_exists: Callable[[str, str], bool],
    *,
    project_id: str,
    repository: str,
    required_shas: list[str],
) -> tuple[bool, list[str], list[str]]:
    """Check every required commit — not just the event's commit.

    A commit is ready only when BOTH hold:
      - CommitProcessingRecord exists with status COMPLETED, AND
      - the canonical EnrichedChangeRecord exists (missing enriched despite
        raw record must NOT start PR processing).

    Returns (ready, missing_shas, failed_shas).
    """
    missing: list[str] = []
    failed: list[str] = []
    get = getattr(status_store, "get")

    for sha in required_shas or []:
        try:
            rec = get(project_id, repository, sha)
        except Exception:
            logger.exception("Status lookup failed for %s", sha[:8])
            rec = None
        status = rec.status if rec is not None else None
        if status == "FAILED":
            failed.append(sha)
            missing.append(sha)
            continue
        if status != "COMPLETED":
            missing.append(sha)
            continue
        try:
            if not enriched_exists(project_id, sha):
                # Enriched missing despite COMPLETED status — treat as missing.
                missing.append(sha)
        except Exception:
            logger.exception("Enriched check failed for %s", sha[:8])
            missing.append(sha)

    return (len(missing) == 0, missing, failed)


def classify_commit_error(exc: BaseException) -> tuple[str, str]:
    """Safe (kind, summary) classification — never includes secrets."""
    status_code = getattr(exc, "status_code", None)
    text = f"{type(exc).__name__}: {exc}"[:500]
    if status_code == 404:
        return "permanent", text
    if status_code is not None and isinstance(status_code, int) and 400 <= status_code < 500:
        return "permanent", text
    return "transient", text


def can_produce_partial(available: int, required: int) -> bool:
    """Partial report only when at least one enriched record exists."""
    return required > 0 and 0 < available < required
