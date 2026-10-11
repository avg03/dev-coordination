"""Deterministic ownership resolution: components -> teams -> people.

Evidence priority (doc §10) — deterministic sources only in V1:
  1. Explicit project ownership (ProjectContext components/teams/stakeholders)
  2. CODEOWNERS from the repository snapshot
No owner found -> the entity is omitted. Ownership is never invented.
"""

from __future__ import annotations

import logging
from typing import Optional

from github_infrastructure.change_records import Evidence

from .models import BlastRadiusEntry

logger = logging.getLogger("blast_radius.ownership")

CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


def parse_codeowners(read_text) -> list[tuple[str, list[str]]]:
    """Parse CODEOWNERS via a reader callable (gateway-gated sandbox reads).
    Returns (pattern, [owner...]) entries in file order (last match wins,
    per GitHub semantics). `read_text(rel_path)` should return the file
    contents or raise; missing files are treated as absent.

    Owner forms: @username, @org/team-name, user@example.com. Team references
    are returned verbatim — mapping them to ProjectContext teams is the
    caller's job (alignment is an assumption documented in the plan).
    """
    for location in CODEOWNERS_LOCATIONS:
        try:
            text = read_text(location)
        except Exception:
            continue
        if not text:
            continue
        lines = text.splitlines()
        entries: list[tuple[str, list[str]]] = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            pattern, owners = parts[0], parts[1:]
            entries.append((pattern, owners))
        logger.info("Parsed CODEOWNERS from %s (%d entries)", location, len(entries))
        return entries
    return []


def codeowners_owner_for_path(entries: list[tuple[str, list[str]]], file_path: str) -> list[str]:
    """Last-matching CODEOWNERS pattern owns the path (GitHub rule)."""
    normalized = "/" + file_path.replace("\\", "/").lstrip("/")
    matched: list[str] = []
    for pattern, owners in entries:
        if _codeowners_match(pattern, normalized):
            matched = owners
    return matched


def _codeowners_match(pattern: str, posix_path: str) -> bool:
    """Simplified GitHub CODEOWNERS matching for V1.

    - '/dir/' or 'dir/' anchored to repo root (prefix match)
    - '*.ext' matches basename suffix
    - exact path matches exactly
    - bare 'name/' prefix match; bare 'name' matches any path segment
    """
    path = posix_path
    if pattern.startswith("*") or ("." in pattern.split("/")[-1] and "*" in pattern):
        import fnmatch

        return fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern)
    clean = pattern.strip("/")
    if pattern.endswith("/"):
        return path.startswith("/" + clean + "/") or path == "/" + clean
    if pattern.startswith("/"):
        return path == "/" + clean or path.startswith("/" + clean + "/")
    # Bare pattern: exact segment match anywhere
    segments = path.strip("/").split("/")
    return clean in segments or path == "/" + clean


class OwnershipIndex:
    """Component/team/person ownership built from ProjectContext + CODEOWNERS."""

    def __init__(self, project_context: dict, codeowners_entries: Optional[list[tuple[str, list[str]]]] = None) -> None:
        self.codeowners = codeowners_entries or []
        architecture = project_context.get("architecture") or {}
        self._component_teams: dict[str, list[str]] = {}
        for component in architecture.get("components") or []:
            cid = str(component.get("id") or component.get("name") or "")
            if cid:
                self._component_teams[cid] = [str(t) for t in (component.get("responsible_teams") or [])]

        self._team_members: dict[str, list[dict]] = {}
        self._team_channels: dict[str, str] = {}
        for team in project_context.get("team_context") or []:
            name = str(team.get("name") or "")
            if not name:
                continue
            self._team_members.setdefault(name, [])
            self._team_channels[name] = str(team.get("slack_channel_id") or "")
            for owned in team.get("owned_components") or []:
                owned_id = str(owned)
                self._component_teams.setdefault(owned_id, [])
                if name not in self._component_teams[owned_id]:
                    self._component_teams[owned_id].append(name)

        self._people: list[dict] = list(project_context.get("stakeholders") or [])
        for person in self._people:
            team = str(person.get("team") or "")
            if team:
                self._team_members.setdefault(team, []).append(person)

    def teams_for_component(self, component_id: str) -> list[str]:
        return list(self._component_teams.get(component_id, []))

    def people_for_team(self, team: str) -> list[dict]:
        return list(self._team_members.get(team, []))

    def owner_for_file(self, file_path: str) -> list[str]:
        """CODEOWNERS owners for a file (verbatim entries)."""
        return codeowners_owner_for_path(self.codeowners, file_path)

    def component_entry(self, component_id: str) -> Optional[BlastRadiusEntry]:
        teams = self.teams_for_component(component_id)
        if not teams:
            return None
        evidence = [
            Evidence(
                type="project_context",
                source=f"architecture.components.{component_id}",
                detail=f"owned by team(s): {', '.join(teams)}",
            )
        ]
        return BlastRadiusEntry(
            entity_type="component",
            entity_id=component_id,
            name=component_id,
            impact_level="direct",
            impact_type="none",  # the Impact Agent classifies significance
            reason=f"Component {component_id} contains changed files; owned by {', '.join(teams)}",
            evidence=evidence,
            confidence=0.9,
        )

    def team_entry(self, team: str, component_id: str, impact_level: str, depth: int) -> Optional[BlastRadiusEntry]:
        members = self.people_for_team(team)
        evidence = [
            Evidence(
                type="project_context",
                source=f"team_context.{team}",
                detail=f"team owns component {component_id}",
            )
        ]
        if not members:
            # Team known but no members mapped — still surface the team itself.
            return BlastRadiusEntry(
                entity_type="team",
                entity_id=team,
                name=team,
                impact_level=impact_level,  # type: ignore[arg-type]
                impact_type="none",
                reason=f"Team {team} owns {component_id} (affected via dependency, depth {depth}); no members mapped in ProjectContext",
                evidence=evidence,
                confidence=0.7,
            )
        return BlastRadiusEntry(
            entity_type="team",
            entity_id=team,
            name=team,
            impact_level=impact_level,  # type: ignore[arg-type]
            impact_type="none",
            reason=f"Team {team} owns affected component {component_id} (depth {depth})",
            evidence=evidence,
            confidence=0.8,
        )

    def person_entries_for_team(self, team: str, component_id: str, impact_level: str, depth: int) -> list[BlastRadiusEntry]:
        entries: list[BlastRadiusEntry] = []
        for person in self.people_for_team(team):
            name = str(person.get("name") or "")
            if not name:
                continue
            entries.append(
                BlastRadiusEntry(
                    entity_type="person",
                    entity_id=name,
                    name=name,
                    impact_level=impact_level,  # type: ignore[arg-type]
                    impact_type="none",
                    reason=(
                        f"{name} ({person.get('role') or 'stakeholder'}, team {team}) is responsible "
                        f"for {component_id}, affected at depth {depth}"
                    ),
                    evidence=[
                        Evidence(
                            type="project_context",
                            source=f"stakeholders.{name}",
                            detail=f"member of team {team}, team owns {component_id}",
                        )
                    ],
                    confidence=0.75,
                )
            )
        return entries

    def codeowners_entries_for_files(self, changed_files: list[str]) -> list[BlastRadiusEntry]:
        """Person/team entries from CODEOWNERS for directly changed files."""
        entries: list[BlastRadiusEntry] = []
        seen: set[str] = set()
        for file_path in changed_files:
            for owner in self.owner_for_file(file_path):
                if owner in seen:
                    continue
                seen.add(owner)
                is_team_ref = owner.startswith("@") and "/" in owner
                entries.append(
                    BlastRadiusEntry(
                        entity_type="team" if is_team_ref else "person",
                        entity_id=owner,
                        name=owner,
                        impact_level="direct",
                        impact_type="none",
                        reason=f"{owner} owns '{file_path}' per CODEOWNERS",
                        evidence=[
                            Evidence(
                                type="file_change",
                                source=file_path,
                                detail=f"CODEOWNERS rule assigns {owner}",
                            )
                        ],
                        confidence=0.9,
                    )
                )
        return entries
