"""Blast Radius permission tests — sandbox capabilities are locked to one identity.

Run: python backend/tests/test_blast_radius_permissions.py  (no GCP needed)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from security.audit import InMemoryAuditSink
from security.gateway import PermissionGateway
from security.identities import Action, AgentIdentity, Resource
from security.permissions import (
    CAPABILITY_SANDBOX_CREATE,
    CAPABILITY_SANDBOX_READ_REPO,
    CAPABILITY_SANDBOX_WRITE_TEMP,
    CAPABILITY_STATIC_ANALYSIS_EXECUTE,
    AuthorizationRequest,
    capability_granted,
)


def _gw():
    return PermissionGateway(audit_sink=InMemoryAuditSink())


def _req(agent_id, action, resource, **ctx):
    return AuthorizationRequest(
        agent_id=agent_id,
        action=action,
        resource=resource,
        project_id=ctx.pop("project_id", "proj-1"),
        workflow_id=ctx.pop("workflow_id", "pr:proj-1:7:abc"),
        context=ctx.pop("context", {"repo": "owner/repo", "pr_number": 7, "workspace_root": "/tmp/ws"}),
    )


def test_analyzer_has_all_four_named_capabilities():
    for cap in (
        CAPABILITY_SANDBOX_CREATE,
        CAPABILITY_SANDBOX_READ_REPO,
        CAPABILITY_SANDBOX_WRITE_TEMP,
        CAPABILITY_STATIC_ANALYSIS_EXECUTE,
    ):
        assert capability_granted(AgentIdentity.BLAST_RADIUS_ANALYZER.value, cap), cap


def test_named_capabilities_denied_for_everyone_else():
    others = [
        AgentIdentity.CONTEXT_AGENT.value,
        AgentIdentity.CHANGE_ANALYSIS_AGENT.value,
        AgentIdentity.IMPACT_ANALYSIS_AGENT.value,
        AgentIdentity.COORDINATION_AGENT.value,
        AgentIdentity.COMMUNICATION_AGENT.value,
        AgentIdentity.SUPERVISOR_AGENT.value,
        AgentIdentity.SLACK_EXECUTOR.value,
    ]
    for agent in others:
        for cap in (
            CAPABILITY_SANDBOX_CREATE,
            CAPABILITY_SANDBOX_READ_REPO,
            CAPABILITY_SANDBOX_WRITE_TEMP,
            CAPABILITY_STATIC_ANALYSIS_EXECUTE,
        ):
            assert not capability_granted(agent, cap), f"{agent} must not have {cap}"


def test_analyzer_cannot_send_slack_or_approve():
    gw = _gw()
    d = gw.authorize(_req(AgentIdentity.BLAST_RADIUS_ANALYZER.value, Action.SEND, Resource.SLACK))
    assert d.allowed is False
    d = gw.authorize(
        _req(
            AgentIdentity.BLAST_RADIUS_ANALYZER.value,
            Action.SEND,
            Resource.SLACK,
            context={"repo": "o/r", "pr_number": 7, "workspace_root": "/tmp/ws", "approval_id": "x"},
        )
    )
    assert d.allowed is False, "analyzer must never send Slack even with an approval id"


def test_impact_agent_has_no_sandbox_capabilities():
    gw = _gw()
    for action, resource in [
        (Action.EXECUTE, Resource.SANDBOX),
        (Action.READ, Resource.REPOSITORY),
        (Action.WRITE, Resource.SANDBOX_TEMP),
        (Action.EXECUTE, Resource.STATIC_ANALYSIS),
    ]:
        d = gw.authorize(
            _req(AgentIdentity.IMPACT_ANALYSIS_AGENT.value, action, resource)
        )
        assert d.allowed is False, f"impact agent must not have {action} on {resource}"


def test_impact_agent_can_read_blast_radius_only():
    gw = _gw()
    d = gw.authorize(_req(AgentIdentity.IMPACT_ANALYSIS_AGENT.value, Action.READ, Resource.BLAST_RADIUS))
    assert d.allowed is True
    d = gw.authorize(_req(AgentIdentity.IMPACT_ANALYSIS_AGENT.value, Action.WRITE, Resource.BLAST_RADIUS))
    assert d.allowed is False, "impact agent must not modify blast radius results"


def test_unknown_identity_sandbox_denied():
    gw = _gw()
    d = gw.authorize(_req("mystery", Action.EXECUTE, Resource.SANDBOX))
    assert d.allowed is False
    assert d.policy == "unknown_identity"


def test_analyzer_sandbox_ops_allowed_with_scope():
    gw = _gw()
    for action, resource in [
        (Action.EXECUTE, Resource.SANDBOX),
        (Action.READ, Resource.REPOSITORY),
        (Action.WRITE, Resource.SANDBOX_TEMP),
        (Action.EXECUTE, Resource.STATIC_ANALYSIS),
    ]:
        d = gw.authorize(
            _req(AgentIdentity.BLAST_RADIUS_ANALYZER.value, action, resource)
        )
        assert d.allowed is True, f"{action}/{resource}: {d.policy} {d.reason}"


def test_repository_graph_is_read_only():
    from blast_radius.models import DependencyEdge, RepositoryGraph

    graph = RepositoryGraph(nodes=["a"], edges=[DependencyEdge(source="a", target="b", relationship="depends_on")])
    try:
        graph.nodes = ["c"]  # type: ignore[misc]
        raise AssertionError("RepositoryGraph must be immutable (frozen)")
    except ValueError:
        pass
    assert not hasattr(graph, "add_edge") and not hasattr(graph, "add_node")


if __name__ == "__main__":
    test_analyzer_has_all_four_named_capabilities()
    test_named_capabilities_denied_for_everyone_else()
    test_analyzer_cannot_send_slack_or_approve()
    test_impact_agent_has_no_sandbox_capabilities()
    test_impact_agent_can_read_blast_radius_only()
    test_unknown_identity_sandbox_denied()
    test_analyzer_sandbox_ops_allowed_with_scope()
    test_repository_graph_is_read_only()
    print("=== ALL BLAST RADIUS PERMISSION TESTS PASSED ===")
