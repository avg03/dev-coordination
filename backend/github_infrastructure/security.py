from __future__ import annotations

import hashlib
import hmac

SIGNATURE_PREFIX = "sha256="


def compute_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header:
        return False
    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected, signature_header.strip().lower())
