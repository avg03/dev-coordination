from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

SUPPORTED_EVENTS = {"push", "pull_request"}


class WebhookMetadata(BaseModel):
    event: str
    delivery_id: str = ""
    repo: str = ""
    branch: Optional[str] = None
    sha: Optional[str] = None
    pr_number: Optional[int] = None
    action: Optional[str] = None
    received_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def extract_metadata(
    event: str, delivery_id: str, payload: dict[str, Any]
) -> Optional[WebhookMetadata]:
    if event not in SUPPORTED_EVENTS:
        return None

    metadata = WebhookMetadata(
        event=event,
        delivery_id=delivery_id,
        repo=_repo_full_name(payload),
    )

    if event == "push":
        metadata.sha = payload.get("after") or (payload.get("head_commit") or {}).get("id")
        metadata.branch = _strip_ref(payload.get("ref"))
    else:
        pull_request = payload.get("pull_request") or {}
        head = pull_request.get("head") or {}
        metadata.action = payload.get("action")
        metadata.pr_number = pull_request.get("number")
        metadata.sha = head.get("sha")
        metadata.branch = head.get("ref")

    return metadata


def _repo_full_name(payload: dict[str, Any]) -> str:
    repository = payload.get("repository") or {}
    return repository.get("full_name", "")


def _strip_ref(ref: Any) -> Optional[str]:
    if not isinstance(ref, str):
        return None
    prefix = "refs/heads/"
    if ref.startswith(prefix):
        return ref[len(prefix):]
    return ref
