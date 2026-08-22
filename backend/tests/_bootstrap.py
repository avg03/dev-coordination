from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]


def load_env() -> str:
    load_dotenv(dotenv_path=BACKEND_DIR / ".env")
    creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    if creds and not os.path.isabs(creds) and not Path(creds).exists():
        candidate = BACKEND_DIR / creds
        if candidate.exists():
            creds = str(candidate)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds
    return os.getenv("GOOGLE_CLOUD_PROJECT", "")


if __name__ == "__main__":
    print(load_env())
