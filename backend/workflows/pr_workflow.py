from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from agents.communication_agent import CommunicationAgent, CommunicationResult, SlackMessage
from agents.coordination_agent import CoordinationAgent, CoordinationPlan
from agents.impact_analysis_agent import ImpactReport
from github_infrastructure.github_service import GitHubService
from project_context.models import ProjectContextOutput

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

    async def run(self, pr_event: PREvent) -> CommunicationBundle:
        commits = await self._fetch_pr_commits(pr_event)
        project_context = await self._load_project_context(pr_event.project_id)
        enriched_changes = await self._load_enriched_changes(pr_event.project_id, commits)
        pr_input = self._build_pr_analysis_input(project_context, enriched_changes, pr_event)

        # Execute agents sequentially - workflow orchestrates, agents don't call each other
        impact_report = await self._run_impact_agent(pr_input)
        coordination_plan = await self._run_coordinator_agent(pr_input, impact_report)
        communication_bundle = await self._run_communication_agent(pr_input, coordination_plan)

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

    async def _run_impact_agent(self, pr_input: PRAnalysisInput) -> ImpactReport:
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
        report, _, _ = await self._to_thread(agent.analyze, pr_input.project_id, first.commit_sha)
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
    ) -> CommunicationBundle:
        agent = self.communication_agent or self._get_communication_agent()
        if hasattr(agent, "analyze_pr"):
            return await agent.analyze_pr(pr_input, coordination_plan)  # type: ignore
        await self._ensure_coordination_persisted(pr_input.project_id, coordination_plan)
        surrogate_sha = pr_input.enriched_changes[0].commit_sha if pr_input.enriched_changes else str(pr_input.pr_number)
        result, _, _ = await self._to_thread(agent.analyze, pr_input.project_id, surrogate_sha) if hasattr(agent, "analyze") else (None, None, None)
        # Adapt CommunicationResult to CommunicationBundle for PR
        if isinstance(result, CommunicationResult):
            bundle = CommunicationBundle(
                project_id=pr_input.project_id,
                pr_number=pr_input.pr_number,
                owner=pr_input.project_context.project.name,
                repository="",
                impact_report=None,
                coordination_plan=coordination_plan,
                communication_result=result,
                messages=result.messages,
                summary=result.summary,
            )
            return bundle
        # Fallback bundle construction
        return CommunicationBundle(
            project_id=pr_input.project_id,
            pr_number=pr_input.pr_number,
            owner="",
            repository="",
            coordination_plan=coordination_plan,
            messages=[],
            summary=coordination_plan.summary,
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
            comm_doc.set({**comm_payload, "pr_number": pr_event.pr_number, "persisted_at": pr_payload_time()})

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
