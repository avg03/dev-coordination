"""Sandbox policy tests — workspace containment + secret deny list (deny by default).

Run: python backend/tests/test_sandbox_policy.py  (no GCP, no network)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from security.audit import InMemoryAuditSink
from security.gateway import PermissionGateway
from security.identities import Action, AgentIdentity, Resource
from security.policies import is_secret_file
from security.permissions import AuthorizationRequest

ANALYZER = AgentIdentity.BLAST_RADIUS_ANALYZER.value


def _file_read_request(file_path: str, workspace_root: str) -> AuthorizationRequest:
    return AuthorizationRequest(
        agent_id=ANALYZER,
        action=Action.READ,
        resource=Resource.REPOSITORY,
        project_id="proj-1",
        workflow_id="pr:proj-1:7:abc",
        context={
            "repo": "owner/repo",
            "pr_number": 7,
            "workspace_root": workspace_root,
            "file_path": file_path,
        },
    )


def test_secret_files_denied():
    gw = PermissionGateway(audit_sink=InMemoryAuditSink())
    root = tempfile.mkdtemp(prefix="sbx-test-")
    for path in [
        ".env",
        ".env.local",
        "config/server.pem",
        "keys/server.key",
        "id_rsa",
        "deploy/id_ed25519",
        "golden-furnace-service-account.json",
        "credentials.json",
        ".git/config",
        ".ssh/known_hosts",
        "release.keystore",
    ]:
        decision = gw.authorize(_file_read_request(str(Path(root) / path), root))
        assert decision.allowed is False, f"{path} must be denied"
        assert decision.policy == "sandbox_path_denied", f"{path}: {decision.policy}"


def test_is_secret_file_variants():
    assert is_secret_file(".env")
    assert is_secret_file("backend/.env.production")
    assert is_secret_file("a/b/server.pem")
    assert is_secret_file("x/id_rsa.pub")
    assert is_secret_file("deep/nested/.git/config")
    assert is_secret_file("my-service-account-creds.json")
    assert not is_secret_file("backend/payment/refund.py")
    assert not is_secret_file("docs/keys.md")  # .md doc about keys, not a key file
    assert not is_secret_file("src/envelope.py")  # basename contains 'env' but not .env


def test_traversal_outside_workspace_denied():
    gw = PermissionGateway(audit_sink=InMemoryAuditSink())
    root = tempfile.mkdtemp(prefix="sbx-test-")
    outside = str(Path(root) / ".." / "unrelated" / "secret.txt")
    decision = gw.authorize(_file_read_request(outside, root))
    assert decision.allowed is False
    assert decision.policy == "sandbox_path_denied"


def test_absolute_path_outside_workspace_denied():
    gw = PermissionGateway(audit_sink=InMemoryAuditSink())
    root = tempfile.mkdtemp(prefix="sbx-test-")
    decision = gw.authorize(_file_read_request("C:/Windows/system32/config", root))
    assert decision.allowed is False


def test_normal_source_file_allowed():
    gw = PermissionGateway(audit_sink=InMemoryAuditSink())
    root = tempfile.mkdtemp(prefix="sbx-test-")
    decision = gw.authorize(_file_read_request(str(Path(root) / "payment" / "refund.py"), root))
    assert decision.allowed is True, decision.reason


def test_missing_workspace_root_denied():
    gw = PermissionGateway(audit_sink=InMemoryAuditSink())
    decision = AuthorizationRequest(
        agent_id=ANALYZER,
        action=Action.READ,
        resource=Resource.REPOSITORY,
        project_id="proj-1",
        context={"repo": "owner/repo", "pr_number": 7, "file_path": "some/file.py"},
    )
    result = gw.authorize(decision)
    # No workspace_root in context -> the file-read scoping must not pass silently.
    assert result.allowed is False


def test_sandbox_scope_requires_repo_and_pr():
    gw = PermissionGateway(audit_sink=InMemoryAuditSink())
    no_repo = AuthorizationRequest(
        agent_id=ANALYZER,
        action=Action.EXECUTE,
        resource=Resource.SANDBOX,
        project_id="proj-1",
        context={"pr_number": 7},
    )
    d = gw.authorize(no_repo)
    assert d.allowed is False and d.policy == "sandbox_scope_violation"

    no_pr = AuthorizationRequest(
        agent_id=ANALYZER,
        action=Action.EXECUTE,
        resource=Resource.SANDBOX,
        project_id="proj-1",
        context={"repo": "owner/repo"},
    )
    d = gw.authorize(no_pr)
    assert d.allowed is False and d.policy == "sandbox_scope_violation"


if __name__ == "__main__":
    test_secret_files_denied()
    test_is_secret_file_variants()
    test_traversal_outside_workspace_denied()
    test_absolute_path_outside_workspace_denied()
    test_normal_source_file_allowed()
    test_missing_workspace_root_denied()
    test_sandbox_scope_requires_repo_and_pr()
    print("=== ALL SANDBOX POLICY TESTS PASSED ===")
