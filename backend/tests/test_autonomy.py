"""Risk-based autonomous delivery tests — policy, dispatch, dry-run, approval.

Run: python backend/tests/test_autonomy.py (no GCP, no Slack needed)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_ENV_KEYS = (
    "AUTONOMY_ENABLED", "AUTONOMY_DRY_RUN", "AUTONOMY_MIN_CONFIDENCE",
    "AUTONOMY_MIN_EVIDENCE", "AUTONOMY_ALLOW_ACTION", "AUTONOMY_MAX_AUTO_PER_PR",
)


def _env(**overrides):
    saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
    os.environ.update({
        "AUTONOMY_MIN_CONFIDENCE": "0.9",
        "AUTONOMY_MIN_EVIDENCE": "2",
        "AUTONOMY_ALLOW_ACTION": "false",
        "AUTONOMY_MAX_AUTO_PER_PR": "10",
    })
    os.environ.update(overrides)
    return saved


def _restore(saved):
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


def _msg(mid="msg_1", text="<@U111111> Docs typo fixed on the guides page."):
    return {
        "id": mid, "recipient": "Rahul", "team": "mobile", "channel": "C012345",
        "mode": "group", "text": text, "evidence": ["src/docs.md changed", "readme updated"],
        "slack_user_id": "U111111", "unmapped": False,
    }


def _impact(confidence=0.95, risks=None):
    return {
        "confidence": confidence, "risks": risks or [],
        "affected_components": ["docs-site"],
        "evidence": [{"type": "file_change", "source": "src/docs.md", "detail": "typo fix"}],
    }


def _task(impact_type="awareness", priority="low"):
    return {"team": "mobile", "impact_type": impact_type, "priority": priority,
            "action_required": False, "component": "docs-site"}


# 1. Low-risk automatic delivery ---------------------------------------------
def test_01_low_risk_auto_send():
    from autonomy.policy import evaluate

    saved = _env()
    try:
        decision = evaluate(
            _msg(), impact=_impact(), task=_task(), supervisor_recommendation="AUTO_SEND",
        )
        assert decision.decision == "AUTO_SEND", decision
        assert decision.risk_level == "low"
        # Silent supervisor (no advisory, e.g. old bundle) fails closed.
        decision2 = evaluate(_msg(), impact=_impact(), task=_task())
        assert decision2.decision == "HUMAN_REVIEW", decision2
    finally:
        _restore(saved)


# 2. High confidence but security-sensitive -> HUMAN_REVIEW -------------------
def test_02_security_sensitive_requires_human():
    from autonomy.policy import evaluate

    saved = _env()
    try:
        decision = evaluate(
            _msg(), impact=_impact(confidence=0.99, risks=["OAuth token handling changed"]),
            task=_task(),
        )
        assert decision.decision == "HUMAN_REVIEW", decision
        assert any("human approval" in r for r in decision.reasons), decision.reasons
        # Approval impact always human, regardless of confidence.
        decision2 = evaluate(_msg(), impact=_impact(confidence=0.99), task=_task("approval", "high"))
        assert decision2.decision == "HUMAN_REVIEW", decision2
    finally:
        _restore(saved)


# 3. Insufficient evidence -> HUMAN_REVIEW ------------------------------------
def test_03_insufficient_evidence_review():
    from autonomy.policy import evaluate

    saved = _env()
    try:
        thin = _msg()
        thin["evidence"] = []
        impact = _impact()
        impact["evidence"] = []
        decision = evaluate(thin, impact=impact, task=_task())
        assert decision.decision == "HUMAN_REVIEW", decision
    finally:
        _restore(saved)


# 4. Invalid / unmapped recipients BLOCK --------------------------------------
def test_04_unmapped_blocks():
    from autonomy.policy import evaluate

    saved = _env()
    try:
        ghost = _msg()
        ghost.update({"recipient": "Ghost", "slack_user_id": "", "channel": "", "unmapped": True})
        decision = evaluate(ghost, impact=_impact(), task=_task())
        assert decision.decision == "BLOCK", decision
        # Policy can only downgrade: supervisor AUTO_SEND must not override BLOCK.
        decision2 = evaluate(ghost, impact=_impact(), task=_task(), supervisor_recommendation="AUTO_SEND")
        assert decision2.decision == "BLOCK", decision2
        # Supervisor BLOCK is terminal even if policy would auto-send.
        decision3 = evaluate(_msg(), impact=_impact(), task=_task(), supervisor_recommendation="BLOCK")
        assert decision3.decision == "BLOCK", decision3
    finally:
        _restore(saved)


def _executor(sender):
    from security.audit import InMemoryAuditSink
    from security.execution import InMemoryDeliveryStore, SlackExecutor
    from security.gateway import PermissionGateway
    from security.idempotency import IdempotencyManager, InMemoryIdempotencyStore

    return SlackExecutor(
        gateway=PermissionGateway(audit_sink=InMemoryAuditSink()),
        idempotency=IdempotencyManager(InMemoryIdempotencyStore()),
        delivery_store=InMemoryDeliveryStore(),
        sender=sender,
    )


# 5. Duplicate webhook delivery -> single send ---------------------------------
def test_05_duplicate_delivery_sends_once():
    from autonomy.runner import dispatch_auto_sends, evaluate_batch

    saved = _env()
    try:
        calls: list = []
        ex = _executor(lambda channel, text: (
            calls.append((channel, text)),
            {"status": "sent", "ok": True, "ts": "1.0"},
        )[1])
        kwargs = dict(
            project_id="proj-1", pr_number=7, head_sha="abc",
            messages=[_msg()], impact=_impact(), plan={"tasks": [_task()]},
            supervisor_autonomy=[{"message_id": "msg_1", "recommendation": "AUTO_SEND",
                                  "reasons": ["routine docs fix"], "risk_level": "low",
                                  "confidence": 0.95}],
        )
        batch = evaluate_batch(**kwargs)
        assert batch.decisions[0].decision == "AUTO_SEND"
        # Same idempotency manager across both deliveries (one worker).
        idem = ex._get_idempotency() if hasattr(ex, "_get_idempotency") else None
        out1 = dispatch_auto_sends(
            batch, {"msg_1": _msg()},
            gateway=ex.gateway, idempotency=ex.idempotency,
            delivery_store=ex.delivery_store, sender=ex.sender,
        )
        assert out1["msg_1"]["delivered"] is True and len(calls) == 1
        out2 = dispatch_auto_sends(
            batch, {"msg_1": _msg()},
            gateway=ex.gateway, idempotency=ex.idempotency,
            delivery_store=ex.delivery_store, sender=ex.sender,
        )
        assert out2["msg_1"]["delivered"] is True and len(calls) == 1, "redelivery must reuse, not resend"
        assert idem is not None
    finally:
        _restore(saved)


# 6. Slack API failure recorded, never marked delivered -------------------------
def test_06_slack_failure_not_delivered():
    from autonomy.runner import dispatch_auto_sends, evaluate_batch

    saved = _env()
    try:
        def failing_sender(channel, text):
            raise RuntimeError("slack down")

        ex = _executor(failing_sender)
        batch = evaluate_batch(
            project_id="proj-1", pr_number=7, head_sha="abc",
            messages=[_msg()], impact=_impact(), plan={"tasks": [_task()]},
            supervisor_autonomy=[{"message_id": "msg_1", "recommendation": "AUTO_SEND"}],
        )
        out = dispatch_auto_sends(
            batch, {"msg_1": _msg()},
            gateway=ex.gateway, idempotency=ex.idempotency,
            delivery_store=ex.delivery_store, sender=ex.sender,
        )
        assert out["msg_1"]["delivered"] is False and out["msg_1"]["status"] == "failed"

        # Simulated (mock) send is also never marked delivered.
        ex2 = _executor(lambda channel, text: {"status": "mock_sent", "ok": True, "mock": True})
        out2 = dispatch_auto_sends(
            batch, {"msg_1": _msg()},
            gateway=ex2.gateway, idempotency=ex2.idempotency,
            delivery_store=ex2.delivery_store, sender=ex2.sender,
        )
        assert out2["msg_1"]["delivered"] is False and out2["msg_1"]["status"] == "mock_sent"
    finally:
        _restore(saved)


# 7. Dry-run evaluates without sending ------------------------------------------
def test_07_dry_run_sends_nothing():
    from autonomy.runner import dispatch_auto_sends, evaluate_batch

    saved = _env()
    try:
        calls: list = []
        ex = _executor(lambda channel, text: (calls.append(1), {"status": "sent", "ok": True, "ts": "1"})[1])
        batch = evaluate_batch(
            project_id="proj-1", pr_number=7, head_sha="abc",
            messages=[_msg()], impact=_impact(), plan={"tasks": [_task()]},
            supervisor_autonomy=[{"message_id": "msg_1", "recommendation": "AUTO_SEND"}],
            dry_run=True,
        )
        assert batch.dry_run is True and batch.decisions[0].decision == "AUTO_SEND"
        out = dispatch_auto_sends(
            batch, {"msg_1": _msg()},
            gateway=ex.gateway, idempotency=ex.idempotency,
            delivery_store=ex.delivery_store, sender=ex.sender, dry_run=True,
        )
        assert out["msg_1"] == {"status": "dry_run", "delivered": False}
        assert calls == [], "dry-run must never invoke the sender"
    finally:
        _restore(saved)


# 8. Human approval path still delivers ------------------------------------------
def test_08_human_approval_delivers_review_message():
    from autonomy.policy import evaluate

    saved = _env()
    try:
        decision = evaluate(_msg(), impact=_impact(), task=_task("action", "high"))
        assert decision.decision == "HUMAN_REVIEW", decision
        # The manual path: human approves -> executor sends with human approval id.
        calls: list = []
        ex = _executor(lambda channel, text: (calls.append(1), {"status": "sent", "ok": True, "ts": "2"})[1])
        msg = _msg()
        result = ex.execute_send("proj-1", 7, msg, approval_id="appr_human_1")
        assert result.status == "sent" and result.ok is True and len(calls) == 1
    finally:
        _restore(saved)


def test_azure_strict_schema_typed_autonomy():
    """Azure structured-output requires typed objects (additionalProperties
    handling) — bare dict fields like the old autonomy: list[dict] are
    rejected. Every output-schema collection field must resolve to a model."""
    from agents.supervisor_agent import SupervisionResult

    schema = SupervisionResult.model_json_schema()
    defs = schema.get("$defs", {})
    root_props = schema.get("properties") or defs.get("SupervisionResult", {}).get("properties", {})
    items = root_props["autonomy"]["items"]
    assert "$ref" in items, f"autonomy items must reference a model, got {items}"
    target = defs[items["$ref"].split("/")[-1]]
    assert target.get("type") == "object", target
    for field in ("message_id", "recommendation", "reasons", "risk_level", "confidence"):
        assert field in target.get("properties", {}), field


if __name__ == "__main__":
    test_01_low_risk_auto_send()
    test_02_security_sensitive_requires_human()
    test_03_insufficient_evidence_review()
    test_04_unmapped_blocks()
    test_05_duplicate_delivery_sends_once()
    test_06_slack_failure_not_delivered()
    test_07_dry_run_sends_nothing()
    test_08_human_approval_delivers_review_message()
    test_azure_strict_schema_typed_autonomy()
    print("=== ALL AUTONOMY TESTS PASSED ===")
