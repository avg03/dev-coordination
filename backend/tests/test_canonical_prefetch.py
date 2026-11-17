"""Canonical change context + concurrent evidence prefetch tests.

Run: python backend/tests/test_canonical_prefetch.py (no GCP, no LLM needed)
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _enriched(sha, change_type, components=None, summary=""):
    return {
        "commit_sha": sha,
        "repository": "acme/widget",
        "change_type": change_type,
        "components": components or ["auth"],
        "impact_scope": components or ["auth"],
        "summary": summary or f"{change_type} in auth",
        "changed_files": [{"path": "src/auth.py"}],
    }


def _base_kwargs(**over):
    kwargs = dict(
        project_id="proj-1", owner="acme", repository="widget",
        pr_number=3, head_sha="headAAA", enriched_records=[],
    )
    kwargs.update(over)
    return kwargs


# Canonical resolution -------------------------------------------------------
def test_fix_stays_fix():
    from coordination.canonical import resolve_canonical

    canonical = resolve_canonical(**_base_kwargs(enriched_records=[
        _enriched("a1", "fix"), _enriched("b2", "fix"),
    ]))
    assert canonical.change_type == "fix" and canonical.classification_status == "validated", canonical
    assert canonical.component == "auth" and canonical.head_sha == "headAAA"


def test_refactor_stays_refactor():
    from coordination.canonical import resolve_canonical

    canonical = resolve_canonical(**_base_kwargs(enriched_records=[_enriched("a1", "refactor")]))
    assert canonical.change_type == "refactor" and canonical.classification_status == "validated"


def test_bug_normalizes_to_fix():
    from coordination.canonical import normalize_change_type, resolve_canonical

    assert normalize_change_type("bug") == "fix"
    canonical = resolve_canonical(**_base_kwargs(enriched_records=[
        _enriched("a1", "bug"), _enriched("b2", "fix"),
    ]))
    assert canonical.change_type == "fix" and canonical.classification_status == "validated"


def test_unknown_abstains():
    from coordination.canonical import resolve_canonical

    canonical = resolve_canonical(**_base_kwargs(enriched_records=[
        _enriched("a1", "unknown"), _enriched("b2", "fix"),
    ]))
    assert canonical.change_type == "fix" and canonical.classification_status == "validated"
    empty = resolve_canonical(**_base_kwargs(enriched_records=[
        _enriched("a1", "unknown"),
    ]))
    assert empty.classification_status == "needs_review"
    missing = resolve_canonical(**_base_kwargs(enriched_records=[]))
    assert missing.classification_status == "needs_review"


def test_contradiction_records_conflict():
    from coordination.canonical import resolve_canonical

    canonical = resolve_canonical(**_base_kwargs(enriched_records=[
        _enriched("a1", "fix"), _enriched("b2", "refactor"),
    ]))
    assert canonical.classification_status == "conflict", canonical
    assert any("conflict" in e for e in canonical.evidence), canonical.evidence


def test_component_and_type_not_conflated():
    from coordination.canonical import resolve_canonical

    canonical = resolve_canonical(**_base_kwargs(enriched_records=[_enriched("a1", "fix", ["auth"])]))
    assert canonical.component == "auth" and canonical.change_type == "fix"
    assert canonical.component != canonical.change_type


# Consistency validation ------------------------------------------------------
def test_matching_messages_pass():
    from coordination.canonical import resolve_canonical, validate_consistency

    canonical = resolve_canonical(**_base_kwargs(enriched_records=[_enriched("a1", "fix")]))
    issues = validate_consistency(
        canonical,
        [{"id": "msg_1", "recipient": "Rahul", "team": "m", "component": "auth",
          "text": "<@U1> corrected the authorization check", "slack_user_id": "U1"}],
        [{"team": "m"}],
    )
    assert issues == [], issues


def test_component_contradiction_flagged():
    from coordination.canonical import resolve_canonical, validate_consistency

    canonical = resolve_canonical(**_base_kwargs(enriched_records=[_enriched("a1", "fix")]))
    issues = validate_consistency(
        canonical,
        [{"id": "msg_1", "recipient": "Rahul", "team": "m", "component": "billing",
          "text": "x", "slack_user_id": "U1"}],
    )
    assert any("msg_1" in i and "contradicts canonical" in i for i in issues), issues


def test_conflict_and_unmapped_block_auto():
    from coordination.canonical import validate_consistency

    conflict = {"classification_status": "conflict", "component": "auth", "change_type": "fix"}
    issues = validate_consistency(conflict, [{"id": "msg_1", "recipient": "R", "text": "x", "slack_user_id": "U1"}])
    assert any("conflict" in i for i in issues), issues
    ghost = validate_consistency(
        {"classification_status": "validated", "component": ""},
        [{"id": "msg_9", "recipient": "Ghost", "text": "x", "unmapped": True}],
    )
    assert any("msg_9" in i for i in ghost), ghost


class _FakeSnap:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return self._data


class _FakeDocRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def get(self):
        return _FakeSnap(self._store.get(self._path))

    def set(self, payload):
        self._store[self._path] = payload

    def collection(self, name):
        return _FakeCol(self._store, self._path + (name,))


class _FakeCol:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, doc_id):
        return _FakeDocRef(self._store, self._path + (doc_id,))


class _FakeClient:
    def __init__(self):
        self.store: dict = {}

    def collection(self, name):
        return _FakeCol(self.store, (name,))


def test_stale_canonical_rejected_and_roundtrip():
    from coordination.canonical import load_canonical, put_canonical, resolve_canonical

    client = _FakeClient()
    canonical = resolve_canonical(**_base_kwargs(enriched_records=[_enriched("a1", "fix")]))
    put_canonical(client, canonical)
    assert ("projects", "proj-1", "canonical_context", "3") in client.store
    loaded = load_canonical(client, "proj-1", 3, "headAAA")
    assert loaded is not None and loaded.change_type == "fix"
    assert loaded.classification_status == "validated"
    assert load_canonical(client, "proj-1", 3, "headBBB") is None, "stale revision must be rejected"
    assert load_canonical(client, "proj-1", 9, "headAAA") is None


def test_canonical_persist_shape():
    """put_canonical writes projects/{id}/canonical_context/{pr} (nested-coll aware fake)."""
    from coordination.canonical import put_canonical, resolve_canonical

    writes: list = []

    class _NestDoc:
        def set(self, payload):
            writes.append(payload)

    class _NestCol:
        def document(self, _id):
            return _NestDoc()

    class _ProjDoc:
        def collection(self, _name):
            assert _name == "canonical_context"
            return _NestCol()

    class _RootCol:
        def document(self, _id):
            assert _id == "proj-1"
            return _ProjDoc()

    class _Root:
        def collection(self, name):
            assert name == "projects"
            return _RootCol()

    canonical = resolve_canonical(**_base_kwargs(enriched_records=[_enriched("a1", "fix")]))
    put_canonical(_Root(), canonical)
    assert len(writes) == 1 and writes[0]["change_type"] == "fix"
    assert writes[0]["classification_status"] == "validated"


# Prefetch --------------------------------------------------------------------
def _workflow():
    from workflows.pr_workflow import PRWorkflow

    return PRWorkflow(project="test-proj")


def test_prefetch_runs_concurrently():
    wf = _workflow()

    async def slow_enriched(project_id, commits):
        await asyncio.sleep(0.2)
        return []

    async def slow_files(owner, repo, pr):
        await asyncio.sleep(0.2)
        return []

    wf._load_enriched_changes = slow_enriched  # type: ignore[assignment]
    wf.github_service.fetch_pr_files = slow_files  # type: ignore[assignment]

    async def _run():
        from workflows.pr_workflow import PREvent

        event = PREvent(project_id="p", owner="o", repository="r", pr_number=1, head_sha="h")
        return await wf._prefetch_evidence(event, [], {"unused": True})

    started = time.perf_counter()
    result = asyncio.run(_run())
    elapsed = time.perf_counter() - started
    assert result == {"enriched": [], "raw_files": []}, result
    assert elapsed < 0.35, f"sequential (${elapsed:.2f}s) — prefetch must overlap"


def test_prefetch_failure_propagates_explicitly():
    wf = _workflow()

    async def ok_enriched(project_id, commits):
        return []

    async def bad_files(owner, repo, pr):
        raise RuntimeError("github exploded")

    wf._load_enriched_changes = ok_enriched  # type: ignore[assignment]
    wf.github_service.fetch_pr_files = bad_files  # type: ignore[assignment]

    async def _run():
        from workflows.pr_workflow import PREvent

        event = PREvent(project_id="p", owner="o", repository="r", pr_number=1, head_sha="h")
        try:
            await wf._prefetch_evidence(event, ["a"], {"unused": True})
            return "NO-RAISE"
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"

    outcome = asyncio.run(_run())
    assert outcome.startswith("PrefetchError:"), outcome
    assert "pr_files" in outcome and "github exploded" in outcome


def test_prefetch_rejects_foreign_revision_and_repo():
    wf = _workflow()

    class _Rec:
        def __init__(self, sha, repo):
            self.commit_sha = sha
            self.repository = repo

        def model_dump(self, mode="json"):
            return {"commit_sha": self.commit_sha}

    async def _run_mismatch():
        from workflows.pr_workflow import PREvent

        event = PREvent(project_id="p", owner="acme", repository="widget", pr_number=1, head_sha="h")
        problems = wf._validate_prefetch(event, "acme/widget", {"aaa"}, [_Rec("zzz", "acme/widget")])
        assert any("foreign revision" in p for p in problems), problems
        problems2 = wf._validate_prefetch(event, "acme/widget", {"aaa"}, [_Rec("aaa", "evil/other")])
        assert any("mismatch" in p for p in problems2), problems2
        problems3 = wf._validate_prefetch(event, "acme/widget", {"aaa"}, [_Rec("aaa", "acme/widget")])
        assert problems3 == [], problems3

    asyncio.run(_run_mismatch())


def test_agent_payloads_accept_prefetch():
    import inspect

    from agents.communication_agent import CommunicationAgent
    from agents.coordination_agent import CoordinationAgent
    from agents.impact_analysis_agent import ImpactAnalysisAgent

    for cls in (ImpactAnalysisAgent, CoordinationAgent, CommunicationAgent):
        assert "prefetch" in inspect.signature(cls.analyze).parameters, cls
    import pathlib

    for cls in (ImpactAnalysisAgent, CoordinationAgent, CommunicationAgent):
        source = pathlib.Path(sys.modules[cls.__module__].__file__).read_text()
        assert "PREFETCHED EVIDENCE" in source, cls


def test_supervisor_prompt_canonical_section():
    from agents.supervisor_agent import SUPERVISION_INSTRUCTION, SupervisionResult

    assert "canonical_change" in SUPERVISION_INSTRUCTION
    assert "SEMANTICALLY consistent" in SUPERVISION_INSTRUCTION
    assert "mark the ambiguity" in SUPERVISION_INSTRUCTION
    # Structured output contract intact.
    result = SupervisionResult.model_validate({
        "project_id": "p", "pr_number": 1, "verdicts": [], "final_messages": [],
    })
    assert result.autonomy == [] and result.confidence == 0.0


if __name__ == "__main__":
    test_fix_stays_fix()
    test_refactor_stays_refactor()
    test_bug_normalizes_to_fix()
    test_unknown_abstains()
    test_contradiction_records_conflict()
    test_component_and_type_not_conflated()
    test_matching_messages_pass()
    test_component_contradiction_flagged()
    test_conflict_and_unmapped_block_auto()
    test_stale_canonical_rejected_and_roundtrip()
    test_canonical_persist_shape()
    test_prefetch_runs_concurrently()
    test_prefetch_failure_propagates_explicitly()
    test_prefetch_rejects_foreign_revision_and_repo()
    test_agent_payloads_accept_prefetch()
    test_supervisor_prompt_canonical_section()
    print("=== ALL CANONICAL/PREFETCH TESTS PASSED ===")
