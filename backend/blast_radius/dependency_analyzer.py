"""Dependency analysis: imports -> component graph -> bounded downstream traversal.

Builds an in-memory, read-only graph for one analysis run (no graph database).
Changed components are expanded along *dependents* (who imports the changed
module), with every hop recorded as a DependencyEdge for evidence.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Optional

from utils.component_extractor import extract_component_root

from .models import DependencyEdge, RepositoryGraph
from .symbol_analyzer import FileFacts, module_name

logger = logging.getLogger("blast_radius.deps")

DEFAULT_MAX_DEPTH = 2


def _component_from_context_paths(path: str, components: list[dict]) -> Optional[str]:
    """Longest-prefix match of a file path against ProjectContext component
    repository_paths. `components` entries: {id, name, repository_paths: [...]}."""
    normalized = path.replace("\\", "/")
    best: tuple[int, str] | None = None
    for component in components:
        for repo_path in component.get("repository_paths") or []:
            prefix = str(repo_path).replace("\\", "/").rstrip("/")
            if not prefix:
                continue
            if normalized == prefix or normalized.startswith(prefix + "/"):
                length = len(prefix)
                if best is None or length > best[0]:
                    best = (length, str(component.get("id") or component.get("name") or ""))
    return best[1] if best and best[1] else None


def resolve_component(
    file_path: str,
    context_components: Optional[list[dict]] = None,
) -> str:
    """file -> component id. ProjectContext paths win; falls back to the
    deterministic path-root extractor."""
    if context_components:
        from_context = _component_from_context_paths(file_path, context_components)
        if from_context:
            return from_context
    return extract_component_root(file_path)


def build_import_graph(facts: dict[str, FileFacts]) -> dict[str, list[str]]:
    """file -> files that import it (dependents), from parsed imports.

    A file F is a dependent of file T when F imports T's module path.
    """
    module_to_file = {module_name(path): path for path in facts}
    dependents: dict[str, list[str]] = {}

    for importer_path, importer_facts in facts.items():
        seen: set[str] = set()
        for imported_module in importer_facts.imports:
            target = _resolve_import(imported_module, module_to_file)
            if target and target != importer_path and target not in seen:
                seen.add(target)
                dependents.setdefault(target, []).append(importer_path)
    return dependents


def _resolve_import(imported_module: str, module_to_file: dict[str, str]) -> Optional[str]:
    """Exact match first, then package __init__ (importing a package imports
    its __init__)."""
    if imported_module in module_to_file:
        return module_to_file[imported_module]
    parts = imported_module.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in module_to_file:
            return module_to_file[candidate]
        parts.pop()
    return None


def traverse_dependents(
    changed_files: list[str],
    dependents: dict[str, list[str]],
    component_of,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> tuple[list[DependencyEdge], list[str]]:
    """Bounded BFS from changed files along dependents.

    Returns (edges, affected_component_ids). depth 1 = direct dependent.
    Edges carry the relationship chain for evidence; traversal stops at
    max_depth to keep the blast radius meaningful.
    """
    edges: list[DependencyEdge] = []
    affected_components: list[str] = []
    seen_components: set[str] = set()

    queue: deque[tuple[str, int]] = deque((path, 0) for path in changed_files)
    visited_files: set[str] = set(changed_files)

    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for dependent in dependents.get(current, []):
            if dependent in visited_files:
                continue
            visited_files.add(dependent)
            edges.append(
                DependencyEdge(
                    source=component_of(current) or current,
                    target=component_of(dependent) or dependent,
                    relationship="depends_on",
                    depth=depth + 1,
                    reason=f"'{dependent}' imports '{current}' (depth {depth + 1})",
                )
            )
            component = component_of(dependent)
            if component and component not in seen_components:
                seen_components.add(component)
                affected_components.append(component)
            queue.append((dependent, depth + 1))

    return edges, affected_components


def build_graph(
    changed_paths: list[str],
    facts: dict[str, FileFacts],
    context_components: Optional[list[dict]] = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> tuple[RepositoryGraph, list[str]]:
    """Full read-only graph for one run: nodes, import edges, bounded traversal."""
    dependents = build_import_graph(facts)

    def component_of(path: str) -> str:
        return resolve_component(path, context_components)

    changed_components: list[str] = []
    seen: set[str] = set()
    for path in changed_paths:
        component = component_of(path)
        if component and component not in seen:
            seen.add(component)
            changed_components.append(component)

    edges, affected = traverse_dependents(changed_paths, dependents, component_of, max_depth=max_depth)

    nodes = sorted(set(list(facts.keys()) + changed_components + affected))
    graph = RepositoryGraph(nodes=nodes, edges=edges)
    return graph, list(dict.fromkeys(changed_components + affected))
