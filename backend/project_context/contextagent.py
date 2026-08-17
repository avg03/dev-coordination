from __future__ import annotations
from dotenv import load_dotenv
import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from google.genai import types
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

load_dotenv()  # Load environment variables from .env file
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

DEFAULT_MODEL = os.getenv("CONTEXT_AGENT_MODEL", "gemini-3.6-flash")


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
            agent=self.agent,
            app_name=self.app_name,
            session_service=self.session_service,
            auto_create_session=True,
        )

    def _build_agent(self) -> Agent:
        return Agent(
            name="context_agent",
            model=Gemini(model=self.model),
            instruction=CONTEXT_AGENT_INSTRUCTION,
            output_schema=AgentAnalysisOutput,
        )

    def analyze(self, description: str, source_type: str = "text") -> ProjectContextOutput:
        if not description or not description.strip():
            raise ValueError("Project description cannot be empty.")

        session_id = uuid.uuid4().hex
        new_message = types.Content(role="user", parts=[types.Part(text=description)])
        events = list(
            self.runner.run(
            user_id="user",
            session_id=session_id,
            new_message=new_message,
        )
        )
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

    def _extract_analysis(self, events: list[object]) -> AgentAnalysisOutput:
        if not events:
            raise RuntimeError("Agent returned no events.")
        candidates: list[object] = []
        for event in reversed(events):
            if getattr(event, "author", "") == "user":
                continue
            output = getattr(event, "output", None)
            if output is not None:
                candidates.append(output)
            content = getattr(event, "content", None)
            parts = getattr(content, "parts", None) if content is not None else None
            if parts:
                text = "".join(part.text or "" for part in parts if getattr(part, "text", None))
                if text.strip():
                    candidates.append(text)

        for candidate in candidates:
            if isinstance(candidate, AgentAnalysisOutput):
                return candidate
            try:
                return AgentAnalysisOutput.model_validate(self._coerce(candidate))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue

        raise RuntimeError("Could not extract structured output from the agent response.")

    @staticmethod
    def _coerce(value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines)
            return json.loads(text)
        return value

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
