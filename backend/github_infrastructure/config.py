from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=ENV_FILE)


class Settings:
    def __init__(self) -> None:
        self.webhook_secret: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")
        self.gcp_project: str = (
            os.getenv("GITHUB_EVENTS_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT") or ""
        )
        self.pubsub_topic: str = os.getenv("GITHUB_EVENTS_TOPIC", "github-webhook-events")
        self._resolve_credentials_path()

    def _resolve_credentials_path(self) -> None:
        creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        if creds and not os.path.isabs(creds) and not Path(creds).exists():
            candidate = ENV_FILE.parent / creds
            if candidate.exists():
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(candidate)


settings = Settings()
