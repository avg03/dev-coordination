"""Ownership resolver tests — ProjectContext + CODEOWNERS, never invented.

Run: python backend/tests/test_ownership_resolver.py  (offline)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from blast_radius.ownership_resolver import (
    OwnershipIndex,
    codeowners_owner_for_path,
    parse_codeowners,
)

PROJECT_CONTEXT = {
    "architecture": {
        "components": [
            {"id": "payment", "repository_paths": ["payment"], "responsible_teams": ["Payments"]},
            {"id": "checkout", "repository_paths": ["checkout"], "responsible_teams": []},
        ]
    },
    "team_context": [
        {"name": "Payments", "owned_components": ["payment"], "slack_channel_id": "C1"},
    ],
    "stakeholders": [
        {"name": "Alice", "role": "Engineer", "team": "Payments"},
        {"name": "Bob", "role": "Engineer", "team": "Payments"},
    ],
}


def test_component_to_team_to_people():
    idx = OwnershipIndex(PROJECT_CONTEXT)
    assert idx.teams_for_component("payment") == ["Payments"]
    # team_context also contributes ownership
    assert "Payments" in idx.teams_for_component("payment")

    entry = idx.component_entry("payment")
    assert entry is not None and entry.entity_type == "component"
    assert entry.evidence[0].type == "project_context"

    people = idx.person_entries_for_team("Payments", "payment", "direct", 0)
    assert {p.entity_id for p in people} == {"Alice", "Bob"}
    assert all(p.entity_type == "person" for p in people)


def test_unknown_owner_omitted_not_invented():
    idx = OwnershipIndex(PROJECT_CONTEXT)
    # 'checkout' has no responsible team and no team_context entry
    assert idx.teams_for_component("checkout") == []
    assert idx.component_entry("checkout") is None, "no owner -> no entry, never invented"
    assert idx.person_entries_for_team("GhostTeam", "x", "direct", 0) == []


def test_parse_codeowners_reader():
    sample = "# comment\n\n* @global-owner\npayment/ @payments-squad @alice\n*.md @docs-team\n"

    def reader(rel: str) -> str:
        if rel == ".github/CODEOWNERS":
            return sample
        raise FileNotFoundError(rel)

    entries = parse_codeowners(reader)
    assert len(entries) == 3
    assert entries[1] == ("payment/", ["@payments-squad", "@alice"])


def test_codeowners_last_match_wins():
    entries = [("*", ["@global"]), ("payment/", ["@payments-squad"]), ("payment/refund.py", ["@alice"])]
    assert codeowners_owner_for_path(entries, "payment/refund.py") == ["@alice"]
    assert codeowners_owner_for_path(entries, "payment/other.py") == ["@payments-squad"]
    assert codeowners_owner_for_path(entries, "unrelated/x.py") == ["@global"]


def test_codeowners_entries_for_files():
    idx = OwnershipIndex(PROJECT_CONTEXT, codeowners_entries=[("payment/", ["@payments-squad", "@alice"])])
    got = idx.codeowners_entries_for_files(["payment/refund.py", "payment/fee.py"])
    ids = {e.entity_id for e in got}
    assert "@payments-squad" in ids and "@alice" in ids
    assert all(e.impact_level == "direct" for e in got)
    assert got[0].evidence[0].detail.startswith("CODEOWNERS rule assigns")


if __name__ == "__main__":
    test_component_to_team_to_people()
    test_unknown_owner_omitted_not_invented()
    test_parse_codeowners_reader()
    test_codeowners_last_match_wins()
    test_codeowners_entries_for_files()
    print("=== ALL OWNERSHIP TESTS PASSED ===")
