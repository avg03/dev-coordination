"""Span helpers: manual spans with latency + tokens + output summaries.

All helpers are fail-open (no-op spans when OTel is missing/disabled) and
never log secrets. Only IDs, counts, and short status strings are recorded
as attributes — never message bodies, diffs, or credentials.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger("telemetry.spans")

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import Link as _OtelLink
    from opentelemetry.trace import NonRecordingSpan as _NonRecordingSpan
    from opentelemetry.trace import SpanContext as _SpanContext
    from opentelemetry.trace import SpanKind as _SpanKind
    from opentelemetry.trace import Status as _Status
    from opentelemetry.trace import StatusCode as _StatusCode

    _OTEL = True
except ImportError:  # pragma: no cover — offline envs without OTel
    _otel_trace = None  # type: ignore[assignment]
    _OTEL = False

_MAX_ATTR_LEN = 500


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_ATTR_LEN:
        return value[:_MAX_ATTR_LEN] + "…"
    if isinstance(value, (list, tuple)):
        return [_short(v) for v in value[:20]]
    return value


def _set_attrs(span: Any, attributes: dict[str, Any]) -> None:
    if span is None:
        return
    try:
        for key, value in (attributes or {}).items():
            if value is None:
                continue
            if isinstance(value, bool):
                span.set_attribute(key, value)
            elif isinstance(value, (int, float)):
                span.set_attribute(key, value)
            elif isinstance(value, str):
                span.set_attribute(key, _short(value))
            elif isinstance(value, (list, tuple)):
                flat = [v for v in (_short(value) or []) if isinstance(v, (bool, int, float, str))]
                if flat:
                    span.set_attribute(key, flat)
            else:
                span.set_attribute(key, _short(str(value)))
    except Exception:
        pass


def extract_token_usage(events: list[Any]) -> dict[str, Any]:
    """Best-effort token totals from ADK Event.usage_metadata.

    Sums prompt/candidates/total across all events that report usage.
    Returns {} when nothing reports usage (caller records tokens.unknown).
    """
    prompt = 0
    completion = 0
    total = 0
    seen = False
    for event in events or []:
        usage = getattr(event, "usage_metadata", None)
        if usage is None:
            continue
        try:
            p = int(getattr(usage, "prompt_token_count", 0) or 0)
            c = int(getattr(usage, "candidates_token_count", 0) or 0)
            t = int(getattr(usage, "total_token_count", 0) or 0)
        except (TypeError, ValueError):
            continue
        if p or c or t:
            seen = True
            prompt += p
            completion += c
            total += t or (p + c)
    if not seen:
        return {}
    return {
        "llm.token_count.prompt": prompt,
        "llm.token_count.completion": completion,
        "llm.token_count.total": total or (prompt + completion),
    }


class _Tracker:
    """Handle yielded by track_agent_run for usage/output recording."""

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_usage_from_events(self, events: list[Any]) -> None:
        usage = extract_token_usage(events)
        if usage:
            _set_attrs(self._span, usage)
        else:
            _set_attrs(self._span, {"tokens.unknown": True})

    def set_outputs(self, outputs: dict[str, Any]) -> None:
        _set_attrs(self._span, outputs)


class _NoOpTracker(_Tracker):
    def __init__(self) -> None:
        super().__init__(None)


def _get_tracer(name: str, tracer: Any = None) -> Any:
    if tracer is not None:
        return tracer
    if not _OTEL:
        return None
    try:
        return _otel_trace.get_tracer(name)
    except Exception:
        return None


@contextmanager
def track_agent_run(
    agent_name: str,
    attributes: Optional[dict[str, Any]] = None,
    tracer: Any = None,
) -> Iterator[_Tracker]:
    """Span `agent.{name}` around one LLM invocation. Latency is automatic
    (span start→end); tokens/outputs via the yielded tracker."""
    tr = _get_tracer("dev-coordination.agents", tracer)
    if tr is None:
        yield _NoOpTracker()
        return
    try:
        span = tr.start_span(f"agent.{agent_name}", kind=_SpanKind.INTERNAL)
    except Exception:
        yield _NoOpTracker()
        return
    tracker = _Tracker(span)
    _set_attrs(span, {"agent.name": agent_name, **(attributes or {})})
    try:
        with _otel_trace.use_span(span, end_on_exit=False):
            try:
                yield tracker
            except Exception as exc:
                try:
                    span.set_status(_Status(_StatusCode.ERROR, str(exc)[:500]))
                    span.record_exception(exc)
                except Exception:
                    pass
                raise
            else:
                try:
                    span.set_status(_Status(_StatusCode.OK))
                except Exception:
                    pass
    finally:
        try:
            span.end()
        except Exception:
            pass


@contextmanager
def workflow_span(
    name: str,
    attributes: Optional[dict[str, Any]] = None,
    links: Optional[list[Any]] = None,
    tracer: Any = None,
) -> Iterator[Any]:
    """Generic workflow span (PR root, gate, blast radius, commit enrich)
    yielding the raw span. Latency is automatic."""
    tr = _get_tracer("dev-coordination.workflows", tracer)
    if tr is None:
        yield None
        return
    try:
        span = tr.start_span(name, kind=_SpanKind.INTERNAL, links=links or None)
    except Exception:
        yield None
        return
    _set_attrs(span, attributes or {})
    try:
        with _otel_trace.use_span(span, end_on_exit=False):
            try:
                yield span
            except Exception as exc:
                try:
                    span.set_status(_Status(_StatusCode.ERROR, str(exc)[:500]))
                    span.record_exception(exc)
                except Exception:
                    pass
                raise
            else:
                try:
                    span.set_status(_Status(_StatusCode.OK))
                except Exception:
                    pass
    finally:
        try:
            span.end()
        except Exception:
            pass


def set_span_attrs(span: Any, attributes: dict[str, Any]) -> None:
    _set_attrs(span, attributes)


def annotate_current_span(attributes: dict[str, Any]) -> None:
    """Set attributes on whatever span is current (no-op when none)."""
    if not _OTEL:
        return
    try:
        span = _otel_trace.get_current_span()
        if isinstance(span, _NonRecordingSpan):
            return
        _set_attrs(span, attributes)
    except Exception:
        pass


def make_link(trace_id_hex: str, span_id_hex: str) -> Any:
    """OTel Link to a prior trace (wait→resume). Returns None when invalid."""
    if not _OTEL or not trace_id_hex or not span_id_hex:
        return None
    try:
        ctx = _SpanContext(
            trace_id=int(trace_id_hex, 16),
            span_id=int(span_id_hex, 16),
            is_remote=True,
        )
        if not ctx.is_valid:
            return None
        return _OtelLink(ctx)
    except (ValueError, TypeError):
        return None


def link_prior_trace(trace_id_hex: str, span_id_hex: str) -> None:
    """Attach a link from the current span to a prior (waiting) trace."""
    if not _OTEL:
        return
    try:
        link = make_link(trace_id_hex, span_id_hex)
        if link is None:
            return
        span = _otel_trace.get_current_span()
        if isinstance(span, _NonRecordingSpan):
            return
        add_link = getattr(span, "add_link", None)
        if callable(add_link):
            add_link(link.context, link.attributes)
    except Exception:
        pass


def current_span_ids() -> tuple[Optional[str], Optional[str]]:
    """(trace_id, span_id) hex of the current span, or (None, None)."""
    if not _OTEL:
        return None, None
    try:
        ctx = _otel_trace.get_current_span().get_span_context()
        if ctx is None or not ctx.is_valid:
            return None, None
        return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:
        return None, None
