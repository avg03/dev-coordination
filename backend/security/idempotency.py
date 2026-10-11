"""Step-level idempotency backed by Firestore.

Idempotency operates at the workflow/business-step level (e.g. impact:analyze,
slack:send) — not on every internal LLM request. Keys are deterministic hashes
of (project_id, workflow_id, step_name, input fingerprint, step_version), so a
changed input always produces a different key.

Lifecycle:
  claim (atomic create-if-absent) -> execute -> persist result ->
  complete(key, result) -> return. COMPLETED is only ever set AFTER the result
  has been durably persisted (in the record itself and/or the domain store).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger("security.idempotency")

IDEMPOTENCY_COLLECTION = "idempotency_keys"
DEFAULT_LEASE_SECONDS = 900
DEFAULT_STEP_VERSION = "v1"


class IdempotencyRecord(BaseModel):
    key: str
    workflow_id: str = ""
    project_id: str = ""
    step_name: str = ""
    step_version: str = DEFAULT_STEP_VERSION
    status: Literal["IN_PROGRESS", "COMPLETED", "FAILED"] = "IN_PROGRESS"
    input_hash: str = ""
    result: Optional[dict[str, Any]] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: Optional[str] = None
    attempt_count: int = 1
    error: Optional[str] = None
    lease_until: Optional[str] = None


class ClaimStatus:
    EXECUTE = "EXECUTE"  # caller should execute the step (fresh or reclaimed)
    COMPLETED = "COMPLETED"  # already done — use record.result
    IN_PROGRESS = "IN_PROGRESS"  # another worker holds a valid lease


class ClaimOutcome(BaseModel):
    status: str
    record: Optional[IdempotencyRecord] = None

    @property
    def result(self) -> Optional[dict[str, Any]]:
        return self.record.result if self.record else None


class IdempotencyStore(Protocol):
    def get(self, key: str) -> Optional[IdempotencyRecord]: ...
    def create_if_absent(self, record: IdempotencyRecord) -> bool: ...
    def update(self, record: IdempotencyRecord) -> None: ...


class InMemoryIdempotencyStore:
    """Thread-safe in-memory store — used by tests and as the reference semantics."""

    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[IdempotencyRecord]:
        with self._lock:
            record = self._records.get(key)
            return IdempotencyRecord.model_validate(record.model_dump(mode="json")) if record else None

    def create_if_absent(self, record: IdempotencyRecord) -> bool:
        with self._lock:
            if record.key in self._records:
                return False
            self._records[record.key] = record
            return True

    def update(self, record: IdempotencyRecord) -> None:
        with self._lock:
            self._records[record.key] = record


class FirestoreIdempotencyStore:
    """Firestore-backed store. Claims are atomic via transaction create-if-absent."""

    def __init__(self, project: str, database: Optional[str] = None) -> None:
        self.project = project
        self.database = database or os.getenv("FIRESTORE_DATABASE", "(default)")
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import firestore

            kwargs: dict = {"project": self.project}
            if self.database and self.database != "(default)":
                kwargs["database"] = self.database
            self._client = firestore.Client(**kwargs)
        return self._client

    def _to_record(self, data: dict[str, Any]) -> IdempotencyRecord:
        return IdempotencyRecord.model_validate(data)

    def get(self, key: str) -> Optional[IdempotencyRecord]:
        snap = self._get_client().collection(IDEMPOTENCY_COLLECTION).document(key).get()
        if not snap.exists:
            return None
        return self._to_record(snap.to_dict() or {})

    def create_if_absent(self, record: IdempotencyRecord) -> bool:
        client = self._get_client()

        @firestore.transactional
        def _txn(transaction: Any) -> bool:
            ref = client.collection(IDEMPOTENCY_COLLECTION).document(record.key)
            snapshot = transaction.get(ref)
            if snapshot.exists:
                return False
            transaction.set(ref, record.model_dump(mode="json"))
            return True

        transaction = client.transaction()
        return _txn(transaction)

    def update(self, record: IdempotencyRecord) -> None:
        self._get_client().collection(IDEMPOTENCY_COLLECTION).document(record.key).set(
            record.model_dump(mode="json")
        )


class IdempotencyManager:
    def __init__(self, store: IdempotencyStore, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        self.store = store
        self.lease_seconds = lease_seconds

    # -- key generation -----------------------------------------------------

    @staticmethod
    def build_key(
        project_id: str,
        workflow_id: str,
        step_name: str,
        input_payload: Any,
        step_version: str = DEFAULT_STEP_VERSION,
    ) -> str:
        """Deterministic key: any change to the input changes the key."""
        fingerprint = json.dumps(
            {
                "project_id": project_id,
                "workflow_id": workflow_id,
                "step_name": step_name,
                "step_version": step_version,
                "input": _canonical(input_payload),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return "idem_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    @staticmethod
    def input_hash(input_payload: Any) -> str:
        return hashlib.sha256(
            json.dumps(_canonical(input_payload), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()

    # -- lifecycle ----------------------------------------------------------

    def claim(
        self,
        key: str,
        *,
        workflow_id: str = "",
        project_id: str = "",
        step_name: str = "",
        step_version: str = DEFAULT_STEP_VERSION,
        input_payload: Any = None,
    ) -> ClaimOutcome:
        """Atomically claim a step execution. Never marks COMPLETED here."""
        now = datetime.now(timezone.utc)
        lease_until = (now + timedelta(seconds=self.lease_seconds)).isoformat()

        existing = self.store.get(key)

        if existing is None:
            record = IdempotencyRecord(
                key=key,
                workflow_id=workflow_id,
                project_id=project_id,
                step_name=step_name,
                step_version=step_version,
                status="IN_PROGRESS",
                input_hash=self.input_hash(input_payload) if input_payload is not None else "",
                lease_until=lease_until,
            )
            if self.store.create_if_absent(record):
                logger.info("idempotency.claimed key=%s step=%s workflow=%s", key, step_name, workflow_id)
                return ClaimOutcome(status=ClaimStatus.EXECUTE, record=record)
            existing = self.store.get(key)
            if existing is None:  # extremely unlikely — treat as claimed
                return ClaimOutcome(status=ClaimStatus.EXECUTE, record=record)

        if existing.status == "COMPLETED":
            logger.info("idempotency.hit key=%s step=%s workflow=%s", key, existing.step_name, existing.workflow_id)
            return ClaimOutcome(status=ClaimStatus.COMPLETED, record=existing)

        if existing.status == "IN_PROGRESS":
            if _lease_valid(existing, now):
                logger.info(
                    "idempotency.in_progress key=%s step=%s lease_until=%s",
                    key,
                    existing.step_name,
                    existing.lease_until,
                )
                return ClaimOutcome(status=ClaimStatus.IN_PROGRESS, record=existing)
            # Expired lease — crashed worker. Reclaim with a fresh lease.
            existing.status = "IN_PROGRESS"
            existing.lease_until = lease_until
            existing.attempt_count += 1
            self.store.update(existing)
            logger.info(
                "idempotency.reclaimed key=%s step=%s attempt=%s", key, existing.step_name, existing.attempt_count
            )
            return ClaimOutcome(status=ClaimStatus.EXECUTE, record=existing)

        # FAILED — retry allowed per policy: reset to IN_PROGRESS with new lease.
        existing.status = "IN_PROGRESS"
        existing.error = None
        existing.lease_until = lease_until
        existing.started_at = now.isoformat()
        existing.attempt_count += 1
        self.store.update(existing)
        logger.info("idempotency.retry key=%s step=%s attempt=%s", key, existing.step_name, existing.attempt_count)
        return ClaimOutcome(status=ClaimStatus.EXECUTE, record=existing)

    def complete(self, key: str, result: dict[str, Any]) -> None:
        """Mark COMPLETED — call only after the result is durably persisted."""
        record = self.store.get(key)
        if record is None:
            logger.error("idempotency.complete called for unknown key=%s", key)
            return
        record.status = "COMPLETED"
        record.result = result
        record.completed_at = datetime.now(timezone.utc).isoformat()
        record.error = None
        self.store.update(record)
        logger.info("idempotency.completed key=%s step=%s", key, record.step_name)

    def fail(self, key: str, error: str) -> None:
        record = self.store.get(key)
        if record is None:
            logger.error("idempotency.fail called for unknown key=%s", key)
            return
        record.status = "FAILED"
        record.error = error[:2000]
        record.lease_until = None
        self.store.update(record)
        logger.warning("idempotency.failed key=%s step=%s error=%s", key, record.step_name, error[:200])

    def find_by_workflow(self, workflow_id: str) -> list[IdempotencyRecord]:
        """Firestore-only helper (not supported by the in-memory store)."""
        get_fn = getattr(self.store, "query_by_workflow", None)
        if get_fn is None:
            return []
        return get_fn(workflow_id)


class StepInProgressError(RuntimeError):
    """Raised when a step is already held by a valid lease on another worker.

    Callers (Pub/Sub workers) should nack so the delivery is retried later,
    at which point the step is typically COMPLETED and the stored result
    is returned.
    """


def _lease_valid(record: IdempotencyRecord, now: datetime) -> bool:
    if not record.lease_until:
        return False
    try:
        lease_until = datetime.fromisoformat(record.lease_until)
    except ValueError:
        return False
    return lease_until > now


def _canonical(payload: Any) -> Any:
    """Canonicalize payloads for stable hashing (sort dicts, drop volatile keys)."""
    if isinstance(payload, dict):
        return {
            k: _canonical(v)
            for k, v in sorted(payload.items())
            if k not in ("saved_at", "persisted_at", "created_at", "updated_at")
        }
    if isinstance(payload, (list, tuple)):
        return [_canonical(item) for item in payload]
    if isinstance(payload, (str, int, float, bool)) or payload is None:
        return payload
    return str(payload)


_manager: Optional[IdempotencyManager] = None


def get_idempotency_manager() -> IdempotencyManager:
    """Process-wide manager backed by Firestore."""
    global _manager
    if _manager is None:
        from github_infrastructure.config import settings

        if not settings.gcp_project:
            raise ValueError("GCP project is not configured — cannot create IdempotencyManager")
        _manager = IdempotencyManager(FirestoreIdempotencyStore(project=settings.gcp_project))
    return _manager
