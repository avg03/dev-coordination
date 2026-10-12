from __future__ import annotations

import logging
import os
from typing import Any, Optional

from google.api_core.exceptions import AlreadyExists
from google.cloud import pubsub_v1

from .commit_events import CommitEvent, CommitEventError, process_pubsub_commit_message
from .config import settings

try:
    from .pipeline import process_commit_event
except ImportError:
    process_commit_event = None  # type: ignore

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

    @property
    def topic_path(self) -> str:
        if self._publisher is None:
            self._publisher = pubsub_v1.PublisherClient()
        return self._publisher.topic_path(self.project, settings.pubsub_topic)

    def ensure_subscription(self) -> None:
        try:
            from telemetry.tracer import init_telemetry

            init_telemetry()
        except Exception:
            pass
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
        if process_commit_event is None:
            raise RuntimeError("pipeline.process_commit_event not available")

        self.ensure_subscription()
        response = self.subscriber.pull(
            subscription=self.subscription_path,
            max_messages=max_messages,
            timeout=timeout,
        )

        events: list[CommitEvent] = []
        ack_ids: list[str] = []

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
                ack_ids.append(received.ack_id)
                continue

            if event is None:
                ack_ids.append(received.ack_id)
                continue

            try:
                results = process_commit_event(event)
                logger.info(
                    "Pipeline persisted %d record(s) for %s@%s",
                    len(results),
                    event.repository,
                    event.after_sha[:7] if event.after_sha else "",
                )
            except Exception as exc:
                # 404 from GitHub API means commit/repo doesn't exist (e.g., fake test data acme/widget)
                # Ack to avoid poison-pill loop; transient errors (5xx, network) will be retried via NOT acking
                is_not_found = getattr(exc, "status_code", None) == 404
                if is_not_found:
                    logger.warning(
                        "Pipeline 404 for delivery %s (%s@%s) - acking to discard poison pill: %s",
                        received.message.message_id,
                        event.repository,
                        event.after_sha[:7] if event.after_sha else "",
                        exc,
                    )
                    events.append(event)
                    ack_ids.append(received.ack_id)
                    continue
                logger.exception(
                    "Pipeline failed for delivery %s (%s@%s) - NOT acking for redelivery: %s",
                    received.message.message_id,
                    event.repository,
                    event.after_sha[:7] if event.after_sha else "",
                    exc,
                )
                continue

            events.append(event)
            ack_ids.append(received.ack_id)

        if ack_ids:
            self.subscriber.acknowledge(
                subscription=self.subscription_path,
                ack_ids=ack_ids,
            )
        logger.info(
            "Batch complete: %d commit event(s) normalized and persisted", len(events)
        )
        return events

    def start(self) -> None:
        """Streaming pull that normalizes and persists via pipeline. Ack only after pipeline success."""
        if process_commit_event is None:
            raise RuntimeError("pipeline.process_commit_event not available")

        self.ensure_subscription()

        def _callback(message) -> None:
            item = {
                "subscription": self.subscription_path,
                "message": {
                    "data": message.data,
                    "attributes": dict(message.attributes),
                    "messageId": message.message_id,
                },
            }
            try:
                event = self.handle_pull_item(item)
            except CommitEventError as exc:
                logger.error("Discarding malformed delivery %s: %s", message.message_id, exc)
                message.ack()
                return

            if event is None:
                message.ack()
                return

            try:
                process_commit_event(event)
            except Exception as exc:
                is_not_found = getattr(exc, "status_code", None) == 404
                if is_not_found:
                    logger.warning(
                        "Pipeline 404 for delivery %s - acking to discard poison pill: %s",
                        message.message_id,
                        exc,
                    )
                    message.ack()
                    return
                logger.exception(
                    "Pipeline failed for delivery %s - nacking for redelivery: %s",
                    message.message_id,
                    exc,
                )
                message.nack()
                return

            message.ack()
            logger.info(
                "Acked streaming commit %s@%s", event.repository, event.after_sha[:7] if event.after_sha else ""
            )

        streaming_future = self.subscriber.subscribe(self.subscription_path, callback=_callback)
        logger.info("Listening on %s (topic %s)", self.subscription_path, self.topic_path)
        try:
            streaming_future.result()
        except KeyboardInterrupt:
            streaming_future.cancel()
            logger.info("Commit worker stopped by interrupt")
