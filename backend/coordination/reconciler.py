"""Reconciliation: recover lost completion events and interrupted dispatches.

Correctness never depends solely on receiving a completion event:
  - PRWorkflow persists WAITING_FOR_COMMITS before the initial check, then
    rechecks authoritative state (post-registration recheck in pr_workflow).
  - This module periodically (or operationally) finds waiting PRs whose
    dependencies are already complete, plus dispatches stuck in PROCESSING
    past their lease, plus outbox entries pending publication.

All functions take explicit stores/callbacks so unit tests use in-memory
fakes and production wires Firestore + real dispatch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from .config import pr_wait_timeout_seconds
from .gate import can_produce_partial, check_readiness

logger = logging.getLogger("coordination.reconciler")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _past_deadline(record: object, now: datetime | None = None) -> bool:
    now = now or _now()
    deadline = getattr(record, "deadline_at", None)
    if not deadline:
        return False
    try:
        return datetime.fromisoformat(str(deadline)) <= now
    except ValueError:
        return False


def reconcile_waiting(
    pr_store: object,
    status_store: object,
    enriched_exists: Callable[[str, str], bool],
    dispatch: Callable[[object], bool],
    *,
    timeout_seconds: int | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """One reconciliation pass over WAITING_FOR_COMMITS workflows.

    dispatch(record) must atomically claim (idempotency) and schedule/run
    the PR pipeline; return True when the PR was claimed for execution.
    Returns counters for observability/tests.
    """
    now = now or _now()
    timeout = timeout_seconds if timeout_seconds is not None else pr_wait_timeout_seconds()
    stats = {"checked": 0, "dispatched": 0, "still_waiting": 0, "timed_out": 0, "failed": 0}

    try:
        waiting = pr_store.list_waiting()  # type: ignore[attr-defined]
    except Exception:
        logger.exception("Reconciler: list_waiting failed")
        return stats

    for record in waiting:
        stats["checked"] += 1
        try:
            ready, missing, failed = check_readiness(
                status_store,
                enriched_exists,
                project_id=record.project_id,
                repository=record.repo_full,
                required_shas=list(record.required_shas or []),
            )
        except Exception:
            logger.exception("Reconciler: readiness check failed for PR#%s", getattr(record, "pr_number", "?"))
            continue

        if ready:
            try:
                if dispatch(record):
                    stats["dispatched"] += 1
                else:
                    stats["still_waiting"] += 1
            except Exception:
                logger.exception("Reconciler: dispatch failed for PR#%s", getattr(record, "pr_number", "?"))
            continue

        # Not ready — timeout?
        if _past_deadline(record, now):
            stats["timed_out"] += 1
            _handle_timeout(pr_store, record, missing, failed, dispatch)
        else:
            # Update missing set so operators can observe progress.
            try:
                record.missing_shas = list(missing)
                record.failed_shas = list(failed)
                record.reconcile_count = int(getattr(record, "reconcile_count", 0)) + 1
                pr_store.put(record)  # type: ignore[attr-defined]
            except Exception:
                logger.exception("Reconciler: state update failed for PR#%s", getattr(record, "pr_number", "?"))
            stats["still_waiting"] += 1

    return stats


def _handle_timeout(
    pr_store: object,
    record: object,
    missing: list[str],
    failed: list[str],
    dispatch: Callable[[object], bool],
) -> None:
    """Deadline reached: rechecked already by caller. Partial when meaningful,
    else terminal FAILED. Never silently treat missing as no-changes."""
    from datetime import timezone as _tz

    available = len(list(getattr(record, "required_shas", []) or [])) - len(missing)
    required = len(list(getattr(record, "required_shas", []) or []))
    record.missing_shas = list(missing)  # type: ignore[attr-defined]
    record.failed_shas = list(failed)  # type: ignore[attr-defined]
    record.error = (  # type: ignore[attr-defined]
        f"PR wait timeout: {len(missing)} of {required} commits missing "
        f"({','.join(s[:8] for s in missing[:5])}); failed=[{','.join(s[:8] for s in failed[:5])}]"
    )
    if can_produce_partial(available, required):
        # Caller (PRWorkflow timeout path) runs the pipeline with available
        # data and marks PARTIAL. Here we only flag; execution happens via
        # dispatch so external side effects stay approval-gated.
        record.partial = True  # type: ignore[attr-defined]
        record.status = "READY"  # type: ignore[attr-defined]
        try:
            pr_store.put(record)  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Reconciler: timeout READY persist failed")
            return
        try:
            dispatch(record)
        except Exception:
            logger.exception("Reconciler: partial dispatch failed")
    else:
        record.status = "FAILED"  # type: ignore[attr-defined]
        record.updated_at = datetime.now(_tz.utc).isoformat()  # type: ignore[attr-defined]
        try:
            pr_store.put(record)  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Reconciler: timeout FAILED persist failed")
