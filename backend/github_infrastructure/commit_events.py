from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field


class CommitEvent(BaseModel):
    """Normalized push event published to Pub/Sub."""

    event: Literal["push"] = "push"

    delivery_id: str = Field(..., description="GitHub webhook delivery UUID")
    repository: str
    owner: str
    branch: str

    before_sha: str
    after_sha: str
    commit_shas: list[str]

    pusher: str

    timestamp: datetime


class CommitEventError(ValueError):
    pass


def decode_pubsub_message(data: Union[bytes, str]) -> dict[str, Any]:
    if isinstance(data, bytes):
        raw = data
    else:
        try:
            raw = base64.b64decode(data)
        except (binascii.Error, ValueError):
            raw = data.encode("utf-8")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CommitEventError("Pub/Sub message data is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise CommitEventError("Pub/Sub message data must be a JSON object")
    return parsed


def build_commit_event(
    envelope: dict[str, Any],
    attributes: Optional[dict[str, str]] = None,
) -> CommitEvent:
    attributes = attributes or {}
    payload = envelope.get("payload") or envelope
    metadata = envelope.get("metadata") or {}

    if "repository" not in payload:
        raise CommitEventError("Missing 'repository' in push payload")

    delivery_id = (
        attributes.get("delivery_id")
        or metadata.get("delivery_id")
        or ""
    )
    if not delivery_id:
        raise CommitEventError("delivery_id missing from message and attributes")

    repo_full_name = (payload.get("repository") or {}).get("full_name", "")
    if not repo_full_name:
        raise CommitEventError("repository.full_name is empty")

    owner = (
        ((payload.get("repository") or {}).get("owner") or {}).get("login")
        or ((payload.get("repository") or {}).get("owner") or {}).get("name")
        or repo_full_name.split("/")[0]
    )

    branch = metadata.get("branch") or _strip_ref(payload.get("ref"))
    if not branch:
        raise CommitEventError("branch could not be determined from ref/metadata")

    before_sha = payload.get("before") or ""
    after_sha = payload.get("after") or ""
    commit_shas = [
        str(commit["id"])
        for commit in payload.get("commits") or []
        if isinstance(commit, dict) and commit.get("id")
    ]

    pusher = (
        (payload.get("pusher") or {}).get("name")
        or (payload.get("sender") or {}).get("login")
        or ""
    )

    timestamp_raw = (payload.get("head_commit") or {}).get("timestamp")
    timestamp = (
        datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
        if isinstance(timestamp_raw, str)
        else datetime.now(timezone.utc)
    )

    return CommitEvent(
        event="push",
        delivery_id=delivery_id,
        repository=repo_full_name,
        owner=owner,
        branch=branch,
        before_sha=before_sha,
        after_sha=after_sha,
        commit_shas=commit_shas,
        pusher=pusher,
        timestamp=timestamp,
    )


def process_pubsub_commit_message(
    pull_message: dict[str, Any],
) -> CommitEvent:
    """
    Accepts either:
      - a Pub/Sub pull item: {"message": {"data": "<base64>", "attributes": {...}}}
      - already-decoded envelope: {"metadata": ..., "payload": ...} (+ optional attributes)
    """
    inner = pull_message.get("message")
    if isinstance(inner, dict) and "data" in inner:
        envelope = decode_pubsub_message(inner["data"])
        attributes = {
            key: str(value)
            for key, value in (inner.get("attributes") or {}).items()
        }
    else:
        envelope = pull_message
        attributes = {
            key: str(value)
            for key, value in (pull_message.get("attributes") or {}).items()
        }
    return build_commit_event(envelope, attributes)


def _strip_ref(ref: Any) -> str:
    if isinstance(ref, str) and ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    return ref or ""
