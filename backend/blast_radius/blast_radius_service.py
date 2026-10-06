"""BlastRadiusService — deterministic orchestration of sandbox + static analysis.

Flow (every phase PermissionGateway-gated under blast_radius_analyzer):

    PR files + ProjectContext
        -> sandbox.create (tarball snapshot at head SHA, no secrets on disk)
        -> static_analysis.execute (AST import/symbol parse — no code execution)
        -> component resolution + bounded dependency traversal (read-only graph)
        -> ownership mapping (ProjectContext + CODEOWNERS, never invented)
        -> candidate BlastRadiusReport with evidence
        -> persisted to projects/{id}/blast_radius/{pr_number}
        -> Impact Agent reasons over the evidence (read-only access)

Failures degrade gracefully: a sandbox/analysis error returns an
`incomplete=True` report so the Impact Agent still runs on PR-diff +
ProjectContext evidence (doc §21).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from github_infrastructure.change_records import Evidence
from security.audit import AuditEvent, AuditLogger, AuditSink
from security.gateway import PermissionGateway, get_permission_gateway

from .dependency_analyzer import build_graph
from .diff_analyzer import changed_symbol_names, effective_changed_paths
from .models import (
    ANALYSIS_VERSION,
    BlastRadiusEntry,
    BlastRadiusReport,
    ChangedFileInfo,
    EVIDENCE_WEIGHTS,
)
from .ownership_resolver import OwnershipIndex, parse_codeowners
from .sandbox import SandboxError, SandboxManager
from .symbol_analyzer import analyze_snapshot

logger = logging.getLogger("blast_radius.service")


class BlastRadiusService:
    def __init__(
        self,
        gateway: Optional[PermissionGateway] = None,
        sandbox: Optional[SandboxManager] = None,
        audit_sink: Optional[AuditSink] = None,
        max_depth: Optional[int] = None,
    ) -> None:
        self.gateway = gateway or get_permission_gateway()
        self.sandbox = sandbox or SandboxManager(gateway=self.gateway)
        self.audit = AuditLogger(audit_sink)
        if max_depth is None:
            try:
                max_depth = int(os.getenv("BLAST_RADIUS_MAX_DEPTH", "2"))
            except ValueError:
                max_depth = 2
        self.max_depth = max(1, min(max_depth, 5))

    # ------------------------------------------------------------------ #

    def analyze(
        self,
        *,
        project_id: str,
        pr_number: int,
        repo: str,
        head_sha: str,
        changed_files: list[ChangedFileInfo],
        project_context: dict[str, Any],
        patches: Optional[dict[str, str]] = None,
    ) -> BlastRadiusReport:
        """Run one full blast-radius analysis. Never raises — degrades to an
        incomplete report on sandbox/analysis failure."""
        patches = patches or {}
        report = BlastRadiusReport(
            project_id=project_id,
            pr_number=pr_number,
            head_sha=head_sha,
            repository=repo,
            changed_files=changed_files,
        )
        report.evidence = [
            Evidence(
                type="file_change",
                source=f.path,
                detail=f"{f.status} (+{f.additions}/-{f.deletions})",
            )
            for f in changed_files
        ]

        try:
            report = self._analyze_in_sandbox(
                report, project_id, pr_number, repo, head_sha, project_context, patches
            )
        except SandboxError as exc:
            logger.warning("Blast radius degraded for PR#%s: %s", pr_number, exc)
            report.incomplete = True
            report.incomplete_reason = str(exc)
            report.changed_components = _components_from_paths(
                [f.path for f in changed_files if f.status != "removed"], None
            )
            self._audit(project_id, pr_number, "degraded", str(exc))
        except Exception as exc:  # noqa: BLE001 — never break the PR workflow
            logger.exception("Blast radius analysis failed for PR#%s", pr_number)
            report.incomplete = True
            report.incomplete_reason = f"analysis error: {exc}"
            report.changed_components = _components_from_paths(
                [f.path for f in changed_files if f.status != "removed"], None
            )
            self._audit(project_id, pr_number, "failed", str(exc))
        return report

    # ------------------------------------------------------------------ #

    def _analyze_in_sandbox(
        self,
        report: BlastRadiusReport,
        project_id: str,
        pr_number: int,
        repo: str,
        head_sha: str,
        project_context: dict[str, Any],
        patches: dict[str, str],
    ) -> BlastRadiusReport:
        handle = self.sandbox.create(project_id, pr_number, repo, head_sha)
        try:
            # Phase 1: symbol/import parse over the WHOLE snapshot (gateway-gated).
            # Dependents are files that import the changed module — they must be
            # parsed even though they didn't change.
            self.sandbox.execute_static_analysis(handle, "ast_symbol_parse")
            read_text: Callable[[str], str] = lambda rel: self.sandbox.read_file(handle, rel)
            all_python = [f for f in self.sandbox.list_files(handle) if f.endswith(".py")]
            facts = analyze_snapshot(read_text, all_python)
            unparsed = [
                path
                for path, f in facts.items()
                if not f.parse_ok and path in {f2.path for f2 in report.changed_files}
            ]

            # Phase 2: import graph + bounded traversal (read-only).
            self.sandbox.execute_static_analysis(handle, "dependency_traversal")
            context_components = (project_context.get("architecture") or {}).get("components") or []
            changed_paths = effective_changed_paths(report.changed_files)
            graph, affected_components = build_graph(
                changed_paths,
                facts,
                context_components=context_components,
                max_depth=self.max_depth,
            )
            report.graph = graph
            report.changed_components = _components_from_paths(changed_paths, context_components)
            report.changed_symbols = {
                path: changed_symbol_names(patches.get(path, "")) for path in changed_paths
            }

            # Phase 3: ownership mapping (ProjectContext + CODEOWNERS).
            codeowners = parse_codeowners(read_text)
            ownership = OwnershipIndex(project_context, codeowners)
            report.entries = self._build_entries(
                report, ownership, affected_components, len(report.changed_files)
            )
            report.downstream_workflows = self._resolve_workflows(project_context, affected_components)

            if unparsed:
                report.incomplete = True
                report.incomplete_reason = f"symbols not parsed (syntax/unreadable): {unparsed[:5]}"

            self._audit(project_id, pr_number, "completed", f"entries={len(report.entries)}")
            return report
        finally:
            self.sandbox.cleanup(handle)

    # ------------------------------------------------------------------ #

    def _build_entries(
        self,
        report: BlastRadiusReport,
        ownership: OwnershipIndex,
        affected_components: list[str],
        changed_file_count: int,
    ) -> list[BlastRadiusEntry]:
        """Assemble candidate entities with evidence. Components are factual
        (derived from changed files); teams/people come only from explicit
        ownership sources; nothing is invented."""
        entries: list[BlastRadiusEntry] = []
        seen: set[tuple[str, str]] = set()

        def add(entry: BlastRadiusEntry) -> None:
            key = (entry.entity_type, entry.entity_id)
            if key in seen:
                return
            seen.add(key)
            entries.append(entry)

        depth_by_component: dict[str, int] = {}
        for edge in report.graph.edges:
            if edge.target not in depth_by_component or edge.depth < depth_by_component[edge.target]:
                depth_by_component[edge.target] = edge.depth

        for component_id in affected_components:
            depth = depth_by_component.get(component_id, 0)
            impact_level = "direct" if depth == 0 else ("dependency" if depth == 1 else "indirect")

            # Component entity — factual: files inside it changed.
            teams = ownership.teams_for_component(component_id)
            component_evidence = [
                Evidence(type="file_change", source=f.path, detail=f"changed file in {component_id}")
                for f in report.changed_files
                if _component_of_file(f.path, component_id)
            ][:5]
            if teams:
                component_evidence.append(
                    Evidence(
                        type="project_context",
                        source=f"architecture.components.{component_id}",
                        detail=f"owned by {', '.join(teams)}",
                    )
                )
            add(
                BlastRadiusEntry(
                    entity_type="component",
                    entity_id=component_id,
                    name=component_id,
                    impact_level=impact_level,  # type: ignore[arg-type]
                    impact_type="none",
                    reason=(
                        f"Changed files map to component {component_id}"
                        if depth == 0
                        else f"{component_id} depends on changed components (depth {depth})"
                    ),
                    evidence=component_evidence,
                    confidence=EVIDENCE_WEIGHTS["component_ownership"] if teams else EVIDENCE_WEIGHTS["component_mapping"],
                )
            )

            # Team + person entities — only from explicit ownership.
            for team in teams:
                team_entry = ownership.team_entry(team, component_id, impact_level, depth)
                if team_entry:
                    add(team_entry)
                for person_entry in ownership.person_entries_for_team(team, component_id, impact_level, depth):
                    add(person_entry)

        # CODEOWNERS for directly changed files (explicit ownership source).
        direct_files = [f.path for f in report.changed_files if f.status != "removed"]
        for entry in ownership.codeowners_entries_for_files(direct_files):
            add(entry)

        return entries

    def _resolve_workflows(self, project_context: dict[str, Any], affected_components: list[str]) -> list[str]:
        """Deterministic workflow mapping: planned tasks whose description/title
        references an affected component id."""
        workflows: list[str] = []
        work_scope = project_context.get("work_scope") or {}
        for task in work_scope.get("planned_tasks") or []:
            title = str(task.get("title") or "")
            description = str(task.get("description") or "")
            haystack = f"{title} {description}".lower()
            if any(component.lower() in haystack for component in affected_components):
                workflows.append(title or str(task.get("id") or ""))
        return [w for w in workflows if w]

    def _audit(self, project_id: str, pr_number: int, outcome: str, detail: str) -> None:
        self.audit.record(
            AuditEvent(
                agent_id="blast_radius_analyzer",
                action="execute",
                resource="static_analysis",
                project_id=project_id,
                workflow_id=f"pr:{project_id}:{pr_number}",
                decision=outcome,
                reason=detail,
                policy="blast_radius_analysis",
                event_type="static_analysis.execution",
            )
        )


def _component_of_file(file_path: str, component_id: str) -> bool:
    from .dependency_analyzer import resolve_component

    try:
        return resolve_component(file_path) == component_id
    except Exception:
        return False


def _components_from_paths(paths: list[str], context_components: Optional[list[dict]]) -> list[str]:
    from .dependency_analyzer import resolve_component

    components: list[str] = []
    for path in paths:
        component = resolve_component(path, context_components)
        if component and component not in components:
            components.append(component)
    return components
