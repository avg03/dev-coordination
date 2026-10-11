"""BlastRadiusService tests — idempotent step, duplicate requests, graceful
degradation, audit integration. Runs fully offline via a fixture sandbox.

Run: python backend/tests/test_blast_radius_service.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from blast_radius.blast_radius_service import BlastRadiusService
from blast_radius.models import ANALYSIS_VERSION, BlastRadiusReport, ChangedFileInfo
from blast_radius.sandbox import SandboxError, SandboxHandle, SandboxManager
from security.audit import InMemoryAuditSink
from security.gateway import PermissionGateway
from security.idempotency import ClaimStatus, IdempotencyManager, InMemoryIdempotencyStore

ANALYZER = "blast_radius_analyzer"

PROJECT_CONTEXT = {
    "project": {"name": "Commerce Platform"},
    "architecture": {
        "components": [
            {"id": "payment", "repository_paths": ["payment"], "responsible_teams": ["Payments"]},
            {"id": "checkout", "repository_paths": ["checkout"], "responsible_teams": ["Checkout"]},
        ]
    },
    "team_context": [
        {"name": "Payments", "owned_components": ["payment"], "slack_channel_id": "C1"},
        {"name": "Checkout", "owned_components": ["checkout"], "slack_channel_id": "C2"},
    ],
    "stakeholders": [
        {"name": "Alice", "role": "Engineer", "team": "Payments"},
        {"name": "Carol", "role": "Engineer", "team": "Checkout"},
    ],
    "work_scope": {
        "planned_tasks": [
            {"id": "T1", "title": "Refund processing", "description": "payment flow for refunds"},
        ]
    },
}


class FixtureSandboxManager(SandboxManager):
    """Serves a pre-populated fixture directory instead of downloading."""

    def __init__(self, gateway, fixture_dir: str, fail_create: bool = False) -> None:
        super().__init__(gateway=gateway)
        self.fixture_dir = fixture_dir
        self.fail_create = fail_create
        self.create_calls = 0

    def create(self, project_id, pr_number, repo, head_sha) -> SandboxHandle:
        self.create_calls += 1
        if self.fail_create:
            raise SandboxError("simulated sandbox provisioning failure")
        return SandboxHandle(self.fixture_dir, repo, head_sha, project_id, pr_number)

    def cleanup(self, handle) -> None:
        pass  # keep the fixture for assertions


def _fixture_repo(tmp: Path) -> str:
    (tmp / "payment").mkdir(parents=True, exist_ok=True)
    (tmp / "checkout").mkdir(parents=True, exist_ok=True)
    (tmp / ".github").mkdir(parents=True, exist_ok=True)
    (tmp / "payment" / "refund.py").write_text(
        "class RefundProcessor:\n    pass\n", encoding="utf-8"
    )
    (tmp / "checkout" / "service.py").write_text(
        "from payment.refund import RefundProcessor\n\n\nclass CheckoutService:\n    pass\n",
        encoding="utf-8",
    )
    (tmp / ".github" / "CODEOWNERS").write_text("payment/ @payments-squad\n", encoding="utf-8")
    return str(tmp)


def _service(fixture_dir: str, fail_create: bool = False):
    sink = InMemoryAuditSink()
    gw = PermissionGateway(audit_sink=sink)
    sandbox = FixtureSandboxManager(gw, fixture_dir, fail_create=fail_create)
    service = BlastRadiusService(gateway=gw, sandbox=sandbox, audit_sink=sink, max_depth=2)
    return service, sandbox, sink


def _changed_files():
    return [ChangedFileInfo(path="payment/refund.py", status="modified", additions=3, deletions=1)]


def test_full_analysis_produces_evidence_backed_entries():
    with tempfile.TemporaryDirectory() as tmp_str:
        fixture = _fixture_repo(Path(tmp_str))
        service, sandbox, sink = _service(fixture)
        report = service.analyze(
            project_id="proj-1",
            pr_number=7,
            repo="owner/repo",
            head_sha="abc123def",
            changed_files=_changed_files(),
            project_context=PROJECT_CONTEXT,
        )

        assert report.incomplete is False, report.incomplete_reason
        assert report.changed_components == ["payment"]

        entity_ids = {(e.entity_type, e.entity_id) for e in report.entries}
        assert ("component", "payment") in entity_ids
        assert ("component", "checkout") in entity_ids, "direct dependent must appear"
        assert ("team", "Payments") in entity_ids
        assert ("person", "Alice") in entity_ids
        assert ("person", "@payments-squad") in entity_ids, "CODEOWNERS owner must appear"

        payment_entry = next(e for e in report.entries if e.entity_id == "payment")
        assert payment_entry.impact_level == "direct"
        assert payment_entry.evidence, "entries must carry evidence"

        checkout_entry = next(e for e in report.entries if e.entity_id == "checkout")
        assert checkout_entry.impact_level == "dependency"

        assert report.downstream_workflows == ["Refund processing"]
        assert sandbox.create_calls == 1
        assert any(e.event_type == "static_analysis.execution" for e in sink.events), "analysis must be audited"


def test_duplicate_analysis_request_same_key_no_rerun():
    """The workflow-level idempotency contract: same key -> stored result,
    sandbox never re-provisioned."""
    with tempfile.TemporaryDirectory() as tmp_str:
        fixture = _fixture_repo(Path(tmp_str))
        service, sandbox, _ = _service(fixture)
        idem = IdempotencyManager(InMemoryIdempotencyStore())
        key = idem.build_key(
            "proj-1",
            "pr:proj-1:7:abc123def",
            "blast_radius:analyze",
            {"pr_number": 7, "head_sha": "abc123def", "analysis_version": ANALYSIS_VERSION},
        )

        def run_analysis() -> BlastRadiusReport:
            return service.analyze(
                project_id="proj-1",
                pr_number=7,
                repo="owner/repo",
                head_sha="abc123def",
                changed_files=_changed_files(),
                project_context=PROJECT_CONTEXT,
            )

        out = idem.claim(key, workflow_id="pr:proj-1:7:abc123def", project_id="proj-1",
                         step_name="blast_radius:analyze", input_payload={"head_sha": "abc123def"})
        assert out.status == ClaimStatus.EXECUTE
        report = run_analysis()
        idem.complete(key, report.model_dump(mode="json"))

        # Retried request (Pub/Sub redelivery):
        out2 = idem.claim(key, workflow_id="pr:proj-1:7:abc123def", project_id="proj-1",
                          step_name="blast_radius:analyze", input_payload={"head_sha": "abc123def"})
        assert out2.status == ClaimStatus.COMPLETED
        assert BlastRadiusReport.model_validate(out2.result).pr_number == 7
        assert sandbox.create_calls == 1, "sandbox must not be re-provisioned for the same key"


def test_new_head_sha_produces_new_key():
    idem = IdempotencyManager(InMemoryIdempotencyStore())
    k1 = idem.build_key("proj-1", "pr:proj-1:7:aaa", "blast_radius:analyze",
                        {"pr_number": 7, "head_sha": "aaa", "analysis_version": ANALYSIS_VERSION})
    k2 = idem.build_key("proj-1", "pr:proj-1:7:bbb", "blast_radius:analyze",
                        {"pr_number": 7, "head_sha": "bbb", "analysis_version": ANALYSIS_VERSION})
    assert k1 != k2, "PR change must trigger re-analysis"


def test_sandbox_failure_degrades_gracefully():
    with tempfile.TemporaryDirectory() as tmp_str:
        _fixture_repo(Path(tmp_str))
        service, sandbox, sink = _service(tmp_str, fail_create=True)
        report = service.analyze(
            project_id="proj-1",
            pr_number=7,
            repo="owner/repo",
            head_sha="abc123def",
            changed_files=_changed_files(),
            project_context=PROJECT_CONTEXT,
        )
        assert report.incomplete is True
        assert "simulated sandbox provisioning failure" in report.incomplete_reason
        assert report.changed_components == ["payment"], "diff-level fallback still resolves components"
        assert report.entries == [], "no fabricated entries without a sandbox"
        assert any(e.decision == "degraded" for e in sink.events)


def test_analysis_version_changes_key():
    idem = IdempotencyManager(InMemoryIdempotencyStore())
    k1 = idem.build_key("proj-1", "wf", "blast_radius:analyze", {"analysis_version": "v1"})
    k2 = idem.build_key("proj-1", "wf", "blast_radius:analyze", {"analysis_version": "v2"})
    assert k1 != k2


if __name__ == "__main__":
    test_full_analysis_produces_evidence_backed_entries()
    test_duplicate_analysis_request_same_key_no_rerun()
    test_new_head_sha_produces_new_key()
    test_sandbox_failure_degrades_gracefully()
    test_analysis_version_changes_key()
    print("=== ALL BLAST RADIUS SERVICE TESTS PASSED ===")
