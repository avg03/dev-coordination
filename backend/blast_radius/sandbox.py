"""Static-analysis sandbox for Blast Radius V1.

Provisions an isolated snapshot of the repository at the PR head SHA by
streaming the GitHub tarball API into a temporary workspace. Constraints:

- The GITHUB_TOKEN travels only in the HTTP request header — it is never
  written to the workspace, the tarball, or any file on disk.
- Zero network use after provisioning: all analysis is local reads.
- Zero code execution: PR/application code is parsed, never run.
- Every operation is authorized by the PermissionGateway under the
  blast_radius_analyzer identity before it executes.
- File reads are policy-checked: workspace containment + secret deny list.
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Optional

import httpx

from security.gateway import PermissionGateway, get_permission_gateway
from security.identities import Action, AgentIdentity, Resource
from security.permissions import AuthorizationRequest

from .models import ANALYSIS_VERSION

logger = logging.getLogger("blast_radius.sandbox")

GITHUB_API_BASE = "https://api.github.com"


class SandboxError(RuntimeError):
    pass


class SandboxHandle:
    """An authorized, scoped sandbox workspace for one analysis run."""

    def __init__(self, workspace_root: str, repo: str, head_sha: str, project_id: str, pr_number: int) -> None:
        self.workspace_root = workspace_root
        self.repo = repo
        self.head_sha = head_sha
        self.project_id = project_id
        self.pr_number = pr_number

    def _auth_context(self, file_path: Optional[str] = None) -> dict[str, Any]:
        context: dict[str, Any] = {
            "repo": self.repo,
            "pr_number": self.pr_number,
            "workspace_root": self.workspace_root,
        }
        if file_path is not None:
            context["file_path"] = file_path
        return context

    def _authorize(self, gateway: PermissionGateway, action: Action, resource: Resource, file_path: Optional[str] = None):
        return gateway.authorize(
            AuthorizationRequest(
                agent_id=AgentIdentity.BLAST_RADIUS_ANALYZER.value,
                action=action,
                resource=resource,
                project_id=self.project_id,
                workflow_id=f"pr:{self.project_id}:{self.pr_number}:{self.head_sha or 'na'}",
                context=self._auth_context(file_path),
            )
        )


class SandboxManager:
    """Gateway-gated sandbox lifecycle: provision, read, cleanup."""

    def __init__(self, gateway: Optional[PermissionGateway] = None, token: Optional[str] = None) -> None:
        self.gateway = gateway or get_permission_gateway()
        self._token = token  # tests may inject; production reads env lazily

    # -- authorization helpers ---------------------------------------------

    def _authorize(self, project_id: str, pr_number: int, repo: str, head_sha: str, action: Action, resource: Resource, file_path: Optional[str] = None):
        return self.gateway.authorize(
            AuthorizationRequest(
                agent_id=AgentIdentity.BLAST_RADIUS_ANALYZER.value,
                action=action,
                resource=resource,
                project_id=project_id,
                workflow_id=f"pr:{project_id}:{pr_number}:{head_sha or 'na'}",
                context={
                    "repo": repo,
                    "pr_number": pr_number,
                    "workspace_root": None,  # not yet known for create
                    **({"file_path": file_path} if file_path else {}),
                },
            )
        )

    # -- lifecycle -----------------------------------------------------------

    def create(self, project_id: str, pr_number: int, repo: str, head_sha: str) -> SandboxHandle:
        """Authorize + provision the workspace. Raises SandboxError on failure."""
        decision = self._authorize(project_id, pr_number, repo, head_sha, Action.EXECUTE, Resource.SANDBOX)
        if not decision.allowed:
            raise SandboxError(f"sandbox.create denied: {decision.policy}: {decision.reason}")

        workspace_root = tempfile.mkdtemp(prefix=f"blast-radius-{project_id}-{pr_number}-")
        handle = SandboxHandle(workspace_root, repo, head_sha, project_id, pr_number)
        try:
            self._download_snapshot(handle)
        except SandboxError:
            shutil.rmtree(workspace_root, ignore_errors=True)
            raise
        logger.info("Sandbox created for %s PR#%s @%s -> %s", repo, pr_number, head_sha[:8], workspace_root)
        return handle

    def _download_snapshot(self, handle: SandboxHandle) -> None:
        """Stream the GitHub tarball for the head SHA. Token stays in the header."""
        url = f"{GITHUB_API_BASE}/repos/{handle.repo}/tarball/{handle.head_sha}"
        headers = {"Accept": "application/vnd.github+json"}
        token = self._token if self._token is not None else os.getenv("GITHUB_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            with httpx.stream("GET", url, headers=headers, timeout=120.0, follow_redirects=True) as response:
                if response.status_code == 404:
                    raise SandboxError(f"repository or ref not found: {handle.repo}@{handle.head_sha[:8]}")
                if response.status_code in (401, 403):
                    raise SandboxError(f"not authorized to fetch snapshot for {handle.repo} ({response.status_code})")
                response.raise_for_status()
                # Stream straight into tarfile extraction — token never on disk.
                with tarfile.open(fileobj=response.iter_bytes(), mode="r|gz", errorlevel=1) as tar:
                    _safe_extract(tar, handle.workspace_root)
        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"failed to provision sandbox snapshot: {exc}") from exc

    def read_file(self, handle: SandboxHandle, relative_path: str) -> str:
        """Policy-checked read of one file from the workspace."""
        decision = handle._authorize(self.gateway, Action.READ, Resource.REPOSITORY, file_path=relative_path)
        if not decision.allowed:
            raise SandboxError(f"sandbox.read_repo denied: {decision.policy}: {decision.reason}")
        path = (Path(handle.workspace_root) / relative_path).resolve()
        # Defense in depth: the gateway already checked containment; verify again.
        if not str(path).startswith(str(Path(handle.workspace_root).resolve())):
            raise SandboxError(f"path escapes workspace: {relative_path}")
        if not path.is_file():
            raise SandboxError(f"file not found in sandbox: {relative_path}")
        return path.read_text(encoding="utf-8", errors="replace")

    def list_files(self, handle: SandboxHandle) -> list[str]:
        """Policy-checked listing of all non-denied files in the workspace."""
        decision = handle._authorize(self.gateway, Action.READ, Resource.REPOSITORY)
        if not decision.allowed:
            raise SandboxError(f"sandbox.read_repo denied: {decision.policy}: {decision.reason}")
        from security.policies import is_secret_file

        root = Path(handle.workspace_root)
        files: list[str] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if is_secret_file(relative):
                continue
            files.append(relative)
        return sorted(files)

    def execute_static_analysis(self, handle: SandboxHandle, analyzer_name: str) -> None:
        """Authorize an analyzer phase (ast / import-graph / ripgrep)."""
        decision = handle._authorize(self.gateway, Action.EXECUTE, Resource.STATIC_ANALYSIS)
        if not decision.allowed:
            raise SandboxError(f"static_analysis.execute denied: {decision.policy}: {decision.reason}")
        logger.info("static_analysis.execute analyzer=%s pr=%s", analyzer_name, handle.pr_number)

    def cleanup(self, handle: SandboxHandle) -> None:
        shutil.rmtree(handle.workspace_root, ignore_errors=True)
        logger.info("Sandbox cleaned up: %s", handle.workspace_root)


def _safe_extract(tar: tarfile.TarFile, destination: str) -> None:
    """Extract a GitHub tarball, stripping the single top-level directory.

    Members are validated: no absolute paths, no '..' traversal, no links
    escaping the destination, and only regular files/directories are written.
    """
    dest = Path(destination).resolve()

    def _is_within(member_path: Path) -> bool:
        try:
            member_path.resolve().relative_to(dest)
            return True
        except ValueError:
            return False

    first_top: Optional[str] = None
    for member in tar:
        name = member.name.replace("\\", "/").lstrip("./")
        if not name:
            continue
        top = name.split("/", 1)[0]
        if first_top is None:
            first_top = top
        # Strip the archive's single top-level directory (GitHub uses repo-sha).
        stripped = name.split("/", 1)[1] if "/" in name and top == first_top else name
        if not stripped or stripped in (".", ".."):
            continue
        target = dest / stripped
        if not _is_within(target):
            raise SandboxError(f"tar member escapes destination: {member.name}")
        if not (member.isfile() or member.isdir()):
            continue  # skip symlinks/hardlinks/devices entirely
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
