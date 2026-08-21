from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=ENV_FILE)


class Settings:
    def __init__(self) -> None:
        self.webhook_secret: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")
        self.gcp_project: str = os.getenv(
            "GITHUB_EVENTS_PROJECT", os.getenv("GOOGLE_CLOUD_PROJECT", "")
        )
        self.pubsub_topic: str = os.getenv("GITHUB_EVENTS_TOPIC", "github-webhook-events")


settings = Settings()
