from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any, Optional

from google.api_core.exceptions import AlreadyExists
from google.cloud import pubsub_v1

from github_infrastructure.config import settings

logger = logging.getLogger("workers.pr_worker")

try:
    from workflows.pr_workflow import PREvent, PRWorkflow
except ImportError:
    from workflows.pr_workflow import PREvent, PRWorkflow


class PRWorker:
    """Pub/Sub consumer for `pr-events-sub` that triggers PRWorkflow.run()."""

    def __init__(
        self,
        project: Optional[str] = None,
        subscription_id: str = "pr-events-sub",
        topic_id: str = "pr-events",
        workflow: Optional[PRWorkflow] = None,
    ) -> None:
        self.project = project or settings.gcp_project
        if not self.project:
            raise ValueError("GCP project is not configured")
        self.subscription_id = subscription_id or os.getenv("PR_EVENTS_SUBSCRIPTION", "pr-events-sub")
        self.topic_id = topic_id or os.getenv("PR_EVENTS_TOPIC", "pr-events")
        self.workflow = workflow  # injected for testing; created lazily otherwise
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
        # topic_path needs a publisher or subscriber client; subscriber can also generate it
        return self.subscriber.topic_path(self.project, self.topic_id)

    def _get_workflow(self) -> PRWorkflow:
        if self.workflow is None:
            self.workflow = PRWorkflow(project=self.project)
        return self.workflow

    def ensure_subscription(self) -> None:
        self._ensure_topic()
        try:
            self.subscriber.create_subscription(
                name=self.subscription_path,
                topic=self.topic_path,
            )
            logger.info("Created subscription %s on topic %s", self.subscription_id, self.topic_id)
        except AlreadyExists:
            pass

    def _ensure_topic(self) -> None:
        if self._publisher is None:
            self._publisher = pubsub_v1.PublisherClient()
        topic_path = self._publisher.topic_path(self.project, self.topic_id)
        try:
            self._publisher.create_topic(name=topic_path)
            logger.info("Created topic %s", self.topic_id)
        except AlreadyExists:
            pass

    def _decode_message(self, data: bytes) -> dict[str, Any]:
        if not data:
            raise ValueError("Empty Pub/Sub message data")
        # Subscriber client already base64-decodes, data is raw bytes
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Message data is not valid UTF-8: {exc}") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Message data is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Message JSON must be an object")
        return payload

    def _extract_pr_event_dict(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Direct PREvent shape: {project_id, owner, repository, pr_number, action}
        if {"project_id", "owner", "repository", "pr_number"}.issubset(payload.keys()):
            return payload
        # Wrapped shape from publisher: {metadata: {...}, payload: {...}}
        # Try to derive PREvent from metadata/payload
        metadata = payload.get("metadata") or {}
        raw_payload = payload.get("payload") or payload

        # Attempt to build PREvent from available fields
        # Fallback to raw_payload if it already looks like PREvent
        if isinstance(raw_payload, dict) and {"project_id", "pr_number"}.issubset(raw_payload.keys()):
            return raw_payload

        pr_number = metadata.get("pr_number") or raw_payload.get("number") or raw_payload.get("pr_number")
        # payload for pull_request webhook has pull_request.number and action
        if pr_number is None and isinstance(raw_payload.get("pull_request"), dict):
            pr_number = raw_payload["pull_request"].get("number")
        action = metadata.get("action") or raw_payload.get("action") or "opened"
        repo_full = metadata.get("repo") or ""
        owner = ""
        repository = ""
        if repo_full and "/" in repo_full:
            owner, repository = repo_full.split("/", 1)
        else:
            # Try repository object
            repo_obj = raw_payload.get("repository") or {}
            full_name = repo_obj.get("full_name") or ""
            if full_name and "/" in full_name:
                owner, repository = full_name.split("/", 1)
            else:
                owner = repo_obj.get("owner", {}).get("login", "") if isinstance(repo_obj.get("owner"), dict) else ""
                repository = repo_obj.get("name", "")

        project_id = payload.get("project_id") or metadata.get("project_id") or payload.get("projectId") or ""
        # If project_id missing, fall back to repo full name as project identifier
        if not project_id and repo_full:
            project_id = repo_full

        candidate = {
            "project_id": project_id,
            "owner": owner,
            "repository": repository,
            "pr_number": pr_number,
            "action": action,
        }
        # Only return if we have the required fields to validate
        if candidate["project_id"] and candidate["owner"] and candidate["repository"] and candidate["pr_number"] is not None:
            return candidate
        # Last resort: return original payload to let PREvent validation fail with clear error
        return payload

    def _validate_event(self, data: dict[str, Any]) -> PREvent:
        return PREvent.model_validate(data)

    async def _process_one(self, data: bytes) -> PREvent:
        raw = self._decode_message(data)
        pr_dict = self._extract_pr_event_dict(raw)
        pr_event = self._validate_event(pr_dict)
        logger.info(
            "Received PR event pr_number=%s repository=%s/%s action=%s project_id=%s",
            pr_event.pr_number,
            pr_event.owner,
            pr_event.repository,
            pr_event.action,
            pr_event.project_id,
        )
        workflow = self._get_workflow()
        await workflow.run(pr_event)
        logger.info(
            "PRWorkflow completed pr_number=%s repository=%s/%s action=%s",
            pr_event.pr_number,
            pr_event.owner,
            pr_event.repository,
            pr_event.action,
        )
        return pr_event

    def run_once(self, max_messages: int = 10, timeout: float = 30.0) -> int:
        """Pull-based processing for testing and cron. Acks only on success."""
        self.ensure_subscription()
        response = self.subscriber.pull(
            subscription=self.subscription_path,
            max_messages=max_messages,
            timeout=timeout,
        )

        ack_ids: list[str] = []
        for received in response.received_messages:
            try:
                # Decode, validate, and trigger workflow
                raw = self._decode_message(received.message.data)
                pr_dict = self._extract_pr_event_dict(raw)
                pr_event = self._validate_event(pr_dict)

                logger.info(
                    "Processing PR pr_number=%s repository=%s/%s action=%s",
                    pr_event.pr_number,
                    pr_event.owner,
                    pr_event.repository,
                    pr_event.action,
                )

                workflow = self._get_workflow()
                # Run async workflow from sync context
                asyncio.run(workflow.run(pr_event))

                # Ack only after workflow completes successfully
                ack_ids.append(received.ack_id)
                logger.info(
                    "Acknowledged PR pr_number=%s repository=%s/%s",
                    pr_event.pr_number,
                    pr_event.owner,
                    pr_event.repository,
                )
            except Exception as exc:
                logger.exception(
                    "Failed to process PR message %s; NOT acking for redelivery: %s",
                    received.message.message_id,
                    exc,
                )
                # Do NOT ack - message will be redelivered
                continue

        if ack_ids:
            self.subscriber.acknowledge(
                subscription=self.subscription_path,
                ack_ids=ack_ids,
            )
            logger.info("Acknowledged %d PR message(s)", len(ack_ids))
        else:
            logger.info("No PR messages acknowledged in this batch")
        return len(ack_ids)

    def start(self) -> None:
        """Streaming pull - for production use. Ack only after workflow success."""

        self.ensure_subscription()

        def _callback(message: pubsub_v1.subscriber.message.Message) -> None:
            try:
                raw = self._decode_message(message.data)
                pr_dict = self._extract_pr_event_dict(raw)
                pr_event = self._validate_event(pr_dict)

                logger.info(
                    "Streaming PR pr_number=%s repository=%s/%s action=%s",
                    pr_event.pr_number,
                    pr_event.owner,
                    pr_event.repository,
                    pr_event.action,
                )

                workflow = self._get_workflow()
                asyncio.run(workflow.run(pr_event))

                message.ack()
                logger.info(
                    "Acked streaming PR pr_number=%s repository=%s/%s",
                    pr_event.pr_number,
                    pr_event.owner,
                    pr_event.repository,
                )
            except Exception as exc:
                logger.exception(
                    "Failed to process streaming PR message %s; nacking for redelivery: %s",
                    message.message_id,
                    exc,
                )
                message.nack()

        streaming_future = self.subscriber.subscribe(self.subscription_path, callback=_callback)
        logger.info("Listening on %s (topic %s)", self.subscription_path, self.topic_path)
        try:
            streaming_future.result()
        except KeyboardInterrupt:
            streaming_future.cancel()
            logger.info("PR worker stopped by interrupt")
