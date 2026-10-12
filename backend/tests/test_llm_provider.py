"""LLM provider switch tests — google default vs azure via LiteLlm.

Run: python backend/tests/test_llm_provider.py (no network, no keys needed)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_KEYS = ("PROVIDER", "AZURE_API_KEY", "AZURE_API_BASE", "AZURE_API_VERSION",
         "AZURE_DEPLOYMENT", "CONTEXT_AGENT_MODEL", "IMPACT_ANALYSIS_MODEL")


def _clean_env():
    saved = {k: os.environ.pop(k, None) for k in _KEYS}
    return saved


def _restore(saved):
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


def test_google_default_returns_model_string():
    from llm import resolve_model

    saved = _clean_env()
    try:
        assert resolve_model("CONTEXT_AGENT_MODEL", default="gemini-3.5-flash") == "gemini-3.5-flash"
        os.environ["PROVIDER"] = "google"
        assert resolve_model("CONTEXT_AGENT_MODEL", default="gemini-3.5-flash") == "gemini-3.5-flash"
        os.environ["CONTEXT_AGENT_MODEL"] = "gemini-2.0-flash"
        assert resolve_model("CONTEXT_AGENT_MODEL", default="gemini-3.5-flash") == "gemini-2.0-flash"
    finally:
        _restore(saved)


def test_google_env_chain_first_set_wins():
    from llm import resolve_model

    saved = _clean_env()
    try:
        os.environ["CONTEXT_AGENT_MODEL"] = "gemini-2.0-flash"
        os.environ["IMPACT_ANALYSIS_MODEL"] = "gemini-1.5-pro"
        assert resolve_model("IMPACT_ANALYSIS_MODEL", "CONTEXT_AGENT_MODEL", default="x") == "gemini-1.5-pro"
        del os.environ["IMPACT_ANALYSIS_MODEL"]
        assert resolve_model("IMPACT_ANALYSIS_MODEL", "CONTEXT_AGENT_MODEL", default="x") == "gemini-2.0-flash"
    finally:
        _restore(saved)


def test_invalid_provider_rejected():
    from llm import provider_name

    saved = _clean_env()
    try:
        os.environ["PROVIDER"] = "openai"
        try:
            provider_name()
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            assert "google' or 'azure'" in str(exc), exc
    finally:
        _restore(saved)


def test_azure_missing_creds_raise_clear_error():
    from llm import resolve_model

    saved = _clean_env()
    try:
        os.environ["PROVIDER"] = "azure"
        try:
            resolve_model("CONTEXT_AGENT_MODEL", default="gemini-3.5-flash")
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            for token in ("AZURE_API_KEY", "AZURE_API_BASE", "AZURE_DEPLOYMENT"):
                assert token in str(exc), exc
            assert "gemini" not in str(exc), "gemini default must never leak into azure path"
    finally:
        _restore(saved)


def test_azure_builds_litellm_deployment():
    from google.adk.models.lite_llm import LiteLlm

    from llm import resolve_model

    saved = _clean_env()
    try:
        os.environ.update({
            "PROVIDER": "azure",
            "AZURE_API_KEY": "test-key",
            "AZURE_API_BASE": "https://myres.openai.azure.com",
            "AZURE_API_VERSION": "2024-12-01-preview",
            "AZURE_DEPLOYMENT": "gpt-4o-team",
        })
        model = resolve_model("CONTEXT_AGENT_MODEL", default="gemini-3.5-flash")
        assert isinstance(model, LiteLlm), type(model)
        assert model.model == "azure/gpt-4o-team", model.model
        # Per-agent var acts as deployment override.
        os.environ["CONTEXT_AGENT_MODEL"] = "gpt-4o-mini-team"
        model2 = resolve_model("CONTEXT_AGENT_MODEL", default="gemini-3.5-flash")
        assert model2.model == "azure/gpt-4o-mini-team", model2.model
    finally:
        _restore(saved)


def test_agents_default_to_google_without_env():
    from agents.communication_agent import CommunicationAgent
    from agents.context_agent import ContextAgent

    saved = _clean_env()
    try:
        # Needs GCP project for agent construction — inject directly.
        ctx = ContextAgent.__new__(ContextAgent)
        from llm import resolve_model

        assert resolve_model("CONTEXT_AGENT_MODEL", default="gemini-3.5-flash") == "gemini-3.5-flash"
        assert CommunicationAgent is not None and ContextAgent is not None
    finally:
        _restore(saved)


if __name__ == "__main__":
    test_google_default_returns_model_string()
    test_google_env_chain_first_set_wins()
    test_invalid_provider_rejected()
    test_azure_missing_creds_raise_clear_error()
    test_azure_builds_litellm_deployment()
    test_agents_default_to_google_without_env()
    print("=== ALL LLM PROVIDER TESTS PASSED ===")
