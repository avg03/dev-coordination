from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github_infrastructure.change_records import ChangedFile, ChangeRecord
from utils.component_extractor import extract_component_root, extract_components


def test_documented_examples():
    assert extract_component_root("backend/payment/services/refund_manager.py") == "backend/payment"
    assert extract_component_root("frontend/checkout/hooks/usePayment.ts") == "frontend/checkout"
    print("[PASS] documented examples")


def test_various_shapes():
    cases = {
        "src/main.py": "src",
        "README.md": "",
        "docs/guide.md": "docs",
        "backend/auth.py": "backend",
        "services/auth/login.py": "services/auth",
        "a/b/c/d/e/deep_file.go": "a/b/c/d/e",
        "backend/payment/services/refunds/partial_refund.py": "backend/payment",
        "apps/web/components/ui/Button.tsx": "apps/web",
        "backend\\payment\\windows_style.py": "backend/payment",
        "./packages/core/utils/math.ts": "packages",
        "src/utils/helpers/date.py": "src",
    }
    for path, expected in cases.items():
        got = extract_component_root(path)
        assert got == expected, f"{path}: expected '{expected}', got '{got}'"
    print("[PASS] path shape variations")


def test_extract_components_from_record():
    record = ChangeRecord(
        commit_sha="sha123",
        repository="acme/widget",
        owner="acme",
        timestamp="2026-08-25T10:00:00Z",
        changed_files=[
            ChangedFile(path="backend/payment/services/refund_manager.py"),
            ChangedFile(path="backend/payment/api/routes.py"),
            ChangedFile(path="frontend/checkout/hooks/usePayment.ts"),
            ChangedFile(path="frontend/checkout/pages/CartPage.tsx"),
            ChangedFile(path="README.md"),
        ],
    )
    components = extract_components(record)
    assert components == ["backend/payment", "frontend/checkout"], components
    print("[PASS] record-level extraction ->", components)


def test_no_components():
    empty = ChangeRecord(
        commit_sha="sha123",
        repository="acme/widget",
        owner="acme",
        timestamp="2026-08-25T10:00:00Z",
        changed_files=[ChangedFile(path="README.md"), ChangedFile(path=".gitignore")],
    )
    assert extract_components(empty) == []
    print("[PASS] root-level-only files yield no components")


if __name__ == "__main__":
    test_documented_examples()
    test_various_shapes()
    test_extract_components_from_record()
    test_no_components()
    print("\nCOMPONENT EXTRACTOR TESTS PASSED")
