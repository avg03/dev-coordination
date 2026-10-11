"""Slack execution protection tests — exactly-once delivery through the gateway.

Run: python backend/tests/test_slack_execution.py  (no GCP, no real Slack sends)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from security.approval import ApprovalValidationError, build_approval, validate_approval_for_message
from security.audit import InMemoryAuditSink
from security.execution import InMemoryDeliveryStore, SlackExecutor
from security.gateway import PermissionGateway
from security.idempotency import IdempotencyManager, InMemoryIdempotencyStore


def _executor(sender_calls: list):
    def fake_sender(channel: str, text: str) -> dict:
        sender_calls.append({"channel": channel, "text": text})
        return {"status": "sent", "ok": True, "ts": "1700000000.000100"}

    sink = InMemoryAuditSink()
    return SlackExecutor(
        gateway=PermissionGateway(audit_sink=sink),
        idempotency=IdempotencyManager(InMemoryIdempotencyStore()),
        delivery_store=InMemoryDeliveryStore(),
        sender=fake_sender,
        audit_sink=sink,
    )


def _message(mid: str = "msg_1", channel: str = "C012345") -> dict:
    return {
        "id": mid,
        "recipient": "Rahul",
        "team": "mobile",
        "channel": channel,
        "mode": "personal",
        "text": "PR #7 changed the auth API response.",
        "evidence": ["src/auth.py modified"],
    }


def test_approved_message_sends_exactly_once():
    calls: list = []
    ex = _executor(calls)
    r1 = ex.execute_send("proj-1", 7, _message(), approval_id="appr_1")
    assert r1.status == "sent" and r1.ok is True and r1.reused is False
    assert len(calls) == 1, f"expected exactly one Slack call, got {len(calls)}"


def test_retried_approval_returns_stored_result_not_duplicate():
    calls: list = []
    ex = _executor(calls)
    ex.execute_send("proj-1", 7, _message(), approval_id="appr_1")
    r2 = ex.execute_send("proj-1", 7, _message(), approval_id="appr_2")  # different approval, same send
    assert r2.reused is True, "retry must reuse the stored delivery result"
    assert r2.status == "sent"
    assert len(calls) == 1, "underlying Slack call must NOT execute twice"


def test_unselected_message_never_sent():
    approval = build_approval(
        approval_id="appr_1",
        project_id="proj-1",
        pr_number=7,
        approved=True,
        approved_at="2026-10-06T00:00:00Z",
        selected_message_ids=["msg_1"],
    )
    try:
        validate_approval_for_message(approval, "proj-1", "7", "msg_2", _message("msg_2"))
        raise AssertionError("unselected message must be rejected")
    except ApprovalValidationError:
        pass


def test_invalid_message_id_rejected():
    approval = build_approval(
        approval_id="appr_1",
        project_id="proj-1",
        pr_number=7,
        approved=True,
        approved_at="2026-10-06T00:00:00Z",
        selected_message_ids=["msg_1", "msg_3"],
    )
    try:
        validate_approval_for_message(approval, "proj-1", "7", "msg_999", _message("msg_999"))
        raise AssertionError("invalid message id must be rejected")
    except ApprovalValidationError:
        pass


def test_approval_project_mismatch_rejected():
    approval = build_approval("appr_1", "other-project", 7, True, "2026-10-06T00:00:00Z", ["msg_1"])
    try:
        validate_approval_for_message(approval, "proj-1", "7", "msg_1", _message())
        raise AssertionError("project mismatch must be rejected")
    except ApprovalValidationError:
        pass


def test_message_without_channel_denied():
    calls: list = []
    ex = _executor(calls)
    result = ex.execute_send("proj-1", 7, _message(channel=""), approval_id="appr_1")
    assert result.status == "denied", "send without recipient must be denied"
    assert len(calls) == 0


def test_denied_send_releases_claim_and_can_be_retried():
    """Orphan delivery state (crash before idempotency record) denies the send,
    releases the claim, and the same key can execute once state is reconciled."""
    calls: list = []
    store = InMemoryDeliveryStore()
    ex = _executor(calls)
    ex.delivery_store = store
    # Crash orphan: delivered state exists but no idempotency record.
    store.write_delivery("proj-1", 7, "msg_1", {"status": "sent", "ok": True})
    d1 = ex.execute_send("proj-1", 7, _message(), approval_id="appr_1")
    assert d1.status == "denied", "already-delivered orphan must be denied"
    assert len(calls) == 0
    # Reconciled: orphan state removed -> same key re-executes exactly once.
    store.state.clear()
    d2 = ex.execute_send("proj-1", 7, _message(), approval_id="appr_1")
    assert d2.status == "sent" and d2.reused is False
    assert len(calls) == 1


def test_communication_agent_has_no_send_path():
    from agents.communication_agent import CommunicationAgent
    from security.permissions import AuthorizationRequest
    from security.gateway import PermissionGateway
    from security.audit import InMemoryAuditSink
    from security.identities import Action, Resource

    assert not hasattr(CommunicationAgent, "_mock_send_all"), "_mock_send_all must be removed"
    assert not hasattr(CommunicationAgent, "_send_slack"), "_send_slack must be removed"
    assert not hasattr(CommunicationAgent, "_mock_send_slack"), "_mock_send_slack must be removed"

    source = Path(CommunicationAgent.__module__.replace(".", "/") + ".py")
    # fallback for package-relative resolution
    if not source.exists():
        import agents.communication_agent as mod

        source = Path(mod.__file__)
    text = source.read_text(encoding="utf-8")
    assert "slack_service" not in text, "CommunicationAgent must not import the Slack service"

    gw = PermissionGateway(audit_sink=InMemoryAuditSink())
    d = gw.authorize(
        AuthorizationRequest(
            agent_id="communication_agent", action=Action.SEND, resource=Resource.SLACK, project_id="p"
        )
    )
    assert d.allowed is False


if __name__ == "__main__":
    test_approved_message_sends_exactly_once()
    test_retried_approval_returns_stored_result_not_duplicate()
    test_unselected_message_never_sent()
    test_invalid_message_id_rejected()
    test_approval_project_mismatch_rejected()
    test_message_without_channel_denied()
    test_denied_send_releases_claim_and_can_be_retried()
    test_communication_agent_has_no_send_path()
    print("=== ALL SLACK EXECUTION TESTS PASSED ===")
