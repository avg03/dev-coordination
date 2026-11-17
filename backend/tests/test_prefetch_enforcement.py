"""Prefetch enforcement tests — cache-serve + tool budget cap.

Run: python backend/tests/test_prefetch_enforcement.py (no GCP, no LLM needed)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def test_cache_get_and_miss():
    from agents.prefetch_cache import PrefetchCache

    cache = PrefetchCache({"project_context": {"project": {"name": "x"}}})
    assert cache.get("project_context") == {"project": {"name": "x"}}
    assert cache.hits == 1
    assert cache.get("missing") is None
    assert cache.get("missing", default="d") == "d"
    assert cache.get("project_context", "project", "name") == "x"
    assert cache.hits == 2


def test_find_enriched_by_sha():
    from agents.prefetch_cache import PrefetchCache

    cache = PrefetchCache({"enriched_changes": [
        {"commit_sha": "aaa", "summary": "first"},
        {"commit_sha": "bbb", "summary": "second"},
    ]})
    assert cache.find_enriched("bbb") == {"commit_sha": "bbb", "summary": "second"}
    assert cache.find_enriched("zzz") is None
    assert cache.hits == 1


def test_budget_cap_denies_overuse():
    from agents.prefetch_cache import PrefetchCache, tool_budget

    old = os.environ.get("AGENT_TOOL_BUDGET")
    os.environ["AGENT_TOOL_BUDGET"] = "3"
    try:
        assert tool_budget() == 3
        cache = PrefetchCache({})
        assert cache.charge("t") is None
        assert cache.charge("t") is None
        assert cache.charge("t") is None
        denied = cache.charge("t")
        assert denied is not None and "budget exhausted" in denied, denied
        assert cache.reads == 4
    finally:
        if old is None:
            os.environ.pop("AGENT_TOOL_BUDGET", None)
        else:
            os.environ["AGENT_TOOL_BUDGET"] = old


def test_impact_tools_serve_cache_without_firestore():
    from agents.impact_analysis_agent import ImpactAnalysisAgent

    agent = ImpactAnalysisAgent(project="test-proj")
    from agents.prefetch_cache import PrefetchCache

    agent._pcache = PrefetchCache({
        "project_context": {"project": {"name": "p"}},
        "enriched_changes": [{"commit_sha": "abc123", "summary": "fix auth"}],
        "blast_report": {"entries": []},
    })
    out = agent._get_project_context_tool("test-proj")
    assert out["status"] == "success" and out["project_context"] == {"project": {"name": "p"}}, out
    out2 = agent._get_enriched_change_tool("test-proj", "abc123")
    assert out2["status"] == "success" and out2["enriched_change"]["summary"] == "fix auth", out2
    out3 = agent._get_blast_radius_tool("test-proj", 7)
    assert out3["status"] == "success" and out3["blast_radius_report"] == {"entries": []}, out3
    assert agent._pcache.hits == 3 and agent._pcache.reads == 0


def test_budget_deny_blocks_firestore_read():
    """Over-budget tool call fails closed WITHOUT touching Firestore."""
    from agents.coordination_agent import CoordinationAgent

    agent = CoordinationAgent(project="test-proj")
    from agents.prefetch_cache import PrefetchCache

    agent._pcache = PrefetchCache({})
    agent._pcache.reads = 99  # already over any budget
    out = agent._get_impact_report_tool("test-proj", "sha")
    assert out["status"] == "error" and "budget exhausted" in out["error"], out


def test_supervisor_tools_serve_cache():
    from agents.supervisor_agent import SupervisorAgent

    agent = SupervisorAgent(project="test-proj")
    from agents.prefetch_cache import PrefetchCache

    agent._pcache = PrefetchCache({
        "project_context": {"project": {"name": "p"}},
        "coordination_plan": {"summary": "s", "tasks": []},
    })
    out = agent._get_project_context_tool("test-proj")
    assert out["status"] == "success", out
    out2 = agent._get_coordination_plan_tool("test-proj", 3)
    assert out2["status"] == "success" and out2["coordination_plan"]["summary"] == "s", out2


def test_no_cache_behaves_as_before():
    """Agents without prefetch keep legacy behavior (attribute absent)."""
    from agents.communication_agent import CommunicationAgent

    agent = CommunicationAgent(project="test-proj")
    assert getattr(agent, "_pcache", None) is None


if __name__ == "__main__":
    test_cache_get_and_miss()
    test_find_enriched_by_sha()
    test_budget_cap_denies_overuse()
    test_impact_tools_serve_cache_without_firestore()
    test_budget_deny_blocks_firestore_read()
    test_supervisor_tools_serve_cache()
    test_no_cache_behaves_as_before()
    print("=== ALL PREFETCH ENFORCEMENT TESTS PASSED ===")
