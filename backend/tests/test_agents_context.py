from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import load_env

PROJECT = load_env()
if not PROJECT:
    raise SystemExit("GOOGLE_CLOUD_PROJECT not set in backend/.env")

from agents.context_agent import (
    PROJECT_CONTEXTS_COLLECTION,
    ContextAgent,
    slugify,
)

SAMPLE_DESCRIPTION = """
Authentication Redesign. We are migrating the legacy session-based login
to OAuth 2.0 with refresh tokens. Backend team owns the auth-service and
api-gateway; the mobile team must update the mobile-client app; QA will
run regression tests on authentication flows.
Constraints: must finish before the September release; no downtime allowed.
Decision: using Authlib as the OAuth library.
Success means: all clients migrated and zero auth-related incidents for
two weeks after launch.
"""


def test_slugify():
    assert slugify("Authentication Redesign!") == "authentication-redesign"
    assert slugify("") == "untitled-project"
    assert slugify("  Payments & Billing ") == "payments-billing"
    print("[PASS] slugify")


def test_tool_validation_error():
    agent = ContextAgent()
    result = agent._handle_save({"project": {"name": 12345}})
    assert result["status"] == "error" and "invalid" in result["error"]
    print("[PASS] tool rejects malformed context dict")


def test_tool_save_and_readback():
    agent = ContextAgent()
    analysis = {
        "project": {
            "name": "Auth Redesign Test",
            "description": "OAuth migration",
            "objective": "migrate to oauth",
            "status": "in_progress",
            "domain": "security",
        },
        "work_scope": {
            "current_goal": "oauth migration",
            "planned_tasks": [
                {"id": "T1", "title": "update backend", "description": "", "status": "in_progress", "priority": "high"}
            ],
            "constraints": ["no downtime"],
            "important_decisions": ["use Authlib"],
        },
        "architecture": {"components": [], "dependencies": []},
        "stakeholders": [],
        "team_context": [],
        "planned_dependencies": [],
        "success_criteria": ["zero incidents"],
    }
    result = agent._handle_save(analysis)
    assert result["status"] == "success", result
    doc_id = result["document_id"]
    assert doc_id == "auth-redesign-test"

    stored = agent.firestore_client.collection(PROJECT_CONTEXTS_COLLECTION).document(doc_id).get()
    assert stored.exists, "doc should exist in firestore"
    data = stored.to_dict()
    assert data["project"]["name"] == "Auth Redesign Test"
    assert data["raw_context"]["source_type"] == "text"
    assert data["metadata"]["version"] == 1
    assert "saved_at" in data

    agent.firestore_client.collection(PROJECT_CONTEXTS_COLLECTION).document(doc_id).delete()
    print(f"[PASS] tool saved + read back from firestore db '{agent.database}' (doc {doc_id}), cleaned up")


def test_agent_constructs_adk25():
    agent = ContextAgent()
    assert agent.agent.name == "context_agent"
    assert agent.agent.output_schema is not None
    tool_names = [getattr(t, "__name__", "") for t in agent.agent.tools]
    assert any("save_firestore" in name for name in tool_names), tool_names
    print(f"[PASS] ADK agent constructed: model={agent.model_name} tools={tool_names}")


def run_live(description: str) -> None:
    agent = ContextAgent()
    output, document_id, by_tool = agent.analyze_and_save(description)
    print(f"[live] saved_by_tool={by_tool} document={document_id}")
    print(output.model_dump_json(indent=2)[:800])

    stored = agent.firestore_client.collection(PROJECT_CONTEXTS_COLLECTION).document(document_id).get()
    assert stored.exists, "live doc missing"
    live = stored.to_dict()
    assert live["project"]["name"], "project name empty"
    print(f"[live] verified firestore doc '{document_id}' in db '{agent.database}'")
    print("\nLIVE CONTEXT AGENT TEST PASSED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Run a real Gemini call end-to-end")
    parser.add_argument("-d", "--description", default=SAMPLE_DESCRIPTION)
    args = parser.parse_args()

    test_slugify()
    test_tool_validation_error()
    test_tool_save_and_readback()
    test_agent_constructs_adk25()

    if args.live:
        run_live(args.description.strip())
    else:
        print("\nOFFLINE CONTEXT AGENT TESTS PASSED (use --live for real Gemini run)")
