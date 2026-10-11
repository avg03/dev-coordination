"""Completion-event publication with outbox fallback.

Ordering contract (enforced by callers in pipeline.py):
  1. persist raw ChangeRecord
  2. run enrichment
  3. persist EnrichedChangeRecord
  4. persist COMPLETED status
  5. publish completion event (this module; outbox on failure)

Firestore and Pub/Sub writes are NOT atomic — hence the outbox.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .models import CommitCompletionEvent, CompletionOutboxEntry
from .stores import OutboxStore

logger = logging.getLogger("coordination.events")


def build_completion_event(
    *,
    repository: str,
    owner: str,
    project_id: str,
    commit_sha: str,
    status: str,
    attempt_count: int = 0,
) -> CommitCompletionEvent:
    from .models import COMPLETION_EVENT_TYPE, FAILED_EVENT_TYPE

    return CommitCompletionEvent(
        event_type=COMPLETION_EVENT_TYPE if status == "COMPLETED" else FAILED_EVENT_TYPE,
        repository=repository,
        owner=owner,
        project_id=project_id,
        commit_sha=commit_sha,
        status=status,  # type: ignore[arg-type]
        attempt_count=attempt_count,
    )


def publish_completion_event(
    event: CommitCompletionEvent,
    publish_fn: Callable[[dict[str, Any], dict[str, str]], Any],
    outbox: OutboxStore | None = None,
    outbox_key: str = "",
) -> bool:
    """Publish one event; on failure persist to outbox for later flush.

    Returns True when published, False when queued in outbox (or dropped
    when no outbox is available — caller must log).
    """
    message, attributes = event.to_pubsub()
    try:
        publish_fn(message, attributes)
        if outbox is not None and outbox_key:
            existing = outbox.get(outbox_key)
            if existing is not None:
                outbox.mark_published(outbox_key)
        return True
    except Exception as exc:
        logger.warning(
            "Completion publish failed for %s@%s — queuing in outbox: %s",
            event.repository,
            event.commit_sha[:8],
            exc,
        )
        if outbox is not None:
            key = outbox_key or f"{event.project_id}#{event.repository}#{event.commit_sha}"
            try:
                entry = outbox.get(key)
                if entry is None:
                    outbox.put(
                        CompletionOutboxEntry(
                            key=key,
                            project_id=event.project_id,
                            repository=event.repository,
                            commit_sha=event.commit_sha,
                            event=event.model_dump(mode="json"),
                        )
                    )
                outbox.record_attempt(key, str(exc))
            except Exception:
                logger.exception("Outbox write failed for %s", key)
        return False


def flush_outbox(
    outbox: OutboxStore,
    publish_fn: Callable[[dict[str, Any], dict[str, str]], Any],
    limit: int = 50,
) -> int:
    """Publish pending outbox entries. Returns count newly published."""
    published = 0
    for entry in outbox.list_pending()[:limit]:
        try:
            event = CommitCompletionEvent.model_validate(entry.event)
        except Exception:
            logger.warning("Dropping invalid outbox entry %s", entry.key)
            outbox.mark_published(entry.key)
            continue
        message, attributes = event.to_pubsub()
        try:
            publish_fn(message, attributes)
            outbox.mark_published(entry.key)
            published += 1
        except Exception as exc:
            outbox.record_attempt(entry.key, str(exc))
    return published
