"""OTel SDK bootstrap: provider + Langfuse Cloud OTLP exporter + ADK auto-spans.

Idempotent and fail-open: returns False (tracing stays no-op) when disabled,
keys are missing, or any import/exporter setup fails. Never raises.
"""

from __future__ import annotations

import base64
import logging
import threading

logger = logging.getLogger("telemetry.tracer")

_initialized = False
_lock = threading.Lock()


def init_telemetry() -> bool:
    """Configure the global TracerProvider once. Safe to call everywhere."""
    global _initialized
    with _lock:
        if _initialized:
            return True
        try:
            from telemetry import config as tconfig

            if not tconfig.enabled():
                logger.info("Telemetry disabled via TELEMETRY_ENABLED")
                return False
            if not tconfig.public_key() or not tconfig.secret_key():
                logger.warning(
                    "Telemetry disabled — set LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY in backend/.env"
                )
                return False

            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import SERVICE_NAME, Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            basic = base64.b64encode(
                f"{tconfig.public_key()}:{tconfig.secret_key()}".encode("utf-8")
            ).decode("ascii")
            exporter = OTLPSpanExporter(
                endpoint=tconfig.otlp_traces_endpoint(),
                headers={"Authorization": f"Basic {basic}"},
                timeout=10,
            )
            provider = TracerProvider(
                resource=Resource.create({SERVICE_NAME: tconfig.service_name()})
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            logger.info("Telemetry exporting to %s", tconfig.otlp_traces_endpoint())
        except Exception as exc:
            logger.warning("Telemetry init failed — continuing without tracing: %s", exc)
            return False

        # Auto-spans for every ADK Runner invocation (invoke_agent + llm spans
        # with llm.token_count.* when the model reports usage). Independent of
        # the exporter above; best-effort.
        try:
            from openinference.instrumentation.google_adk import GoogleADKInstrumentor

            GoogleADKInstrumentor().instrument()
            logger.info("GoogleADKInstrumentor enabled")
        except Exception as exc:
            logger.warning("ADK auto-instrumentation unavailable: %s", exc)

        _initialized = True
        return True


def flush_telemetry(timeout_millis: int = 5000) -> None:
    """Best-effort flush (e.g. before process exit). Never raises."""
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        flush = getattr(provider, "force_flush", None)
        if callable(flush):
            flush(timeout_millis=timeout_millis)
    except Exception:
        pass
