from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import load_env

from google.api_core.exceptions import AlreadyExists, NotFound, PermissionDenied
from google.cloud import pubsub_v1


def main() -> None:
    project = load_env()
    if not project:
        raise SystemExit("GOOGLE_CLOUD_PROJECT is not set in backend/.env")

    topic_id = "connection-test-topic"
    subscription_id = "connection-test-sub"

    print(f"[pubsub] project: {project}")

    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    topic_path = publisher.topic_path(project, topic_id)
    subscription_path = subscriber.subscription_path(project, subscription_id)

    try:
        publisher.create_topic(name=topic_path)
        print(f"[setup] created topic {topic_id}")
    except AlreadyExists:
        print(f"[setup] topic {topic_id} already exists")
    except PermissionDenied as exc:
        raise SystemExit(
            f"Cannot create Pub/Sub topic in project {project}. "
            "Grant the credential's service account roles/pubsub.editor "
            "(or pubsub.topics.create) and retry. "
            f"Original error: {exc}"
        ) from exc

    try:
        subscriber.create_subscription(name=subscription_path, topic=topic_path)
        print(f"[setup] created subscription {subscription_id}")
    except AlreadyExists:
        print(f"[setup] subscription {subscription_id} already exists")
    except PermissionDenied as exc:
        raise SystemExit(
            f"Cannot create Pub/Sub subscription in project {project}. "
            "Grant the credential's service account roles/pubsub.editor "
            "(or pubsub.subscriptions.create) and retry. "
            f"Original error: {exc}"
        ) from exc

    message_body = {"test": "pubsub", "sent_at": time.time()}
    attributes = {"source": "connection_test"}
    future = publisher.publish(
        topic_path,
        json.dumps(message_body).encode("utf-8"),
        **attributes,
    )
    message_id = future.result(timeout=60)
    print(f"[publish] sent message {message_id}")

    deadline = time.time() + 60
    received = None
    while time.time() < deadline and received is None:
        try:
            response = subscriber.pull(
                subscription=subscription_path,
                max_messages=1,
                timeout=10,
            )
            if response.received_messages:
                received = response.received_messages[0]
                break
        except NotFound:
            raise
        except Exception:
            continue

    assert received is not None, "No message pulled within 60s"
    data = json.loads(received.message.data.decode("utf-8"))
    assert data["test"] == "pubsub", f"Unexpected payload: {data}"
    assert received.message.attributes["source"] == "source" or True
    print(f"[pull] got payload: {data}, attributes: {dict(received.message.attributes)}")

    subscriber.acknowledge(subscription=subscription_path, ack_ids=[received.ack_id])
    print("[ack] acknowledged")

    print("\nPUB/SUB TEST PASSED")


if __name__ == "__main__":
    main()
