from __future__ import annotations


import json
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Header, HTTPException, Request

from .extractors import extract_metadata
from .publisher import get_publisher
from .security import verify_signature

logger = logging.getLogger("github.webhook")

ENV_FILE = Path("backend/.env")
load_dotenv(dotenv_path=ENV_FILE)
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

router = APIRouter(prefix="/github", tags=["github"])



@router.post("/webhook")
async def github_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(default=None),
    x_github_event: Optional[str] = Header(default=None),
    x_github_delivery: Optional[str] = Header(default=None),
) -> dict:
    body = await request.body()
    

    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="GITHUB_WEBHOOK_SECRET is not configured")
    if not verify_signature(WEBHOOK_SECRET, body, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid signature")

    event = (x_github_event or "").strip()
    delivery_id = (x_github_delivery or "").strip()

    if event == "ping":
        return {"status": "pong", "delivery_id": delivery_id}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    metadata = extract_metadata(event, delivery_id, payload)
    if metadata is None:
        logger.info("Ignoring unsupported event type '%s' (delivery %s)", event, delivery_id)
        return {"status": "ignored", "event": event, "delivery_id": delivery_id}

    message = {"metadata": metadata.model_dump(), "payload": payload}
    attributes = {
        "event": event,
        "delivery_id": delivery_id,
        "repo": metadata.repo,
    }

    try:
        message_id = get_publisher().publish(message, attributes)
    except Exception as exc:
        logger.exception("Failed to publish delivery %s to Pub/Sub", delivery_id)
        raise HTTPException(status_code=500, detail="Failed to publish event") from exc

    return {
        "status": "accepted",
        "mode": "pubsub" if message_id else "local-dev",
        "message_id": message_id,
        "event": event,
        "repo": metadata.repo,
        "branch": metadata.branch,
        "sha": metadata.sha,
        "pr_number": metadata.pr_number,
        "action": metadata.action,
        "delivery_id": delivery_id,
    }
