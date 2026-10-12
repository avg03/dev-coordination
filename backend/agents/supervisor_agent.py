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

from autonomy.models import MessageAutonomy
from github_infrastructure.config import settings
from llm import resolve_model
from security.guard import make_before_tool_guard
from security.identities import AgentIdentity

try:
    from agents.communication_agent import SlackMessage
except ImportError:
    from agents.communication_agent import SlackMessage

try:
    from project_context.models import ProjectContextOutput
except ImportError:
    from project_context.models import ProjectContextOutput

logger = logging.getLogger("agents.supervisor")

DEFAULT_MODEL = os.getenv(
    "SUPERVISOR_MODEL", os.getenv("CONTEXT_AGENT_MODEL", "gemini-3.5-flash")
)
PROJECT_CONTEXTS_COLLECTION = "project_contexts"


class MessageVerdict(BaseModel):
    message_id: str = Field(description="Id of the reviewed draft message")
    verdict: Literal["keep", "revise", "reject"] = Field(
        description="keep = send as-is, revise = use final_messages version, reject = do not send"
    )
    reason: str = Field(description="Grounded reason for the verdict")


class SupervisionResult(BaseModel):
    project_id: str = Field(description="Project identifier")
    pr_number: int = Field(description="Pull request number")
    summary: str = Field(default="", description="Overall supervision summary")
    verdicts: list[MessageVerdict] = Field(default_factory=list)
    final_messages: list[SlackMessage] = Field(
        default_factory=list,
        description="Messages to deliver: kept drafts unchanged, revised drafts, no rejected messages",
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    autonomy: list[MessageAutonomy] = Field(
        default_factory=list,
        description="Advisory per-message autonomy recommendations "
        "({message_id, recommendation, reasons, risk_level, confidence}); "
        "validated downgrade-only by the deterministic autonomy policy.",
    )


SUPERVISION_INSTRUCTION = """
You are the Supervisor Agent. You critique and finalize drafted Slack messages
produced by the Communication Agent before any human approval. You NEVER send
messages — you only evaluate and revise drafts.

Workflow you MUST follow:
1. Call get_project_context with project_id to fetch the ProjectContext.
2. Call get_coordination_plan with project_id and pr_number to fetch the CoordinationPlan.
3. Review each draft message (provided in the input payload as draft_messages) against:
   - Grounding: every claim must be traceable to the CoordinationPlan tasks
     (component, change_type, reason, evidence) and the ProjectContext. Reject
     invented facts, invented recipients, or invented evidence.
    - Recipients: the recipient NAME + TEAM must match a stakeholder in the
      ProjectContext. One message per affected person — reject duplicates of
      the same person. Slack IDs, channels and <@...> mentions are resolved
      by deterministic backend logic after you finish: never invent them and
      never add them yourself.
    - Completeness: a message must state what changed (component + change_type),
      why this person is contacted (reason + impact_type + priority), the
      required action (or explicit none), and cite evidence. Revise when missing.
   - Tone: concise and actionable.
4. Produce a SupervisionResult:
   - verdicts: one per draft message (keep/revise/reject) with a grounded reason.
    - final_messages: the messages to deliver — kept drafts unchanged, revised
      drafts in revised form, and NO rejected messages. Keep ids stable
      (msg_1, msg_2, ...). Every final message keeps recipient and team;
      leave channel/slack_user_id empty and write no mentions.
    - summary + confidence grounded in your review.
4b. For EACH final message, add an autonomy advisory entry
    {message_id, recommendation, reasons, risk_level, confidence} where
    recommendation is one of AUTO_SEND (only for routine, low-risk,
    evidence-backed FYI with a valid mapped recipient), HUMAN_REVIEW
    (default: anything needing judgment, action, approval, or with
    doubt), BLOCK (invalid data, unmapped recipient, prohibited target),
    SKIP (no notification value). Evaluate evidence quality, impact
    severity, risk, confidence, recipient mapping, and message
    correctness. Your recommendation is advisory: deterministic policy
    validates it downgrade-only and humans stay in the loop.
5. You MUST call save_supervised_communication with project_id, pr_number and
    the complete supervision result, then return the same JSON as final output.

Rules:
- Never invent new recipients, channels or evidence.
- Do not remove a message unless it fails grounding or recipient validation.
- Return ONLY the SupervisionResult JSON matching the output_schema.
"""


class SupervisorAgent:
    def __init__(
        self,
        model: Optional[str] = None,
        project: Optional[str] = None,
        database: Optional[str] = None,
    ) -> None:
        self.model_name = model or resolve_model(
            "SUPERVISOR_MODEL", "CONTEXT_AGENT_MODEL", default="gemini-3.5-flash"
        )
        self.project = project or settings.gcp_project
        self.database = database or os.getenv("FIRESTORE_DATABASE", "(default)")
        if not self.project:
            raise ValueError("GCP project is not configured")
        self.app_name = "supervisor_agent"
        self.session_service = InMemorySessionService()
        self.agent = self._build_agent()
        self.runner = Runner(
            app_name=self.app_name,
            agent=self.agent,
            session_service=self.session_service,
        )
        self._firestore: Any = None
        self._current_project_id: str = ""
        self._current_pr_number: int = 0
        self.saved_document_path: Optional[str] = None
        self.saved_by_tool: bool = False

    def _build_agent(self) -> Agent:
        return Agent(
            name="supervisor_agent",
            model=self.model_name,
            instruction=SUPERVISION_INSTRUCTION,
            output_schema=SupervisionResult,
            tools=[
                self._get_project_context_tool,
                self._get_coordination_plan_tool,
                self._save_supervised_communication_tool,
            ],
            before_tool_callback=make_before_tool_guard(AgentIdentity.SUPERVISOR_AGENT),
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

    def _get_coordination_plan_tool(self, project_id: str, pr_number: int) -> dict:
        """Fetch the PR-level CoordinationPlan from Firestore."""
        try:
            client = self.firestore_client
            doc = (
                client.collection("projects")
                .document(project_id)
                .collection("coordination_plans")
                .document(str(pr_number))
                .get()
            )
            if not doc.exists:
                logger.warning(
                    "CoordinationPlan not found for project_id=%s pr_number=%s", project_id, pr_number
                )
                return {
                    "status": "error",
                    "error": f"CoordinationPlan not found for project_id={project_id} pr_number={pr_number}",
                }
            data = doc.to_dict()
            return {"status": "success", "coordination_plan": data}
        except Exception as exc:
            logger.exception("Failed to fetch CoordinationPlan for %s/%s", project_id, pr_number)
            return {"status": "error", "error": str(exc)}

    def _save_supervised_communication_tool(
        self, project_id: str, pr_number: int, supervision: dict[str, Any]
    ) -> dict:
        """Persist the supervision verdict onto projects/{id}/communications/{pr_number}."""
        try:
            result = SupervisionResult.model_validate(supervision)
        except Exception as exc:
            logger.warning("Tool received invalid supervision: %s", exc)
            return {"status": "error", "error": f"invalid supervision: {exc}"}

        if result.project_id != self._current_project_id or result.pr_number != self._current_pr_number:
            result.project_id = self._current_project_id
            result.pr_number = self._current_pr_number

        try:
            path = self.persist_supervision(project_id, int(pr_number), result)
            self.saved_document_path = path
            self.saved_by_tool = True
            return {"status": "success", "document_path": path}
        except Exception as exc:
            logger.exception("Failed to save supervision for %s/%s", project_id, pr_number)
            return {"status": "error", "error": str(exc)}

    def analyze(
        self,
        project_id: str,
        pr_number: int,
        coordination_summary: str = "",
        draft_messages: Optional[list[dict[str, Any]]] = None,
        autonomy_context: Optional[dict[str, Any]] = None,
    ) -> SupervisionResult:
        """Autonomy context (optional, deterministic): impact confidence,
        risks, evidence counts used for the advisory autonomy evaluation."""
        if not project_id or not project_id.strip():
            raise ValueError("project_id cannot be empty")
        if pr_number <= 0:
            raise ValueError("pr_number must be positive")

        self._current_project_id = project_id
        self._current_pr_number = pr_number
        self.saved_document_path = None
        self.saved_by_tool = False

        payload = {
            "project_id": project_id,
            "pr_number": pr_number,
            "coordination_summary": coordination_summary,
            "draft_messages": draft_messages or [],
            "autonomy_context": autonomy_context or {},
        }
        from telemetry.spans import track_agent_run

        with track_agent_run(
            "supervisor_agent", {"project_id": project_id, "pr_number": pr_number}
        ) as t:
            events = asyncio.run(self._run_agent(payload))
            t.set_usage_from_events(events)
            result = self._extract_result(events)
            t.set_outputs(
                {
                    "output.final_messages": len(getattr(result, "final_messages", None) or []),
                    "output.summary": str(getattr(result, "summary", "") or ""),
                    "output.autonomy_advisories": len(getattr(result, "autonomy", None) or []),
                }
            )
        result.project_id = project_id
        result.pr_number = pr_number

        if self.saved_document_path is None:
            logger.info("Agent did not call save_supervised_communication; using fallback save")
            path = self.persist_supervision(project_id, pr_number, result)
            self.saved_document_path = path

        return result

    async def _run_agent(self, payload: dict[str, Any]) -> list[Event]:
        user_id = "user"
        session = await self.session_service.create_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=uuid.uuid4().hex,
        )
        user_message = types.UserContent(parts=[types.Part(text=json.dumps(payload, default=str))])
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

    def _extract_result(self, events: list[Event]) -> SupervisionResult:
        for event in reversed(events):
            if not event.is_final_response():
                continue
            text = self._event_text(event)
            if text:
                try:
                    return SupervisionResult.model_validate(self._coerce(text))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        raise RuntimeError("Could not extract SupervisionResult from agent response.")

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

    def persist_supervision(self, project_id: str, pr_number: int, result: SupervisionResult) -> str:
        doc_ref = (
            self.firestore_client.collection("projects")
            .document(project_id)
            .collection("communications")
            .document(str(pr_number))
        )
        payload = {
            "supervision": result.model_dump(mode="json"),
            "supervised_at": datetime.now(timezone.utc).isoformat(),
        }
        doc_ref.set(payload, merge=True)
        path = f"projects/{project_id}/communications/{pr_number}"
        logger.info("Saved supervision %s", path)
        return path
