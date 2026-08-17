from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

try:
    from .models import (
        Architecture,
        ArchitectureComponent,
        Metadata,
        PlannedTask,
        Project,
        ProjectContextOutput,
        RawContext,
        SourceType,
        TaskPriority,
        TaskStatus,
        WorkScope,
    )
except ImportError:
    from models import (
        Architecture,
        ArchitectureComponent,
        Metadata,
        PlannedTask,
        Project,
        ProjectContextOutput,
        RawContext,
        SourceType,
        TaskPriority,
        TaskStatus,
        WorkScope,
    )

COMPONENT_RULES = [
    ("service", "auth-service", "service", ("auth", "oauth", "login", "session", "token")),
    ("api", "api-gateway", "api", ("api", "gateway", "endpoint", "graphql", "rest")),
    ("backend", "backend", "backend", ("backend", "server")),
    ("frontend", "mobile-client", "frontend", ("mobile", "frontend", "app", "client", "react", "ui")),
    ("database", "database", "database", ("database", "db", "storage", "postgres", "mysql")),
]


def _derive_name(description: str) -> str:
    line = description.strip().splitlines()[0] if description.strip() else "Untitled Project"
    line = re.sub(r"[^\w\s-]", "", line).strip(" -")
    return (line[:60] or "Untitled Project").title()


def _extract_components(description: str) -> list[ArchitectureComponent]:
    lowered = description.lower()
    components = []
    for index, (name, comp_id, comp_type, keywords) in enumerate(COMPONENT_RULES):
        if any(keyword in lowered for keyword in keywords):
            components.append(
                ArchitectureComponent(
                    id=comp_id,
                    name=name,
                    type=comp_type,
                    description=f"Detected from description.",
                )
            )
    return components


def _extract_tasks(description: str) -> list[PlannedTask]:
    tasks = []
    lines = description.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("-", "*", "+")) or re.match(r"^\d+[.)]", stripped):
            title = stripped.lstrip("-*+ ").strip()
            if title and not re.match(r"^\d+[.)]$", title):
                tasks.append(
                    PlannedTask(
                        id=f"TASK-{index + 1:03d}",
                        title=re.sub(r"^\d+[.)]\s*", "", title),
                        description="",
                        status=TaskStatus.PLANNED,
                        priority=TaskPriority.MEDIUM,
                    )
                )
    return tasks


def build_mock_output(description: str, source_type: str = "text") -> ProjectContextOutput:
    now = datetime.now(timezone.utc).isoformat()
    components = _extract_components(description)
    tasks = _extract_tasks(description)
    return ProjectContextOutput(
        project=Project(
            name=_derive_name(description),
            description=description.strip(),
            objective="",
            status="planning",
            domain="",
        ),
        work_scope=WorkScope(
            current_goal="",
            planned_tasks=tasks,
            constraints=[],
            important_decisions=[],
        ),
        architecture=Architecture(components=components, dependencies=[]),
        stakeholders=[],
        team_context=[],
        planned_dependencies=[],
        success_criteria=[],
        raw_context=RawContext(
            source_type=source_type,
            original_description=description,
        ),
        metadata=Metadata(created_at=now, updated_at=now, version=1),
    )


def run_real_agent(description: str, source_type: str, model: Optional[str]) -> ProjectContextOutput:
    try:
        from contextagent import ProjectContextAgent
    except ImportError as exc:
        print(f"Real mode needs google-adk installed: {exc}", file=sys.stderr)
        print("Install dependencies or use --mock to test offline.", file=sys.stderr)
        raise SystemExit(1) from exc
    return ProjectContextAgent(model=model).analyze(description, source_type=source_type)


def _print_output(output: ProjectContextOutput) -> None:
    print(json.dumps(output.model_dump(mode="json"), indent=2, ensure_ascii=False))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Test the Context Agent: input a project description as text, get structured output on the terminal."
    )
    parser.add_argument("description", nargs="?", help="Inline project description text.")
    parser.add_argument("-f", "--file", help="Read the project description from a text file.")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use the deterministic offline analyzer (no Gemini/API key required).",
    )
    parser.add_argument(
        "--source-type",
        choices=["text", "pdf", "image", "mixed"],
        default="text",
    )
    parser.add_argument("--model", help="Gemini model name to use (real mode only).")
    parser.add_argument("-o", "--output", help="Also write the output to a JSON file.")
    args = parser.parse_args(argv)

    description = args.description
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            description = handle.read()
    if not description or not description.strip():
        try:
            description = input("Paste the project description and press Enter (Ctrl+D to finish):\n")
        except EOFError:
            parser.error("No description provided.")
            return 2
    if not description or not description.strip():
        parser.error("Description cannot be empty.")
        return 2

    if args.mock:
        output = build_mock_output(description, source_type=args.source_type)
    else:
        output = run_real_agent(description, args.source_type, args.model)

    _print_output(output)

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output.model_dump_json(indent=2), encoding="utf-8")
        print(f"\nSaved to: {path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
