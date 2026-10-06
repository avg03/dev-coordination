"""Changed-file analysis for the Blast Radius engine.

Consumes the PR files API output (added/modified/removed/renamed, with
previous_filename for renames) and produces the deterministic changed set.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .models import ChangedFileInfo

logger = logging.getLogger("blast_radius.diff")

_STATUS_MAP = {
    "added": "added",
    "modified": "modified",
    "changed": "modified",
    "removed": "removed",
    "deleted": "removed",
    "renamed": "renamed",
    "copied": "added",
}


def parse_pr_files(raw_files: list[dict[str, Any]]) -> list[ChangedFileInfo]:
    """Map GitHub PR-files API entries to ChangedFileInfo."""
    files: list[ChangedFileInfo] = []
    for raw in raw_files or []:
        path = str(raw.get("filename", "") or "")
        if not path:
            continue
        status = _STATUS_MAP.get(str(raw.get("status", "modified")), "modified")
        previous = raw.get("previous_filename")
        files.append(
            ChangedFileInfo(
                path=path,
                status=status,  # type: ignore[arg-type]
                previous_path=str(previous) if previous else None,
                additions=int(raw.get("additions", 0) or 0),
                deletions=int(raw.get("deletions", 0) or 0),
            )
        )
    return files


def changed_python_files(files: list[ChangedFileInfo]) -> list[str]:
    """Python files whose current state should be parsed (added/modified/renamed)."""
    return [f.path for f in files if f.status in ("added", "modified", "renamed") and f.path.endswith(".py")]


def removed_or_renamed_away(files: list[ChangedFileInfo]) -> list[str]:
    """Paths that no longer exist at the PR head (removed, or renamed's source)."""
    gone: list[str] = []
    for f in files:
        if f.status == "removed":
            gone.append(f.path)
        if f.status == "renamed" and f.previous_path:
            gone.append(f.previous_path)
    return gone


def effective_changed_paths(files: list[ChangedFileInfo]) -> list[str]:
    """Current paths that exist at head and changed (for import-graph seeding)."""
    paths: list[str] = []
    for f in files:
        if f.status in ("added", "modified", "renamed"):
            paths.append(f.path)
    return paths


def changed_symbol_names(patch: Optional[str]) -> list[str]:
    """Symbol names touched by a diff hunk (best-effort, no execution)."""
    import re

    if not patch:
        return []
    hunk_func = re.compile(r"@@[^@]*@@\s*(?:async\s+)?def\s+([A-Za-z_]\w*)|@@[^@]*@@\s*class\s+([A-Za-z_]\w*)")
    added_def = re.compile(r"^\+\s*(?:async\s+)?def\s+([A-Za-z_]\w*)|^\+\s*class\s+([A-Za-z_]\w*)")
    found: list[str] = []
    for line in patch.splitlines():
        for pattern in (hunk_func, added_def):
            m = pattern.search(line)
            if m:
                name = m.group(1) or m.group(2)
                if name and name not in found:
                    found.append(name)
    return found[:20]
