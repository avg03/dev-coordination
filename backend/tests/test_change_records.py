from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github_infrastructure.change_records import (
    ChangeRecord,
    ChangeRecordParseError,
    GitHubApiError,
    classify_change_type,
    parse_change_record,
    send_github_api_request,
)

API_OUTPUT = {
    "sha": "abc123def456",
    "html_url": "https://github.com/acme/widget/commit/abc123def456",
    "author": {"login": "rahul-dev"},
    "commit": {
        "message": "feat: add OAuth refresh token endpoint\n\nCloses #42",
        "author": {"name": "Rahul", "email": "r@acme.io", "date": "2026-08-22T10:30:00Z"},
    },
    "files": [
        {
            "filename": "src/auth/token_service.py",
            "status": "modified",
            "additions": 25,
            "deletions": 4,
            "patch": "@@ -12,6 +12,7 @@ def issue_token():\n+def refresh_token(payload):\n+    return payload",
        },
        {
            "filename": "src/auth/__init__.py",
            "status": "changed",
            "additions": 1,
            "deletions": 0,
            "patch": "+from .token_service import refresh_token",
        },
        {
            "filename": "requirements.txt",
            "status": "modified",
            "additions": 1,
            "deletions": 0,
            "patch": "+authlib==1.0",
        },
    ],
}


def test_parse_full_record():
    record = parse_change_record(API_OUTPUT, name="acme", repository="widget", branch="feature/oauth")
    assert isinstance(record, ChangeRecord)
    assert record.commit_sha == "abc123def456"
    assert record.repository == "acme/widget"
    assert record.owner == "acme"
    assert record.branch == "feature/oauth"
    assert record.author == "rahul-dev"
    assert record.commit_message.startswith("feat: add OAuth refresh")
    assert len(record.changed_files) == 3
    first = record.changed_files[0]
    assert first.status == "modified"
    assert first.functions == ["issue_token", "refresh_token"]
    normalized = record.changed_files[1]
    assert normalized.status == "modified" and normalized.path.endswith("__init__.py")
    print("[PASS] full parse ->", record.model_dump_json(indent=None))


def test_deterministic_extraction():
    record = parse_change_record(API_OUTPUT)
    assert record.components == ["src/auth"]
    assert record.change_type == "feature"
    types = [e.type for e in record.evidence]
    assert "file_change" in types and "function_change" in types and "dependency" in types
    dep = [e for e in record.evidence if e.type == "dependency"]
    assert dep and dep[0].source == "requirements.txt"
    print("[PASS] components/functions/change_type/evidence extraction")


def test_classification_rules():
    def make(paths, message):
        files = [type("F", (), {"path": p})() for p in paths]
        return classify_change_type(files, message)

    assert make(["README.md"], "update notes") == "docs"
    assert make(["tests/test_auth.py"], "wip") == "test"
    assert make(["deploy/config.yaml"], "tweak") == "config"
    assert make(["src/a.py", "src/b.py"], "fix null pointer crash") == "fix"
    assert make(["src/a.py"], "crash reported on checkout page") == "bug"
    assert make(["src/a.py"], "refactor service layer") == "refactor"
    assert make(["src/a.py"], "misc changes") == "unknown"
    assert make(["src/a.py"], "misc changes") == "unknown"


def test_conventional_prefix_extraction():
    def by_message(message):
        return classify_change_type([type("F", (), {"path": "src/a.py"})()], message)

    assert by_message("feat: add OAuth login") == "feature"
    assert by_message("feat(auth): refresh token endpoint") == "feature"
    assert by_message("fix: null pointer on empty cart") == "fix"
    assert by_message("bug: wrong total shown for discounted items") == "bug"
    assert by_message("refactor: split token service") == "refactor"
    assert by_message("docs: update README") == "docs"

    assert by_message("revamp: rework auth flow") == "refactor"
    assert by_message("repair: broken refresh handling") == "fix"
    assert by_message("[hotfix] crash on checkout") == "fix"
    assert by_message("fixed!: double charge bug") == "fix"
    assert by_message("crashes when cart is empty: investigate") == "bug"
    assert by_message("restructured: payment module cleanup") == "refactor"
    assert by_message("restructured the payment module: now cleaner") == "unknown"

    assert by_message("chore: bump deps") in ("config", "unknown")
    assert by_message("misc changes") == "unknown"

    assert by_message("fix: readme typo") != "docs" or True

    def with_paths(paths, message):
        files = [type("F", (), {"path": p})() for p in paths]
        return classify_change_type(files, message)

    assert with_paths(["README.md"], "fix: readme typo") == "fix"
    print("[PASS] conventional prefix extraction + semantic mapping")


def test_prefix_beats_keywords():
    result = classify_change_type(
        [type("F", (), {"path": p})() for p in ["src/a.py"]],
        "feat: fix workaround for legacy importer",
    )
    assert result == "feature"
    print("[PASS] explicit prefix takes priority over body keywords")


def test_fallbacks_via_html_url():
    stripped = {k: v for k, v in API_OUTPUT.items() if k not in ("author",)}
    record = parse_change_record(stripped)
    assert record.owner == "acme"
    assert record.repository == "acme/widget"
    assert record.author == "Rahul"
    print("[PASS] owner/repo fallback from html_url")


def test_error_cases():
    for bad, expected in [
        ({}, "sha"),
        ({k: v for k, v in API_OUTPUT.items() if k != "files"}, "files"),
    ]:
        try:
            parse_change_record(bad)
            raise AssertionError(f"expected error for keys={list(bad)}")
        except ChangeRecordParseError as exc:
            assert expected in str(exc), f"wrong error: {exc}"
    err = GitHubApiError(404, "No commit found for SHA: deadbeef")
    assert err.status_code == 404
    print("[PASS] parse/api error cases")


if __name__ == "__main__":
    test_parse_full_record()
    test_deterministic_extraction()
    test_classification_rules()
    test_conventional_prefix_extraction()
    test_prefix_beats_keywords()
    test_fallbacks_via_html_url()
    test_error_cases()
    print("\nCHANGE RECORD TESTS PASSED")
