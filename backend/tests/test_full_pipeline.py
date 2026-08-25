from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import load_env

PROJECT = load_env()
if not PROJECT:
    raise SystemExit("GOOGLE_CLOUD_PROJECT not set in backend/.env")

import httpx
from fastapi.testclient import TestClient

from github_infrastructure.change_records import GitHubApiError
from github_infrastructure.commit_worker import CommitWorker
from github_infrastructure.config import settings
from github_infrastructure.firestore_service import FirestoreChangeStore
from github_infrastructure.main import app
from github_infrastructure.pipeline import process_commit_event


def pick_real_commit(owner: str, repository: str) -> tuple[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    repo_resp = httpx.get(
        f"https://api.github.com/repos/{owner}/{repository}", headers=headers, timeout=30
    )
    repo_resp.raise_for_status()
    default_branch = repo_resp.json()["default_branch"]
    commits = httpx.get(
        f"https://api.github.com/repos/{owner}/{repository}/commits",
        headers=headers,
        params={"per_page": 20},
        timeout=30,
    )
    commits.raise_for_status()
    for item in commits.json():
        if len(item["parents"]) == 1:
            return item["sha"], default_branch
    raise SystemExit("no single-parent commit found")


def main() -> int:
    parser = argparse.ArgumentParser(description="Full pipeline E2E: webhook -> Pub/Sub -> CommitEvent -> GitHub API -> ChangeRecord -> Firestore")
    parser.add_argument("--owner", default="octocat")
    parser.add_argument("--repository", default="Hello-World")
    parser.add_argument("--sha", default="", help="Commit SHA (default: latest single-parent commit)")
    parser.add_argument("--branch", default="e2e-pipeline")
    args = parser.parse_args()

    sha = args.sha or pick_real_commit(args.owner, args.repository)[0]
    print(f"[target] real commit {args.owner}/{args.repository}@{sha[:12]}")

    worker = CommitWorker(project=PROJECT)
    worker.ensure_subscription()
    print(f"[worker] subscription ready: {worker.subscription_id}")

    delivery_id = str(uuid.uuid4())
    push_payload = {
        "ref": f"refs/heads/{args.branch}",
        "before": "0" * 40,
        "after": sha,
        "repository": {
            "full_name": f"{args.owner}/{args.repository}",
            "owner": {"login": args.owner},
            "default_branch": args.branch,
        },
        "pusher": {"name": "pipeline-e2e"},
        "sender": {"login": "pipeline-e2e"},
        "commits": [{"id": sha, "message": "real commit"}],
        "head_commit": {"id": sha},
    }
    body = json.dumps(push_payload).encode("utf-8")
    signature = "sha256=" + hmac.new(
        settings.webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()

    response = TestClient(app).post(
        "/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "pubsub", response.json()
    print(f"[webhook] signed push accepted, delivered to Pub/Sub ({delivery_id[:8]}...)")

    event = None
    from github_infrastructure.commit_events import CommitEvent

    for attempt in range(6):
        for candidate in worker.run_once(max_messages=20, timeout=15):
            if candidate.delivery_id == delivery_id:
                event = candidate
                break
        if event:
            break
        print(f"[worker] batch {attempt + 1}: not arrived yet...")
    assert event is not None, "delivery never arrived"
    assert isinstance(event, CommitEvent)
    print(f"[worker] CommitEvent normalized: {event.repository}@{event.after_sha[:10]} branch={event.branch}")

    results = process_commit_event(event)
    print(f"[pipeline] processed {len(results)} commit(s):")
    for result in results:
        print(f"    - {result.commit_sha[:10]} type={result.change_type} components={result.components} files={result.changed_files} doc={result.document_id}")

    store = FirestoreChangeStore(project=PROJECT)
    processed_sha = results[0].commit_sha
    saved = store.get_change_record(event.repository, processed_sha)
    assert saved is not None, "record missing in Firestore!"
    assert saved["commit_sha"] == processed_sha
    assert saved["repository"] == f"{args.owner}/{args.repository}"
    print(f"[firestore] verified record in DB '{store.database}':")
    print(json.dumps({k: saved[k] for k in ("commit_sha", "repository", "branch", "author", "change_type", "components", "saved_at")}, indent=2))

    print("\nFULL PIPELINE TEST PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GitHubApiError as exc:
        print(f"\nPIPELINE FAILED at GitHub API stage: {exc}")
        raise SystemExit(1)
