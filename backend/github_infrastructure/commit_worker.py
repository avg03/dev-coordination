from __future__ import annotations

import logging
import os
from typing import Any, Optional

from google.api_core.exceptions import AlreadyExists
from google.cloud import pubsub_v1

from .commit_events import CommitEvent, CommitEventError, process_pubsub_commit_message
from .config import settings

logger = logging.getLogger("github.commit_worker")


class CommitWorker:
    """Pulls push notifications from Pub/Sub and normalizes them into CommitEvents."""

    def __init__(
        self,
        project: Optional[str] = None,
        subscription_id: Optional[str] = None,
    ) -> None:
        self.project = project or settings.gcp_project
        if not self.project:
            raise ValueError("GCP project is not configured")
        self.subscription_id = (
            subscription_id
            or os.getenv("GITHUB_EVENTS_SUBSCRIPTION")
            or f"{settings.pubsub_topic}-sub"
        )
        self._subscriber: Optional[pubsub_v1.SubscriberClient] = None
        self._publisher: Optional[pubsub_v1.PublisherClient] = None

    @property
    def subscriber(self) -> pubsub_v1.SubscriberClient:
        if self._subscriber is None:
            self._subscriber = pubsub_v1.SubscriberClient()
        return self._subscriber

    @property
    def subscription_path(self) -> str:
        return self.subscriber.subscription_path(self.project, self.subscription_id)

    def ensure_subscription(self) -> None:
        self._ensure_topic()
        try:
            self.subscriber.create_subscription(
                name=self.subscription_path,
                topic=self.subscriber.topic_path(self.project, settings.pubsub_topic),
            )
            logger.info("Created subscription %s", self.subscription_id)
        except AlreadyExists:
            pass

    def _ensure_topic(self) -> None:
        if self._publisher is None:
            self._publisher = pubsub_v1.PublisherClient()
        topic_path = self._publisher.topic_path(self.project, settings.pubsub_topic)
        try:
            self._publisher.create_topic(name=topic_path)
            logger.info("Created topic %s", settings.pubsub_topic)
        except AlreadyExists:
            pass

    def handle_pull_item(self, pull_item: dict[str, Any]) -> Optional[CommitEvent]:
        inner = pull_item.get("message") or {}
        attributes = {key: str(value) for key, value in (inner.get("attributes") or {}).items()}
        if attributes.get("event") != "push":
            logger.info("Skipping non-push event '%s' (PR normalization comes later)", attributes.get("event"))
            return None
        return process_pubsub_commit_message(pull_item)

    def run_once(self, max_messages: int = 10, timeout: float = 30.0) -> list[CommitEvent]:
        self.ensure_subscription()
        response = self.subscriber.pull(
            subscription=self.subscription_path,
            max_messages=max_messages,
            timeout=timeout,
        )

        events: list[CommitEvent] = []
        processed_ack_ids: list[str] = []

        for received in response.received_messages:
            item = {
                "subscription": self.subscription_path,
                "message": {
                    "data": received.message.data,
                    "attributes": dict(received.message.attributes),
                    "messageId": received.message.message_id,
                },
            }
            try:
                event = self.handle_pull_item(item)
            except CommitEventError as exc:
                logger.error(
                    "Discarding malformed delivery %s: %s",
                    received.message.message_id,
                    exc,
                )
            else:
                if event is not None:
                    events.append(event)
            processed_ack_ids.append(received.ack_id)

        if processed_ack_ids:
            self.subscriber.acknowledge(
                subscription=self.subscription_path,
                ack_ids=processed_ack_ids,
            )
        logger.info("Batch complete: %d commit event(s) normalized", len(events))
        return events
