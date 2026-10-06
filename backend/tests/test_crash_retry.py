"""Crash/retry simulation — the critical failure case from spec §16.

    execute step -> result persisted -> simulated failure before the caller
    receives the response -> retry same step -> stored result returned ->
    underlying operation NOT executed twice.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from security.audit import InMemoryAuditSink
from security.execution import InMemoryDeliveryStore, SlackExecutor
from security.gateway import PermissionGateway
from security.idempotency import ClaimStatus, IdempotencyManager, InMemoryIdempotencyStore


def test_business_step_retry_after_crash_executes_once():
    """Workflow-level step: crash after complete() but before the caller
    sees the response (e.g. Pub/Sub connection drop)."""
    idem = IdempotencyManager(InMemoryIdempotencyStore())
    calls = {"n": 0}

    def expensive_step() -> dict:
        calls["n"] += 1
        return {"affected_components": ["auth", "api"], "confidence": 0.91}

    key = idem.build_key("proj-1", "pr:proj-1:42:abc", "impact:analyze", {"pr": 42})

    # Attempt 1: claim, execute, persist result, mark COMPLETED...
    out = idem.claim(key, workflow_id="pr:proj-1:42:abc", project_id="proj-1",
                     step_name="impact:analyze", input_payload={"pr": 42})
    assert out.status == ClaimStatus.EXECUTE
    result = expensive_step()
    idem.complete(key, result)
    # ...then the response is lost (simulated crash). Nobody retries `expensive_step`.

    # Attempt 2 (redelivery): stored result returned, underlying op NOT re-run.
    out2 = idem.claim(key, workflow_id="pr:proj-1:42:abc", project_id="proj-1",
                      step_name="impact:analyze", input_payload={"pr": 42})
    assert out2.status == ClaimStatus.COMPLETED
    assert out2.result == result
    assert calls["n"] == 1, f"step executed {calls['n']} times after crash-retry"


def test_slack_send_retry_after_crash_sends_once():
    """The Slack boundary: sender succeeds, result persisted, COMPLETED marked,
    response lost. A retry must return the stored delivery, not re-send."""
    slack_calls: list = []

    def flaky_sender(channel: str, text: str) -> dict:
        slack_calls.append({"channel": channel, "text": text})
        return {"status": "sent", "ok": True, "ts": "1700000000.000100"}

    ex = SlackExecutor(
        gateway=PermissionGateway(audit_sink=InMemoryAuditSink()),
        idempotency=IdempotencyManager(InMemoryIdempotencyStore()),
        delivery_store=InMemoryDeliveryStore(),
        sender=flaky_sender,
    )
    message = {
        "id": "msg_1",
        "recipient": "Rahul",
        "team": "mobile",
        "channel": "U012345",
        "mode": "personal",
        "text": "PR #7 changed the auth API response.",
        "evidence": ["src/auth.py modified"],
    }

    r1 = ex.execute_send("proj-1", 7, message, approval_id="appr_1")
    assert r1.status == "sent"
    assert len(slack_calls) == 1

    # Caller crashed before seeing r1 — retry (same bundle, message, channel, text):
    r2 = ex.execute_send("proj-1", 7, message, approval_id="appr_1")
    assert r2.reused is True
    assert r2.status == "sent" and r2.slack_ts == r1.slack_ts
    assert len(slack_calls) == 1, f"Slack called {len(slack_calls)} times; duplicate message sent!"


def test_failed_step_marked_failed_then_retry_executes_again():
    """A step that raises must be marked FAILED and be retryable — not stuck IN_PROGRESS."""
    idem = IdempotencyManager(InMemoryIdempotencyStore())
    key = idem.build_key("proj-1", "wf-1", "communication:generate", {"pr": 5})
    idem.claim(key, workflow_id="wf-1", project_id="proj-1",
               step_name="communication:generate", input_payload={"pr": 5})
    idem.fail(key, "gemini 429 quota exhausted")

    out = idem.claim(key, workflow_id="wf-1", project_id="proj-1",
                     step_name="communication:generate", input_payload={"pr": 5})
    assert out.status == ClaimStatus.EXECUTE, "failed step must re-execute on retry"
    assert out.record.attempt_count == 2


if __name__ == "__main__":
    test_business_step_retry_after_crash_executes_once()
    test_slack_send_retry_after_crash_sends_once()
    test_failed_step_marked_failed_then_retry_executes_again()
    print("=== ALL CRASH/RETRY TESTS PASSED ===")
