from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import aclosing
from typing import Any, Literal, Optional

from google.adk.agents import Agent
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

from github_infrastructure.change_records import ChangeType, Evidence
from github_infrastructure.config import settings

try:
    from agents.impact_analysis_agent import ImpactReport
except ImportError:
    from agents.impact_analysis_agent import ImpactReport

try:
    from project_context.models import ProjectContextOutput
except ImportError:
    from project_context.models import ProjectContextOutput

logger = logging.getLogger("agents.coordination")

DEFAULT_MODEL = os.getenv(
    "COORDINATION_MODEL", os.getenv("CONTEXT_AGENT_MODEL", "gemini-3.5-flash")
)
PROJECT_CONTEXTS_COLLECTION = "project_contexts"


class CoordinationTask(BaseModel):
    team: str = Field(description="Team that must coordinate")
    impact_type: Literal["awareness", "action", "approval"] = Field(
        description="Type of coordination required for the team"
    )
    component: str = Field(description="Component that changed and drives the task")
    change_type: ChangeType = Field(description="Deterministic change type from ChangeRecord")
    priority: Literal["low", "medium", "high"] = Field(description="Priority of the coordination task")
    reason: str = Field(description="Why this team needs attention, grounded in impact report")
    evidence: list[Evidence] = Field(default_factory=list, description="Evidence for the task")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence in the task")
    channel: str = Field(default="", description="Preferred communication channel for the team")
    mode: Literal["group", "personal"] = Field(default="group", description="group or personal notification")
    action_required: bool = Field(description="Whether the team must take action")


class CoordinationPlan(BaseModel):
    project_id: str = Field(description="Project identifier")
    commit_sha: str = Field(description="Commit SHA being coordinated")
    summary: str = Field(default="", description="Overall coordination summary")
    tasks: list[CoordinationTask] = Field(default_factory=list, description="Per-team coordination tasks")
    requires_approval: bool = Field(default=False, description="Whether any task requires approval")


COORDINATION_INSTRUCTION = """
You are the Coordination Agent. You turn an ImpactReport and ProjectContext into a CoordinationPlan.

Workflow you MUST follow:
1. Call get_impact_report with project_id and commit_sha to fetch the ImpactReport.
2. Call get_project_context with project_id to fetch the ProjectContext.
3. Analyze both to build a CoordinationPlan:
   - summary: overall coordination summary grounded in ImpactReport.affected_components, downstream_workflows, risks and ProjectContext.
   - For each stakeholder in ImpactReport where impact_type != 'none', create a CoordinationTask:
     * team: the team identifier
     * impact_type: map stakeholder impact_type to task impact_type (awareness/action/approval). Skip 'none' teams entirely.
     * component: primary affected component for that team, derived from ImpactReport.affected_components and ProjectContext architecture/team_context
     * change_type: use the change_type from the underlying change (infer from ImpactReport evidence/related context if needed, default to 'unknown' if not determinable)
     * priority: high if team needs action/approval on a critical component or workflow, medium if awareness/action on secondary, low otherwise
     * reason: grounded reason why this team needs attention, combining stakeholder.reason and workflow risks
     * evidence: copy and ground evidence from ImpactReport.evidence relevant to that team
     * confidence: propagate or adjust from ImpactReport.confidence, grounded
     * channel: derive from ProjectContext team_context slack_channel_id or component name
     * mode: group if whole team needs same info, personal if individual action
     * action_required: true if impact_type is action or approval, false if awareness
   - requires_approval: true if any task has impact_type == 'approval', else false
4. After generating the CoordinationPlan, you MUST call save_coordination_plan with project_id and the complete plan, then return the same JSON.

Rules:
- Every task must answer for the user: what changed (component + change_type + summary) and why we need your attention (reason + evidence).
- Ground every task in ImpactReport fields and ProjectContext (team_context, architecture, stakeholders).
- Do not create tasks for teams with impact_type 'none'.
- Do not invent teams or components not present in the reports.
- Evidence must be specific and traceable to ImpactReport.evidence or ProjectContext.

Return ONLY the CoordinationPlan JSON matching the output_schema.
"""


class CoordinationAgent:
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
        self.app_name = "coordination_agent"
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

    def _build_agent(self) -> Agent:
        return Agent(
            name="coordination_agent",
            model=self.model_name,
            instruction=COORDINATION_INSTRUCTION,
            output_schema=CoordinationPlan,
            tools=[
                self._get_impact_report_tool,
                self._get_project_context_tool,
                self._save_coordination_plan_tool,
            ],
        )

    def _get_impact_report_tool(self, project_id: str, commit_sha: str) -> dict:
        """Fetch the ImpactReport from Firestore for project_id and commit_sha."""
        try:
            client = self.firestore_client
            doc = (
                client.collection("projects")
                .document(project_id)
                .collection("impact_reports")
                .document(commit_sha)
                .get()
            )
            if not doc.exists:
                logger.warning(
                    "ImpactReport not found for project_id=%s commit_sha=%s", project_id, commit_sha
                )
                return {
                    "status": "error",
                    "error": f"ImpactReport not found for project_id={project_id} commit_sha={commit_sha}",
                }
            data = doc.to_dict()
            validated = ImpactReport.model_validate(data)
            return {"status": "success", "impact_report": validated.model_dump(mode="json")}
        except Exception as exc:
            logger.exception("Failed to fetch ImpactReport for %s %s", project_id, commit_sha)
            return {"status": "error", "error": str(exc)}

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

    def analyze(
        self, project_id: str, commit_sha: str
    ) -> tuple[CoordinationPlan, str, bool]:
        if not project_id or not project_id.strip():
            raise ValueError("project_id cannot be empty")
        if not commit_sha or not commit_sha.strip():
            raise ValueError("commit_sha cannot be empty")

        self._current_project_id = project_id
        self._current_commit_sha = commit_sha
        self._saved_document_path: Optional[str] = None
        self._saved_by_tool: bool = False
        # keep legacy attributes for tool
        self.saved_document_path = None
        self.saved_by_tool = False

        payload = {"project_id": project_id, "commit_sha": commit_sha}
        events = asyncio.run(self._run_agent(payload))
        plan = self._extract_plan(events)
        plan.project_id = project_id
        plan.commit_sha = commit_sha

        if getattr(self, "_saved_document_path", None) is None and getattr(self, "saved_document_path", None) is None:
            logger.info("Agent did not call save_coordination_plan; using fallback save")
            path = self.persist_coordination(project_id, plan)
            self._saved_document_path = path
            self.saved_document_path = path

        doc_path = self._saved_document_path or self.saved_document_path or f"projects/{project_id}/coordination_plans/{commit_sha}"
        by_tool = bool(getattr(self, "_saved_by_tool", False) or getattr(self, "saved_by_tool", False))
        return plan, doc_path, by_tool

    def _save_coordination_plan_tool(self, project_id: str, coordination_plan: dict[str, Any]) -> dict:
        """Save the CoordinationPlan to Firestore under projects/{project_id}/coordination_plans/{commit_sha}."""
        try:
            plan = CoordinationPlan.model_validate(coordination_plan)
        except Exception as exc:
            logger.warning("Tool received invalid coordination_plan: %s", exc)
            return {"status": "error", "error": f"invalid coordination_plan: {exc}"}
        if plan.project_id != self._current_project_id or plan.commit_sha != self._current_commit_sha:
            plan.project_id = self._current_project_id
            plan.commit_sha = self._current_commit_sha
        try:
            path = self.persist_coordination(project_id, plan)
            self._saved_document_path = path
            self._saved_by_tool = True
            self.saved_document_path = path
            self.saved_by_tool = True
            return {"status": "success", "document_path": path}
        except Exception as exc:
            logger.exception("Failed to save coordination plan for %s", project_id)
            return {"status": "error", "error": str(exc)}

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

    def _extract_plan(self, events: list[Event]) -> CoordinationPlan:
        for event in reversed(events):
            if not event.is_final_response():
                continue
            text = self._event_text(event)
            if text:
                try:
                    return CoordinationPlan.model_validate(self._coerce(text))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        raise RuntimeError("Could not extract CoordinationPlan from agent response.")

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

    def persist_coordination(self, project_id: str, plan: CoordinationPlan) -> str:
        doc_ref = (
            self.firestore_client.collection("projects")
            .document(project_id)
            .collection("coordination_plans")
            .document(plan.commit_sha)
        )
        from datetime import datetime, timezone

        payload = plan.model_dump(mode="json")
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        doc_ref.set(payload)
        path = f"projects/{project_id}/coordination_plans/{plan.commit_sha}"
        logger.info("Saved coordination plan %s", path)
        return path

    @property
    def firestore_client(self) -> Any:
        if self._firestore is None:
            from google.cloud import firestore

            kwargs: dict = {"project": self.project}
            if self.database and self.database != "(default)":
                kwargs["database"] = self.database
            self._firestore = firestore.Client(**kwargs)
        return self._firestore
