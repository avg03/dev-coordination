from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import aclosing
from datetime import datetime, timezone
from typing import Any, Optional

from google.adk.agents import Agent
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

from github_infrastructure.change_records import ChangeRecord
from github_infrastructure.config import settings
from security.guard import make_before_tool_guard
from security.identities import AgentIdentity

# Fix ADK output serialization: ChangeRecord/EnrichedChangeRecord contain datetime `timestamp`
# which ADK's _output_schema_processor does json.dumps(response) without handling datetime.
# Patch json.dumps globally to handle datetime via str() so both input and output are JSON-safe
# without modifying ChangeRecord schema or manual timestamp conversion.
import json as _json_module

if not getattr(_json_module, "_adk_datetime_patched", False):
    _orig_dumps = _json_module.dumps

    def _adk_patched_dumps(obj, *args, **kwargs):
        if "default" not in kwargs:
            kwargs["default"] = str
        return _orig_dumps(obj, *args, **kwargs)

    _json_module.dumps = _adk_patched_dumps  # type: ignore
    _json_module._adk_datetime_patched = True  # type: ignore

try:
    from project_context.models import ProjectContextOutput
except ImportError:
    from project_context.models import ProjectContextOutput

logger = logging.getLogger("agents.change_analysis")

DEFAULT_MODEL = os.getenv("CHANGE_ANALYSIS_MODEL", os.getenv("CONTEXT_AGENT_MODEL", "gemini-3.5-flash"))
PROJECT_CONTEXTS_COLLECTION = "project_contexts"


class EnrichedChangeRecord(ChangeRecord):
    summary: str = Field(default="", description="Concise summary of the change")
    behavior_before: str = Field(default="", description="Behavior before the change")
    behavior_after: str = Field(default="", description="Behavior after the change")
    impact_scope: list[str] = Field(default_factory=list, description="Components or areas impacted")
    related_tasks: list[str] = Field(default_factory=list, description="Related task IDs from project context")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence in the analysis")


CHANGE_ANALYSIS_INSTRUCTION = """
You are the Change Analysis Agent. You convert a deterministic ChangeRecord into an EnrichedChangeRecord using the stored ProjectContext.

Workflow you MUST follow:
1. Call get_project_context with the provided project_id to fetch the ProjectContext. Do not proceed without it.
2. Analyze the ChangeRecord using the ProjectContext and the changed_files (patches, functions, components).
3. Generate an EnrichedChangeRecord that inherits all fields from ChangeRecord and adds:
   - summary: concise summary grounded in changed files and project context
   - behavior_before: what the code did before, grounded in patches/context
   - behavior_after: what the code does after, grounded in patches/context
   - impact_scope: list of components or areas impacted, derived from architecture/components in context and changed files
   - related_tasks: list of task IDs from work_scope.planned_tasks that are related to this change
   - confidence: float 0-1 reflecting grounding strength

Rules:
- If ChangeRecord.change_type != 'unknown', you MUST preserve it exactly. Do not reclassify.
- Only classify change_type when its value is 'unknown'. When classifying, choose from: feature, refactor, fix, bug, config, docs, test.
- Every inferred field must be grounded in the ProjectContext and changed files. Do not invent.
- Use evidence from patches, file paths, and project context architecture/teams.
- After generating the EnrichedChangeRecord, you MUST call save_enriched_change with project_id and the complete enriched record, then return the same JSON as final output.

Return ONLY the EnrichedChangeRecord JSON matching the output_schema.
"""

SAVE_TOOL_INSTRUCTION = """
After you have produced the EnrichedChangeRecord, you MUST call save_enriched_change exactly once with project_id and the complete enriched_change, then finish with the same JSON.
"""


class ChangeAnalysisAgent:
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
        self.app_name = "change_analysis_agent"
        self.session_service = InMemorySessionService()
        self.agent = self._build_agent()
        self.runner = Runner(
            app_name=self.app_name,
            agent=self.agent,
            session_service=self.session_service,
        )
        self._firestore: Any = None
        self._current_project_id: str = ""
        self._current_change_record: Optional[ChangeRecord] = None
        self.saved_document_path: Optional[str] = None
        self.saved_by_tool: bool = False

    def _build_agent(self) -> Agent:
        return Agent(
            name="change_analysis_agent",
            model=self.model_name,
            instruction=CHANGE_ANALYSIS_INSTRUCTION + SAVE_TOOL_INSTRUCTION,
            output_schema=EnrichedChangeRecord,
            tools=[self._get_project_context_tool, self._save_enriched_change_tool],
            before_tool_callback=make_before_tool_guard(AgentIdentity.CHANGE_ANALYSIS_AGENT),
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

    def _save_enriched_change_tool(self, project_id: str, enriched_change: dict[str, Any]) -> dict:
        """Save the EnrichedChangeRecord to Firestore under projects/{project_id}/enriched_changes/{commit_sha}."""
        try:
            enriched = EnrichedChangeRecord.model_validate(enriched_change)
        except Exception as exc:
            logger.warning("Tool received invalid enriched_change: %s", exc)
            return {"status": "error", "error": f"invalid enriched_change: {exc}"}

        if self._current_change_record and self._current_change_record.change_type != "unknown":
            if enriched.change_type != self._current_change_record.change_type:
                logger.info(
                    "Preserving original change_type %s over agent value %s",
                    self._current_change_record.change_type,
                    enriched.change_type,
                )
                enriched.change_type = self._current_change_record.change_type

        try:
            path = self.persist_enriched(project_id, enriched)
            self.saved_document_path = path
            self.saved_by_tool = True
            return {"status": "success", "document_path": path}
        except Exception as exc:
            logger.exception("Failed to save enriched change for %s", project_id)
            return {"status": "error", "error": str(exc)}

    def analyze(
        self, project_id: str, change_record: ChangeRecord | dict[str, Any]
    ) -> tuple[EnrichedChangeRecord, str, bool]:
        if not project_id or not project_id.strip():
            raise ValueError("project_id cannot be empty")
        if isinstance(change_record, dict):
            change_record = ChangeRecord.model_validate(change_record)
        if not change_record.commit_sha:
            raise ValueError("ChangeRecord.commit_sha cannot be empty")

        self._current_project_id = project_id
        self._current_change_record = change_record
        self.saved_document_path = None
        self.saved_by_tool = False

        payload = {
            "project_id": project_id,
            "change_record": change_record.model_dump(mode="json"),
        }
        events = asyncio.run(self._run_agent(payload))
        enriched = self._extract_enriched(events)

        if change_record.change_type != "unknown" and enriched.change_type != change_record.change_type:
            enriched.change_type = change_record.change_type

        if self.saved_document_path is None:
            logger.info("Agent did not call save_enriched_change; using fallback save")
            path = self.persist_enriched(project_id, enriched)
            self.saved_document_path = path

        return enriched, self.saved_document_path, self.saved_by_tool

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

    def _extract_enriched(self, events: list[Event]) -> EnrichedChangeRecord:
        for event in reversed(events):
            if not event.is_final_response():
                continue
            text = self._event_text(event)
            if text:
                try:
                    return EnrichedChangeRecord.model_validate(self._coerce(text))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        raise RuntimeError("Could not extract EnrichedChangeRecord from agent response.")

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

    def persist_enriched(self, project_id: str, enriched: EnrichedChangeRecord) -> str:
        doc_ref = (
            self.firestore_client.collection("projects")
            .document(project_id)
            .collection("enriched_changes")
            .document(enriched.commit_sha)
        )
        payload = enriched.model_dump(mode="json")
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        doc_ref.set(payload)
        path = f"projects/{project_id}/enriched_changes/{enriched.commit_sha}"
        logger.info("Saved enriched change %s", path)
        return path

