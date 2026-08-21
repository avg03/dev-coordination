from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from contextlib import aclosing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from google.adk.agents import Agent
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

try:
    from .models import (
        AgentAnalysisOutput,
        CONTEXT_AGENT_INSTRUCTION,
        Metadata,
        ProjectContextOutput,
        RawContext,
    )
except ImportError:
    from models import (
        AgentAnalysisOutput,
        CONTEXT_AGENT_INSTRUCTION,
        Metadata,
        ProjectContextOutput,
        RawContext,
    )

DEFAULT_MODEL = os.getenv("CONTEXT_AGENT_MODEL", "gemini-3.5-flash")


class ProjectContextAgent:
    def __init__(
        self,
        model: Optional[str] = None,
        output_dir: Union[str, Path, None] = None,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.output_dir = Path(output_dir) if output_dir else Path(__file__).resolve().parent
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.app_name = "project_context_agent"
        self.session_service = InMemorySessionService()
        self.agent = self._build_agent()
        self.runner = Runner(
            app_name=self.app_name,
            agent=self.agent,
            session_service=self.session_service,
        )

    def _build_agent(self) -> Agent:
        return Agent(
            name="context_agent",
            model=self.model,
            instruction=CONTEXT_AGENT_INSTRUCTION,
            output_schema=AgentAnalysisOutput,
        )

    def analyze(self, description: str, source_type: str = "text") -> ProjectContextOutput:
        if not description or not description.strip():
            raise ValueError("Project description cannot be empty.")

        events = asyncio.run(self._run_agent(description))
        analysis = self._extract_analysis(events)
        now = datetime.now(timezone.utc).isoformat()
        return ProjectContextOutput(
            **analysis.model_dump(),
            raw_context=RawContext(
                source_type=source_type,
                original_description=description,
            ),
            metadata=Metadata(
                created_at=now,
                updated_at=now,
                version=1,
            ),
        )

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

    def save(self, output: ProjectContextOutput, name: Optional[str] = None) -> Path:
        filename = name or f"project_context_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path = self.output_dir / filename
        path.write_text(output.model_dump_json(indent=2), encoding="utf-8")
        return path


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze a project description with the Context Agent.")
    parser.add_argument("description", nargs="?", help="Inline project description text.")
    parser.add_argument("-f", "--file", help="Read the project description from a text file.")
    parser.add_argument(
        "--source-type",
        choices=["text", "pdf", "image", "mixed"],
        default="text",
        help="Format of the original input (default: text).",
    )
    parser.add_argument("-o", "--output", help="Explicit output JSON file path.")
    parser.add_argument("--output-dir", help="Directory to save the JSON output (default: this folder).")
    parser.add_argument("--model", help="Gemini model name to use.")
    args = parser.parse_args(argv)

    description = args.description
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            description = handle.read()
    if not description or not description.strip():
        parser.error("Provide the project description as an argument or via --file.")
        return 2

    agent = ProjectContextAgent(model=args.model, output_dir=args.output_dir)
    output = agent.analyze(description, source_type=args.source_type)

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output.model_dump_json(indent=2), encoding="utf-8")
    else:
        path = agent.save(output)

    print(f"Saved project context to: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
