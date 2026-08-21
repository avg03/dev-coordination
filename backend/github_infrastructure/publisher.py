from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .config import settings

logger = logging.getLogger("github.publisher")


class WebhookPublisher:
    def __init__(self, project: str, topic_id: str) -> None:
        self.project = project
        self.topic_id = topic_id
        self._client: Any = None
        
    @property
    def enabled(self) -> bool:
        return bool(self.project and self.topic_id)

    def publish(
        self, message: dict[str, Any], attributes: dict[str, str]
    ) -> Optional[str]:
        if not self.enabled:
            logger.warning(
                "Pub/Sub not configured (set GITHUB_EVENTS_PROJECT/GOOGLE_CLOUD_PROJECT); "
                "event handled in local-dev mode: %s",
                attributes,
            )
            return None
        client = self._get_client()
        topic_path = client.topic_path(self.project, self.topic_id)
        future = client.publish(
            topic_path,
            json.dumps(message, default=str).encode("utf-8"),
            **attributes,
        )
        return future.result(timeout=30)

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import pubsub_v1

            self._client = pubsub_v1.PublisherClient()
        return self._client


_publisher: Optional[WebhookPublisher] = None


def get_publisher() -> WebhookPublisher:
    global _publisher
    if _publisher is None:
        _publisher = WebhookPublisher(settings.gcp_project, settings.pubsub_topic)
    return _publisher
