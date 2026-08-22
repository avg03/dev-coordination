from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import load_env

from google.api_core.exceptions import PermissionDenied
from google.cloud import firestore


def main() -> None:
    project = load_env()
    if not project:
        raise SystemExit("GOOGLE_CLOUD_PROJECT is not set in backend/.env")

    database = os.getenv("FIRESTORE_DATABASE", "(default)")
    db = firestore.Client(project=project, database=database)
    doc_ref = db.collection("connection_tests").document("smoke_test")
    now = datetime.now(timezone.utc).isoformat()

    print(f"[firestore] project: {project}, database: {database}")

    payload = {"message": "hello-firestore", "run_at": now, "value": 42}
    try:
        doc_ref.set(payload)
    except PermissionDenied as exc:
        raise SystemExit(
            f"The credential cannot write to Firestore database {database!r} "
            f"in project {project}. Grant the service account "
            "roles/datastore.user (or a broader Firestore role) and retry. "
            f"Original error: {exc}"
        ) from exc
    except Exception as exc:
        if "database" in str(exc).lower() and "does not exist" in str(exc).lower():
            raise SystemExit(
                f"Firestore database {database!r} does not exist in project {project}. "
                "Set FIRESTORE_DATABASE in backend/.env to the database ID shown in "
                "Firestore -> Databases, or create the (default) database. "
                f"Original error: {exc}"
            ) from exc
        raise
    print("[write] set document connection_tests/smoke_test")

    snapshot = doc_ref.get()
    assert snapshot.exists, "Document should exist after write"
    data = snapshot.to_dict()
    assert data["message"] == "hello-firestore", f"Unexpected message: {data}"
    assert data["value"] == 42
    print(f"[read] got document back: {data['message']}, value={data['value']}")

    doc_ref.update({"value": 43})
    updated = doc_ref.get().to_dict()
    assert updated["value"] == 43, f"Update failed: {updated}"
    print("[update] value -> 43 verified on re-read")

    query = list(
        db.collection("connection_tests").where("message", "==", "hello-firestore").stream()
    )
    assert any(q.id == "smoke_test" for q in query), "Query should find the test doc"
    print(f"[query] found {len(query)} matching document(s)")

    doc_ref.delete()
    assert not doc_ref.get().exists, "Document should be gone after delete"
    print("[delete] cleanup done")

    print("\nFIRESTORE TEST PASSED")


if __name__ == "__main__":
    main()
