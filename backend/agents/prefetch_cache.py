"""Prefetch enforcement: cache-serve + tool-call budget cap.

Prompt lines alone cannot stop duplicate retrievals — this is the
deterministic enforcement. Each agent builds one PrefetchCache per analyze()
invocation from its `prefetch` payload; every Firestore-backed tool then:

1. Cache check first: hit → return prefetched data immediately, zero reads,
   budget untouched.
2. Budget charge: each REAL read consumes one unit of AGENT_TOOL_BUDGET
   (default 12); beyond it the tool returns an error directing the model
   back to the payload instead of retrying.

Fail-open: no cache (prefetch=None) behaves exactly like before.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("agents.prefetch_cache")


def tool_budget() -> int:
    try:
        return max(1, int(os.getenv("AGENT_TOOL_BUDGET", "12")))
    except ValueError:
        return 12


class PrefetchCache:
    """Per-invocation evidence cache + spend cap. Not shared across runs."""

    def __init__(self, prefetch: Optional[dict[str, Any]] = None) -> None:
        self.data: dict[str, Any] = dict(prefetch or {})
        self.hits = 0
        self.reads = 0

    def get(self, *path: str, default: Any = None) -> Any:
        """Nested-dict lookup; counts a hit only on success."""
        node: Any = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        if node is None:
            return default
        self.hits += 1
        return node

    def find_enriched(self, commit_sha: str) -> Optional[dict[str, Any]]:
        """Search prefetched enriched_changes list by SHA (PR multi-commit)."""
        for record in self.data.get("enriched_changes") or []:
            if isinstance(record, dict) and record.get("commit_sha") == commit_sha:
                self.hits += 1
                return record
        return None

    def charge(self, tool_name: str) -> Optional[str]:
        """Spend one budget unit for a REAL read. Returns an error message
        when the cap is exceeded (model must use payload data instead)."""
        self.reads += 1
        limit = tool_budget()
        if self.reads > limit:
            logger.warning("Tool budget exhausted (%d): denying %s", limit, tool_name)
            return (
                f"tool budget exhausted ({limit} reads this run): the evidence "
                f"you need is in the payload's prefetch section — use it and "
                f"do not call {tool_name} again"
            )
        return None
