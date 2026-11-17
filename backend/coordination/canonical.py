"""Canonical change context — single-writer/multiple-reader classification.

Problem: each agent re-describes the change, so a `fix` in Change Analysis
can drift into a `refactor` by Communication time. The workflow resolves ONE
canonical context deterministically from the enriched records; downstream
agents consume it (via payloads) and deterministic validators enforce it.
Prompts alone are not the mechanism.

Single-writer: PRWorkflow (resolve_canonical). Multiple readers: Impact,
Coordinator, Communication, Supervisor (payloads) + validators + policy.
Persisted under projects/{project_id}/canonical_context/{pr_number} so
retries/resumes recover it. Keyed by head SHA — stale revisions rejected.

Category discipline: component (auth) vs change_type (fix) vs impact_type
(awareness/action/approval) vs risk/autonomy are never conflated. A
security-sensitive fix stays a fix (security rides on risks, not type).
`bug` normalizes to `fix` (same family); `unknown` abstains (never decides).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger("coordination.canonical")

CANONICAL_COLLECTION = "canonical_context"

CanonicalStatus = Literal["validated", "conflict", "needs_review"]

# bug is the same family as fix; unknown abstains from the vote.
_TYPE_NORMALIZATION = {"bug": "fix"}


def normalize_change_type(value: str) -> str:
    normalized = (value or "").strip().lower() or "unknown"
    return _TYPE_NORMALIZATION.get(normalized, normalized)


class CanonicalChange(BaseModel):
    project_id: str = ""
    owner: str = ""
    repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    component: str = Field(default="", description="Primary affected component/domain, e.g. auth")
    change_type: str = Field(default="unknown", description="Normalized category: feature|fix|refactor|security|docs|test|unknown")
    change_summary: str = ""
    evidence: list[str] = Field(default_factory=list, description="Concise evidence refs backing the classification")
    source_record_ids: list[str] = Field(default_factory=list, description="Enriched commit SHAs used")
    classification_status: CanonicalStatus = "needs_review"  # type: ignore[assignment]
    decided_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def repo_full(self) -> str:
        return f"{self.owner}/{self.repository}" if self.owner else self.repository


def _record_component(record: dict[str, Any]) -> str:
    for key in ("components", "impact_scope"):
        values = record.get(key) or []
        if values and str(values[0]).strip():
            return str(values[0]).strip()
    return ""


def _record_evidence(record: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for changed in (record.get("changed_files") or [])[:5]:
        if isinstance(changed, dict) and changed.get("path"):
            out.append(f"file_change:{changed['path']}")
    summary = str(record.get("summary", "") or "").strip()
    if summary:
        out.append(f"summary:{summary[:160]}")
    return out


def resolve_canonical(
    *,
    project_id: str,
    owner: str,
    repository: str,
    pr_number: int,
    head_sha: str,
    enriched_records: list[dict[str, Any]],
) -> CanonicalChange:
    """Deterministic resolution from enriched records. Never raises."""
    records = [r for r in (enriched_records or []) if isinstance(r, dict)]
    canonical = CanonicalChange(
        project_id=project_id, owner=owner, repository=repository,
        pr_number=pr_number, head_sha=head_sha or "",
        source_record_ids=[str(r.get("commit_sha", "")) for r in records if r.get("commit_sha")],
    )
    if not records:
        canonical.classification_status = "needs_review"
        canonical.change_summary = "no enriched records available"
        return canonical

    votes = Counter(
        normalize_change_type(str(r.get("change_type", "") or ""))
        for r in records
    )
    decided = [t for t in votes if t != "unknown"]
    if not decided:
        canonical.classification_status = "needs_review"
    elif len(decided) == 1:
        canonical.classification_status = "validated"
        canonical.change_type = decided[0]
    else:
        canonical.classification_status = "conflict"
        breakdown = ", ".join(f"{t}x{votes[t]}" for t in sorted(decided))
        canonical.change_type = sorted(decided, key=lambda t: (-votes[t], t))[0]
        canonical.evidence.append(f"conflict:{breakdown}")

    components = Counter(_record_component(r) for r in records)
    components.pop("", None)
    if components:
        canonical.component = sorted(components, key=lambda c: (-components[c], c))[0]

    first_summary = str(records[0].get("summary", "") or "").strip()
    if len(records) == 1:
        canonical.change_summary = first_summary[:500]
    else:
        canonical.change_summary = f"{len(records)} commits ({canonical.change_type}): {first_summary[:300]}".strip()
    for record in records:
        for item in _record_evidence(record):
            if item not in canonical.evidence and len(canonical.evidence) < 8:
                canonical.evidence.append(item)
    return canonical


def validate_consistency(
    canonical: CanonicalChange | dict[str, Any],
    messages: list[dict[str, Any]],
    tasks: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Deterministic cross-artifact checks. Returns issue strings ([] = clean).

    Checks structured fields only (no NLP): canonical status/revision,
    message component vs canonical component, recipient mapping presence.
    Wording flexibility is the Supervisor LLM's job; hard contradictions
    and missing groundings are caught here.
    """
    issues: list[str] = []
    data = canonical.model_dump(mode="json") if hasattr(canonical, "model_dump") else dict(canonical or {})
    status = str(data.get("classification_status", "needs_review"))
    if status == "conflict":
        issues.append("unresolved canonical change-type conflict — explicit review required")
    elif status == "needs_review":
        issues.append("canonical context needs review — autonomous delivery blocked")
    canonical_component = str(data.get("component", "") or "").strip().lower()
    for position, raw in enumerate(messages or []):
        msg = raw if isinstance(raw, dict) else {}
        mid = str(msg.get("id") or f"msg_{position + 1}")
        component = str(msg.get("component", "") or "").strip().lower()
        if canonical_component and component and component != canonical_component:
            issues.append(f"{mid}: component {component!r} contradicts canonical {canonical_component!r}")
        if not str(msg.get("recipient", "") or "").strip():
            issues.append(f"{mid}: missing recipient identity")
        if msg.get("unmapped"):
            issues.append(f"{mid}: recipient has no trusted Slack mapping")
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if not str(task.get("team", "") or "").strip():
            issues.append("coordination task with missing team")
    return issues


def put_canonical(client: Any, canonical: CanonicalChange) -> None:
    """Persist for retry/resume recovery. Never raises."""
    try:
        client.collection("projects").document(canonical.project_id).collection(
            CANONICAL_COLLECTION
        ).document(str(canonical.pr_number)).set(canonical.model_dump(mode="json"))
    except Exception:
        logger.exception("Canonical persist failed for PR#%s", canonical.pr_number)


def load_canonical(client: Any, project_id: str, pr_number: int, head_sha: str) -> CanonicalChange | None:
    """Load only when the stored revision matches — stale contexts rejected."""
    try:
        snap = (
            client.collection("projects").document(project_id)
            .collection(CANONICAL_COLLECTION).document(str(pr_number)).get()
        )
        if not snap.exists:
            return None
        stored = CanonicalChange.model_validate(snap.to_dict() or {})
        if (head_sha or "") and stored.head_sha and stored.head_sha != head_sha:
            logger.warning("Stale canonical rejected for PR#%s (%s != %s)",
                           pr_number, stored.head_sha[:8], head_sha[:8])
            return None
        return stored
    except Exception:
        logger.exception("Canonical load failed for PR#%s", pr_number)
        return None
