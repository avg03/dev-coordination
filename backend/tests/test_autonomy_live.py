"""Opt-in live autonomy dispatch test for the configured Slack test channel.

Run explicitly:

    RUN_LIVE_AUTONOMY_SLACK_TEST=true python -m pytest backend/tests/test_autonomy_live.py -s

This test sends one clearly labelled message and intentionally does not delete
it, so the Slack result remains visible for verification.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=False)


def _trace(name: str, value: object) -> None:
    print(f"[TRACE] {name}={value!r}")


def test_live_autonomy_send_to_slack_test_channel() -> None:
    """Run the current autonomy policy and deliver one real Slack message."""
    if os.getenv("RUN_LIVE_AUTONOMY_SLACK_TEST", "").strip().lower() not in {
        "true",
        "1",
        "yes",
        "on",
    }:
        pytest.skip("Set RUN_LIVE_AUTONOMY_SLACK_TEST=true to enable the live send")

    from autonomy import config
    from autonomy.runner import dispatch_auto_sends, evaluate_batch
    from security.audit import InMemoryAuditSink
    from security.execution import InMemoryDeliveryStore
    from security.gateway import PermissionGateway
    from security.idempotency import IdempotencyManager, InMemoryIdempotencyStore

    channel = os.getenv("SLACK_TEST_CHANNEL", "").strip()
    token_present = bool(os.getenv("SLACK_BOT_TOKEN", "").strip())
    if not channel:
        pytest.fail("SLACK_TEST_CHANNEL is not configured")
    if not token_present:
        pytest.fail("SLACK_BOT_TOKEN is not configured")

    original_strict = os.environ.get("SLACK_STRICT")
    os.environ["SLACK_STRICT"] = "true"
    try:
        message = {
            "id": "live-autonomy-test-msg",
            "recipient": "Slack test channel",
            "team": "test",
            "channel": channel,
            "mode": "group",
            "text": ":robot_face: <@U99999999> Live autonomy test message - please ignore :test_tube:",
            "evidence": ["live autonomy integration test", "configured Slack test channel"],
            "slack_user_id": "U99999999",
            "unmapped": False,
            "impact_type": "awareness",
        }
        impact = {
            "confidence": 0.95,
            "risks": [],
            "affected_components": ["test-only"],
            "evidence": [
                {"type": "test", "source": "test_autonomy_live.py", "detail": "live dispatch"},
            ],
        }
        task = {
            "team": "test",
            "impact_type": "awareness",
            "priority": "low",
            "action_required": False,
            "component": "test-only",
        }

        _trace("AUTONOMY_ENABLED", config.enabled())
        _trace("AUTONOMY_DRY_RUN", config.dry_run())
        _trace("AUTONOMY_ALLOW_ACTION", config.allow_action())
        _trace("AUTONOMY_MIN_CONFIDENCE", config.min_confidence())
        _trace("AUTONOMY_MIN_EVIDENCE", config.min_evidence())
        _trace("AUTONOMY_MAX_AUTO_PER_PR", config.max_auto_per_pr())
        _trace("SLACK_TEST_CHANNEL_SET", bool(channel))
        _trace("SLACK_BOT_TOKEN_SET", token_present)
        _trace("SLACK_STRICT", os.environ["SLACK_STRICT"])

        if not config.enabled():
            pytest.fail("AUTONOMY_ENABLED must be true for this live workflow test")
        if config.dry_run():
            pytest.fail("AUTONOMY_DRY_RUN must be false for a real Slack send")

        batch = evaluate_batch(
            project_id="live-autonomy-test",
            pr_number=0,
            head_sha="live-autonomy-test",
            messages=[message],
            impact=impact,
            plan={"tasks": [task], "requires_approval": False},
            canonical={"classification_status": "validated", "change_type": "test"},
            supervisor_autonomy=[
                {
                    "message_id": message["id"],
                    "recommendation": "AUTO_SEND",
                    "reasons": ["explicit live integration test"],
                    "risk_level": "low",
                    "confidence": 0.95,
                }
            ],
            delivered_ids=set(),
            dry_run=False,
        )
        decision = batch.decisions[0]
        _trace("decision", decision.decision)
        _trace("decision_reasons", decision.reasons)
        _trace("decision_risk_level", decision.risk_level)
        _trace("decision_confidence", decision.confidence)
        _trace("decision_evidence_count", decision.evidence_count)
        assert decision.decision == "AUTO_SEND", decision

        outcomes = dispatch_auto_sends(
            batch,
            {message["id"]: message},
            gateway=PermissionGateway(audit_sink=InMemoryAuditSink()),
            idempotency=IdempotencyManager(InMemoryIdempotencyStore()),
            delivery_store=InMemoryDeliveryStore(),
        )
        outcome = outcomes[message["id"]]
        _trace("send_status", outcome.get("status"))
        _trace("send_delivered", outcome.get("delivered"))
        _trace("send_channel", outcome.get("channel"))
        _trace("send_slack_ts", outcome.get("slack_ts"))
        _trace("send_reused", outcome.get("reused"))
        _trace("send_reason", outcome.get("reason"))
        assert outcome["status"] == "sent", outcome
        assert outcome["delivered"] is True, outcome
    finally:
        if original_strict is None:
            os.environ.pop("SLACK_STRICT", None)
        else:
            os.environ["SLACK_STRICT"] = original_strict
