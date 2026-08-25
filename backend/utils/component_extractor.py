from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from github_infrastructure.change_records import ChangeRecord

GENERIC_FOLDER_NAMES = {
    "src",
    "lib",
    "app",
    "apps",
    "packages",
    "pkg",
    "cmd",
    "internal",
    "core",
    "common",
    "shared",
    "services",
    "hooks",
    "utils",
    "helpers",
    "components",
    "views",
    "pages",
    "screens",
    "containers",
    "controllers",
    "routes",
    "api",
    "middleware",
    "models",
    "entities",
    "dto",
    "adapters",
    "repositories",
    "domain",
    "config",
    "configs",
    "tests",
    "test",
    "__tests__",
    "spec",
    "specs",
    "fixtures",
    "migrations",
}


def extract_component_root(file_path: str) -> str:
    normalized = file_path.replace("\\", "/").strip().lstrip("./")
    segments = [segment for segment in normalized.split("/") if segment]
    folders = segments[:-1]

    if not folders:
        return ""

    prefix = [folders[0]]
    for folder in folders[1:]:
        if folder.lower() in GENERIC_FOLDER_NAMES:
            break
        prefix.append(folder)

    return "/".join(prefix)


def extract_components(record: ChangeRecord) -> list[str]:
    components: list[str] = []
    for changed_file in record.changed_files:
        root = extract_component_root(changed_file.path)
        if root and root not in components:
            components.append(root)
    return components
