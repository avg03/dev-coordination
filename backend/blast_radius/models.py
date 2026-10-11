"""Blast Radius domain models.

The repository graph and report are read-only evidence structures: agents may
reason over them but there is no mutation API — graph relationships are only
produced by the deterministic analyzers.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from github_infrastructure.change_records import Evidence

ANALYSIS_VERSION = "v1"

EntityType = Literal["person", "team", "component", "workflow"]
ImpactLevel = Literal["direct", "indirect", "dependency", "workflow"]
ImpactType = Literal["awareness", "action", "approval", "none"]
FileStatus = Literal["added", "modified", "removed", "renamed"]

# Deterministic confidence weights by strongest supporting evidence kind.
EVIDENCE_WEIGHTS: dict[str, float] = {
    "explicit_ownership": 0.95,
    "codeowners": 0.9,
    "direct_dependency": 0.85,
    "component_ownership": 0.8,
    "workflow_ownership": 0.75,
    "indirect_dependency": 0.6,
    "component_mapping": 0.6,
}


class ChangedFileInfo(BaseModel):
    """One entry from the PR files API."""

    path: str
    status: FileStatus = "modified"
    previous_path: Optional[str] = Field(
        default=None, description="Original path for renamed files"
    )
    additions: int = 0
    deletions: int = 0


class DependencyEdge(BaseModel):
    """A single relationship discovered in the repository snapshot."""

    source: str = Field(description="Path or component id at the edge origin")
    target: str = Field(description="Path or component id at the edge destination")
    relationship: Literal[
        "imports", "calls", "references", "inherits_from", "depends_on", "used_by", "owns", "member_of"
    ] = "depends_on"
    depth: int = Field(default=1, ge=1, description="Traversal depth from the changed set")
    reason: str = ""


class RepositoryGraph(BaseModel):
    """Read-only view of the relationships used for one analysis run."""

    model_config = ConfigDict(frozen=True)

    nodes: list[str] = Field(default_factory=list, description="File paths and component ids")
    edges: list[DependencyEdge] = Field(default_factory=list)


class BlastRadiusEntry(BaseModel):
    entity_type: EntityType
    entity_id: str = Field(description="Stable id: component id, team name, person name, or workflow name")
    name: str = ""
    impact_level: ImpactLevel
    impact_type: ImpactType
    reason: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class BlastRadiusReport(BaseModel):
    """Deterministic output of one blast-radius analysis run."""

    project_id: str
    pr_number: int
    head_sha: str = ""
    analysis_version: str = ANALYSIS_VERSION
    repository: str = ""
    changed_files: list[ChangedFileInfo] = Field(default_factory=list)
    changed_components: list[str] = Field(default_factory=list)
    changed_symbols: dict[str, list[str]] = Field(
        default_factory=dict, description="file path -> changed symbol names"
    )
    graph: RepositoryGraph = Field(default_factory=RepositoryGraph)
    entries: list[BlastRadiusEntry] = Field(default_factory=list)
    downstream_workflows: list[str] = Field(default_factory=list)
    incomplete: bool = Field(
        default=False, description="True when analysis degraded (sandbox failure, missing data)"
    )
    incomplete_reason: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
