from __future__ import annotations

import hashlib
import hmac
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import load_env

PROJECT = load_env()
if not PROJECT:
    raise SystemExit("GOOGLE_CLOUD_PROJECT not set in backend/.env")

from fastapi.testclient import TestClient

from github_infrastructure.commit_events import CommitEvent
from github_infrastructure.commit_worker import CommitWorker
from github_infrastructure.config import settings
from github_infrastructure.main import app

SECRET = settings.webhook_secret
DELIVERY_ID = str(uuid.uuid4())
COMMIT_IDS = ["e2esha0000001", "e2esha0000002"]

PUSH_PAYLOAD = {
    "ref": "refs/heads/e2e-pipeline-branch",
    "before": "b" * 40,
    "after": "a" * 40,
    "repository": {
        "full_name": "acme/widget",
        "owner": {"login": "acme"},
    },
    "pusher": {"name": "e2e-bot"},
    "sender": {"login": "e2e-bot"},
    "commits": [{"id": sha, "message": f"commit {sha}"} for sha in COMMIT_IDS],
    "head_commit": {"id": "a" * 40, "timestamp": "2026-08-24T10:00:00Z"},
}


def main() -> None:
    worker = CommitWorker(project=PROJECT)
    worker.ensure_subscription()
    print(f"[worker] subscription ready: {worker.subscription_id}")

    body = json.dumps(PUSH_PAYLOAD).encode("utf-8")
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = TestClient(app).post(
        "/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": DELIVERY_ID,
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["mode"] == "pubsub", f"expected real Pub/Sub publish, got: {data}"
    print(f"[webhook] accepted delivery={data['delivery_id'][:8]}... published to topic '{settings.pubsub_topic}'")

    pulled: list[CommitEvent] = []
    match = None
    for attempt in range(6):
        pulled = worker.run_once(max_messages=20, timeout=15)
        match = next((e for e in pulled if e.delivery_id == DELIVERY_ID), None)
        if match:
            break
        print(f"[worker] batch {attempt + 1}: {len(pulled)} event(s), our message not arrived yet...")

    assert match is not None, f"delivery {DELIVERY_ID} never arrived; pulled={pulled}"
    print(f"[worker] received and normalized delivery after Pub/Sub round-trip")

    assert isinstance(match, CommitEvent)
    assert match.event == "push"
    assert match.repository == "acme/widget"
    assert match.owner == "acme"
    assert match.branch == "e2e-pipeline-branch"
    assert match.before_sha == "b" * 40
    assert match.after_sha == "a" * 40
    assert match.commit_shas == COMMIT_IDS
    assert match.pusher == "e2e-bot"
    print("[worker] all CommitEvent fields verified")

    print("\nNormalized CommitEvent:")
    print(match.model_dump_json(indent=2))
    print("\nEND-TO-END PUSH PIPELINE PASSED")


if __name__ == "__main__":
    main()
