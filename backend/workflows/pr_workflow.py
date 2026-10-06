from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from agents.communication_agent import CommunicationAgent, CommunicationResult, SlackMessage
from agents.coordination_agent import CoordinationAgent, CoordinationPlan
from agents.impact_analysis_agent import ImpactReport
from agents.supervisor_agent import SupervisionResult, SupervisorAgent
from blast_radius.blast_radius_service import BlastRadiusService
from blast_radius.diff_analyzer import parse_pr_files
from blast_radius.models import ANALYSIS_VERSION, BlastRadiusReport, ChangedFileInfo
from github_infrastructure.github_service import GitHubService
from project_context.models import ProjectContextOutput
from security.idempotency import ClaimStatus, IdempotencyManager, StepInProgressError

try:
    from agents.change_analysis_agent import EnrichedChangeRecord
except ImportError:
    from agents.change_analysis_agent import EnrichedChangeRecord

logger = logging.getLogger("workflows.pr_workflow")


class PREvent(BaseModel):
    project_id: str = Field(description="Project identifier")
    owner: str = Field(description="GitHub owner/org")
    repository: str = Field(description="Repository name")
    pr_number: int = Field(description="Pull request number")
    action: str = Field(default="opened", description="PR action: opened, synchronize, reopened, closed")
    head_sha: str = Field(default="", description="PR head SHA — changes the idempotency key so updated PRs re-analyze")


class PRAnalysisInput(BaseModel):
    project_id: str
    pr_number: int
    project_context: ProjectContextOutput
    enriched_changes: List[EnrichedChangeRecord] = Field(default_factory=list)


class CommunicationBundle(BaseModel):
    project_id: str
    pr_number: int
    owner: str
    repository: str
    impact_report: Optional[ImpactReport] = None
    coordination_plan: Optional[CoordinationPlan] = None
    communication_result: Optional[CommunicationResult] = None
    messages: List[SlackMessage] = Field(default_factory=list)
    summary: str = Field(default="")


class PRWorkflow:
    def __init__(
        self,
        github_service: Optional[GitHubService] = None,
        impact_agent: Optional[Any] = None,
        coordinator_agent: Optional[Any] = None,
        communication_agent: Optional[Any] = None,
        supervisor_agent: Optional[Any] = None,
        idempotency_manager: Optional[IdempotencyManager] = None,
        blast_radius_service: Optional[Any] = None,
        project: Optional[str] = None,
        database: Optional[str] = None,
    ) -> None:
        from github_infrastructure.config import settings

        self.github_service = github_service or GitHubService()
        self.project = project or settings.gcp_project
        self.database = database or os.getenv("FIRESTORE_DATABASE", "(default)")
        # Lazy import to avoid circular deps, use existing agents
        self.impact_agent = impact_agent
        self.coordinator_agent = coordinator_agent
        self.communication_agent = communication_agent
        self.supervisor_agent = supervisor_agent
        self.blast_radius_service = blast_radius_service
        self._idempotency = idempotency_manager
        self._firestore = None

    @property
    def firestore_client(self):
        if self._firestore is None:
            from google.cloud import firestore

            kwargs: dict = {"project": self.project}
            if self.database and self.database != "(default)":
                kwargs["database"] = self.database
            self._firestore = firestore.Client(**kwargs)
        return self._firestore

    async def _load_existing_bundle(self, pr_event: PREvent) -> CommunicationBundle | None:
        """Fast path: if this PR head already has a communications doc, skip LLM re-runs.

        A changed head SHA means new work — the workflow re-runs (idempotency
        keys incorporate the head SHA, so each head analyzes exactly once).
        """
        import asyncio

        def _sync_check():
            base = self.firestore_client.collection("projects").document(pr_event.project_id)
            comm_snap = base.collection("communications").document(str(pr_event.pr_number)).get()
            if not comm_snap.exists:
                return None
            data = comm_snap.to_dict() or {}
            stored_head = str(data.get("head_sha", "") or "")
            if pr_event.head_sha and stored_head and stored_head != pr_event.head_sha:
                logger.info(
                    "PR %s head changed (%s -> %s) — re-running workflow",
                    pr_event.pr_number,
                    stored_head[:10],
                    pr_event.head_sha[:10],
                )
                return None
            try:
                return CommunicationBundle.model_validate(data)
            except Exception:
                return None

        return await asyncio.to_thread(_sync_check)

    def _get_idempotency_manager(self) -> IdempotencyManager:
        if self._idempotency is None:
            from security.idempotency import get_idempotency_manager

            self._idempotency = get_idempotency_manager()
        return self._idempotency

    async def _run_step(
        self,
        step_name: str,
        pr_event: PREvent,
        workflow_id: str,
        input_fingerprint: dict[str, Any],
        execute,
        model_cls,
    ):
        """Claim -> execute -> persist result -> mark COMPLETED (spec §8 ordering).

        The full result is stored inside the idempotency record before it is
        marked COMPLETED, so a retry after a crash returns the stored result
        instead of re-executing the (LLM) step.
        """
        import asyncio

        idem = self._get_idempotency_manager()
        key = idem.build_key(pr_event.project_id, workflow_id, step_name, input_fingerprint)
        outcome = await asyncio.to_thread(
            idem.claim,
            key,
            workflow_id=workflow_id,
            project_id=pr_event.project_id,
            step_name=step_name,
            input_payload=input_fingerprint,
        )
        if outcome.status == ClaimStatus.COMPLETED:
            logger.info("Step %s already completed for %s — reusing stored result", step_name, workflow_id)
            return model_cls.model_validate(outcome.result or {})
        if outcome.status == ClaimStatus.IN_PROGRESS:
            raise StepInProgressError(f"{step_name} is in progress for {workflow_id}")

        try:
            result = await execute()
        except Exception as exc:
            await asyncio.to_thread(idem.fail, key, str(exc))
            raise

        await asyncio.to_thread(idem.complete, key, result.model_dump(mode="json"))
        return result

    async def run(self, pr_event: PREvent) -> CommunicationBundle:
        # Fast path: same head SHA already analyzed -> skip LLM calls (prevents 429 loops)
        existing = await self._load_existing_bundle(pr_event)
        if existing is not None:
            logger.info("PR %s already has communications bundle for %s — skipping LLM re-run", pr_event.pr_number, pr_event.project_id)
            return existing

        workflow_id = f"pr:{pr_event.project_id}:{pr_event.pr_number}:{pr_event.head_sha or 'na'}"
        commits = await self._fetch_pr_commits(pr_event)
        project_context = await self._load_project_context(pr_event.project_id)
        enriched_changes = await self._load_enriched_changes(pr_event.project_id, commits)
        pr_input = self._build_pr_analysis_input(project_context, enriched_changes, pr_event)

        # Input fingerprint: any change (new head, new commits, updated context)
        # produces different idempotency keys, so the new input is analyzed.
        input_fingerprint = {
            "pr_number": pr_event.pr_number,
            "head_sha": pr_event.head_sha,
            "action": pr_event.action,
            "commit_shas": commits,
            "context_updated_at": project_context.metadata.updated_at,
            "enriched_shas": [c.commit_sha for c in enriched_changes],
        }

        # Deterministic blast radius FIRST: sandboxed static analysis produces
        # the evidence the Impact Agent reasons over (never the reverse).
        # Keyed by PR head SHA + context version + analysis version.
        blast_radius_fingerprint = {
            "pr_number": pr_event.pr_number,
            "head_sha": pr_event.head_sha,
            "context_updated_at": project_context.metadata.updated_at,
            "analysis_version": ANALYSIS_VERSION,
        }
        blast_report = await self._run_blast_radius_step(
            pr_event, workflow_id, blast_radius_fingerprint, project_context
        )

        # Execute agents sequentially - workflow orchestrates, agents don't call each other.
        # Agents reason and persist drafts; nothing here sends Slack.
        impact_report = await self._run_step(
            "impact:analyze", pr_event, workflow_id, input_fingerprint,
            lambda: self._run_impact_agent(pr_input, blast_report), ImpactReport,
        )
        self._reconcile_blast_radius(impact_report, blast_report)
        coordination_plan = await self._run_step(
            "coordination:create", pr_event, workflow_id, input_fingerprint,
            lambda: self._run_coordinator_agent(pr_input, impact_report), CoordinationPlan,
        )
        communication_result = await self._run_step(
            "communication:generate", pr_event, workflow_id, input_fingerprint,
            lambda: self._run_communication_agent(pr_input, coordination_plan), CommunicationResult,
        )
        supervision = await self._run_step(
            "communication:supervise", pr_event, workflow_id, input_fingerprint,
            lambda: self._run_supervisor_agent(pr_input, coordination_plan, communication_result),
            SupervisionResult,
        )

        final_messages = supervision.final_messages or communication_result.messages
        communication_bundle = CommunicationBundle(
            project_id=pr_event.project_id,
            pr_number=pr_event.pr_number,
            owner=pr_event.owner,
            repository=pr_event.repository,
            coordination_plan=coordination_plan,
            communication_result=communication_result,
            messages=list(final_messages),
            summary=supervision.summary or communication_result.summary,
        )

        await self._persist_outputs(pr_event, impact_report, coordination_plan, communication_bundle)

        return communication_bundle

    async def _fetch_pr_commits(self, pr_event: PREvent) -> List[str]:
        return await self.github_service.fetch_pr_commits(
            pr_event.owner, pr_event.repository, pr_event.pr_number
        )

    async def _load_project_context(self, project_id: str) -> ProjectContextOutput:
        # Use Firestore service if available, otherwise direct client via repository pattern
        # No dedicated ProjectContextService exists, so we encapsulate Firestore access here
        # to keep workflow free of raw queries - this method *is* the service boundary
        import asyncio

        def _sync_load() -> ProjectContextOutput:
            doc = self.firestore_client.collection("project_contexts").document(project_id).get()
            if not doc.exists:
                raise ValueError(f"ProjectContext not found for project_id={project_id}")
            return ProjectContextOutput.model_validate(doc.to_dict())

        return await asyncio.to_thread(_sync_load)

    async def _load_enriched_changes(self, project_id: str, commit_shas: List[str]) -> List[EnrichedChangeRecord]:
        import asyncio

        def _sync_load() -> List[EnrichedChangeRecord]:
            records: List[EnrichedChangeRecord] = []
            for sha in commit_shas:
                doc = (
                    self.firestore_client.collection("projects")
                    .document(project_id)
                    .collection("enriched_changes")
                    .document(sha)
                    .get()
                )
                if not doc.exists:
                    logger.warning("EnrichedChange not found for %s %s", project_id, sha)
                    continue
                records.append(EnrichedChangeRecord.model_validate(doc.to_dict()))
            return records

        return await asyncio.to_thread(_sync_load)

    def _build_pr_analysis_input(
        self,
        project_context: ProjectContextOutput,
        enriched_changes: List[EnrichedChangeRecord],
        pr_event: PREvent,
    ) -> PRAnalysisInput:
        return PRAnalysisInput(
            project_id=pr_event.project_id,
            pr_number=pr_event.pr_number,
            project_context=project_context,
            enriched_changes=enriched_changes,
        )

    def _get_blast_radius_service(self) -> BlastRadiusService:
        if self.blast_radius_service is None:
            self.blast_radius_service = BlastRadiusService()
        return self.blast_radius_service

    async def _run_blast_radius_step(
        self,
        pr_event: PREvent,
        workflow_id: str,
        fingerprint: dict[str, Any],
        project_context: ProjectContextOutput,
    ) -> BlastRadiusReport:
        """Idempotent blast_radius:analyze step (spec: keyed by PR head SHA +
        project/context version + analysis version). Result is persisted to
        projects/{id}/blast_radius/{pr_number} BEFORE the record is marked
        COMPLETED, so retries return the stored report and the sandbox is
        never re-provisioned for the same key."""
        import asyncio

        async def _execute() -> BlastRadiusReport:
            raw_files = await self.github_service.fetch_pr_files(
                pr_event.owner, pr_event.repository, pr_event.pr_number
            )

            def _sync() -> BlastRadiusReport:
                changed_files = parse_pr_files(raw_files)
                patches = {
                    str(f.get("filename")): str(f.get("patch") or "")
                    for f in raw_files
                    if f.get("filename") and f.get("patch")
                }
                service = self._get_blast_radius_service()
                return service.analyze(
                    project_id=pr_event.project_id,
                    pr_number=pr_event.pr_number,
                    repo=f"{pr_event.owner}/{pr_event.repository}",
                    head_sha=pr_event.head_sha,
                    changed_files=changed_files,
                    project_context=project_context.model_dump(mode="json"),
                    patches=patches,
                )

            report = await asyncio.to_thread(_sync)
            await asyncio.to_thread(self._persist_blast_radius, pr_event, report)
            return report

        return await self._run_step(
            "blast_radius:analyze",
            pr_event,
            workflow_id,
            fingerprint,
            _execute,
            BlastRadiusReport,
        )

    def _persist_blast_radius(self, pr_event: PREvent, report: BlastRadiusReport) -> None:
        from datetime import datetime, timezone

        self.firestore_client.collection("projects").document(pr_event.project_id).collection(
            "blast_radius"
        ).document(str(pr_event.pr_number)).set(
            {**report.model_dump(mode="json"), "persisted_at": datetime.now(timezone.utc).isoformat()}
        )

    def _reconcile_blast_radius(self, impact_report: ImpactReport, blast_report: Optional[BlastRadiusReport]) -> None:
        """Evidence integrity: the Impact Agent must not drop deterministic
        blast-radius entries — union any missing ones back from the stored report."""
        if blast_report is None:
            return
        present_ids = {entry.entity_id for entry in impact_report.blast_radius}
        missing = [entry for entry in blast_report.entries if entry.entity_id not in present_ids]
        if missing:
            logger.info(
                "Re-attaching %d blast radius entries dropped by the Impact Agent",
                len(missing),
            )
        impact_report.blast_radius = list(impact_report.blast_radius) + missing
        if not impact_report.changed_components:
            impact_report.changed_components = list(blast_report.changed_components)

    async def _run_impact_agent(self, pr_input: PRAnalysisInput, blast_report: Optional[BlastRadiusReport] = None) -> ImpactReport:
        # Delegate to ImpactAgent - workflow does not contain prompt engineering
        agent = self.impact_agent or self._get_impact_agent()
        # For PR, we create a synthetic aggregated impact via the agent's PR-aware method if available,
        # otherwise fall back to single-commit handling. Keep orchestration, not business logic.
        if hasattr(agent, "analyze_pr"):
            return await agent.analyze_pr(pr_input)  # type: ignore
        # Fallback: use first enriched change to drive existing single-commit agent, then adapt IDs to PR
        if not pr_input.enriched_changes:
            raise ValueError("No enriched changes to analyze for PR")
        # Use first commit as representative for PR-level analysis (workflow-level aggregation is minimal)
        first = pr_input.enriched_changes[0]
        # Ensure enriched change is persisted so tool fetch succeeds
        await self._ensure_enriched_persisted(pr_input.project_id, first)
        report, _, _ = await self._to_thread(
            agent.analyze, pr_input.project_id, first.commit_sha, pr_input.pr_number
        )
        # Re-key report to PR number for persistence
        report.project_id = pr_input.project_id
        # keep original commit_sha for traceability, but workflow will persist under pr_number
        return report

    async def _run_coordinator_agent(
        self, pr_input: PRAnalysisInput, impact_report: ImpactReport
    ) -> CoordinationPlan:
        agent = self.coordinator_agent or self._get_coordinator_agent()
        if hasattr(agent, "analyze_pr"):
            return await agent.analyze_pr(pr_input, impact_report)  # type: ignore
        # Fallback: ensure impact report is persisted so coordinator's tool can fetch it
        await self._ensure_impact_persisted(pr_input.project_id, impact_report)
        # Coordinator expects project_id + commit_sha; use first commit's sha as surrogate for PR
        surrogate_sha = pr_input.enriched_changes[0].commit_sha if pr_input.enriched_changes else str(pr_input.pr_number)
        plan, _, _ = await self._to_thread(agent.analyze, pr_input.project_id, surrogate_sha)
        plan.project_id = pr_input.project_id
        return plan

    async def _run_communication_agent(
        self, pr_input: PRAnalysisInput, coordination_plan: CoordinationPlan
    ) -> CommunicationResult:
        """Draft messages only — never sends. Delivery goes through the approval endpoint."""
        agent = self.communication_agent or self._get_communication_agent()
        if hasattr(agent, "analyze_pr"):
            return await agent.analyze_pr(pr_input, coordination_plan)  # type: ignore
        await self._ensure_coordination_persisted(pr_input.project_id, coordination_plan)
        surrogate_sha = pr_input.enriched_changes[0].commit_sha if pr_input.enriched_changes else str(pr_input.pr_number)
        result = await self._to_thread(agent.analyze, pr_input.project_id, surrogate_sha)
        if not isinstance(result, CommunicationResult):
            raise ValueError("CommunicationAgent did not return a CommunicationResult")
        return result

    async def _run_supervisor_agent(
        self,
        pr_input: PRAnalysisInput,
        coordination_plan: CoordinationPlan,
        communication_result: CommunicationResult,
    ) -> SupervisionResult:
        """Supervisor critiques/validates drafts. No send capability."""
        agent = self.supervisor_agent
        if agent is None:
            agent = SupervisorAgent(project=self.project, database=self.database)
        if hasattr(agent, "analyze_pr"):
            return await agent.analyze_pr(pr_input, coordination_plan, communication_result)  # type: ignore
        drafts = [m.model_dump(mode="json") for m in communication_result.messages]
        return await self._to_thread(
            agent.analyze,
            pr_input.project_id,
            pr_input.pr_number,
            coordination_plan.summary,
            drafts,
        )

    async def _persist_outputs(
        self,
        pr_event: PREvent,
        impact_report: ImpactReport,
        coordination_plan: CoordinationPlan,
        communication_bundle: CommunicationBundle,
    ) -> None:
        import asyncio

        pr_id = str(pr_event.pr_number)

        def _sync_persist() -> None:
            # Persist under projects/{project_id}/<collection>/{pr_number} as per spec
            base = self.firestore_client.collection("projects").document(pr_event.project_id)
            # ImpactReport
            impact_payload = impact_report.model_dump(mode="json")
            # Re-key to PR number for PR-level doc
            impact_doc = base.collection("impact_reports").document(pr_id)
            impact_doc.set({**impact_payload, "pr_number": pr_event.pr_number, "persisted_at": pr_payload_time()})

            # CoordinationPlan
            coord_payload = coordination_plan.model_dump(mode="json")
            coord_doc = base.collection("coordination_plans").document(pr_id)
            coord_doc.set({**coord_payload, "pr_number": pr_event.pr_number, "persisted_at": pr_payload_time()})

            # CommunicationBundle
            comm_payload = communication_bundle.model_dump(mode="json")
            comm_doc = base.collection("communications").document(pr_id)
            comm_doc.set({**comm_payload, "pr_number": pr_event.pr_number, "head_sha": pr_event.head_sha, "persisted_at": pr_payload_time()})

        await asyncio.to_thread(_sync_persist)

    # --- internal helpers to avoid business logic leakage ---

    def _get_impact_agent(self):
        from agents.impact_analysis_agent import ImpactAnalysisAgent

        return ImpactAnalysisAgent(project=self.project, database=self.database)

    def _get_coordinator_agent(self):
        from agents.coordination_agent import CoordinationAgent

        return CoordinationAgent(project=self.project, database=self.database)

    def _get_communication_agent(self):
        from agents.communication_agent import CommunicationAgent

        return CommunicationAgent(project=self.project, database=self.database)

    async def _ensure_enriched_persisted(self, project_id: str, enriched: EnrichedChangeRecord) -> None:
        import asyncio

        def _sync() -> None:
            doc_ref = (
                self.firestore_client.collection("projects")
                .document(project_id)
                .collection("enriched_changes")
                .document(enriched.commit_sha)
            )
            if not doc_ref.get().exists:
                doc_ref.set(enriched.model_dump(mode="json"))

        await asyncio.to_thread(_sync)

    async def _ensure_impact_persisted(self, project_id: str, report: ImpactReport) -> None:
        import asyncio

        def _sync() -> None:
            doc_ref = (
                self.firestore_client.collection("projects")
                .document(project_id)
                .collection("impact_reports")
                .document(report.commit_sha)
            )
            if not doc_ref.get().exists:
                from datetime import datetime, timezone

                payload = report.model_dump(mode="json")
                payload["saved_at"] = datetime.now(timezone.utc).isoformat()
                doc_ref.set(payload)

        await asyncio.to_thread(_sync)

    async def _ensure_coordination_persisted(self, project_id: str, plan: CoordinationPlan) -> None:
        import asyncio

        def _sync() -> None:
            doc_ref = (
                self.firestore_client.collection("projects")
                .document(project_id)
                .collection("coordination_plans")
                .document(plan.commit_sha)
            )
            if not doc_ref.get().exists:
                from datetime import datetime, timezone

                payload = plan.model_dump(mode="json")
                payload["saved_at"] = datetime.now(timezone.utc).isoformat()
                doc_ref.set(payload)

        await asyncio.to_thread(_sync)

    async def _to_thread(self, func, *args, **kwargs):
        import asyncio

        return await asyncio.to_thread(func, *args, **kwargs)


def pr_payload_time() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
