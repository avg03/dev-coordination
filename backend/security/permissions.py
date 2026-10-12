"""Permission matrix, authorization request/response models, and tool mapping.

The matrix is deny-by-default: an (agent, action, resource) triple must be
explicitly granted or the PermissionGateway denies it. Tool names are the
ADK FunctionTool names (func.__name__ of the bound tool method).
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from .identities import Action, AgentIdentity, Resource

# ---------------------------------------------------------------------------
# Permission matrix — explicit grants, everything else denied
# ---------------------------------------------------------------------------

PERMISSION_MATRIX: dict[str, set[tuple[Action, Resource]]] = {
    AgentIdentity.CONTEXT_AGENT.value: {
        (Action.READ, Resource.PROJECT_CONTEXT),
        (Action.WRITE, Resource.PROJECT_CONTEXT),
    },
    AgentIdentity.CHANGE_ANALYSIS_AGENT.value: {
        (Action.READ, Resource.PROJECT_CONTEXT),
        (Action.READ, Resource.CHANGE_RECORDS),
        (Action.WRITE, Resource.ENRICHED_CHANGES),
    },
    AgentIdentity.IMPACT_ANALYSIS_AGENT.value: {
        (Action.READ, Resource.PROJECT_CONTEXT),
        (Action.READ, Resource.ENRICHED_CHANGES),
        (Action.WRITE, Resource.IMPACT_REPORTS),
        # Read-only access to the deterministic blast-radius evidence.
        # NO sandbox / repository-execution capabilities.
        (Action.READ, Resource.BLAST_RADIUS),
    },
    AgentIdentity.COORDINATION_AGENT.value: {
        (Action.READ, Resource.PROJECT_CONTEXT),
        (Action.READ, Resource.IMPACT_REPORTS),
        (Action.WRITE, Resource.COORDINATION_PLANS),
    },
    AgentIdentity.COMMUNICATION_AGENT.value: {
        (Action.READ, Resource.PROJECT_CONTEXT),
        (Action.READ, Resource.COORDINATION_PLANS),
        (Action.WRITE, Resource.COMMUNICATIONS),
    },
    AgentIdentity.SUPERVISOR_AGENT.value: {
        (Action.READ, Resource.PROJECT_CONTEXT),
        (Action.READ, Resource.IMPACT_REPORTS),
        (Action.READ, Resource.COORDINATION_PLANS),
        (Action.READ, Resource.COMMUNICATIONS),
        (Action.WRITE, Resource.COMMUNICATIONS),
    },
    # The only identity allowed to touch Slack. Requires approval (policy).
    AgentIdentity.SLACK_EXECUTOR.value: {
        (Action.READ, Resource.COMMUNICATIONS),
        (Action.WRITE, Resource.DELIVERY_STATE),
        (Action.EXECUTE, Resource.SLACK),
        (Action.SEND, Resource.SLACK),
    },
    # Blast Radius / Sandbox: static analysis only. No Slack, no approval,
    # no arbitrary external side effects, no network during analysis.
    AgentIdentity.BLAST_RADIUS_ANALYZER.value: {
        (Action.EXECUTE, Resource.SANDBOX),
        (Action.READ, Resource.REPOSITORY),
        (Action.WRITE, Resource.SANDBOX_TEMP),
        (Action.EXECUTE, Resource.STATIC_ANALYSIS),
        (Action.WRITE, Resource.BLAST_RADIUS),
    },
    # Commit readiness gate: deterministic pipeline services (not LLM agents).
    # CommitWorker tracks per-commit status and publishes completion events.
    AgentIdentity.COMMIT_WORKER.value: {
        (Action.READ, Resource.CHANGE_RECORDS),
        (Action.WRITE, Resource.CHANGE_RECORDS),
        (Action.READ, Resource.ENRICHED_CHANGES),
        (Action.WRITE, Resource.ENRICHED_CHANGES),
        (Action.READ, Resource.PROJECT_CONTEXT),
        (Action.READ, Resource.COMMIT_STATUS),
        (Action.WRITE, Resource.COMMIT_STATUS),
        (Action.EXECUTE, Resource.COMPLETION_EVENTS),
    },
    # Completion consumer: inspect statuses + schedule PR processing only.
    # No Slack, no agent execution, no external side effects.
    AgentIdentity.COMPLETION_CONSUMER.value: {
        (Action.READ, Resource.COMMIT_STATUS),
        (Action.READ, Resource.ENRICHED_CHANGES),
        (Action.READ, Resource.PR_WORKFLOWS),
        (Action.WRITE, Resource.PR_WORKFLOWS),
    },
}

# Actions considered external side effects — always policy-gated.
EXTERNAL_SIDE_EFFECT_ACTIONS = {Action.EXECUTE, Action.SEND}

POLICY_SLACK_SEND_REQUIRES_APPROVAL = "slack_send_requires_approval"
POLICY_UNKNOWN_IDENTITY = "unknown_identity"
POLICY_CAPABILITY_NOT_GRANTED = "capability_not_granted"
POLICY_INVALID_SCOPE = "invalid_project_scope"
POLICY_RECIPIENT_NOT_ALLOWED = "recipient_not_allowed"
POLICY_ALREADY_DELIVERED = "already_delivered"
POLICY_UNKNOWN_TOOL = "unknown_tool"
POLICY_ALLOWED = "allowed"
POLICY_SANDBOX_SCOPE = "sandbox_scope_violation"
POLICY_SANDBOX_PATH_DENIED = "sandbox_path_denied"

# ---------------------------------------------------------------------------
# Explicit named capabilities (requirement: sandbox.create, sandbox.read_repo,
# sandbox.write_temp, static_analysis.execute) — granted ONLY to the Blast
# Radius analyzer identity via PERMISSION_MATRIX above.
# ---------------------------------------------------------------------------

CAPABILITY_SANDBOX_CREATE = "sandbox.create"
CAPABILITY_SANDBOX_READ_REPO = "sandbox.read_repo"
CAPABILITY_SANDBOX_WRITE_TEMP = "sandbox.write_temp"
CAPABILITY_STATIC_ANALYSIS_EXECUTE = "static_analysis.execute"

NAMED_CAPABILITIES: dict[str, tuple[Action, Resource]] = {
    CAPABILITY_SANDBOX_CREATE: (Action.EXECUTE, Resource.SANDBOX),
    CAPABILITY_SANDBOX_READ_REPO: (Action.READ, Resource.REPOSITORY),
    CAPABILITY_SANDBOX_WRITE_TEMP: (Action.WRITE, Resource.SANDBOX_TEMP),
    CAPABILITY_STATIC_ANALYSIS_EXECUTE: (Action.EXECUTE, Resource.STATIC_ANALYSIS),
}


def capability_granted(agent_id: str, capability_name: str) -> bool:
    """Check a named capability (e.g. 'sandbox.create') for an agent identity."""
    mapped = NAMED_CAPABILITIES.get(capability_name)
    if mapped is None:
        return False
    return is_granted(agent_id, mapped[0], mapped[1])


# ---------------------------------------------------------------------------
# Request / decision models
# ---------------------------------------------------------------------------


class AuthorizationRequest(BaseModel):
    """Everything the gateway needs to make a deterministic decision."""

    agent_id: str = Field(description="Identity from AgentIdentity")
    action: Action = Field(description="Requested action")
    resource: Resource = Field(description="Target resource")
    project_id: str = Field(default="", description="Project scope of the operation")
    workflow_id: str = Field(default="", description="Workflow/run identifier")
    tool_name: str = Field(default="", description="ADK tool name when guarding a tool call")
    approval_id: Optional[str] = Field(default=None, description="Human approval reference")
    message_id: Optional[str] = Field(default=None, description="SlackMessage id for sends")
    bundle_id: Optional[str] = Field(default=None, description="Communication bundle id")
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra policy inputs, e.g. {'channel': 'C123', 'already_delivered': True}",
    )


class PolicyDecision(BaseModel):
    """Structured allow/deny result — never a bare boolean."""

    allowed: bool
    reason: str = ""
    policy: str = ""
    agent_id: str = ""
    action: str = ""
    resource: str = ""
    project_id: str = ""
    workflow_id: str = ""
    approval_id: Optional[str] = None


# ---------------------------------------------------------------------------
# ADK tool mapping: (agent_id, tool name) -> (action, resource)
# Tool names come from func.__name__ of the bound tool methods in agents/.
# Any (agent, tool) pair not listed here is denied by the guard.
# ---------------------------------------------------------------------------


TOOL_RESOURCE_MAP: dict[tuple[str, str], tuple[Action, Resource]] = {
    # ContextAgent
    ("context_agent", "_save_firestore_tool"): (Action.WRITE, Resource.PROJECT_CONTEXT),
    # ChangeAnalysisAgent
    ("change_analysis_agent", "_get_project_context_tool"): (Action.READ, Resource.PROJECT_CONTEXT),
    ("change_analysis_agent", "_save_enriched_change_tool"): (Action.WRITE, Resource.ENRICHED_CHANGES),
    # ImpactAnalysisAgent
    ("impact_analysis_agent", "_get_project_context_tool"): (Action.READ, Resource.PROJECT_CONTEXT),
    ("impact_analysis_agent", "_get_enriched_change_tool"): (Action.READ, Resource.ENRICHED_CHANGES),
    ("impact_analysis_agent", "_get_blast_radius_tool"): (Action.READ, Resource.BLAST_RADIUS),
    ("impact_analysis_agent", "_save_impact_report_tool"): (Action.WRITE, Resource.IMPACT_REPORTS),
    # CoordinationAgent
    ("coordination_agent", "_get_impact_report_tool"): (Action.READ, Resource.IMPACT_REPORTS),
    ("coordination_agent", "_get_project_context_tool"): (Action.READ, Resource.PROJECT_CONTEXT),
    ("coordination_agent", "_save_coordination_plan_tool"): (Action.WRITE, Resource.COORDINATION_PLANS),
    # CommunicationAgent — read/draft only, NO slack.send
    ("communication_agent", "_get_project_context_tool"): (Action.READ, Resource.PROJECT_CONTEXT),
    ("communication_agent", "_get_coordination_plan_tool"): (Action.READ, Resource.COORDINATION_PLANS),
    # SupervisorAgent — critique/revise only, NO slack.send
    ("supervisor_agent", "_get_project_context_tool"): (Action.READ, Resource.PROJECT_CONTEXT),
    ("supervisor_agent", "_get_coordination_plan_tool"): (Action.READ, Resource.COORDINATION_PLANS),
    ("supervisor_agent", "_save_supervised_communication_tool"): (Action.WRITE, Resource.COMMUNICATIONS),
}


def is_granted(agent_id: str, action: Action, resource: Resource) -> bool:
    """Deny-by-default matrix lookup."""
    granted = PERMISSION_MATRIX.get(agent_id)
    if not granted:
        return False
    return (action, resource) in granted
