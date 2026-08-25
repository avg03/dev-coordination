from __future__ import annotations

import difflib
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger("github.change_records")

GITHUB_API_BASE = "https://api.github.com"
_RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0)

FileStatus = Literal["added", "modified", "removed", "renamed"]
ChangeType = Literal["feature", "refactor", "fix", "bug", "config", "docs", "test", "unknown"]
EvidenceType = Literal["file_change", "function_change", "dependency", "project_context", "previous_commit"]

_DEPENDENCY_MANIFESTS = {
    "requirements.txt",
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "poetry.lock",
    "go.mod",
    "pom.xml",
    "build.gradle",
}

_HUNK_FUNCTION = re.compile(r"@@[^@]*@@\s*(?:async\s+)?def\s+([A-Za-z_]\w*)")
_ADDED_FUNCTION = re.compile(r"^\+\s*(?:async\s+)?def\s+([A-Za-z_]\w*)")
_FIX_RE = re.compile(r"\b(fix(es|ed|ing)?|bugfix|hotfix|patch(ed|es)?|repair(ed|s)?|mend(ed|s)?|correct(ed)?|resolve[sd]?)\b")
_BUG_RE = re.compile(r"\b(bug(s|gy)?|crash(es|ed|ing)?|defect(ive)?|regression)\b")
_REFACTOR_RE = re.compile(r"\b(refactor|clean\s?up|rename|restructure|moved?)\b")
_FEATURE_RE = re.compile(r"\b(feat(:|\s)|feature|add(s|ed)?|implement|introduce|support)\b")
_CONFIG_SUFFIXES = (".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".lock", ".env", ".properties")
_CONFIG_BASENAMES = {"dockerfile", "makefile", ".gitignore", ".editorconfig"}
_DOC_SUFFIXES = (".md", ".rst", ".txt")

PREFIX_TYPE_MAP: dict[str, ChangeType] = {
    "feat": "feature",
    "feature": "feature",
    "add": "feature",
    "added": "feature",
    "adds": "feature",
    "new": "feature",
    "create": "feature",
    "created": "feature",
    "implement": "feature",
    "introduce": "feature",
    "initial": "feature",
    "support": "feature",
    "fix": "fix",
    "fixed": "fix",
    "fixes": "fix",
    "fixing": "fix",
    "bugfix": "fix",
    "hotfix": "fix",
    "patch": "fix",
    "patched": "fix",
    "repair": "fix",
    "repaired": "fix",
    "mend": "fix",
    "correct": "fix",
    "corrected": "fix",
    "resolve": "fix",
    "resolved": "fix",
    "overhaul": "refactor",
    "bug": "bug",
    "bugs": "bug",
    "crash": "bug",
    "crashes": "bug",
    "defect": "bug",
    "regression": "bug",
    "refactor": "refactor",
    "refactoring": "refactor",
    "revamp": "refactor",
    "revamped": "refactor",
    "rework": "refactor",
    "reworked": "refactor",
    "cleanup": "refactor",
    "clean": "refactor",
    "tidy": "refactor",
    "restructure": "refactor",
    "rename": "refactor",
    "renamed": "refactor",
    "move": "refactor",
    "moved": "refactor",
    "simplify": "refactor",
    "polish": "refactor",
    "docs": "docs",
    "doc": "docs",
    "documentation": "docs",
    "readme": "docs",
    "test": "test",
    "tests": "test",
    "testing": "test",
    "config": "config",
    "configuration": "config",
    "ci": "config",
    "build": "config",
    "deps": "config",
    "dependencies": "config",
}

_COMMIT_COLON_PREFIX = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9_+\-]{2,24})(?:\([^)]{0,80}\))?!?\s*:")
_COMMIT_BRACKET_PREFIX = re.compile(r"^\s*\[([a-zA-Z][a-zA-Z0-9_+\-]{2,24})\]")
_FUZZY_PREFIX_CUTOFF = 0.8


class ChangedFile(BaseModel):
    path: str
    status: FileStatus = "modified"
    additions: int = 0
    deletions: int = 0
    patch: Optional[str] = None
    functions: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    type: EvidenceType
    source: str
    detail: str


class ChangeRecord(BaseModel):
    commit_sha: str
    repository: str
    owner: str
    branch: str = ""

    author: str = ""
    timestamp: datetime
    commit_message: str = ""

    changed_files: list[ChangedFile] = Field(default_factory=list)

    components: list[str] = Field(default_factory=list)
    change_type: ChangeType = "unknown"
    evidence: list[Evidence] = Field(default_factory=list)


class GitHubApiError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"GitHub API error {status_code}: {message}")
        self.status_code = status_code


class ChangeRecordParseError(ValueError):
    pass


def send_github_api_request(
    name: str,
    repository: str,
    commit_sha: str,
    token: Optional[str] = None,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> dict[str, Any]:
    url = f"{GITHUB_API_BASE}/repos/{name}/{repository}/commits/{commit_sha}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resolved_token = token or os.getenv("GITHUB_TOKEN")
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"

    last_error: Exception = GitHubApiError(0, "no attempts made")
    for attempt in range(max_retries):
        try:
            response = httpx.get(url, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            last_error = exc
            logger.warning(
                "GitHub API attempt %d/%d failed (%s): %s",
                attempt + 1,
                max_retries,
                url,
                exc,
            )
        else:
            if response.status_code == 200:
                return response.json()
            if response.status_code >= 500 and attempt < max_retries - 1:
                last_error = GitHubApiError(response.status_code, _error_detail(response))
                logger.warning(
                    "GitHub API attempt %d/%d returned %d, retrying",
                    attempt + 1,
                    max_retries,
                    response.status_code,
                )
            else:
                raise GitHubApiError(response.status_code, _error_detail(response))
        time.sleep(_RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)])
    raise GitHubApiError(0, f"request failed after {max_retries} attempts: {last_error}")


def parse_change_record(
    api_output: dict[str, Any],
    name: str = "",
    repository: str = "",
    branch: str = "",
) -> ChangeRecord:
    commit_block = api_output.get("commit") or {}
    sha = api_output.get("sha", "")
    if not sha:
        raise ChangeRecordParseError("API output missing 'sha'")
    if "files" not in api_output:
        raise ChangeRecordParseError("API output missing 'files' (did you request a single commit?)")

    owner = _resolve_owner(api_output, name)
    full_repo = _resolve_repository(api_output, owner, name, repository)

    changed_files = [_build_changed_file(f) for f in api_output.get("files") or []]
    message = commit_block.get("message") or ""

    record = ChangeRecord(
        commit_sha=sha,
        repository=full_repo,
        owner=owner,
        branch=branch,
        author=_resolve_author(api_output, commit_block),
        timestamp=_parse_iso((commit_block.get("author") or {}).get("date")),
        commit_message=message,
        changed_files=changed_files,
        components=_extract_components(changed_files),
    )
    record.change_type = classify_change_type(record)
    record.evidence = build_evidence(changed_files)
    return record


def build_change_record(
    name: str,
    repository: str,
    commit_sha: str,
    branch: str = "",
    token: Optional[str] = None,
) -> ChangeRecord:
    api_output = send_github_api_request(name, repository, commit_sha, token=token)
    return parse_change_record(api_output, name=name, repository=repository, branch=branch)


def classify_change_type(record_or_files: Any, message: str = "") -> ChangeType:
    if isinstance(record_or_files, ChangeRecord):
        paths = [f.path for f in record_or_files.changed_files]
        message = record_or_files.commit_message or message
    else:
        paths = [getattr(f, "path", str(f)) for f in record_or_files]

    subject = message.splitlines()[0].strip() if message.strip() else ""
    prefix_token = _extract_commit_prefix(subject)
    if prefix_token:
        mapped = _resolve_prefix_token(prefix_token)
        if mapped is not None:
            return mapped

    if paths and all(_is_doc(p) for p in paths):
        return "docs"
    if paths and all(_is_test(p) for p in paths):
        return "test"
    if paths and all(_is_config(p) for p in paths):
        return "config"

    msg = message.lower()
    if _FIX_RE.search(msg):
        return "fix"
    if _BUG_RE.search(msg):
        return "bug"
    if _REFACTOR_RE.search(msg):
        return "refactor"
    if _FEATURE_RE.search(msg):
        return "feature"
    return "unknown"


def _extract_commit_prefix(subject: str) -> str:
    if not subject:
        return ""
    match = _COMMIT_COLON_PREFIX.match(subject) or _COMMIT_BRACKET_PREFIX.match(subject)
    return match.group(1).lower() if match else ""


def _resolve_prefix_token(token: str) -> Optional[ChangeType]:
    if token in PREFIX_TYPE_MAP:
        return PREFIX_TYPE_MAP[token]
    for known, change_type in PREFIX_TYPE_MAP.items():
        if len(known) >= 4 and (token.startswith(known) or known.startswith(token)):
            return change_type
    close = difflib.get_close_matches(token, PREFIX_TYPE_MAP.keys(), n=1, cutoff=_FUZZY_PREFIX_CUTOFF)
    if close:
        return PREFIX_TYPE_MAP[close[0]]
    return None


def build_evidence(changed_files: list[ChangedFile]) -> list[Evidence]:
    evidence: list[Evidence] = []
    for changed in changed_files:
        evidence.append(
            Evidence(
                type="file_change",
                source=changed.path,
                detail=f"{changed.status} (+{changed.additions}/-{changed.deletions})",
            )
        )
        for function_name in changed.functions:
            evidence.append(
                Evidence(
                    type="function_change",
                    source=changed.path,
                    detail=f"function '{function_name}' touched",
                )
            )
        basename = changed.path.rsplit("/", 1)[-1].lower()
        if basename in _DEPENDENCY_MANIFESTS:
            evidence.append(
                Evidence(type="dependency", source=changed.path, detail="dependency manifest changed")
            )
    return evidence


def _build_changed_file(raw: dict[str, Any]) -> ChangedFile:
    status = raw.get("status", "modified")
    if status not in ("added", "modified", "removed", "renamed"):
        status = "modified" if status == "changed" else "added" if status == "copied" else "modified"
    patch = raw.get("patch")
    return ChangedFile(
        path=raw.get("filename", ""),
        status=status,
        additions=int(raw.get("additions", 0)),
        deletions=int(raw.get("deletions", 0)),
        patch=patch,
        functions=_extract_functions(patch),
    )


def _extract_functions(patch: Optional[str]) -> list[str]:
    if not patch:
        return []
    found: list[str] = []
    for line in patch.splitlines():
        for pattern in (_HUNK_FUNCTION, _ADDED_FUNCTION):
            match = pattern.search(line)
            if match and match.group(1) not in found:
                found.append(match.group(1))
    return found[:10]


def _extract_components(changed_files: list[ChangedFile]) -> list[str]:
    try:
        from utils.component_extractor import extract_component_root
    except ImportError:
        def extract_component_root(path: str) -> str:
            parts = path.replace("\\", "/").split("/")
            return parts[0] if len(parts) > 1 else ""

    components: list[str] = []
    for changed in changed_files:
        root = extract_component_root(changed.path)
        if root and root not in components:
            components.append(root)
    return components


def _is_doc(path: str) -> bool:
    return path.lower().endswith(_DOC_SUFFIXES) or path.lower().startswith(("docs/", "doc/"))


def _is_test(path: str) -> bool:
    lowered = path.lower().replace("\\", "/")
    return "/tests/" in lowered or lowered.startswith("tests/") or lowered.endswith("_test.py") or "/test_" in lowered or lowered.startswith("test_")


def _is_config(path: str) -> bool:
    lowered = path.lower()
    basename = lowered.rsplit("/", 1)[-1]
    return lowered.endswith(_CONFIG_SUFFIXES) or basename in _CONFIG_BASENAMES


def _resolve_owner(api_output: dict[str, Any], name: str) -> str:
    if name:
        return name
    html_url = api_output.get("html_url") or ""
    if "github.com/" in html_url:
        return html_url.split("github.com/")[1].split("/")[0]
    return ""


def _resolve_repository(api_output: dict[str, Any], owner: str, name: str, repository: str) -> str:
    if name and repository:
        return f"{name}/{repository}"
    html_url = api_output.get("html_url") or ""
    if "github.com/" in html_url:
        parts = html_url.split("github.com/")[1].split("/")[:2]
        if len(parts) == 2:
            return "/".join(parts)
    return f"{owner}/{repository}".strip("/")


def _resolve_author(api_output: dict[str, Any], commit_block: dict[str, Any]) -> str:
    login = (api_output.get("author") or {}).get("login")
    if login:
        return login
    return (commit_block.get("author") or {}).get("name", "")


def _parse_iso(value: Optional[str]) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        return str(body.get("message", body))
    except Exception:
        return response.text[:200]
