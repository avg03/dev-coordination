"""Telemetry tests — spans, token extraction, links, fail-open behavior.

Run: python backend/tests/test_telemetry.py (no GCP, no Langfuse keys needed)
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _test_tracer():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def _fake_event(prompt=0, candidates=0, total=0):
    usage = types.SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=candidates,
        total_token_count=total,
    )
    return types.SimpleNamespace(usage_metadata=usage)


def test_extract_token_usage_sums():
    from telemetry.spans import extract_token_usage

    usage = extract_token_usage([_fake_event(100, 50, 150), _fake_event(200, 100, 300)])
    assert usage == {
        "llm.token_count.prompt": 300,
        "llm.token_count.completion": 150,
        "llm.token_count.total": 450,
    }, usage


def test_extract_token_usage_empty_when_absent():
    from telemetry.spans import extract_token_usage

    assert extract_token_usage([]) == {}
    assert extract_token_usage([types.SimpleNamespace()]) == {}
    assert extract_token_usage([_fake_event(0, 0, 0)]) == {}


def test_agent_span_records_latency_tokens_outputs():
    from telemetry.spans import track_agent_run

    tracer, exporter = _test_tracer()
    with track_agent_run("impact_agent", {"project_id": "p1"}, tracer=tracer) as t:
        t.set_usage_from_events([_fake_event(1000, 450, 1450)])
        t.set_outputs({"output.affected_count": 4})
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, spans
    span = spans[0]
    assert span.name == "agent.impact_agent", span.name
    attrs = dict(span.attributes or {})
    assert attrs.get("llm.token_count.prompt") == 1000, attrs
    assert attrs.get("llm.token_count.completion") == 450, attrs
    assert attrs.get("llm.token_count.total") == 1450, attrs
    assert attrs.get("output.affected_count") == 4, attrs
    assert attrs.get("agent.name") == "impact_agent", attrs
    assert span.end_time is not None and span.end_time >= span.start_time  # latency


def test_agent_span_marks_unknown_tokens():
    from telemetry.spans import track_agent_run

    tracer, exporter = _test_tracer()
    with track_agent_run("coordination_agent", {}, tracer=tracer) as t:
        t.set_usage_from_events([])
        t.set_outputs({"output.tasks": 2})
    attrs = dict(exporter.get_finished_spans()[0].attributes or {})
    assert attrs.get("tokens.unknown") is True, attrs


def test_agent_span_error_status():
    from telemetry.spans import track_agent_run

    from opentelemetry.trace import StatusCode

    tracer, exporter = _test_tracer()
    try:
        with track_agent_run("communication_agent", {}, tracer=tracer):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR, span.status


def test_gate_span_shape_matches_spec():
    """pr.workflow -> commit.readiness_check with pending_commits, then agent spans."""
    from telemetry.spans import set_span_attrs, workflow_span

    tracer, exporter = _test_tracer()
    with workflow_span("pr.workflow", {"pr.number": 7}, tracer=tracer):
        with workflow_span("commit.readiness_check", {"gate.required_commits": 3}, tracer=tracer) as g:
            set_span_attrs(g, {"pending_commits": 2, "pr.status": "waiting"})
        with workflow_span("agent.impact_agent", {}, tracer=tracer):
            pass
    names = [s.name for s in exporter.get_finished_spans()]
    assert names == ["commit.readiness_check", "agent.impact_agent", "pr.workflow"], names
    gate = [s for s in exporter.get_finished_spans() if s.name == "commit.readiness_check"][0]
    assert dict(gate.attributes or {}).get("pending_commits") == 2


def test_make_link_round_trip():
    from telemetry.spans import current_span_ids, link_prior_trace, make_link, workflow_span

    assert make_link("", "") is None
    assert make_link("nothex", "123") is None
    link = make_link("a" * 32, "b" * 16)
    assert link is not None

    tracer, exporter = _test_tracer()
    with workflow_span("pr.workflow", {}, tracer=tracer):
        trace_id, span_id = current_span_ids()
        assert trace_id and span_id, (trace_id, span_id)
        # Links to the waiting trace without raising (no-op safe).
        link_prior_trace(trace_id, span_id)
        link_prior_trace("", "")
    assert len(exporter.get_finished_spans()) == 1


def test_pr_workflow_record_carries_trace_ids():
    from coordination.models import PRWorkflowRecord

    rec = PRWorkflowRecord(
        project_id="p", owner="o", repository="r", pr_number=1,
        head_sha="h", trace_id="a" * 32, span_id="b" * 16,
    )
    again = PRWorkflowRecord.model_validate(rec.model_dump(mode="json"))
    assert (again.trace_id, again.span_id) == ("a" * 32, "b" * 16)


def test_init_telemetry_fail_open_without_keys():
    import telemetry.tracer as tracer_mod
    from telemetry import config as tconfig

    assert tconfig.otlp_traces_endpoint().endswith("/api/public/otel/v1/traces")
    old_pub, old_sec, old_flag = os.environ.pop("LANGFUSE_PUBLIC_KEY", None), os.environ.pop("LANGFUSE_SECRET_KEY", None), os.getenv("TELEMETRY_ENABLED")
    os.environ["TELEMETRY_ENABLED"] = "false"
    try:
        assert tracer_mod.init_telemetry() is False
        # Helpers stay usable (no-op) when tracing is off.
        from telemetry.spans import track_agent_run

        with track_agent_run("impact_agent", {}) as t:
            t.set_usage_from_events([])
    finally:
        if old_pub is not None:
            os.environ["LANGFUSE_PUBLIC_KEY"] = old_pub
        if old_sec is not None:
            os.environ["LANGFUSE_SECRET_KEY"] = old_sec
        if old_flag is None:
            os.environ.pop("TELEMETRY_ENABLED", None)
        else:
            os.environ["TELEMETRY_ENABLED"] = old_flag


if __name__ == "__main__":
    test_extract_token_usage_sums()
    test_extract_token_usage_empty_when_absent()
    test_agent_span_records_latency_tokens_outputs()
    test_agent_span_marks_unknown_tokens()
    test_agent_span_error_status()
    test_gate_span_shape_matches_spec()
    test_make_link_round_trip()
    test_pr_workflow_record_carries_trace_ids()
    test_init_telemetry_fail_open_without_keys()
    print("=== ALL TELEMETRY TESTS PASSED ===")
