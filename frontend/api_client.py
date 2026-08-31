"""Minimal HTTP client for the Impact-Aware Engineering Change Coordinator backend.

All backend communication is isolated here so that ``streamlit_app.py`` remains
a pure presentation layer.  Business logic, Firestore, GitHub and Slack logic
must NOT be moved into Streamlit.

Environment:
    BACKEND_URL: base URL of the FastAPI backend, e.g. ``https://api.example.com``.
                 Must NOT be hard-coded.  Trailing slashes are stripped.

Endpoints covered (do not invent new ones):
    POST /api/projects/context
    GET  /api/projects/{project_id}/commits
    GET  /api/projects/{project_id}/prs/{pr_number}
    POST /api/projects/{project_id}/prs/{pr_number}/approve
"""

from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

# ---------------------------------------------------------------------------
# Configuration — keep same Pub/Sub worker, increase frontend wait to 60s for
# project context (Gemini ADK) which often exceeds 30s.
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS = 60.0
PROJECT_CONTEXT_TIMEOUT_SECONDS = 60.0


class BackendApiError(RuntimeError):
    """Raised when the backend returns a non-2xx response or is unreachable."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        detail: Optional[str] = None,
        url: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.url = url

    def __str__(self) -> str:  # pragma: no cover
        parts = [super().__str__()]
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.url:
            parts.append(f"url={self.url}")
        if self.detail:
            parts.append(f"detail={self.detail}")
        return " | ".join(parts)


def get_backend_url() -> str:
    """Return the configured backend base URL (without trailing slash).

    Raises:
        BackendApiError: if BACKEND_URL is not set.
    """
    raw = os.getenv("BACKEND_URL", "").strip()
    if not raw:
        raise BackendApiError(
            "BACKEND_URL environment variable is not set. "
            "Set BACKEND_URL to the FastAPI backend base URL, e.g. https://api.example.com"
        )
    return raw.rstrip("/")


def _build_url(path: str) -> str:
    base = get_backend_url()
    # Ensure path starts with /
    if not path.startswith("/"):
        path = "/" + path
    # urljoin needs base to end with /
    return urljoin(base + "/", path.lstrip("/"))


def _handle_response(response: httpx.Response) -> dict[str, Any] | list[Any]:
    """Raise BackendApiError on HTTP errors, otherwise return parsed JSON."""
    url = str(response.url)
    if response.is_success:
        # 204 No Content returns empty body
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except Exception:
            # Fallback to text if JSON parsing fails
            return {"raw": response.text}

    # Error path: try to extract detail from JSON body
    detail: str = response.text[:2000]
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("message") or body)
        else:
            detail = str(body)
    except Exception:
        pass

    raise BackendApiError(
        f"Backend request failed: {response.status_code} {response.reason_phrase}",
        status_code=response.status_code,
        detail=detail,
        url=url,
    )


def _request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json: Optional[dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any] | list[Any]:
    url = _build_url(path)
    try:
        response = httpx.request(
            method,
            url,
            params=params,
            json=json,
            timeout=timeout,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
    except httpx.TimeoutException as exc:
        raise BackendApiError(
            f"Backend request timed out after {timeout}s", url=url, detail=str(exc)
        ) from exc
    except httpx.RequestError as exc:
        raise BackendApiError(
            f"Could not connect to backend at {url}", url=url, detail=str(exc)
        ) from exc

    return _handle_response(response)


# ---------------------------------------------------------------------------
# Public API — one function per backend endpoint (typed/simple)
# ---------------------------------------------------------------------------


def create_project_context(project_id: str, project_description: str) -> dict[str, Any]:
    """POST /api/projects/context

    Args:
        project_id: client-provided project identifier (used as Firestore doc ID)
        project_description: raw unstructured project description text

    Returns:
        Parsed JSON response matching ProjectContextResponse:
        { "success": true, "project_context": { ... } }

    Raises:
        BackendApiError: on HTTP error or connection failure
        ValueError: if inputs are empty
    """
    if not project_id or not project_id.strip():
        raise ValueError("project_id cannot be empty")
    if not project_description or not project_description.strip():
        raise ValueError("project_description cannot be empty")

    payload = {
        "project_id": project_id.strip(),
        "project_description": project_description.strip(),
    }
    result = _request("POST", "/api/projects/context", json=payload, timeout=PROJECT_CONTEXT_TIMEOUT_SECONDS)
    # Backend returns object; ensure dict
    if isinstance(result, list):
        return {"data": result}
    return result  # type: ignore[return-value]


def get_project_commits(project_id: str, limit: Optional[int] = None) -> dict[str, Any]:
    """GET /api/projects/{project_id}/commits

    Args:
        project_id: project identifier
        limit: optional max commits to return (1-100). None means no limit.

    Returns:
        Parsed JSON matching CommitTimelineResponse:
        { "commits": [ { "commit_sha": ..., "summary": ..., "components": ..., "timestamp": ... }, ... ] }

    Raises:
        BackendApiError: on HTTP error
        ValueError: if project_id is empty or limit out of range
    """
    if not project_id or not project_id.strip():
        raise ValueError("project_id cannot be empty")
    if limit is not None and (limit < 1 or limit > 100):
        raise ValueError("limit must be between 1 and 100")

    params: Optional[dict[str, Any]] = None
    if limit is not None:
        params = {"limit": limit}

    result = _request("GET", f"/api/projects/{project_id.strip()}/commits", params=params)
    if isinstance(result, list):
        return {"commits": result}
    return result  # type: ignore[return-value]


def get_pr_review(project_id: str, pr_number: int) -> dict[str, Any]:
    """GET /api/projects/{project_id}/prs/{pr_number}

    Args:
        project_id: project identifier
        pr_number: pull request number (positive int)

    Returns:
        Parsed JSON matching PRReviewResponse:
        {
          "project_id": ...,
          "pr_number": ...,
          "impact_report": ... | None,
          "coordination_plan": ... | None,
          "communication_bundle": ... | None
        }

    Raises:
        BackendApiError: on HTTP error (404 if PR not found)
        ValueError: if inputs are invalid
    """
    if not project_id or not project_id.strip():
        raise ValueError("project_id cannot be empty")
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError("pr_number must be a positive integer")

    result = _request("GET", f"/api/projects/{project_id.strip()}/prs/{pr_number}")
    if isinstance(result, list):
        return {"data": result}
    return result  # type: ignore[return-value]


def approve_pr(
    project_id: str,
    pr_number: int,
    approved: bool,
    selected_messages: list[str],
) -> dict[str, Any]:
    """POST /api/projects/{project_id}/prs/{pr_number}/approve

    Human-in-the-loop approval for Slack delivery.

    Args:
        project_id: project identifier
        pr_number: pull request number
        approved: whether the user approved delivery (True) or rejected (False)
        selected_messages: list of SlackMessage ids to send, e.g. ["msg_1","msg_3"].
                           Empty list means all messages when approved=True.

    Returns:
        Parsed JSON matching ApprovePRResponse:
        {
          "success": true,
          "delivery_status": "sent" | "rejected" | "failed" | "mock_sent",
          "selected_messages": [...],
          "sent_results": [...],
          "message": "..."
        }

    Raises:
        BackendApiError: on HTTP error (400 for invalid ids, 404 if bundle missing)
        ValueError: if inputs are invalid
    """
    if not project_id or not project_id.strip():
        raise ValueError("project_id cannot be empty")
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError("pr_number must be a positive integer")
    if not isinstance(approved, bool):
        raise ValueError("approved must be a boolean")
    if selected_messages is None:
        selected_messages = []
    if not isinstance(selected_messages, list):
        raise ValueError("selected_messages must be a list of strings")

    # Normalize ids to stripped strings
    normalized_ids = [str(m).strip() for m in selected_messages if str(m).strip()]

    payload = {
        "approved": approved,
        "selected_messages": normalized_ids,
    }
    result = _request(
        "POST",
        f"/api/projects/{project_id.strip()}/prs/{pr_number}/approve",
        json=payload,
    )
    if isinstance(result, list):
        return {"data": result}
    return result  # type: ignore[return-value]


def check_backend_health(timeout: float = 5.0) -> dict[str, Any]:
    """Check backend health via GET /health.

    Returns:
        dict with keys: ok (bool), status_code, body, url, error
    """
    try:
        base = get_backend_url()
    except BackendApiError as exc:
        return {"ok": False, "error": str(exc), "url": None, "status_code": None, "body": None}

    url = _build_url("/health")
    try:
        response = httpx.get(url, timeout=timeout, headers={"Accept": "application/json"})
        body: Any
        try:
            body = response.json()
        except Exception:
            body = response.text[:1000]
        return {
            "ok": response.is_success,
            "status_code": response.status_code,
            "body": body,
            "url": url,
            "error": None if response.is_success else f"{response.status_code} {response.reason_phrase}",
        }
    except httpx.TimeoutException as exc:
        return {"ok": False, "error": f"timeout after {timeout}s: {exc}", "url": url, "status_code": None, "body": None}
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"connection failed: {exc}", "url": url, "status_code": None, "body": None}
