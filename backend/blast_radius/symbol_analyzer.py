"""Symbol analysis via Python AST — no code execution, stdlib only.

Parses files from the sandbox snapshot to extract imports and definitions.
Files that fail to parse are skipped and reported as incomplete evidence;
they are never executed.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("blast_radius.symbols")


@dataclass
class FileFacts:
    path: str
    imports: list[str] = field(default_factory=list)  # module names, dotted
    definitions: list[str] = field(default_factory=list)  # classes + functions
    parse_ok: bool = True


def _module_name_from_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if normalized.endswith(".py"):
        normalized = normalized[: -len(".py")]
    if normalized.endswith("/__init__"):
        normalized = normalized[: -len("/__init__")]
    return normalized.replace("/", ".")


def module_name(path: str) -> str:
    return _module_name_from_path(path)


def analyze_file(read_text, relative_path: str) -> FileFacts:
    """Parse one Python file via a reader callable (the sandbox's gateway-gated
    read_file). Never executes the file."""
    facts = FileFacts(path=relative_path)
    try:
        source = read_text(relative_path)
    except Exception as exc:
        logger.warning("Cannot read %s: %s", relative_path, exc)
        facts.parse_ok = False
        return facts

    try:
        tree = ast.parse(source, filename=relative_path)
    except SyntaxError as exc:
        logger.info("AST parse failed for %s: %s", relative_path, exc)
        facts.parse_ok = False
        return facts

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                facts.imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                facts.imports.append(node.module)
            elif node.level > 0:
                # Relative import: resolve against the file's own package.
                package = _module_name_from_path(relative_path).rsplit(".", 1)[0]
                parts = package.split(".") if package else []
                for _ in range(node.level - 1):
                    if parts:
                        parts.pop()
                if node.module:
                    parts.append(node.module)
                if parts:
                    facts.imports.append(".".join(parts))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name not in facts.definitions:
                facts.definitions.append(node.name)
    return facts


def analyze_snapshot(read_text, python_files: list[str]) -> dict[str, FileFacts]:
    """Parse every changed Python file; returns path -> facts."""
    facts: dict[str, FileFacts] = {}
    for rel in python_files:
        facts[rel] = analyze_file(read_text, rel)
    return facts


def find_symbol_span(source: str, symbol: str) -> Optional[tuple[int, int]]:
    """Line span (1-based, inclusive) of a top-level or nested symbol, or None."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
            return (node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno)
    return None
