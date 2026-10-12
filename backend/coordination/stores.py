"""Firestore-backed (and in-memory, for tests) stores for the readiness gate."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from .models import CommitProcessingRecord, CompletionOutboxEntry, PRWorkflowRecord

logger = logging.getLogger("coordination.stores")

COMMIT_PROCESSING_COLLECTION = "commit_processing"
PR_WORKFLOWS_COLLECTION = "pr_workflows"
COMPLETION_OUTBOX_COLLECTION = "commit_completion_outbox"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Commit status store
# ---------------------------------------------------------------------------

class CommitStatusStore(Protocol):
    def get(self, project_id: str, repository: str, commit_sha: str) -> Optional[CommitProcessingRecord]: ...
    def put(self, record: CommitProcessingRecord) -> None: ...


class InMemoryCommitStatusStore:
    def __init__(self) -> None:
        self._records: dict[str, CommitProcessingRecord] = {}
        self._lock = threading.Lock()

    def get(self, project_id: str, repository: str, commit_sha: str) -> Optional[CommitProcessingRecord]:
        key = CommitProcessingRecord.doc_id(project_id, repository, commit_sha)
        with self._lock:
            rec = self._records.get(key)
            return CommitProcessingRecord.model_validate(rec.model_dump(mode="json")) if rec else None

    def put(self, record: CommitProcessingRecord) -> None:
        key = CommitProcessingRecord.doc_id(record.project_id, record.repository, record.commit_sha)
        with self._lock:
            self._records[key] = CommitProcessingRecord.model_validate(record.model_dump(mode="json"))


class FirestoreCommitStatusStore:
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

    def get(self, project_id: str, repository: str, commit_sha: str) -> Optional[CommitProcessingRecord]:
        snap = (
            self._get_client()
            .collection(COMMIT_PROCESSING_COLLECTION)
            .document(CommitProcessingRecord.doc_id(project_id, repository, commit_sha))
            .get()
        )
        if not snap.exists:
            return None
        try:
            return CommitProcessingRecord.model_validate(snap.to_dict() or {})
        except Exception:
            logger.warning("Invalid CommitProcessingRecord shape for %s/%s", repository, commit_sha[:8])
            return None

    def put(self, record: CommitProcessingRecord) -> None:
        record.updated_at = _now_iso()
        self._get_client().collection(COMMIT_PROCESSING_COLLECTION).document(
            CommitProcessingRecord.doc_id(record.project_id, record.repository, record.commit_sha)
        ).set(record.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# PR workflow store
# ---------------------------------------------------------------------------

class PRWorkflowStore(Protocol):
    def get(self, project_id: str, pr_number: int, head_sha: str) -> Optional[PRWorkflowRecord]: ...
    def put(self, record: PRWorkflowRecord) -> None: ...
    def list_waiting(self) -> list[PRWorkflowRecord]: ...
    def list_waiting_for_commit(self, commit_sha: str) -> list[PRWorkflowRecord]: ...
    def list_for_pr(self, project_id: str, pr_number: int) -> list[PRWorkflowRecord]: ...


class InMemoryPRWorkflowStore:
    def __init__(self) -> None:
        self._records: dict[str, PRWorkflowRecord] = {}
        self._lock = threading.Lock()

    def get(self, project_id: str, pr_number: int, head_sha: str) -> Optional[PRWorkflowRecord]:
        key = PRWorkflowRecord.doc_id(project_id, pr_number, head_sha)
        with self._lock:
            rec = self._records.get(key)
            return PRWorkflowRecord.model_validate(rec.model_dump(mode="json")) if rec else None

    def put(self, record: PRWorkflowRecord) -> None:
        record.updated_at = _now_iso()
        key = PRWorkflowRecord.doc_id(record.project_id, record.pr_number, record.head_sha)
        with self._lock:
            self._records[key] = PRWorkflowRecord.model_validate(record.model_dump(mode="json"))

    def list_waiting(self) -> list[PRWorkflowRecord]:
        with self._lock:
            return [r for r in self._records.values() if r.status == "WAITING_FOR_COMMITS"]

    def list_waiting_for_commit(self, commit_sha: str) -> list[PRWorkflowRecord]:
        with self._lock:
            return [
                r for r in self._records.values()
                if r.status == "WAITING_FOR_COMMITS" and commit_sha in (r.required_shas or [])
            ]

    def list_for_pr(self, project_id: str, pr_number: int) -> list[PRWorkflowRecord]:
        with self._lock:
            return [r for r in self._records.values() if r.project_id == project_id and r.pr_number == pr_number]


class FirestorePRWorkflowStore:
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

    def get(self, project_id: str, pr_number: int, head_sha: str) -> Optional[PRWorkflowRecord]:
        snap = (
            self._get_client()
            .collection(PR_WORKFLOWS_COLLECTION)
            .document(PRWorkflowRecord.doc_id(project_id, pr_number, head_sha))
            .get()
        )
        if not snap.exists:
            return None
        try:
            return PRWorkflowRecord.model_validate(snap.to_dict() or {})
        except Exception:
            logger.warning("Invalid PRWorkflowRecord shape for %s/%s", project_id, pr_number)
            return None

    def put(self, record: PRWorkflowRecord) -> None:
        record.updated_at = _now_iso()
        self._get_client().collection(PR_WORKFLOWS_COLLECTION).document(
            PRWorkflowRecord.doc_id(record.project_id, record.pr_number, record.head_sha)
        ).set(record.model_dump(mode="json"))

    def list_waiting(self) -> list[PRWorkflowRecord]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        out: list[PRWorkflowRecord] = []
        query = self._get_client().collection(PR_WORKFLOWS_COLLECTION).where(
            filter=FieldFilter("status", "==", "WAITING_FOR_COMMITS")
        )
        for doc in query.stream():
            try:
                out.append(PRWorkflowRecord.model_validate(doc.to_dict() or {}))
            except Exception:
                continue
        return out

    def list_waiting_for_commit(self, commit_sha: str) -> list[PRWorkflowRecord]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        out: list[PRWorkflowRecord] = []
        query = (
            self._get_client()
            .collection(PR_WORKFLOWS_COLLECTION)
            .where(filter=FieldFilter("status", "==", "WAITING_FOR_COMMITS"))
            .where(filter=FieldFilter("required_shas", "array_contains", commit_sha))
        )
        for doc in query.stream():
            try:
                out.append(PRWorkflowRecord.model_validate(doc.to_dict() or {}))
            except Exception:
                continue
        return out

    def list_for_pr(self, project_id: str, pr_number: int) -> list[PRWorkflowRecord]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        out: list[PRWorkflowRecord] = []
        query = (
            self._get_client()
            .collection(PR_WORKFLOWS_COLLECTION)
            .where(filter=FieldFilter("project_id", "==", project_id))
            .where(filter=FieldFilter("pr_number", "==", pr_number))
        )
        for doc in query.stream():
            try:
                out.append(PRWorkflowRecord.model_validate(doc.to_dict() or {}))
            except Exception:
                continue
        return out


# ---------------------------------------------------------------------------
# Outbox store (publish-after-persist fallback)
# ---------------------------------------------------------------------------

class OutboxStore(Protocol):
    def put(self, entry: CompletionOutboxEntry) -> None: ...
    def get(self, key: str) -> Optional[CompletionOutboxEntry]: ...
    def list_pending(self) -> list[CompletionOutboxEntry]: ...
    def mark_published(self, key: str) -> None: ...
    def record_attempt(self, key: str, error: str) -> None: ...


class InMemoryOutboxStore:
    def __init__(self) -> None:
        self._entries: dict[str, CompletionOutboxEntry] = {}
        self._lock = threading.Lock()

    def put(self, entry: CompletionOutboxEntry) -> None:
        with self._lock:
            self._entries[entry.key] = CompletionOutboxEntry.model_validate(entry.model_dump(mode="json"))

    def get(self, key: str) -> Optional[CompletionOutboxEntry]:
        with self._lock:
            e = self._entries.get(key)
            return CompletionOutboxEntry.model_validate(e.model_dump(mode="json")) if e else None

    def list_pending(self) -> list[CompletionOutboxEntry]:
        with self._lock:
            return [e for e in self._entries.values() if not e.published]

    def mark_published(self, key: str) -> None:
        with self._lock:
            e = self._entries.get(key)
            if e is not None:
                e.published = True

    def record_attempt(self, key: str, error: str) -> None:
        with self._lock:
            e = self._entries.get(key)
            if e is not None:
                e.attempts += 1
                e.last_error = error[:500]


class FirestoreOutboxStore:
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

    def put(self, entry: CompletionOutboxEntry) -> None:
        self._get_client().collection(COMPLETION_OUTBOX_COLLECTION).document(entry.key).set(
            entry.model_dump(mode="json")
        )

    def get(self, key: str) -> Optional[CompletionOutboxEntry]:
        snap = self._get_client().collection(COMPLETION_OUTBOX_COLLECTION).document(key).get()
        if not snap.exists:
            return None
        try:
            return CompletionOutboxEntry.model_validate(snap.to_dict() or {})
        except Exception:
            return None

    def list_pending(self) -> list[CompletionOutboxEntry]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        out: list[CompletionOutboxEntry] = []
        query = self._get_client().collection(COMPLETION_OUTBOX_COLLECTION).where(
            filter=FieldFilter("published", "==", False)
        )
        for doc in query.stream():
            try:
                out.append(CompletionOutboxEntry.model_validate(doc.to_dict() or {}))
            except Exception:
                continue
        return out

    def mark_published(self, key: str) -> None:
        self._get_client().collection(COMPLETION_OUTBOX_COLLECTION).document(key).set(
            {"published": True}, merge=True
        )

    def record_attempt(self, key: str, error: str) -> None:
        ref = self._get_client().collection(COMPLETION_OUTBOX_COLLECTION).document(key)
        snap = ref.get()
        attempts = 0
        if snap.exists:
            try:
                attempts = int((snap.to_dict() or {}).get("attempts", 0))
            except Exception:
                attempts = 0
        ref.set({"attempts": attempts + 1, "last_error": error[:500]}, merge=True)
