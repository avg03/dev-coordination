from __future__ import annotations

import base64
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github_infrastructure.commit_events import (
    CommitEvent,
    CommitEventError,
    build_commit_event,
    decode_pubsub_message,
    process_pubsub_commit_message,
)

DELIVERY_ID = str(uuid.uuid4())
PUSHED_AT = "2026-08-22T12:00:00Z"

GITHUB_PUSH_PAYLOAD = {
    "ref": "refs/heads/feature/oauth",
    "before": "0000000000000000000000000000000000000001",
    "after": "0000000000000000000000000000000000000002",
    "repository": {
        "full_name": "acme/widget",
        "owner": {"login": "acme"},
    },
    "pusher": {"name": "rahul-dev"},
    "sender": {"login": "rahul-dev"},
    "commits": [
        {"id": "sha-111", "message": "start oauth"},
        {"id": "sha-222", "message": "add refresh tokens"},
    ],
    "head_commit": {"id": "sha-222", "timestamp": PUSHED_AT},
}

ENVELOPE = {
    "metadata": {
        "event": "push",
        "delivery_id": DELIVERY_ID,
        "repo": "acme/widget",
        "branch": "feature/oauth",
        "received_at": "2026-08-22T12:00:01+00:00",
    },
    "payload": GITHUB_PUSH_PAYLOAD,
}


def test_decode_base64_pull_item():
    encoded = base64.b64encode(json.dumps(ENVELOPE).encode()).decode()
    pull_item = {
        "subscription": "projects/golden-furnace-369010/subscriptions/github-webhook-events-sub",
        "message": {
            "data": encoded,
            "messageId": "1",
            "attributes": {"event": "push", "delivery_id": DELIVERY_ID},
        },
    }
    event = process_pubsub_commit_message(pull_item)
    assert isinstance(event, CommitEvent)
    assert event.delivery_id == DELIVERY_ID
    assert event.repository == "acme/widget"
    print("[PASS] pub/sub pull item (base64 data) ->", event.model_dump_json(indent=None))


def test_decoded_envelope():
    event = build_commit_event(ENVELOPE)
    assert event.event == "push"
    assert event.owner == "acme"
    assert event.branch == "feature/oauth"
    assert event.before_sha.endswith("001")
    assert event.after_sha.endswith("002")
    assert event.commit_shas == ["sha-111", "sha-222"]
    assert event.pusher == "rahul-dev"
    assert event.timestamp == datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    print("[PASS] decoded envelope ->", event.model_dump_json(indent=None))


def test_attributes_override_metadata():
    other = str(uuid.uuid4())
    event = build_commit_event(ENVELOPE, attributes={"delivery_id": other})
    assert event.delivery_id == other
    print("[PASS] attribute delivery_id overrides metadata")


def test_raw_payload_without_envelope():
    payload = dict(GITHUB_PUSH_PAYLOAD)
    event = build_commit_event({"payload": payload}, attributes={"delivery_id": DELIVERY_ID})
    assert event.branch == "feature/oauth"
    assert event.commit_shas == ["sha-111", "sha-222"]
    print("[PASS] raw push payload path")


def test_error_cases():
    for bad, expected in [
        ({}, "repository"),
        ({"payload": {"repository": {}}}, "full_name"),
    ]:
        try:
            build_commit_event(bad, attributes={"delivery_id": DELIVERY_ID})
            raise AssertionError(f"Expected CommitEventError for {bad}")
        except CommitEventError as exc:
            assert expected in str(exc), f"Wrong error: {exc}"
    no_ref_metadata = {k: v for k, v in ENVELOPE["metadata"].items() if k != "branch"}
    no_ref = {"metadata": no_ref_metadata, "payload": {k: v for k, v in GITHUB_PUSH_PAYLOAD.items() if k != "ref"}}
    try:
        build_commit_event(no_ref)
        raise AssertionError("Expected branch error")
    except CommitEventError as exc:
        assert "branch" in str(exc)
    try:
        decode_pubsub_message(base64.b64encode(b"not json").decode())
        raise AssertionError("Expected JSON error")
    except CommitEventError as exc:
        assert "JSON" in str(exc)
    print("[PASS] error cases raise CommitEventError")


if __name__ == "__main__":
    test_decode_base64_pull_item()
    test_decoded_envelope()
    test_attributes_override_metadata()
    test_raw_payload_without_envelope()
    test_error_cases()
    print("\nCOMMIT EVENT TESTS PASSED")
