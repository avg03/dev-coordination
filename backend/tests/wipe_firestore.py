"""Full Firestore wipe for single-project reset — deletes ALL documents but keeps
the schema (collections recreate implicitly on next write).

Run: python backend/tests/wipe_firestore.py --confirm FULL-WIPE  (needs backend/.env creds)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import load_env

TOP_LEVEL_COLLECTIONS = [
    "project_contexts",
    "projects",
    "change_records",
    "commit_processing",
    "pr_workflows",
    "commit_completion_outbox",
    "idempotency_keys",
    "audit_logs",
]

_BATCH = 400


def _delete_doc_recursive(client, doc_ref) -> int:
    """Delete a document and all nested subcollections. Returns doc count."""
    count = 0
    for sub in doc_ref.collections():
        count += _delete_collection_recursive(client, sub)
    doc_ref.delete()
    return count + 1


def _delete_collection_recursive(client, col_ref) -> int:
    total = 0
    while True:
        docs = list(col_ref.limit(_BATCH).stream())
        if not docs:
            break
        batch = client.batch()
        for doc in docs:
            # Subcollections must go first (batch can't touch them).
            for sub in doc.reference.collections():
                total += _delete_collection_recursive(client, sub)
            batch.delete(doc.reference)
        batch.commit()
        total += len(docs)
    return total


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--confirm" or sys.argv[2] != "FULL-WIPE":
        print("Refusing: re-run with --confirm FULL-WIPE")
        return 2
    project = load_env()
    if not project:
        raise SystemExit("GOOGLE_CLOUD_PROJECT not set in backend/.env")
    from github_infrastructure.firestore_service import get_change_store

    client = get_change_store().client
    grand = 0
    for name in TOP_LEVEL_COLLECTIONS:
        n = _delete_collection_recursive(client, client.collection(name))
        print(f"{name}: deleted {n} doc(s)")
        grand += n
    print(f"FULL WIPE COMPLETE — {grand} doc(s) deleted. Schema recreates on next write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
