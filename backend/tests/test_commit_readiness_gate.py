"""Commit readiness gate tests — event-driven PR dependency coordination.

Covers the 15 required scenarios with in-memory fakes (no GCP, no secrets):
  1. all completed -> ready
  2. one pending -> not ready
  3. one processing -> waiting
  4. enriched missing despite COMPLETED -> not ready
  5. completion event -> readiness re-evaluated + dispatch
  6. multi-commit PR starts only after every commit ready
  7. event before waiting persisted -> post-registration recheck recovers
  8. duplicate delivery -> no duplicate execution
  9. concurrent completions -> single claim
  10. publish failure after persist -> outbox flush later
  11. crash after claim -> lease recovery
  12. revision change -> stale cannot overwrite
  13. timeout recoverable -> partial path used
  14. timeout permanently missing -> FAILED persisted
  15. gateway/idempotency invariants for new identities hold

Run: python backend/tests/test_commit_readiness_gate.py (no GCP needed)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from coordination.events import build_completion_event, flush_outbox, publish_completion_event
from coordination.gate import can_produce_partial, check_readiness, classify_commit_error
from coordination.models import CommitProcessingRecord, PRWorkflowRecord
from coordination.reconciler import reconcile_waiting
from coordination.stores import (
    InMemoryCommitStatusStore,
    InMemoryOutboxStore,
    InMemoryPRWorkflowStore,
)
from security.idempotency import ClaimStatus, IdempotencyManager, InMemoryIdempotencyStore
from workers.completion_consumer import CompletionConsumer

PROJ = "proj-test"
REPO = "acme/widget"


def _completed(store, sha, project=PROJ, repo=REPO):
    store.put(CommitProcessingRecord(project_id=project, repository=repo, commit_sha=sha, status="COMPLETED", attempt_count=1))


def _status(store, sha, status, project=PROJ, repo=REPO):
    store.put(CommitProcessingRecord(project_id=project, repository=repo, commit_sha=sha, status=status, attempt_count=1))


def _enriched_yes(*a):
    return True


def _waiting_pr(store, pr=7, head="headAAA", shas=None):
    rec = PRWorkflowRecord(
        project_id=PROJ, owner="acme", repository="widget", pr_number=pr,
        head_sha=head, required_shas=list(shas or []), status="WAITING_FOR_COMMITS",
    )
    store.put(rec)
    return rec


# 1. All completed -> ready -------------------------------------------------
def test_01_all_completed_ready():
    s = InMemoryCommitStatusStore()
    _completed(s, "sha1")
    _completed(s, "sha2")
    ready, missing, failed = check_readiness(s, _enriched_yes, project_id=PROJ, repository=REPO, required_shas=["sha1", "sha2"])
    assert ready and missing == [] and failed == [], (ready, missing, failed)


# 2. One pending -> not ready -----------------------------------------------
def test_02_one_pending_not_ready():
    s = InMemoryCommitStatusStore()
    _completed(s, "sha1")
    _status(s, "sha2", "PENDING")
    ready, missing, _ = check_readiness(s, _enriched_yes, project_id=PROJ, repository=REPO, required_shas=["sha1", "sha2"])
    assert not ready and missing == ["sha2"], missing


# 3. One processing -> waiting ----------------------------------------------
def test_03_one_processing_waiting():
    s = InMemoryCommitStatusStore()
    _completed(s, "sha1")
    _status(s, "sha2", "PROCESSING")
    ready, missing, _ = check_readiness(s, _enriched_yes, project_id=PROJ, repository=REPO, required_shas=["sha1", "sha2"])
    assert not ready and missing == ["sha2"]


# 4. Enriched missing despite COMPLETED -> not ready -------------------------
def test_04_enriched_missing_blocks():
    s = InMemoryCommitStatusStore()
    _completed(s, "sha1")
    ready, missing, _ = check_readiness(s, lambda p, sh: False, project_id=PROJ, repository=REPO, required_shas=["sha1"])
    assert not ready and missing == ["sha1"], "missing enriched must block PR processing"


# 5+6. Completion event re-evaluates; multi-commit starts only when all ready
def _consumer(pr_store, status_store, dispatched, enriched_fn=_enriched_yes):
    idem = IdempotencyManager(InMemoryIdempotencyStore())
    return CompletionConsumer(
        project="test-proj", pr_store=pr_store, commit_status_store=status_store,
        idempotency_manager=idem, enriched_exists_fn=enriched_fn,
        dispatch_fn=lambda rec: dispatched.append((rec.pr_number, rec.head_sha)),
    )


def test_05_completion_triggers_dispatch_when_ready():
    pr_store = InMemoryPRWorkflowStore()
    status_store = InMemoryCommitStatusStore()
    _waiting_pr(pr_store, shas=["sha1"])
    _completed(status_store, "sha1")
    dispatched: list = []
    consumer = _consumer(pr_store, status_store, dispatched)
    event = build_completion_event(repository=REPO, owner="acme", project_id=PROJ, commit_sha="sha1", status="COMPLETED")
    n = consumer.handle_event_dict({"completion": event.model_dump(mode="json")})
    assert n == 1 and dispatched == [(7, "headAAA")], (n, dispatched)


def test_06_multi_commit_waits_for_all():
    pr_store = InMemoryPRWorkflowStore()
    status_store = InMemoryCommitStatusStore()
    _waiting_pr(pr_store, shas=["sha1", "sha2"])
    _completed(status_store, "sha1")
    _status(status_store, "sha2", "PROCESSING")
    dispatched: list = []
    consumer = _consumer(pr_store, status_store, dispatched)
    event = build_completion_event(repository=REPO, owner="acme", project_id=PROJ, commit_sha="sha1", status="COMPLETED")
    n = consumer.handle_event_dict({"completion": event.model_dump(mode="json")})
    assert n == 0 and dispatched == [], "must not start until EVERY commit is ready"
    _completed(status_store, "sha2")
    event2 = build_completion_event(repository=REPO, owner="acme", project_id=PROJ, commit_sha="sha2", status="COMPLETED")
    n2 = consumer.handle_event_dict({"completion": event2.model_dump(mode="json")})
    assert n2 == 1 and dispatched == [(7, "headAAA")]


# 7. Event before waiting persisted -> recheck recovers ----------------------
def test_07_lost_event_recovered_by_recheck():
    pr_store = InMemoryPRWorkflowStore()
    status_store = InMemoryCommitStatusStore()
    _completed(status_store, "sha1")
    # Event arrives first: no waiting PR exists yet -> nothing dispatched.
    dispatched: list = []
    consumer = _consumer(pr_store, status_store, dispatched)
    event = build_completion_event(repository=REPO, owner="acme", project_id=PROJ, commit_sha="sha1", status="COMPLETED")
    assert consumer.handle_event_dict({"completion": event.model_dump(mode="json")}) == 0
    # PR registers WAITING afterwards; post-registration recheck finds ready.
    _waiting_pr(pr_store, shas=["sha1"])
    ready, _, _ = check_readiness(status_store, _enriched_yes, project_id=PROJ, repository=REPO, required_shas=["sha1"])
    assert ready, "post-registration recheck must recover the lost event"

    def _dispatch(rec):
        dispatched.append(1)
        return True

    stats = reconcile_waiting(pr_store, status_store, _enriched_yes, _dispatch)
    assert stats["dispatched"] == 1, stats


# 8. Duplicate delivery -> no duplicate execution ----------------------------
def test_08_duplicate_delivery_harmless():
    pr_store = InMemoryPRWorkflowStore()
    status_store = InMemoryCommitStatusStore()
    _waiting_pr(pr_store, shas=["sha1"])
    _completed(status_store, "sha1")
    dispatched: list = []
    consumer = _consumer(pr_store, status_store, dispatched)
    payload = {"completion": build_completion_event(repository=REPO, owner="acme", project_id=PROJ, commit_sha="sha1", status="COMPLETED").model_dump(mode="json")}
    assert consumer.handle_event_dict(dict(payload)) == 1
    assert consumer.handle_event_dict(dict(payload)) == 0, "duplicate must be deduped"
    assert len(dispatched) == 1, dispatched


# 9. Concurrent completions -> single claim ----------------------------------
def test_09_concurrent_events_single_claim():
    pr_store = InMemoryPRWorkflowStore()
    status_store = InMemoryCommitStatusStore()
    _waiting_pr(pr_store, shas=["sha1"])
    _completed(status_store, "sha1")
    dispatched: list = []
    shared_idem = IdempotencyManager(InMemoryIdempotencyStore())
    c1 = CompletionConsumer(project="test-proj", pr_store=pr_store, commit_status_store=status_store, idempotency_manager=shared_idem, enriched_exists_fn=_enriched_yes, dispatch_fn=lambda rec: dispatched.append(1))
    c2 = CompletionConsumer(project="test-proj", pr_store=pr_store, commit_status_store=status_store, idempotency_manager=shared_idem, enriched_exists_fn=_enriched_yes, dispatch_fn=lambda rec: dispatched.append(1))
    payload = {"completion": build_completion_event(repository=REPO, owner="acme", project_id=PROJ, commit_sha="sha1", status="COMPLETED").model_dump(mode="json")}
    # Same event through two consumers sharing idempotency + dispatch claim.
    r1 = c1.handle_event_dict(dict(payload))
    r2 = c2.handle_event_dict(dict(payload))
    assert r1 + r2 == 1 and len(dispatched) == 1, (r1, r2, dispatched)


# 10. Publish failure after persist -> outbox flush later --------------------
def test_10_outbox_recovers_failed_publish():
    outbox = InMemoryOutboxStore()
    event = build_completion_event(repository=REPO, owner="acme", project_id=PROJ, commit_sha="sha1", status="COMPLETED")
    calls = {"n": 0}

    def flaky_publish(msg, attrs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("pubsub unavailable")
        return "msg-id-1"

    assert publish_completion_event(event, flaky_publish, outbox=outbox, outbox_key="k1") is False
    assert len(outbox.list_pending()) == 1
    assert flush_outbox(outbox, flaky_publish) == 1
    assert outbox.list_pending() == []


# 11. Crash after claim -> lease recovery ------------------------------------
def test_11_crash_after_claim_recovers():
    idem = IdempotencyManager(InMemoryIdempotencyStore(), lease_seconds=0)
    key = idem.build_key(PROJ, "pr:proj:7:headAAA", "pr:dispatch", {"head_sha": "headAAA"})
    first = idem.claim(key, workflow_id="pr:proj:7:headAAA", project_id=PROJ, step_name="pr:dispatch", input_payload={"head_sha": "headAAA"})
    assert first.status == ClaimStatus.EXECUTE
    # Crash before complete() — expired lease is reclaimable, never stuck.
    import time

    time.sleep(0.01)
    second = idem.claim(key, workflow_id="pr:proj:7:headAAA", project_id=PROJ, step_name="pr:dispatch", input_payload={"head_sha": "headAAA"})
    assert second.status == ClaimStatus.EXECUTE and second.record.attempt_count == 2


# 12. Revision change -> stale cannot overwrite -------------------------------
def test_12_revision_change_isolated():
    assert PRWorkflowRecord.doc_id(PROJ, 7, "headAAA") != PRWorkflowRecord.doc_id(PROJ, 7, "headBBB")
    pr_store = InMemoryPRWorkflowStore()
    old = PRWorkflowRecord(project_id=PROJ, owner="acme", repository="widget", pr_number=7, head_sha="headAAA", required_shas=["s1"], status="WAITING_FOR_COMMITS")
    pr_store.put(old)
    # New revision registers separately; old is superseded, never overwrites new.
    new = PRWorkflowRecord(project_id=PROJ, owner="acme", repository="widget", pr_number=7, head_sha="headBBB", required_shas=["s1", "s2"], status="WAITING_FOR_COMMITS")
    pr_store.put(new)
    olds = [r for r in pr_store.list_for_pr(PROJ, 7) if r.head_sha == "headAAA"]
    news = [r for r in pr_store.list_for_pr(PROJ, 7) if r.head_sha == "headBBB"]
    assert len(olds) == 1 and len(news) == 1 and news[0].required_shas == ["s1", "s2"]


# 13+14. Timeout: partial when meaningful, FAILED when nothing available ------
def test_13_timeout_partial_path():
    assert can_produce_partial(1, 2) is True
    pr_store = InMemoryPRWorkflowStore()
    status_store = InMemoryCommitStatusStore()
    _completed(status_store, "sha1")  # 1 of 2 available
    rec = PRWorkflowRecord(
        project_id=PROJ, owner="acme", repository="widget", pr_number=7, head_sha="h",
        required_shas=["sha1", "sha2"], status="WAITING_FOR_COMMITS",
        wait_started_at="2020-01-01T00:00:00+00:00", deadline_at="2020-01-01T00:01:00+00:00",
    )
    pr_store.put(rec)
    dispatched: list = []

    def _dispatch13(r):
        dispatched.append(1)
        return True

    stats = reconcile_waiting(pr_store, status_store, _enriched_yes, _dispatch13)
    assert stats["timed_out"] == 1, stats
    updated = pr_store.get(PROJ, 7, "h")
    assert updated is not None and updated.partial is True and updated.status in ("READY", "PROCESSING", "COMPLETED", "PARTIAL"), updated
    assert dispatched == [1]


def test_14_timeout_permanent_missing_failed():
    assert can_produce_partial(0, 2) is False
    pr_store = InMemoryPRWorkflowStore()
    status_store = InMemoryCommitStatusStore()  # nothing completed
    rec = PRWorkflowRecord(
        project_id=PROJ, owner="acme", repository="widget", pr_number=7, head_sha="h",
        required_shas=["sha1", "sha2"], status="WAITING_FOR_COMMITS",
        wait_started_at="2020-01-01T00:00:00+00:00", deadline_at="2020-01-01T00:01:00+00:00",
    )
    pr_store.put(rec)
    stats = reconcile_waiting(pr_store, status_store, _enriched_yes, lambda r: True)
    assert stats["timed_out"] == 1, stats
    updated = pr_store.get(PROJ, 7, "h")
    assert updated is not None and updated.status == "FAILED" and set(updated.missing_shas) == {"sha1", "sha2"}, updated


# 15. Gateway/idempotency invariants for new identities -----------------------
def test_15_new_identities_constrained():
    from security.gateway import PermissionGateway
    from security.audit import InMemoryAuditSink
    from security.identities import Action, AgentIdentity, Resource
    from security.permissions import AuthorizationRequest

    gw = PermissionGateway(audit_sink=InMemoryAuditSink())
    # Completion consumer CAN read PR workflows...
    allowed = gw.authorize(AuthorizationRequest(agent_id=AgentIdentity.COMPLETION_CONSUMER.value, action=Action.READ, resource=Resource.PR_WORKFLOWS, project_id=PROJ))
    assert allowed.allowed, allowed
    # ...but CANNOT send Slack or execute agents.
    denied_slack = gw.authorize(AuthorizationRequest(agent_id=AgentIdentity.COMPLETION_CONSUMER.value, action=Action.SEND, resource=Resource.SLACK, project_id=PROJ))
    assert not denied_slack.allowed
    denied_exec = gw.authorize(AuthorizationRequest(agent_id=AgentIdentity.COMPLETION_CONSUMER.value, action=Action.EXECUTE, resource=Resource.SLACK, project_id=PROJ))
    assert not denied_exec.allowed
    # Commit worker CAN write status + publish events but CANNOT send Slack.
    w = gw.authorize(AuthorizationRequest(agent_id=AgentIdentity.COMMIT_WORKER.value, action=Action.WRITE, resource=Resource.COMMIT_STATUS, project_id=PROJ))
    assert w.allowed, w
    w_slack = gw.authorize(AuthorizationRequest(agent_id=AgentIdentity.COMMIT_WORKER.value, action=Action.SEND, resource=Resource.SLACK, project_id=PROJ))
    assert not w_slack.allowed
    # Unknown identities still denied.
    unknown = gw.authorize(AuthorizationRequest(agent_id="evil_agent", action=Action.READ, resource=Resource.PR_WORKFLOWS, project_id=PROJ))
    assert not unknown.allowed


def test_gate_error_classification_safe():
    from github_infrastructure.change_records import GitHubApiError

    kind, summary = classify_commit_error(GitHubApiError(404, "Not Found"))
    assert kind == "permanent" and "GITHUB_TOKEN" not in summary
    kind2, _ = classify_commit_error(RuntimeError("connection reset"))
    assert kind2 == "transient"


def test_pr_gate_blocks_agents_until_ready():
    """PRWorkflow._gate_check persists WAITING and returns wait without agents."""
    from workflows.pr_workflow import PRWorkflow

    pr_store = InMemoryPRWorkflowStore()
    status_store = InMemoryCommitStatusStore()
    _status(status_store, "sha1", "PROCESSING")
    wf = PRWorkflow(
        project="test-proj", pr_store=pr_store, commit_status_store=status_store,
        enriched_exists_fn=_enriched_yes,
    )
    from workflows.pr_workflow import PREvent

    event = PREvent(project_id=PROJ, owner="acme", repository="widget", pr_number=7, action="opened", head_sha="headAAA")
    gate = asyncio.run(wf._gate_check(event, "pr:proj-test:7:headAAA", ["sha1"]))
    assert gate["decision"] == "wait", gate
    stored = pr_store.get(PROJ, 7, "headAAA")
    assert stored is not None and stored.status == "WAITING_FOR_COMMITS" and stored.missing_shas == ["sha1"], stored
    # Post-registration recheck path: complete the commit, gate now proceeds.
    _completed(status_store, "sha1")
    gate2 = asyncio.run(wf._gate_check(event, "pr:proj-test:7:headAAA", ["sha1"]))
    assert gate2["decision"] == "proceed", gate2


if __name__ == "__main__":
    test_01_all_completed_ready()
    test_02_one_pending_not_ready()
    test_03_one_processing_waiting()
    test_04_enriched_missing_blocks()
    test_05_completion_triggers_dispatch_when_ready()
    test_06_multi_commit_waits_for_all()
    test_07_lost_event_recovered_by_recheck()
    test_08_duplicate_delivery_harmless()
    test_09_concurrent_events_single_claim()
    test_10_outbox_recovers_failed_publish()
    test_11_crash_after_claim_recovers()
    test_12_revision_change_isolated()
    test_13_timeout_partial_path()
    test_14_timeout_permanent_missing_failed()
    test_15_new_identities_constrained()
    test_gate_error_classification_safe()
    test_pr_gate_blocks_agents_until_ready()
    print("=== ALL COMMIT READINESS GATE TESTS PASSED ===")
