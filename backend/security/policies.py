"""Deterministic policy checks evaluated by the PermissionGateway.

Each check returns None when satisfied, or a denying PolicyDecision.
No LLM involvement anywhere in this module.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Optional

from .identities import Action, Resource
from .permissions import (
    EXTERNAL_SIDE_EFFECT_ACTIONS,
    POLICY_ALREADY_DELIVERED,
    POLICY_CAPABILITY_NOT_GRANTED,
    POLICY_INVALID_SCOPE,
    POLICY_RECIPIENT_NOT_ALLOWED,
    POLICY_SANDBOX_PATH_DENIED,
    POLICY_SANDBOX_SCOPE,
    POLICY_SLACK_SEND_REQUIRES_APPROVAL,
    POLICY_UNKNOWN_IDENTITY,
    AuthorizationRequest,
    PolicyDecision,
    is_granted,
)

_DENY = False

SANDBOX_RESOURCES = {
    Resource.SANDBOX,
    Resource.REPOSITORY,
    Resource.SANDBOX_TEMP,
    Resource.STATIC_ANALYSIS,
}

# Deny-by-default secret/credential patterns — matched against the file's
# relative posix path AND its basename. Nothing on this list is ever readable
# through the sandbox, regardless of where it sits in the workspace.
SECRET_FILE_GLOBS = [
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*service-account*.json",
    "credentials*.json",
    ".git/*",
    ".git/**",
    ".ssh/*",
    ".ssh/**",
    "*.keystore",
    "*.jks",
    ".npmrc",
    ".pypirc",
    "secrets.*",
]


def _deny(request: AuthorizationRequest, policy: str, reason: str) -> PolicyDecision:
    return PolicyDecision(
        allowed=_DENY,
        reason=reason,
        policy=policy,
        agent_id=request.agent_id,
        action=request.action.value,
        resource=request.resource.value,
        project_id=request.project_id,
        workflow_id=request.workflow_id,
        approval_id=request.approval_id,
    )


def check_identity_known(request: AuthorizationRequest) -> Optional[PolicyDecision]:
    from .identities import AgentIdentity

    try:
        AgentIdentity(request.agent_id)
    except ValueError:
        return _deny(
            request,
            POLICY_UNKNOWN_IDENTITY,
            f"Unknown agent identity '{request.agent_id}'",
        )
    return None


def check_capability(request: AuthorizationRequest) -> Optional[PolicyDecision]:
    if not is_granted(request.agent_id, request.action, request.resource):
        return _deny(
            request,
            POLICY_CAPABILITY_NOT_GRANTED,
            f"Agent '{request.agent_id}' is not granted {request.action.value} "
            f"on {request.resource.value}",
        )
    return None


def check_project_scope(request: AuthorizationRequest) -> Optional[PolicyDecision]:
    # Writing a project context is how a project comes into existence, so the
    # scope may be assigned outside the request (document_id chosen by caller).
    if (
        request.resource == Resource.PROJECT_CONTEXT
        and request.action == Action.WRITE
    ):
        return None
    if not request.project_id or not request.project_id.strip():
        return _deny(
            request,
            POLICY_INVALID_SCOPE,
            "project_id is required for this operation",
        )
    return None


def check_external_side_effect(request: AuthorizationRequest) -> Optional[PolicyDecision]:
    """External side effects (Slack send) require a bound human approval."""
    if request.resource != Resource.SLACK:
        return None
    if request.action not in EXTERNAL_SIDE_EFFECT_ACTIONS:
        return None
    if not request.approval_id:
        return _deny(
            request,
            POLICY_SLACK_SEND_REQUIRES_APPROVAL,
            "Human approval required before Slack execution",
        )
    if not request.message_id or not request.bundle_id:
        return _deny(
            request,
            POLICY_SLACK_SEND_REQUIRES_APPROVAL,
            "Slack send must reference an exact communication bundle and message",
        )
    return None


def check_recipient_allowed(request: AuthorizationRequest) -> Optional[PolicyDecision]:
    """A Slack send needs a concrete recipient channel."""
    if request.resource != Resource.SLACK or request.action not in EXTERNAL_SIDE_EFFECT_ACTIONS:
        return None
    channel = str(request.context.get("channel", "") or "")
    if not channel.strip():
        return _deny(
            request,
            POLICY_RECIPIENT_NOT_ALLOWED,
            f"Message {request.message_id} has no recipient channel",
        )
    return None


def check_already_delivered(request: AuthorizationRequest) -> Optional[PolicyDecision]:
    """A message already delivered must not be sent again."""
    if request.resource != Resource.SLACK or request.action not in EXTERNAL_SIDE_EFFECT_ACTIONS:
        return None
    if bool(request.context.get("already_delivered", False)):
        return _deny(
            request,
            POLICY_ALREADY_DELIVERED,
            f"Message {request.message_id} was already delivered",
        )
    return None


def check_sandbox_scope(request: AuthorizationRequest) -> Optional[PolicyDecision]:
    """Sandbox operations are scoped to the target PR/repository and its
    temporary workspace: repo+PR binding, workspace containment, and no
    secret/credential files (deny by default)."""
    if request.resource not in SANDBOX_RESOURCES:
        return None

    repo = str(request.context.get("repo", "") or "")
    pr_number = request.context.get("pr_number")
    if not repo or "/" not in repo:
        return _deny(
            request,
            POLICY_SANDBOX_SCOPE,
            "Sandbox access must be scoped to an 'owner/name' repository",
        )
    if not pr_number or int(pr_number) <= 0:
        return _deny(
            request,
            POLICY_SANDBOX_SCOPE,
            "Sandbox access must be scoped to a PR number",
        )
    if not request.project_id:
        return _deny(
            request,
            POLICY_SANDBOX_SCOPE,
            "Sandbox access requires a project scope",
        )

    file_path = request.context.get("file_path")
    if file_path is not None:
        return check_sandbox_file_path(request, str(file_path), request.context)
    return None


def is_secret_file(relative_posix_path: str) -> bool:
    """True when a path matches the secret/credential deny list."""
    normalized = relative_posix_path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    basename = normalized.rsplit("/", 1)[-1]
    for pattern in SECRET_FILE_GLOBS:
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(basename, pattern):
            return True
        # Glob patterns containing '/' must match against the full path with
        # a wildcard prefix so ".git/config" is caught at any depth.
        if "/" in pattern and fnmatch.fnmatch(normalized, f"*{pattern}"):
            return True
    return False


def check_sandbox_file_path(
    request: AuthorizationRequest,
    file_path: str,
    context: Optional[dict] = None,
) -> Optional[PolicyDecision]:
    """Containment within the sandbox workspace + secret deny list.

    file_path may be workspace-relative (the sandbox convention) or absolute;
    it is resolved against the workspace root for containment."""
    context = context or {}
    if is_secret_file(file_path):
        return _deny(
            request,
            POLICY_SANDBOX_PATH_DENIED,
            f"Access to '{file_path}' is denied: secret/credential files are never readable",
        )

    workspace_root = str(context.get("workspace_root", "") or "")
    if not workspace_root:
        return _deny(
            request,
            POLICY_SANDBOX_SCOPE,
            "File access requires a sandbox workspace root",
        )
    try:
        root = Path(workspace_root).resolve()
        candidate = Path(file_path)
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        return _deny(
            request,
            POLICY_SANDBOX_PATH_DENIED,
            f"Access to '{file_path}' is denied: outside the sandbox workspace",
        )
    return None


POLICY_CHECKS = [
    check_identity_known,
    check_capability,
    check_project_scope,
    check_external_side_effect,
    check_recipient_allowed,
    check_already_delivered,
    check_sandbox_scope,
]
