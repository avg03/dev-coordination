"""Human approval validation for Slack execution.

The approval endpoint (POST .../prs/{pr_number}/approve) is the authoritative
human-in-the-loop entry point. This module validates that an approval actually
applies to the exact communication bundle and message being executed.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import BaseModel

logger = logging.getLogger("security.approval")


class ApprovalRecord(BaseModel):
    """A human approval bound to one communication bundle."""

    approval_id: str
    project_id: str
    pr_number: int
    bundle_id: str
    approved: bool
    approved_at: str = ""
    selected_message_ids: list[str] = []


class ApprovalValidationError(ValueError):
    pass


def build_approval(
    approval_id: str,
    project_id: str,
    pr_number: int,
    approved: bool,
    approved_at: str,
    selected_message_ids: list[str],
) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=approval_id,
        project_id=project_id,
        pr_number=pr_number,
        bundle_id=str(pr_number),
        approved=approved,
        approved_at=approved_at,
        selected_message_ids=selected_message_ids,
    )


def validate_approval_for_message(
    approval: ApprovalRecord,
    project_id: str,
    bundle_id: str,
    message_id: str,
    message: dict[str, Any],
) -> None:
    """Raise ApprovalValidationError unless the approval covers this exact send."""
    if not approval.approved:
        raise ApprovalValidationError("Approval record is not approved")
    if approval.project_id != project_id:
        raise ApprovalValidationError(
            f"Approval project mismatch: approval={approval.project_id} request={project_id}"
        )
    if approval.bundle_id != bundle_id:
        raise ApprovalValidationError(
            f"Approval bundle mismatch: approval={approval.bundle_id} request={bundle_id}"
        )
    if approval.selected_message_ids and message_id not in approval.selected_message_ids:
        raise ApprovalValidationError(
            f"Message {message_id} is not in the approved selection {approval.selected_message_ids}"
        )
    if not str(message.get("channel", "") or "").strip():
        raise ApprovalValidationError(f"Message {message_id} has no recipient channel")


def load_delivered_state(bundle_data: dict[str, Any]) -> dict[str, Any]:
    """Per-message delivery state stored on the communications doc."""
    delivered = bundle_data.get("delivered")
    return delivered if isinstance(delivered, dict) else {}


def extract_messages(bundle_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """id -> message map from a stored CommunicationBundle/CommunicationResult."""
    messages: dict[str, dict[str, Any]] = {}
    for idx, msg in enumerate(bundle_data.get("messages") or [], start=1):
        if not isinstance(msg, dict):
            continue
        mid = str(msg.get("id") or f"msg_{idx}")
        msg.setdefault("id", mid)
        messages[mid] = msg
    return messages
