"""Telemetry configuration — env-driven with safe defaults.

Required for export (never logged):
  LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY  (Langfuse Cloud project keys)
Optional:
  LANGFUSE_HOST or LANGFUSE_BASE_URL (default https://cloud.langfuse.com)
  TELEMETRY_ENABLED    (default "true"; "false"/"0"/"no" disables)
  OTEL_SERVICE_NAME    (default "dev-coordination")
"""

from __future__ import annotations

import os


def enabled(default: bool = True) -> bool:
    raw = os.getenv("TELEMETRY_ENABLED", "true" if default else "false")
    return raw.strip().lower() not in ("false", "0", "no", "off", "")


def public_key() -> str:
    return os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()


def secret_key() -> str:
    return os.getenv("LANGFUSE_SECRET_KEY", "").strip()


def host() -> str:
    raw = os.getenv("LANGFUSE_HOST", "") or os.getenv("LANGFUSE_BASE_URL", "")
    return (raw.strip() or "https://cloud.langfuse.com").rstrip("/")


def service_name() -> str:
    return os.getenv("OTEL_SERVICE_NAME", "dev-coordination").strip() or "dev-coordination"


def otlp_traces_endpoint() -> str:
    return f"{host()}/api/public/otel/v1/traces"
