from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import aclosing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from google.adk.agents import Agent
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from github_infrastructure.config import settings
from security.guard import make_before_tool_guard
from security.identities import AgentIdentity

try:
    from project_context.models import (
        CONTEXT_AGENT_INSTRUCTION,
        AgentAnalysisOutput,
        Metadata,
        ProjectContextOutput,
        RawContext,
    )
except ImportError:
    from project_context.models import (
        CONTEXT_AGENT_INSTRUCTION,
        AgentAnalysisOutput,
        Metadata,
        ProjectContextOutput,
        RawContext,
    )

logger = logging.getLogger("agents.context")

DEFAULT_MODEL = os.getenv("CONTEXT_AGENT_MODEL", "gemini-3.5-flash")
PROJECT_CONTEXTS_COLLECTION = "project_contexts"

SAVE_TOOL_INSTRUCTION = """

After you have produced the structured project context, you MUST call the
`save_firestore` tool exactly once, passing the complete structured context
as the `project_context` argument. Then finish with the same JSON as your
final answer.
"""


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "untitled-project"


class ContextAgent:
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
        self.app_name = "context_agent"
        self.session_service = InMemorySessionService()
        self.agent = self._build_agent()
        self.runner = Runner(
            app_name=self.app_name,
            agent=self.agent,
            session_service=self.session_service,
        )
        self._firestore: Any = None
        self._current_description: str = ""
        self.saved_document_id: Optional[str] = None
        self.saved_by_tool: bool = False

    def _build_agent(self) -> Agent:
        return Agent(
            name="context_agent",
            model=self.model_name,
            instruction=CONTEXT_AGENT_INSTRUCTION + SAVE_TOOL_INSTRUCTION,
            output_schema=AgentAnalysisOutput,
            tools=[self._save_firestore_tool],
            before_tool_callback=make_before_tool_guard(AgentIdentity.CONTEXT_AGENT),
        )

    def _save_firestore_tool(self, project_context: dict[str, Any]) -> dict:
        """Save the analyzed project context dictionary to Firestore."""
        return self._handle_save(project_context)

    def _handle_save(self, analysis_dict: dict[str, Any]) -> dict:
        try:
            analysis = AgentAnalysisOutput.model_validate(analysis_dict)
        except Exception as exc:
            logger.warning("Tool received invalid context: %s", exc)
            return {"status": "error", "error": f"invalid project_context: {exc}"}

        output = self._compose_output(analysis)
        document_id = self.persist(output, document_id=getattr(self, "_pending_document_id", None))
        self.saved_document_id = document_id
        self.saved_by_tool = True
        return {"status": "success", "document_id": document_id}

    def analyze_and_save(
        self, description: str, document_id: str | None = None
    ) -> tuple[ProjectContextOutput, str, bool]:
        if not description or not description.strip():
            raise ValueError("Project description cannot be empty.")

        self.saved_document_id = None
        self.saved_by_tool = False
        self._current_description = description
        self._pending_document_id = document_id

        events = asyncio.run(self._run_agent(description))
        analysis = self._extract_analysis(events)
        output = self._compose_output(analysis)

        resolved_id = self.saved_document_id
        if resolved_id is None:
            logger.info("Agent did not call save_firestore; using fallback save")
            resolved_id = self.persist(output, document_id=document_id)

        return output, resolved_id, self.saved_by_tool

    async def _run_agent(self, description: str) -> list[Event]:
        user_id = "user"
        session = await self.session_service.create_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=uuid.uuid4().hex,
        )
        user_message = types.UserContent(parts=[types.Part(text=description)])
        events = []
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

    def _extract_analysis(self, events: list[Event]) -> AgentAnalysisOutput:
        for event in reversed(events):
            if not event.is_final_response():
                continue
            text = self._event_text(event)
            if text:
                try:
                    return AgentAnalysisOutput.model_validate(self._coerce(text))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        raise RuntimeError("Could not extract structured output from the agent response.")

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

    def _compose_output(self, analysis: AgentAnalysisOutput) -> ProjectContextOutput:
        now = datetime.now(timezone.utc).isoformat()
        return ProjectContextOutput(
            **analysis.model_dump(),
            raw_context=RawContext(
                source_type="text",
                original_description=self._current_description,
            ),
            metadata=Metadata(created_at=now, updated_at=now, version=1),
        )

    @property
    def firestore_client(self) -> Any:
        if self._firestore is None:
            from google.cloud import firestore

            kwargs: dict = {"project": self.project}
            if self.database and self.database != "(default)":
                kwargs["database"] = self.database
            self._firestore = firestore.Client(**kwargs)
        return self._firestore

    def persist(
        self, output: ProjectContextOutput, document_id: str | None = None
    ) -> str:
        resolved_id = document_id if document_id else slugify(output.project.name)
        payload = output.model_dump(mode="json")
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        self.firestore_client.collection(PROJECT_CONTEXTS_COLLECTION).document(resolved_id).set(payload)
        logger.info("Saved project context %s", resolved_id)
        return resolved_id


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the ADK Context Agent on a project description.")
    parser.add_argument("description", nargs="?")
    parser.add_argument("-f", "--file")
    parser.add_argument("--model")
    args = parser.parse_args(argv)

    description = args.description
    if args.file:
        description = Path(args.file).read_text(encoding="utf-8")
    if not description or not description.strip():
        parser.error("Provide a description argument or --file.")
        return 2

    agent = ContextAgent(model=args.model)
    output, document_id, by_tool = agent.analyze_and_save(description)
    print(f"saved={by_tool} document={document_id}")
    print(output.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


