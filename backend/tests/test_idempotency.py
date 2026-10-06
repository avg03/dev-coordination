"""Idempotency lifecycle tests — atomic claim, lease, retry semantics.

Run: python backend/tests/test_idempotency.py  (no GCP needed)
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from security.idempotency import (
    ClaimStatus,
    IdempotencyManager,
    InMemoryIdempotencyStore,
)


def _manager(lease_seconds: int = 900) -> IdempotencyManager:
    return IdempotencyManager(InMemoryIdempotencyStore(), lease_seconds=lease_seconds)


def test_first_execution_claims():
    m = _manager()
    key = m.build_key("p1", "wf1", "impact:analyze", {"pr": 1})
    out = m.claim(key, workflow_id="wf1", project_id="p1", step_name="impact:analyze", input_payload={"pr": 1})
    assert out.status == ClaimStatus.EXECUTE, out


def test_completed_returns_stored_result():
    m = _manager()
    key = m.build_key("p1", "wf1", "impact:analyze", {"pr": 1})
    m.claim(key, workflow_id="wf1", project_id="p1", step_name="impact:analyze", input_payload={"pr": 1})
    m.complete(key, {"affected_components": ["auth"]})
    out = m.claim(key, workflow_id="wf1", project_id="p1", step_name="impact:analyze", input_payload={"pr": 1})
    assert out.status == ClaimStatus.COMPLETED
    assert out.result == {"affected_components": ["auth"]}


def test_in_progress_valid_lease_blocks_duplicate():
    m = _manager()
    key = m.build_key("p1", "wf1", "impact:analyze", {"pr": 1})
    m.claim(key, workflow_id="wf1", project_id="p1", step_name="impact:analyze", input_payload={"pr": 1})
    out = m.claim(key, workflow_id="wf1", project_id="p1", step_name="impact:analyze", input_payload={"pr": 1})
    assert out.status == ClaimStatus.IN_PROGRESS, "valid lease must block duplicate execution"


def test_expired_lease_is_reclaimed():
    m = _manager(lease_seconds=0)  # lease already expired on next claim
    key = m.build_key("p1", "wf1", "impact:analyze", {"pr": 1})
    first = m.claim(key, workflow_id="wf1", project_id="p1", step_name="impact:analyze", input_payload={"pr": 1})
    assert first.status == ClaimStatus.EXECUTE
    time.sleep(0.01)
    second = m.claim(key, workflow_id="wf1", project_id="p1", step_name="impact:analyze", input_payload={"pr": 1})
    assert second.status == ClaimStatus.EXECUTE, "expired lease must be reclaimable"
    assert second.record.attempt_count == 2


def test_failed_is_retryable():
    m = _manager()
    key = m.build_key("p1", "wf1", "communication:generate", {"pr": 2})
    m.claim(key, workflow_id="wf1", project_id="p1", step_name="communication:generate", input_payload={"pr": 2})
    m.fail(key, "gemini quota exhausted")
    out = m.claim(key, workflow_id="wf1", project_id="p1", step_name="communication:generate", input_payload={"pr": 2})
    assert out.status == ClaimStatus.EXECUTE, "FAILED steps must be retryable"
    assert out.record.attempt_count == 2


def test_different_input_changes_key():
    m = _manager()
    k1 = m.build_key("p1", "wf1", "impact:analyze", {"pr": 42, "context_version": "v1"})
    k2 = m.build_key("p1", "wf1", "impact:analyze", {"pr": 42, "context_version": "v2"})
    k3 = m.build_key("p1", "wf1", "impact:analyze", {"pr": 43, "context_version": "v1"})
    assert k1 != k2, "ImpactAnalysis(PR42, context_v1) must not reuse (PR42, context_v2)"
    assert k1 != k3
    k4 = m.build_key("p1", "wf1", "impact:analyze", {"pr": 42, "context_version": "v1"})
    assert k1 == k4, "same input must produce the same key"


def test_different_workflow_changes_key():
    m = _manager()
    k1 = m.build_key("p1", "pr:p1:7:aaa", "slack:send", {"message_id": "msg_1"})
    k2 = m.build_key("p1", "pr:p1:7:bbb", "slack:send", {"message_id": "msg_1"})
    assert k1 != k2


def test_volatile_fields_do_not_change_key():
    m = _manager()
    k1 = m.build_key("p1", "wf1", "impact:analyze", {"pr": 1, "saved_at": "2026-01-01T00:00:00Z"})
    k2 = m.build_key("p1", "wf1", "impact:analyze", {"pr": 1, "saved_at": "2026-06-01T00:00:00Z"})
    assert k1 == k2, "timestamps must not affect the key"


def test_complete_requires_known_key():
    m = _manager()
    m.complete("idem_nonexistent", {"x": 1})  # must log-and-return, not raise


def test_lease_fields_present():
    m = _manager()
    key = m.build_key("p1", "wf1", "impact:analyze", {"pr": 9})
    out = m.claim(key, workflow_id="wf1", project_id="p1", step_name="impact:analyze", input_payload={"pr": 9})
    assert out.record.lease_until is not None
    lease = datetime.fromisoformat(out.record.lease_until)
    assert lease > datetime.now(timezone.utc), "lease must be in the future"
    assert out.record.status == "IN_PROGRESS"
    assert out.record.completed_at is None


if __name__ == "__main__":
    test_first_execution_claims()
    test_completed_returns_stored_result()
    test_in_progress_valid_lease_blocks_duplicate()
    test_expired_lease_is_reclaimed()
    test_failed_is_retryable()
    test_different_input_changes_key()
    test_different_workflow_changes_key()
    test_volatile_fields_do_not_change_key()
    test_complete_requires_known_key()
    test_lease_fields_present()
    print("=== ALL IDEMPOTENCY TESTS PASSED ===")
