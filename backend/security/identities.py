"""Agent identities, actions, and resources for the permission system.

Deny-by-default: any identity/action/resource combination not explicitly
listed in security.permissions.PERMISSION_MATRIX is denied.
"""

from __future__ import annotations

from enum import Enum


class AgentIdentity(str, Enum):
    """Known agent identities. Unknown identities are always denied."""

    CONTEXT_AGENT = "context_agent"
    CHANGE_ANALYSIS_AGENT = "change_analysis_agent"
    IMPACT_ANALYSIS_AGENT = "impact_analysis_agent"
    COORDINATION_AGENT = "coordination_agent"
    COMMUNICATION_AGENT = "communication_agent"
    SUPERVISOR_AGENT = "supervisor_agent"
    SLACK_EXECUTOR = "slack_executor"
    BLAST_RADIUS_ANALYZER = "blast_radius_analyzer"


class Action(str, Enum):
    """Explicit action types requested against a resource."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    SEND = "send"
    APPROVE = "approve"
    DELEGATE = "delegate"


class Resource(str, Enum):
    """Resources agents may act upon."""

    PROJECT_CONTEXT = "project_context"
    CHANGE_RECORDS = "change_records"
    ENRICHED_CHANGES = "enriched_changes"
    IMPACT_REPORTS = "impact_reports"
    COORDINATION_PLANS = "coordination_plans"
    COMMUNICATIONS = "communications"
    DELIVERY_STATE = "delivery_state"
    SLACK = "slack"
    # Blast Radius / static-analysis sandbox resources
    SANDBOX = "sandbox"
    REPOSITORY = "repository"
    SANDBOX_TEMP = "sandbox_temp"
    STATIC_ANALYSIS = "static_analysis"
    BLAST_RADIUS = "blast_radius"
