"""Deterministic PermissionGateway.

authoriza(request) -> PolicyDecision. Pure Python, no LLM. Every decision
(allow or deny) is recorded via the audit logger.
"""

from __future__ import annotations

import logging
from typing import Optional

from .audit import AuditEvent, AuditLogger, AuditSink
from .permissions import POLICY_ALLOWED
from .policies import POLICY_CHECKS
from .permissions import AuthorizationRequest, PolicyDecision

logger = logging.getLogger("security.gateway")


class PermissionGateway:
    def __init__(self, audit_sink: Optional[AuditSink] = None) -> None:
        self.audit = AuditLogger(audit_sink)

    def authorize(self, request: AuthorizationRequest) -> PolicyDecision:
        decision: Optional[PolicyDecision] = None
        for check in POLICY_CHECKS:
            decision = check(request)
            if decision is not None:
                break

        if decision is None:
            decision = PolicyDecision(
                allowed=True,
                reason="All policy checks passed",
                policy=POLICY_ALLOWED,
                agent_id=request.agent_id,
                action=request.action.value,
                resource=request.resource.value,
                project_id=request.project_id,
                workflow_id=request.workflow_id,
                approval_id=request.approval_id,
            )

        self.audit.record(
            AuditEvent(
                agent_id=request.agent_id,
                action=request.action.value,
                resource=request.resource.value,
                project_id=request.project_id,
                workflow_id=request.workflow_id,
                decision="allowed" if decision.allowed else "denied",
                reason=decision.reason,
                policy=decision.policy,
                approval_id=request.approval_id,
                message_id=request.message_id,
                event_type="permission.check",
            )
        )
        return decision


_gateway: Optional[PermissionGateway] = None


def get_permission_gateway() -> PermissionGateway:
    """Process-wide gateway with the Firestore audit sink (lazy, best-effort)."""
    global _gateway
    if _gateway is None:
        sink = None
        try:
            from github_infrastructure.config import settings

            if settings.gcp_project:
                from .audit import FirestoreAuditSink

                import os

                sink = FirestoreAuditSink(
                    project=settings.gcp_project,
                    database=os.getenv("FIRESTORE_DATABASE", "(default)"),
                )
        except Exception:
            logger.warning("Firestore audit sink unavailable — audit records will be log-only")
        _gateway = PermissionGateway(audit_sink=sink)
    return _gateway
