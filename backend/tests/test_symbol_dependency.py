"""Symbol + dependency analyzer tests on a fixture mini-repo (offline).

Run: python backend/tests/test_symbol_dependency.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from blast_radius.dependency_analyzer import build_graph, resolve_component
from blast_radius.diff_analyzer import (
    changed_python_files,
    effective_changed_paths,
    parse_pr_files,
    removed_or_renamed_away,
)
from blast_radius.models import ChangedFileInfo
from blast_radius.symbol_analyzer import analyze_snapshot

CONTEXT_COMPONENTS = [
    {"id": "payment", "name": "Payment", "repository_paths": ["payment"], "responsible_teams": ["Payments"]},
    {"id": "checkout", "name": "Checkout", "repository_paths": ["checkout"], "responsible_teams": ["Checkout"]},
]


def _fixture_repo(tmp: Path) -> None:
    (tmp / "payment").mkdir(parents=True, exist_ok=True)
    (tmp / "checkout").mkdir(parents=True, exist_ok=True)
    (tmp / "notify").mkdir(parents=True, exist_ok=True)
    (tmp / "payment" / "refund.py").write_text(
        "import logging\n\n\nclass RefundProcessor:\n    pass\n\n\ndef process_refund():\n    return True\n",
        encoding="utf-8",
    )
    (tmp / "checkout" / "service.py").write_text(
        "from payment.refund import RefundProcessor\n\n\nclass CheckoutService:\n    pass\n",
        encoding="utf-8",
    )
    (tmp / "notify" / "alerts.py").write_text(
        "from checkout.service import CheckoutService\n\n\ndef alert():\n    pass\n",
        encoding="utf-8",
    )


def test_parse_pr_files_statuses():
    raw = [
        {"filename": "payment/refund.py", "status": "modified", "additions": 5, "deletions": 2},
        {"filename": "checkout/new.py", "status": "added"},
        {"filename": "old/thing.py", "status": "removed"},
        {"filename": "checkout/moved.py", "status": "renamed", "previous_filename": "checkout/old.py"},
        {"filename": "", "status": "added"},  # ignored
    ]
    files = parse_pr_files(raw)
    assert len(files) == 4
    assert files[2].status == "removed"
    assert files[3].status == "renamed" and files[3].previous_path == "checkout/old.py"
    assert removed_or_renamed_away(files) == ["old/thing.py", "checkout/old.py"]
    assert changed_python_files(files) == ["payment/refund.py", "checkout/new.py", "checkout/moved.py"]
    assert effective_changed_paths(files) == ["payment/refund.py", "checkout/new.py", "checkout/moved.py"]


def test_component_resolution_prefers_project_context():
    assert resolve_component("payment/refund.py", CONTEXT_COMPONENTS) == "payment"
    assert resolve_component("checkout/deep/service.py", CONTEXT_COMPONENTS) == "checkout"
    # Falls back to deterministic path-root extractor for unmapped paths
    assert resolve_component("notify/alerts.py", CONTEXT_COMPONENTS) == "notify"
    assert resolve_component("payment/refund.py") == "payment"


def test_multi_level_dependency_traversal():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        _fixture_repo(tmp)
        facts = analyze_snapshot(lambda rel: (tmp / rel).read_text(encoding="utf-8"), [
            "payment/refund.py",
            "checkout/service.py",
            "notify/alerts.py",
        ])
        assert all(f.parse_ok for f in facts.values())
        assert facts["checkout/service.py"].imports == ["payment.refund"]

        graph, affected = build_graph(
            ["payment/refund.py"],
            facts,
            context_components=CONTEXT_COMPONENTS,
            max_depth=2,
        )
        assert affected[0] == "payment", "changed component comes first"
        assert "checkout" in affected, "direct dependent must be affected"
        assert "notify" in affected, "second-level dependent must be affected (depth 2)"

        edge_targets = {e.target: e for e in graph.edges}
        assert edge_targets["checkout"].depth == 1
        assert edge_targets["notify"].depth == 2
        assert edge_targets["checkout"].reason, "edges must carry traversal evidence"
        assert graph.edges[0].relationship == "depends_on"


def test_depth_bound_limits_traversal():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        _fixture_repo(tmp)
        facts = analyze_snapshot(lambda rel: (tmp / rel).read_text(encoding="utf-8"), [
            "payment/refund.py",
            "checkout/service.py",
            "notify/alerts.py",
        ])
        graph, affected = build_graph(
            ["payment/refund.py"], facts, context_components=CONTEXT_COMPONENTS, max_depth=1
        )
        assert "checkout" in affected
        assert "notify" not in affected, "depth-2 dependent must be excluded at max_depth=1"


def test_unparseable_file_marked_incomplete_not_fatal():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        (tmp / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        facts = analyze_snapshot(lambda rel: (tmp / rel).read_text(encoding="utf-8"), ["broken.py"])
        assert facts["broken.py"].parse_ok is False


def test_symbols_extracted():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        _fixture_repo(tmp)
        facts = analyze_snapshot(lambda rel: (tmp / rel).read_text(encoding="utf-8"), ["payment/refund.py"])
        syms = facts["payment/refund.py"].definitions
        assert "RefundProcessor" in syms and "process_refund" in syms


def test_changed_file_info_roundtrip():
    info = ChangedFileInfo(path="a/b.py", status="renamed", previous_path="a/c.py")
    assert info.previous_path == "a/c.py"


if __name__ == "__main__":
    test_parse_pr_files_statuses()
    test_component_resolution_prefers_project_context()
    test_multi_level_dependency_traversal()
    test_depth_bound_limits_traversal()
    test_unparseable_file_marked_incomplete_not_fatal()
    test_symbols_extracted()
    test_changed_file_info_roundtrip()
    print("=== ALL SYMBOL/DEPENDENCY TESTS PASSED ===")
