"""SlackExecutor — the only component allowed to send Slack messages.

Execution boundary for external side effects:
  PermissionGateway (approval binding) -> IdempotencyManager (atomic claim)
  -> SlackService (actual API call) -> persist delivery state -> COMPLETED.

A retried send with a COMPLETED record returns the stored delivery result
instead of calling Slack again.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from .audit import AuditEvent
from .gateway import PermissionGateway, get_permission_gateway
from .idempotency import ClaimStatus, IdempotencyManager, get_idempotency_manager
from .identities import Action, AgentIdentity, Resource
from .permissions import AuthorizationRequest

logger = logging.getLogger("security.execution")

SLACK_STEP_VERSION = "v1"


class DeliveryResult(BaseModel):
    message_id: str
    status: str = Field(description="sent | mock_sent | denied | failed | in_progress")
    channel: str = ""
    ok: bool = False
    reason: str = ""
    slack_ts: Optional[str] = None
    idempotency_key: str = ""
    reused: bool = Field(default=False, description="True when a stored result was reused")


# Sender signature: (channel, text) -> {"status": ..., "ok": ..., "ts": ...}
SlackSender = Callable[..., dict[str, Any]]

# Delivery-state persister: (project_id, pr_number, message_id, result_dict) -> None
DeliveryStateWriter = Callable[[str, int, str, dict[str, Any]], None]

# Delivery-state reader: (project_id, pr_number) -> {"msg_id": {...}}
DeliveredStateReader = Callable[[str, int], dict[str, Any]]


def default_slack_sender(channel: str, text: str) -> dict[str, Any]:
    from services.slack_service import send_slack_message

    return send_slack_message(channel=channel, text=text)


class FirestoreDeliveryStore:
    """Reads/writes per-message delivery state on projects/{id}/communications/{pr_id}."""

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

    def _doc(self, project_id: str, pr_number: int) -> Any:
        return (
            self._get_client()
            .collection("projects")
            .document(project_id)
            .collection("communications")
            .document(str(pr_number))
        )

    def read_delivered(self, project_id: str, pr_number: int) -> dict[str, Any]:
        snap = self._doc(project_id, pr_number).get()
        if not snap.exists:
            return {}
        delivered = (snap.to_dict() or {}).get("delivered")
        return delivered if isinstance(delivered, dict) else {}

    def write_delivery(self, project_id: str, pr_number: int, message_id: str, result: dict[str, Any]) -> None:
        from datetime import datetime, timezone

        self._doc(project_id, pr_number).set(
            {
                "delivered": {
                    message_id: {
                        "status": result.get("status", ""),
                        "ok": bool(result.get("ok", False)),
                        "slack_ts": result.get("slack_ts"),
                        "delivered_at": datetime.now(timezone.utc).isoformat(),
                    }
                }
            },
            merge=True,
        )


class InMemoryDeliveryStore:
    """In-memory delivery state — used by tests."""

    def __init__(self) -> None:
        self.state: dict[tuple[str, int], dict[str, Any]] = {}

    def read_delivered(self, project_id: str, pr_number: int) -> dict[str, Any]:
        return dict(self.state.get((project_id, pr_number), {}))

    def write_delivery(self, project_id: str, pr_number: int, message_id: str, result: dict[str, Any]) -> None:
        from datetime import datetime, timezone

        key = (project_id, pr_number)
        self.state.setdefault(key, {})[message_id] = {
            "status": result.get("status", ""),
            "ok": bool(result.get("ok", False)),
            "slack_ts": result.get("slack_ts"),
            "delivered_at": datetime.now(timezone.utc).isoformat(),
        }


class SlackExecutor:
    def __init__(
        self,
        gateway: Optional[PermissionGateway] = None,
        idempotency: Optional[IdempotencyManager] = None,
        delivery_store: Any = None,
        sender: Optional[SlackSender] = None,
        audit_sink: Any = None,
    ) -> None:
        self.gateway = gateway
        self.idempotency = idempotency
        self.delivery_store = delivery_store
        self.sender = sender or default_slack_sender
        self._audit_sink = audit_sink

    def _get_gateway(self) -> PermissionGateway:
        return self.gateway or get_permission_gateway()

    def _get_idempotency(self) -> IdempotencyManager:
        if self.idempotency is None:
            from github_infrastructure.config import settings

            from .idempotency import FirestoreIdempotencyStore

            self.idempotency = IdempotencyManager(
                FirestoreIdempotencyStore(project=settings.gcp_project)
            )
        return self.idempotency

    def _get_delivery_store(self) -> Any:
        if self.delivery_store is None:
            from github_infrastructure.config import settings

            self.delivery_store = FirestoreDeliveryStore(project=settings.gcp_project)
        return self.delivery_store

    def execute_send(
        self,
        project_id: str,
        pr_number: int,
        message: dict[str, Any],
        approval_id: str,
    ) -> DeliveryResult:
        """Send one approved Slack message exactly once.

        Order per spec §10: idempotency first (a COMPLETED send returns the
        stored result without touching Slack), then the PermissionGateway
        (approval binding, recipient, already-delivered orphan state), then
        the actual Slack call.
        """
        gateway = self._get_gateway()
        idem = self._get_idempotency()
        delivery_store = self._get_delivery_store()

        message_id = str(message.get("id", "") or "")
        bundle_id = str(pr_number)
        workflow_id = f"pr:{project_id}:{pr_number}"
        channel = str(message.get("channel", "") or "")
        text = str(message.get("text", "") or "")

        key = idem.build_key(
            project_id,
            workflow_id,
            "slack:send",
            {
                "bundle_id": bundle_id,
                "message_id": message_id,
                "channel": channel,
                "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
            SLACK_STEP_VERSION,
        )
        outcome = idem.claim(
            key,
            workflow_id=workflow_id,
            project_id=project_id,
            step_name="slack:send",
            step_version=SLACK_STEP_VERSION,
            input_payload={"bundle_id": bundle_id, "message_id": message_id, "channel": channel},
        )

        if outcome.status == ClaimStatus.COMPLETED:
            stored = dict(outcome.result or {})
            stored["reused"] = True
            logger.info("slack.execution reused key=%s message=%s", key, message_id)
            return DeliveryResult.model_validate({**stored, "message_id": message_id})

        if outcome.status == ClaimStatus.IN_PROGRESS:
            return DeliveryResult(
                message_id=message_id,
                status="in_progress",
                channel=channel,
                ok=False,
                reason="Send already in progress by another worker",
                idempotency_key=key,
            )

        # EXECUTE — fresh claim or reclaimed lease. Final deterministic gateway
        # check immediately before the side effect (spec §5).
        request = AuthorizationRequest(
            agent_id=AgentIdentity.SLACK_EXECUTOR.value,
            action=Action.SEND,
            resource=Resource.SLACK,
            project_id=project_id,
            workflow_id=workflow_id,
            approval_id=approval_id,
            message_id=message_id,
            bundle_id=bundle_id,
            context={
                "channel": channel,
                "already_delivered": message_id in delivery_store.read_delivered(project_id, pr_number),
            },
        )
        decision = gateway.authorize(request)
        if not decision.allowed:
            # Release the claim — a deny is final for this approval attempt.
            idem.fail(key, f"denied by policy: {decision.policy}")
            return DeliveryResult(
                message_id=message_id,
                status="denied",
                channel=channel,
                ok=False,
                reason=f"{decision.policy}: {decision.reason}",
                idempotency_key=key,
            )

        try:
            raw = self.sender(channel=channel, text=text)
            result = DeliveryResult(
                message_id=message_id,
                status=str(raw.get("status", "failed")),
                channel=channel,
                ok=bool(raw.get("ok", False)),
                slack_ts=raw.get("ts"),
                idempotency_key=key,
            )
        except Exception as exc:
            idem.fail(key, f"slack send failed: {exc}")
            logger.exception("slack.execution failed message=%s", message_id)
            return DeliveryResult(
                message_id=message_id,
                status="failed",
                channel=channel,
                ok=False,
                reason=str(exc),
                idempotency_key=key,
            )

        # Persist delivery state first, then mark COMPLETED (spec §8 ordering).
        try:
            delivery_store.write_delivery(project_id, pr_number, message_id, result.model_dump(mode="json"))
        except Exception:
            logger.exception("Failed to persist delivery state for message=%s", message_id)
        idem.complete(key, result.model_dump(mode="json"))

        self._audit_execution(project_id, workflow_id, key, approval_id, message_id, result)

        logger.info(
            "slack.execution status=%s channel=%s message=%s key=%s",
            result.status,
            channel,
            message_id,
            key,
        )
        return result

    def _audit_execution(
        self,
        project_id: str,
        workflow_id: str,
        key: str,
        approval_id: str,
        message_id: str,
        result: DeliveryResult,
    ) -> None:
        from .audit import AuditLogger

        AuditLogger(self._audit_sink).record(
            AuditEvent(
                agent_id=AgentIdentity.SLACK_EXECUTOR.value,
                action=Action.SEND.value,
                resource=Resource.SLACK.value,
                project_id=project_id,
                workflow_id=workflow_id,
                idempotency_key=key,
                decision="executed",
                reason=f"delivery_status={result.status}",
                policy="slack_send_requires_approval",
                approval_id=approval_id,
                message_id=message_id,
                event_type="slack.execution",
            )
        )


def new_approval_id() -> str:
    return "appr_" + uuid.uuid4().hex


_executor: Optional[SlackExecutor] = None


def get_slack_executor() -> SlackExecutor:
    global _executor
    if _executor is None:
        _executor = SlackExecutor()
    return _executor
