from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import aclosing
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from google.adk.agents import Agent
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

from github_infrastructure.change_records import Evidence
from github_infrastructure.config import settings
from security.guard import make_before_tool_guard
from security.identities import AgentIdentity

try:
    from agents.change_analysis_agent import EnrichedChangeRecord
except ImportError:
    from agents.change_analysis_agent import EnrichedChangeRecord

try:
    from blast_radius.models import BlastRadiusEntry
except ImportError:
    from blast_radius.models import BlastRadiusEntry

try:
    from project_context.models import ProjectContextOutput
except ImportError:
    from project_context.models import ProjectContextOutput

logger = logging.getLogger("agents.impact_analysis")

DEFAULT_MODEL = os.getenv(
    "IMPACT_ANALYSIS_MODEL", os.getenv("CONTEXT_AGENT_MODEL", "gemini-3.5-flash")
)
PROJECT_CONTEXTS_COLLECTION = "project_contexts"


class StakeholderImpact(BaseModel):
    team: str = Field(description="Team identifier impacted")
    impact_type: Literal["awareness", "action", "approval", "none"] = Field(
        description="Whether team needs awareness, action, approval, or no impact"
    )
    reason: str = Field(description="Grounded reason for the impact classification")


class ImpactReport(BaseModel):
    project_id: str = Field(description="Project identifier")
    commit_sha: str = Field(description="Commit SHA being analyzed")
    affected_components: list[str] = Field(default_factory=list)
    stakeholders: list[StakeholderImpact] = Field(default_factory=list)
    downstream_workflows: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # Deterministic blast-radius evidence (from blast_radius:analyze step).
    changed_components: list[str] = Field(default_factory=list)
    blast_radius: list[BlastRadiusEntry] = Field(default_factory=list)


IMPACT_ANALYSIS_INSTRUCTION = """
You are the Impact Analysis Agent. You analyze an EnrichedChangeRecord together with its ProjectContext to infer impact.

Workflow you MUST follow:
1. Call get_project_context with project_id to fetch the ProjectContext.
2. Call get_enriched_change with project_id and commit_sha to fetch the EnrichedChangeRecord.
3. Call get_blast_radius with project_id and pr_number to fetch the deterministic BlastRadiusReport
   (static sandbox analysis: changed components, dependency traversal, ownership). If it returns an
   error (e.g. analysis unavailable), proceed without it — do not invent its contents.
4. Analyze the available evidence to infer:
   - Affected components: which architecture components are touched, using changed_files,
     architecture.components, and the blast radius changed_components when available.
   - blast_radius: when a BlastRadiusReport was fetched, carry its entries into the output. You may
     adjust impact_type per entry (awareness/action/approval/none) based on the evidence, but you
     MUST preserve entity_type, entity_id, and the evidence of every entry. NEVER invent new
     entities, teams, people, or relationships that are absent from the report. If no report was
     fetched, leave blast_radius empty.
   - Which teams are impacted: map affected_components to team_context owned_components and
     blast radius team entries when available.
   - For each team, decide impact_type: awareness (needs information), action (must perform work), approval (must approve before external/sensitive action), none (should not be notified). Base this on team ownership and nature of change (e.g., mobile team needs action if API contract changed).
   - Downstream workflows affected: processes relying on changed behavior, derived from work_scope.planned_tasks, dependencies, and blast radius downstream_workflows.
   - Risks: potential downstream failures or required follow-up.
   - Evidence for every inference: each stakeholder, workflow, and risk must cite file_change, dependency, or project_context evidence.
   - confidence: 0-1 reflecting grounding strength.

Rules:
- Every stakeholder, workflow, and risk inference must include evidence.
- Ground all inferences in ProjectContext, EnrichedChangeRecord (changed_files, patches, functions, summary, behavior_before/after), and the BlastRadiusReport when available.
- Do not invent teams, components, people, or dependencies not present in the evidence.
- After generating the ImpactReport, you MUST call save_impact_report with project_id and the complete report, then return the same JSON as final output.

Return ONLY the ImpactReport JSON matching the output_schema.
"""

SAVE_TOOL_INSTRUCTION = """
After you have produced the ImpactReport, you MUST call save_impact_report exactly once with project_id and the complete impact_report, then finish with the same JSON.
"""


class ImpactAnalysisAgent:
    def __init__(
        self,
        model: Optional[str] = None,
        project: Optional[str] = None,
        database: Optional[str] = None,
    ) -> None:
        self.model_name = model or DEFAULT_MODEL
        self.project = project or settings.gcp_project
        self.database = database or os.getenv("FIRESTORE_DATABASE", "(default)")
        if not self.project:
            raise ValueError("GCP project is not configured")
        self.app_name = "impact_analysis_agent"
        self.session_service = InMemorySessionService()
        self.agent = self._build_agent()
        self.runner = Runner(
            app_name=self.app_name,
            agent=self.agent,
            session_service=self.session_service,
        )
        self._firestore: Any = None
        self._current_project_id: str = ""
        self._current_commit_sha: str = ""
        self._current_pr_number: int = 0
        self.saved_document_path: Optional[str] = None
        self.saved_by_tool: bool = False

    def _build_agent(self) -> Agent:
        return Agent(
            name="impact_analysis_agent",
            model=self.model_name,
            instruction=IMPACT_ANALYSIS_INSTRUCTION + SAVE_TOOL_INSTRUCTION,
            output_schema=ImpactReport,
            tools=[
                self._get_project_context_tool,
                self._get_enriched_change_tool,
                self._get_blast_radius_tool,
                self._save_impact_report_tool,
            ],
            before_tool_callback=make_before_tool_guard(AgentIdentity.IMPACT_ANALYSIS_AGENT),
        )

    def _get_project_context_tool(self, project_id: str) -> dict:
        """Read the ProjectContext from Firestore for the given project_id."""
        try:
            client = self.firestore_client
            doc = client.collection(PROJECT_CONTEXTS_COLLECTION).document(project_id).get()
            if not doc.exists:
                logger.warning("ProjectContext not found for project_id=%s", project_id)
                return {"status": "error", "error": f"ProjectContext not found for project_id={project_id}"}
            data = doc.to_dict()
            validated = ProjectContextOutput.model_validate(data)
            return {"status": "success", "project_context": validated.model_dump(mode="json")}
        except Exception as exc:
            logger.exception("Failed to fetch ProjectContext for %s", project_id)
            return {"status": "error", "error": str(exc)}

    def _get_enriched_change_tool(self, project_id: str, commit_sha: str) -> dict:
        """Read the EnrichedChangeRecord from Firestore for project_id and commit_sha."""
        try:
            client = self.firestore_client
            doc = (
                client.collection("projects")
                .document(project_id)
                .collection("enriched_changes")
                .document(commit_sha)
                .get()
            )
            if not doc.exists:
                logger.warning(
                    "EnrichedChange not found for project_id=%s commit_sha=%s", project_id, commit_sha
                )
                return {
                    "status": "error",
                    "error": f"EnrichedChange not found for project_id={project_id} commit_sha={commit_sha}",
                }
            data = doc.to_dict()
            validated = EnrichedChangeRecord.model_validate(data)
            return {"status": "success", "enriched_change": validated.model_dump(mode="json")}
        except Exception as exc:
            logger.exception("Failed to fetch EnrichedChange for %s %s", project_id, commit_sha)
            return {"status": "error", "error": str(exc)}

    def _get_blast_radius_tool(self, project_id: str, pr_number: int) -> dict:
        """Read-only access to the deterministic BlastRadiusReport for the PR.

        The Impact Agent gets the generated evidence/graph — never sandbox or
        repository execution permissions.
        """
        try:
            client = self.firestore_client
            doc = (
                client.collection("projects")
                .document(project_id)
                .collection("blast_radius")
                .document(str(pr_number))
                .get()
            )
            if not doc.exists:
                logger.warning(
                    "BlastRadiusReport not found for project_id=%s pr_number=%s", project_id, pr_number
                )
                return {
                    "status": "error",
                    "error": f"BlastRadiusReport not found for project_id={project_id} pr_number={pr_number}",
                }
            return {"status": "success", "blast_radius_report": doc.to_dict() or {}}
        except Exception as exc:
            logger.exception("Failed to fetch BlastRadiusReport for %s/%s", project_id, pr_number)
            return {"status": "error", "error": str(exc)}

    def _save_impact_report_tool(self, project_id: str, impact_report: dict[str, Any]) -> dict:
        """Save the ImpactReport to Firestore under projects/{project_id}/impact_reports/{commit_sha}."""
        try:
            report = ImpactReport.model_validate(impact_report)
        except Exception as exc:
            logger.warning("Tool received invalid impact_report: %s", exc)
            return {"status": "error", "error": f"invalid impact_report: {exc}"}

        if report.project_id != self._current_project_id or report.commit_sha != self._current_commit_sha:
            logger.info(
                "Correcting report identifiers to %s/%s (was %s/%s)",
                self._current_project_id,
                self._current_commit_sha,
                report.project_id,
                report.commit_sha,
            )
            report.project_id = self._current_project_id
            report.commit_sha = self._current_commit_sha

        try:
            path = self.persist_impact(project_id, report)
            self.saved_document_path = path
            self.saved_by_tool = True
            return {"status": "success", "document_path": path}
        except Exception as exc:
            logger.exception("Failed to save impact report for %s", project_id)
            return {"status": "error", "error": str(exc)}

    def analyze(
        self, project_id: str, commit_sha: str, pr_number: Optional[int] = None
    ) -> tuple[ImpactReport, str, bool]:
        if not project_id or not project_id.strip():
            raise ValueError("project_id cannot be empty")
        if not commit_sha or not commit_sha.strip():
            raise ValueError("commit_sha cannot be empty")

        self._current_project_id = project_id
        self._current_commit_sha = commit_sha
        self._current_pr_number = pr_number or 0
        self.saved_document_path = None
        self.saved_by_tool = False

        payload = {"project_id": project_id, "commit_sha": commit_sha}
        if pr_number:
            payload["pr_number"] = pr_number
        events = asyncio.run(self._run_agent(payload))
        report = self._extract_report(events)

        report.project_id = project_id
        report.commit_sha = commit_sha

        if self.saved_document_path is None:
            logger.info("Agent did not call save_impact_report; using fallback save")
            path = self.persist_impact(project_id, report)
            self.saved_document_path = path

        return report, self.saved_document_path, self.saved_by_tool

    def analyze_with_records(
        self,
        project_id: str,
        enriched_change: EnrichedChangeRecord | dict[str, Any],
        project_context: ProjectContextOutput | dict[str, Any],
    ) -> tuple[ImpactReport, str, bool]:
        if isinstance(enriched_change, dict):
            enriched_change = EnrichedChangeRecord.model_validate(enriched_change)
        if isinstance(project_context, dict):
            project_context = ProjectContextOutput.model_validate(project_context)

        commit_sha = enriched_change.commit_sha
        return self.analyze(project_id, commit_sha)

    async def _run_agent(self, payload: dict[str, Any]) -> list[Event]:
        user_id = "user"
        session = await self.session_service.create_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=uuid.uuid4().hex,
        )
        user_message = types.UserContent(parts=[types.Part(text=json.dumps(payload))])
        events: list[Event] = []
        async with aclosing(
            self.runner.run_async(
                user_id=user_id,
                session_id=session.id,
                new_message=user_message,
            )
        ) as stream:
            async for event in stream:
                events.append(event)
        return events

    def _extract_report(self, events: list[Event]) -> ImpactReport:
        for event in reversed(events):
            if not event.is_final_response():
                continue
            text = self._event_text(event)
            if text:
                try:
                    return ImpactReport.model_validate(self._coerce(text))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        raise RuntimeError("Could not extract ImpactReport from agent response.")

    @staticmethod
    def _event_text(event: Event) -> str:
        content = event.content
        if content is None:
            return ""
        parts = getattr(content, "parts", None) or []
        return "".join(part.text for part in parts if part.text and not part.thought)

    @staticmethod
    def _coerce(value: str) -> object:
        text = value.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        return json.loads(text)

    @property
    def firestore_client(self) -> Any:
        if self._firestore is None:
            from google.cloud import firestore

            kwargs: dict = {"project": self.project}
            if self.database and self.database != "(default)":
                kwargs["database"] = self.database
            self._firestore = firestore.Client(**kwargs)
        return self._firestore

    def persist_impact(self, project_id: str, report: ImpactReport) -> str:
        doc_ref = (
            self.firestore_client.collection("projects")
            .document(project_id)
            .collection("impact_reports")
            .document(report.commit_sha)
        )
        payload = report.model_dump(mode="json")
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        doc_ref.set(payload)
        path = f"projects/{project_id}/impact_reports/{report.commit_sha}"
        logger.info("Saved impact report %s", path)
        return path


