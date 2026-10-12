"""LLM provider package — see llm/provider.py."""

from __future__ import annotations

from .provider import provider_name, resolve_model

__all__ = ["provider_name", "resolve_model"]
