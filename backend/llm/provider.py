"""LLM provider switch: Gemini (Google ADK native) or Azure OpenAI (via LiteLLM).

Env:
  PROVIDER=google|azure          (default: google; "gemini" accepted as alias)
  # google: existing per-agent vars hold Gemini model names (unchanged behavior)
  # azure (all required unless noted):
  AZURE_API_KEY=<Keys & Endpoint → KEY 1>
  AZURE_API_BASE=https://<resource>.openai.azure.com   (no trailing slash)
  AZURE_API_VERSION=<e.g. 2024-12-01-preview>          (optional, has default)
  AZURE_DEPLOYMENT=<deployment name>                   (global default deployment)
  # per-agent deployment override: the existing per-agent MODEL vars
  # (CONTEXT_AGENT_MODEL, CHANGE_ANALYSIS_MODEL, ...) hold DEPLOYMENT names
  # when PROVIDER=azure, falling back to AZURE_DEPLOYMENT.

ADK Agents accept either a model-name string (Gemini) or a BaseLlm instance,
so LiteLlm preserves every tool, guard, runner, and output-schema behavior.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("llm.provider")

DEFAULT_AZURE_API_VERSION = "2024-12-01-preview"


def provider_name() -> str:
    raw = os.getenv("PROVIDER", "google").strip().lower()
    if raw in ("google", "gemini"):
        return "google"
    if raw == "azure":
        return "azure"
    raise ValueError(f"Unknown PROVIDER={raw!r}: expected 'google' or 'azure'")


def resolve_model(*env_vars: str, default: str) -> Any:
    """Return what ADK Agent(model=...) needs for the configured provider.

    google → model-name string (first set env var, else default).
    azure  → LiteLlm instance on azure/<deployment> (first set env var holds
             a deployment name, else AZURE_DEPLOYMENT).
    """
    provider = provider_name()
    override: Optional[str] = None
    for var in env_vars:
        value = (os.getenv(var, "") or "").strip()
        if value:
            override = value
            break

    if provider == "google":
        return override or default

    # --- azure ---
    deployment = override or (os.getenv("AZURE_DEPLOYMENT", "") or "").strip()
    api_key = (os.getenv("AZURE_API_KEY", "") or "").strip()
    api_base = (os.getenv("AZURE_API_BASE", "") or "").strip().rstrip("/")
    api_version = (os.getenv("AZURE_API_VERSION", "") or "").strip() or DEFAULT_AZURE_API_VERSION
    missing = [
        name
        for name, value in (
            ("AZURE_API_KEY", api_key),
            ("AZURE_API_BASE", api_base),
            ("AZURE_DEPLOYMENT", deployment),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"PROVIDER=azure but missing {', '.join(missing)} in backend/.env. "
            "Set AZURE_API_KEY, AZURE_API_BASE (https://<resource>.openai.azure.com) "
            "and AZURE_DEPLOYMENT (deployment name, not model name)."
        )
    try:
        from google.adk.models.lite_llm import LiteLlm
    except ImportError as exc:
        raise ValueError(
            "PROVIDER=azure needs LiteLLM support: run "
            "`backend\\venv\\Scripts\\python.exe -m pip install litellm` "
            "(google-adk[extensions])."
        ) from exc
    logger.info("LLM provider=azure deployment=%s base=%s", deployment, api_base)
    return LiteLlm(
        model=f"azure/{deployment}",
        api_key=api_key,
        api_base=api_base,
        api_version=api_version,
    )
