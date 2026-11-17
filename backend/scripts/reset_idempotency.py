"""Helper utility to clear stuck idempotency leases in Firestore.

Usage inside docker:
    docker compose exec pr-worker python scripts/reset_idempotency.py [optional_pr_or_workflow_id]

Example:
    docker compose exec pr-worker python scripts/reset_idempotency.py 10
    docker compose exec pr-worker python scripts/reset_idempotency.py pr:my-project:10:128377e136fab563adb4d0d3449e1a13ef0e9e6e
"""

import sys
import logging
from security.idempotency import get_idempotency_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reset_idempotency")

def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    idem = get_idempotency_manager()
    client = idem.store._get_client()
    collection = client.collection("idempotency_keys")

    deleted_count = 0
    if target:
        if target.startswith("pr:"):
            query = collection.where("workflow_id", "==", target)
        elif target.isdigit():
            # Query documents and filter by PR number
            docs = collection.stream()
            for doc in docs:
                data = doc.to_dict() or {}
                wf_id = data.get("workflow_id", "")
                if f":{target}:" in wf_id or f":{target}" in wf_id:
                    print(f"Deleting key {doc.id} (workflow: {wf_id}, step: {data.get('step_name')}, status: {data.get('status')})")
                    doc.reference.delete()
                    deleted_count += 1
            print(f"Deleted {deleted_count} idempotency keys matching PR {target}.")
            return
        else:
            query = collection.where("workflow_id", "==", target)
        
        for doc in query.stream():
            data = doc.to_dict() or {}
            print(f"Deleting key {doc.id} (step: {data.get('step_name')}, status: {data.get('status')})")
            doc.reference.delete()
            deleted_count += 1
        print(f"Deleted {deleted_count} idempotency keys matching '{target}'.")
    else:
        # Clear all IN_PROGRESS leases
        for doc in collection.where("status", "==", "IN_PROGRESS").stream():
            data = doc.to_dict() or {}
            print(f"Deleting IN_PROGRESS key {doc.id} (workflow: {data.get('workflow_id')}, step: {data.get('step_name')})")
            doc.reference.delete()
            deleted_count += 1
        print(f"Deleted {deleted_count} IN_PROGRESS idempotency keys.")

if __name__ == "__main__":
    main()
