"""Permission Gateway tests — deterministic authorization, deny-by-default.

Run: python backend/tests/test_permissions_gateway.py  (no GCP/Slack needed)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from security.audit import InMemoryAuditSink
from security.gateway import PermissionGateway
from security.guard import make_before_tool_guard
from security.identities import Action, AgentIdentity, Resource
from security.permissions import AuthorizationRequest


def _gateway_with_sink():
    sink = InMemoryAuditSink()
    return PermissionGateway(audit_sink=sink), sink


def test_authorized_agent_allowed():
    gw, sink = _gateway_with_sink()
    d = gw.authorize(
        AuthorizationRequest(
            agent_id=AgentIdentity.IMPACT_ANALYSIS_AGENT.value,
            action=Action.READ,
            resource=Resource.PROJECT_CONTEXT,
            project_id="proj-1",
        )
    )
    assert d.allowed is True, d
    assert d.policy == "allowed"
    assert any(e.decision == "allowed" for e in sink.events), "allow must be audited"


def test_unauthorized_agent_denied():
    gw, _ = _gateway_with_sink()
    # impact agent has no write on communications
    d = gw.authorize(
        AuthorizationRequest(
            agent_id=AgentIdentity.IMPACT_ANALYSIS_AGENT.value,
            action=Action.WRITE,
            resource=Resource.COMMUNICATIONS,
            project_id="proj-1",
        )
    )
    assert d.allowed is False
    assert d.policy == "capability_not_granted"


def test_unknown_agent_denied():
    gw, _ = _gateway_with_sink()
    d = gw.authorize(
        AuthorizationRequest(
            agent_id="mystery_agent",
            action=Action.READ,
            resource=Resource.PROJECT_CONTEXT,
            project_id="proj-1",
        )
    )
    assert d.allowed is False
    assert d.policy == "unknown_identity"


def test_unknown_resource_denied():
    gw, _ = _gateway_with_sink()
    d = gw.authorize(
        AuthorizationRequest(
            agent_id=AgentIdentity.COMMUNICATION_AGENT.value,
            action=Action.WRITE,
            resource=Resource.SLACK,
            project_id="proj-1",
        )
    )
    assert d.allowed is False
    assert d.policy == "capability_not_granted"


def test_communication_agent_cannot_send_slack():
    gw, _ = _gateway_with_sink()
    d = gw.authorize(
        AuthorizationRequest(
            agent_id=AgentIdentity.COMMUNICATION_AGENT.value,
            action=Action.SEND,
            resource=Resource.SLACK,
            project_id="proj-1",
        )
    )
    assert d.allowed is False, "CommunicationAgent must NEVER have slack.send"


def test_supervisor_agent_cannot_send_slack():
    gw, _ = _gateway_with_sink()
    d = gw.authorize(
        AuthorizationRequest(
            agent_id=AgentIdentity.SUPERVISOR_AGENT.value,
            action=Action.SEND,
            resource=Resource.SLACK,
            project_id="proj-1",
        )
    )
    assert d.allowed is False


def test_slack_executor_without_approval_denied():
    gw, _ = _gateway_with_sink()
    d = gw.authorize(
        AuthorizationRequest(
            agent_id=AgentIdentity.SLACK_EXECUTOR.value,
            action=Action.SEND,
            resource=Resource.SLACK,
            project_id="proj-1",
        )
    )
    assert d.allowed is False
    assert d.policy == "slack_send_requires_approval"


def test_slack_executor_with_valid_approval_allowed():
    gw, _ = _gateway_with_sink()
    d = gw.authorize(
        AuthorizationRequest(
            agent_id=AgentIdentity.SLACK_EXECUTOR.value,
            action=Action.SEND,
            resource=Resource.SLACK,
            project_id="proj-1",
            approval_id="appr_123",
            bundle_id="7",
            message_id="msg_1",
            context={"channel": "C012345", "already_delivered": False},
        )
    )
    assert d.allowed is True, d


def test_slack_executor_already_delivered_denied():
    gw, _ = _gateway_with_sink()
    d = gw.authorize(
        AuthorizationRequest(
            agent_id=AgentIdentity.SLACK_EXECUTOR.value,
            action=Action.SEND,
            resource=Resource.SLACK,
            project_id="proj-1",
            approval_id="appr_123",
            bundle_id="7",
            message_id="msg_1",
            context={"channel": "C012345", "already_delivered": True},
        )
    )
    assert d.allowed is False
    assert d.policy == "already_delivered"


def test_scope_required_outside_project_context_write():
    gw, _ = _gateway_with_sink()
    d = gw.authorize(
        AuthorizationRequest(
            agent_id=AgentIdentity.CHANGE_ANALYSIS_AGENT.value,
            action=Action.WRITE,
            resource=Resource.ENRICHED_CHANGES,
            project_id="",
        )
    )
    assert d.allowed is False
    assert d.policy == "invalid_project_scope"


def test_guard_denies_unknown_tool():
    gw, _ = _gateway_with_sink()
    guard = make_before_tool_guard(AgentIdentity.COMMUNICATION_AGENT.value, gateway=gw)

    class FakeTool:
        name = "delete_everything"

    result = guard(FakeTool(), {"project_id": "p1"}, tool_context=None)
    assert isinstance(result, dict) and result["status"] == "error"
    assert "unknown_tool" in result["error"]


def test_guard_allows_mapped_tool():
    gw, _ = _gateway_with_sink()
    guard = make_before_tool_guard(AgentIdentity.COMMUNICATION_AGENT.value, gateway=gw)

    class FakeTool:
        name = "_get_project_context_tool"

    result = guard(FakeTool(), {"project_id": "p1"}, tool_context=None)
    assert result is None, f"mapped read tool must pass, got {result}"


def test_guard_blocks_unmapped_tool_for_agent():
    gw, _ = _gateway_with_sink()
    # save_impact_report_tool belongs to the impact agent, not communication
    guard = make_before_tool_guard(AgentIdentity.COMMUNICATION_AGENT.value, gateway=gw)

    class FakeTool:
        name = "_save_impact_report_tool"

    result = guard(FakeTool(), {"project_id": "p1"}, tool_context=None)
    assert isinstance(result, dict) and result["status"] == "error"


if __name__ == "__main__":
    test_authorized_agent_allowed()
    test_unauthorized_agent_denied()
    test_unknown_agent_denied()
    test_unknown_resource_denied()
    test_communication_agent_cannot_send_slack()
    test_supervisor_agent_cannot_send_slack()
    test_slack_executor_without_approval_denied()
    test_slack_executor_with_valid_approval_allowed()
    test_slack_executor_already_delivered_denied()
    test_scope_required_outside_project_context_write()
    test_guard_denies_unknown_tool()
    test_guard_allows_mapped_tool()
    test_guard_blocks_unmapped_tool_for_agent()
    print("=== ALL PERMISSION GATEWAY TESTS PASSED ===")
