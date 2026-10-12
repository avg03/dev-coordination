"""Pydantic models for the commit readiness gate.

Kept separate from ChangeRecord/EnrichedChangeRecord domain models:
status lives in its own Firestore collection so canonical record fields
are untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Commit processing status
# ---------------------------------------------------------------------------

CommitStatusValue = Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED"]
ErrorKindValue = Literal["transient", "permanent", "unknown"]


class CommitProcessingRecord(BaseModel):
    """Firestore-backed per-(project, repository, sha) processing status."""

    project_id: str = Field(default="", description="Owning project for enrichment isolation")
    repository: str = Field(default="", description="owner/name repository identity")
    commit_sha: str = Field(default="", description="Commit SHA")
    status: CommitStatusValue = Field(default="PENDING")
    stage: str = Field(default="", description="Current processing stage, e.g. enrich")
    attempt_count: int = Field(default=0)
    updated_at: str = Field(default_factory=_now_iso)
    error: str = Field(default="", description="Safe error summary, no secrets")
    error_kind: ErrorKindValue = Field(default="unknown")

    @staticmethod
    def doc_id(project_id: str, repository: str, commit_sha: str) -> str:
        repo_key = repository.replace("/", "__")
        return f"{project_id}#{repo_key}#{commit_sha}"


# ---------------------------------------------------------------------------
# PR workflow persistence / state machine
# ---------------------------------------------------------------------------

PRWorkflowStatusValue = Literal[
    "PENDING",
    "WAITING_FOR_COMMITS",
    "READY",
    "PROCESSING",
    "COMPLETED",
    "PARTIAL",
    "FAILED",
    "SUPERSEDED",
]


class PRWorkflowRecord(BaseModel):
    """Durable waiting/processing state for one PR revision.

    Doc id includes the head SHA so a new revision is a logically different
    run and a stale workflow can never overwrite the newer revision's result.
    """

    project_id: str = ""
    owner: str = ""
    repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    action: str = "opened"
    required_shas: list[str] = Field(default_factory=list)
    status: PRWorkflowStatusValue = "PENDING"
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
    wait_started_at: Optional[str] = None
    deadline_at: Optional[str] = None
    reconcile_count: int = 0
    missing_shas: list[str] = Field(default_factory=list)
    failed_shas: list[str] = Field(default_factory=list)
    error: str = Field(default="", description="Safe error summary, no secrets")
    partial: bool = False
    # Trace linkage (wait -> resume): hex ids of the span that went WAITING.
    # The resume run attaches an OTel Link to it instead of holding spans open.
    trace_id: Optional[str] = None
    span_id: Optional[str] = None

    @property
    def workflow_id(self) -> str:
        return f"pr:{self.project_id}:{self.pr_number}:{self.head_sha or 'na'}"

    @property
    def repo_full(self) -> str:
        return f"{self.owner}/{self.repository}" if self.owner and self.repository else self.repository

    @staticmethod
    def doc_id(project_id: str, pr_number: int, head_sha: str) -> str:
        return f"{project_id}#pr-{pr_number}#{head_sha or 'na'}"


# ---------------------------------------------------------------------------
# Commit-completion event (versioned, no secrets / no source contents)
# ---------------------------------------------------------------------------

COMPLETION_EVENT_TYPE = "commit.processing.completed"
FAILED_EVENT_TYPE = "commit.processing.failed"
EVENT_SCHEMA_VERSION = 1


class CommitCompletionEvent(BaseModel):
    event_type: str = Field(default=COMPLETION_EVENT_TYPE)
    schema_version: int = Field(default=EVENT_SCHEMA_VERSION)
    repository: str = Field(description="owner/name repository identity")
    owner: str = Field(default="")
    project_id: str = Field(default="", description="Project isolation for enrichment")
    commit_sha: str = Field(description="Completed commit SHA")
    status: CommitStatusValue = Field(default="COMPLETED")
    attempt_count: int = Field(default=0)
    timestamp: str = Field(default_factory=_now_iso)

    def to_pubsub(self) -> tuple[dict[str, Any], dict[str, str]]:
        message = {"completion": self.model_dump(mode="json")}
        attributes = {
            "event": "commit.completed" if self.status == "COMPLETED" else "commit.failed",
            "repo": self.repository,
            "commit_sha": self.commit_sha,
            "project_id": self.project_id,
        }
        return message, attributes

    @staticmethod
    def from_envelope(message: dict[str, Any]) -> "CommitCompletionEvent":
        """Accept {completion: {...}} (our publisher) or a bare event dict."""
        payload = message.get("completion") if isinstance(message.get("completion"), dict) else message
        event = CommitCompletionEvent.model_validate(payload)
        if event.schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError(f"unsupported completion schema_version={event.schema_version}")
        if event.event_type not in (COMPLETION_EVENT_TYPE, FAILED_EVENT_TYPE):
            raise ValueError(f"unsupported event_type={event.event_type!r}")
        if not event.commit_sha or not event.repository:
            raise ValueError("completion event missing commit_sha/repository")
        return event


# ---------------------------------------------------------------------------
# Outbox entry (transactional-outbox-lite for publish-after-persist)
# ---------------------------------------------------------------------------

class CompletionOutboxEntry(BaseModel):
    key: str = Field(description="Same as CommitProcessingRecord doc id")
    project_id: str = ""
    repository: str = ""
    commit_sha: str = ""
    event: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now_iso)
    attempts: int = 0
    published: bool = False
    last_error: str = ""
