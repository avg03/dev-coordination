"""Audit logging for authorization decisions and side-effect executions.

Every PermissionGateway decision and Slack execution produces an audit record.
Records are persisted to Firestore (`audit_logs` collection) when a GCP project
is configured; otherwise they are structured-log only. Secrets are never logged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger("security.audit")

AUDIT_COLLECTION = "audit_logs"


class AuditEvent(BaseModel):
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    agent_id: str = ""
    action: str = ""
    resource: str = ""
    project_id: str = ""
    workflow_id: str = ""
    idempotency_key: str = ""
    decision: str = Field(description="allowed | denied | executed")
    reason: str = ""
    policy: str = ""
    approval_id: Optional[str] = None
    message_id: Optional[str] = None
    event_type: str = Field(default="permission.check")


class AuditSink(Protocol):
    def write(self, event: AuditEvent) -> None: ...


class FirestoreAuditSink:
    """Persists audit events to Firestore. Best-effort: never raises."""

    def __init__(self, project: str, database: Optional[str] = None) -> None:
        self.project = project
        self.database = database or "(default)"
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import firestore

            kwargs: dict = {"project": self.project}
            if self.database and self.database != "(default)":
                kwargs["database"] = self.database
            self._client = firestore.Client(**kwargs)
        return self._client

    def write(self, event: AuditEvent) -> None:
        try:
            self._get_client().collection(AUDIT_COLLECTION).add(event.model_dump(mode="json"))
        except Exception:
            # Auditing must never break the business flow; the structured log
            # below already recorded the decision.
            logger.exception("Failed to persist audit event to Firestore")


class InMemoryAuditSink:
    """Collects audit events in memory — used by tests."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def log_event(event: AuditEvent) -> None:
    """Structured log line — always emitted regardless of sink."""
    if event.decision == "allowed":
        logger.info(
            "permission.check allowed agent=%s action=%s resource=%s project=%s workflow=%s policy=%s",
            event.agent_id,
            event.action,
            event.resource,
            event.project_id,
            event.workflow_id,
            event.policy,
        )
    elif event.decision == "denied":
        logger.warning(
            "permission.denied agent=%s action=%s resource=%s project=%s workflow=%s policy=%s reason=%s",
            event.agent_id,
            event.action,
            event.resource,
            event.project_id,
            event.workflow_id,
            event.policy,
            event.reason,
        )
    else:
        logger.info(
            "%s agent=%s project=%s workflow=%s key=%s reason=%s",
            event.event_type,
            event.agent_id,
            event.project_id,
            event.workflow_id,
            event.idempotency_key,
            event.reason,
        )


class AuditLogger:
    """Facade used by the gateway/executor: log always, persist when a sink exists."""

    def __init__(self, sink: Optional[AuditSink] = None) -> None:
        self.sink = sink

    def record(self, event: AuditEvent) -> None:
        log_event(event)
        if self.sink is not None:
            self.sink.write(event)
