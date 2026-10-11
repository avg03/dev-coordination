"""Security layer: permission matrix, gateway, audit, idempotency, execution."""

from security.approval import ApprovalRecord, ApprovalValidationError
from security.audit import AuditEvent, AuditLogger
from security.execution import DeliveryResult, SlackExecutor, get_slack_executor
from security.gateway import PermissionGateway, get_permission_gateway
from security.guard import make_before_tool_guard
from security.idempotency import (
    ClaimOutcome,
    IdempotencyManager,
    IdempotencyRecord,
    StepInProgressError,
    get_idempotency_manager,
)
from security.identities import Action, AgentIdentity, Resource
from security.permissions import AuthorizationRequest, PolicyDecision

__all__ = [
    "Action",
    "AgentIdentity",
    "ApprovalRecord",
    "ApprovalValidationError",
    "AuditEvent",
    "AuditLogger",
    "AuthorizationRequest",
    "ClaimOutcome",
    "DeliveryResult",
    "IdempotencyManager",
    "IdempotencyRecord",
    "PermissionGateway",
    "PolicyDecision",
    "Resource",
    "SlackExecutor",
    "StepInProgressError",
    "get_idempotency_manager",
    "get_permission_gateway",
    "get_slack_executor",
    "make_before_tool_guard",
]
