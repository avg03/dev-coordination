"""OpenTelemetry → Langfuse tracing for the change coordinator.

Fail-open by design: when keys are missing or OTel is unavailable every
helper degrades to a no-op so tracing can never break the pipeline
(same philosophy as the audit sink).
"""

from __future__ import annotations
