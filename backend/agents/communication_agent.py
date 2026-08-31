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

from github_infrastructure.config import settings

try:
    from agents.coordination_agent import CoordinationPlan
except ImportError:
    from agents.coordination_agent import CoordinationPlan

try:
    from project_context.models import ProjectContextOutput
except ImportError:
    from project_context.models import ProjectContextOutput

logger = logging.getLogger("agents.communication")

DEFAULT_MODEL = os.getenv(
    "COMMUNICATION_MODEL", os.getenv("CONTEXT_AGENT_MODEL", "gemini-3.5-flash")
)
PROJECT_CONTEXTS_COLLECTION = "project_contexts"


class SlackMessage(BaseModel):
    id: str = Field(default="", description="Stable message identifier, e.g., msg_1")
    recipient: str = Field(description="Stakeholder name or team name")
    team: str = Field(description="Team of the recipient")
    channel: str = Field(description="Slack channel ID or user ID the message was sent to")
    mode: Literal["group", "personal"] = Field(description="Whether sent to group channel or personal DM")
    text: str = Field(description="Formatted Slack message body")
    evidence: list[str] = Field(default_factory=list, description="Evidence snippets included")


class CommunicationResult(BaseModel):
    project_id: str = Field(description="Project identifier")
    commit_sha: str = Field(description="Commit SHA")
    summary: str = Field(default="", description="Overall communication summary")
    messages: list[SlackMessage] = Field(default_factory=list, description="Messages framed per member")
    total_sent: int = Field(default=0, description="Number of mock sends performed")


COMMUNICATION_INSTRUCTION = """
You are the Communication Agent. You turn a CoordinationPlan and ProjectContext into personalized Slack messages.

Workflow you MUST follow:
1. Call get_project_context with project_id to fetch the ProjectContext (contains stakeholders with slack_user_id/slack_channel_id and team_context with slack_channel_id).
2. Call get_coordination_plan with project_id and commit_sha to fetch the CoordinationPlan.
3. For each member linked to the change:
   - Link each CoordinationTask to its team members via ProjectContext.stakeholders (where stakeholder.team == task.team) and team_context (where team_context.name == task.team).
   - Members with no slack_user_id are still notified via their team's slack_channel_id if mode is group/both.
   - Frame a message that MUST include:
     * what changed: task.component + task.change_type + plan.summary
     * the changed component: task.component
     * the evidence: task.evidence (source + detail) grounded from coordination plan
     * the reason why this person is contacted: task.reason + task.impact_type + priority
   - Personalize using stakeholder name, role, and team responsibilities.
   - Choose channel: use stakeholder.slack_channel_id if present, else stakeholder.slack_user_id for personal mode, else team_context.slack_channel_id for group mode.
   - Keep the tone concise and actionable. State whether action is required (task.action_required) and the confidence.

Rules:
- Ground every message in ProjectContext and CoordinationPlan data. Do not invent.
- Every message must cite evidence from the task.
- Respect communication_preference: if stakeholder prefers group, use team channel; if personal, use DM; if both, prefer the task's mode.
- Do not skip members that have a slack identifier and are linked to a task.
- Assign each SlackMessage a stable id like msg_1, msg_2, etc., in order.

Return ONLY the CommunicationResult JSON matching the output_schema, containing one SlackMessage per member linked to the change.
"""


class CommunicationAgent:
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
        self.app_name = "communication_agent"
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
        self._sent_messages: list[dict[str, Any]] = []

    def _build_agent(self) -> Agent:
        return Agent(
            name="communication_agent",
            model=self.model_name,
            instruction=COMMUNICATION_INSTRUCTION,
            output_schema=CommunicationResult,
            tools=[self._get_project_context_tool, self._get_coordination_plan_tool],
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

    def _get_coordination_plan_tool(self, project_id: str, commit_sha: str) -> dict:
        """Fetch the CoordinationPlan from Firestore for project_id and commit_sha."""
        try:
            client = self.firestore_client
            # Try new collection coordination_plans, fallback to coordination_plans under projects
            doc = (
                client.collection("projects")
                .document(project_id)
                .collection("coordination_plans")
                .document(commit_sha)
                .get()
            )
            if not doc.exists:
                # Fallback: try flat collection
                doc = client.collection("coordination_plans").document(f"{project_id}__{commit_sha}").get()
            if not doc.exists:
                logger.warning(
                    "CoordinationPlan not found for project_id=%s commit_sha=%s", project_id, commit_sha
                )
                return {
                    "status": "error",
                    "error": f"CoordinationPlan not found for project_id={project_id} commit_sha={commit_sha}",
                }
            data = doc.to_dict()
            validated = CoordinationPlan.model_validate(data)
            return {"status": "success", "coordination_plan": validated.model_dump(mode="json")}
        except Exception as exc:
            logger.exception("Failed to fetch CoordinationPlan for %s %s", project_id, commit_sha)
            return {"status": "error", "error": str(exc)}

    def analyze(
        self, project_id: str, commit_sha: str, dry_run: bool = True
    ) -> CommunicationResult:
        if not project_id or not project_id.strip():
            raise ValueError("project_id cannot be empty")
        if not commit_sha or not commit_sha.strip():
            raise ValueError("commit_sha cannot be empty")

        self._current_project_id = project_id
        self._current_commit_sha = commit_sha
        self._sent_messages = []

        payload = {"project_id": project_id, "commit_sha": commit_sha}
        events = asyncio.run(self._run_agent(payload))
        result = self._extract_result(events)
        result.project_id = project_id
        result.commit_sha = commit_sha

        sent = self._mock_send_all(result.messages)
        result.total_sent = sent
        return result

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

    def _extract_result(self, events: list[Event]) -> CommunicationResult:
        for event in reversed(events):
            if not event.is_final_response():
                continue
            text = self._event_text(event)
            if text:
                try:
                    result = CommunicationResult.model_validate(self._coerce(text))
                    # Ensure stable ids like msg_1, msg_2 for selection
                    for idx, msg in enumerate(result.messages, start=1):
                        if not msg.id:
                            msg.id = f"msg_{idx}"
                    return result
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        raise RuntimeError("Could not extract CommunicationResult from agent response.")

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

    def _mock_send_all(self, messages: list[SlackMessage]) -> int:
        count = 0
        for msg in messages:
            self._send_slack(msg.channel, msg.text, msg.mode, msg.recipient, msg.team)
            count += 1
        return count

    def _send_slack(
        self, channel: str, text: str, mode: str, recipient: str, team: str
    ) -> dict:
        """Send via Slack API with mock fallback. Uses services.slack_service if token present."""
        try:
            from services.slack_service import send_slack_message
        except ImportError:
            try:
                from backend.services.slack_service import send_slack_message
            except ImportError:
                send_slack_message = None  # type: ignore

        if send_slack_message:
            result = send_slack_message(channel=channel, text=text)
            entry = {
                "channel": channel or "(no-channel)",
                "recipient": recipient,
                "team": team,
                "mode": mode,
                "text_preview": text[:120].replace("\n", " ") + ("..." if len(text) > 120 else ""),
                "status": result.get("status", "mock_sent"),
                "slack_ts": result.get("ts"),
            }
            self._sent_messages.append(entry)
            if result.get("status") == "sent":
                logger.info("[SLACK] %s -> %s (%s): %s", team, channel, mode, entry["text_preview"])
            else:
                logger.info("[MOCK SLACK] %s -> %s (%s): %s", team, channel or recipient, mode, entry["text_preview"])
            return result

        # Fallback mock if service not importable
        entry = {
            "channel": channel or "(no-channel)",
            "recipient": recipient,
            "team": team,
            "mode": mode,
            "text_preview": text[:120].replace("\n", " ") + ("..." if len(text) > 120 else ""),
            "status": "mock_sent",
        }
        self._sent_messages.append(entry)
        logger.info(
            "[MOCK SLACK] %s -> %s (%s): %s",
            team,
            channel or recipient,
            mode,
            entry["text_preview"],
        )
        return {"status": "mock_sent", "channel": channel, "ok": True}

    # Kept for backward compat
    def _mock_send_slack(self, channel: str, text: str, mode: str, recipient: str, team: str) -> dict:
        return self._send_slack(channel, text, mode, recipient, team)

    def persist_communication(
        self, project_id: str, commit_sha: str, result: CommunicationResult
    ) -> str:
        doc_ref = (
            self.firestore_client.collection("projects")
            .document(project_id)
            .collection("communications")
            .document(commit_sha)
        )
        from datetime import datetime, timezone

        payload = result.model_dump(mode="json")
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        doc_ref.set(payload)
        path = f"projects/{project_id}/communications/{commit_sha}"
        logger.info("Saved communication result %s", path)
        return path
