from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from .change_records import (
    ChangeRecord,
    build_change_record,
    classify_change_type,
)
from .commit_events import CommitEvent, CommitEventError
from .config import settings
from .firestore_service import FirestoreChangeStore, get_change_store

# Commit readiness gate (event-driven PR dependency coordination). All imports
# are lazy-safe: coordination/ has no dependency on this module.
try:
    from coordination.events import build_completion_event, publish_completion_event
    from coordination.gate import classify_commit_error
    from coordination.models import CommitProcessingRecord
    from coordination.stores import (
        FirestoreCommitStatusStore,
        FirestoreOutboxStore,
    )
    _GATE_AVAILABLE = True
except ImportError:  # pragma: no cover — tests inject fakes instead
    _GATE_AVAILABLE = False

try:
    from utils.component_extractor import extract_components as smart_extract_components
except ImportError:
    smart_extract_components = None

logger = logging.getLogger("github.pipeline")


@dataclass
class PipelineResult:
    commit_sha: str
    document_id: str
    change_type: str
    components: list[str]
    changed_files: int


def process_commit_event(
    event: CommitEvent,
    store: Optional[FirestoreChangeStore] = None,
    *,
    status_store: object = None,
    outbox_store: object = None,
    publish_fn: object = None,
    project_ids: Optional[list[str]] = None,
) -> list[PipelineResult]:
    store = store or get_change_store()
    owner, _, repo_name = event.repository.partition("/")
    if not repo_name:
        raise CommitEventError(f"repository '{event.repository}' is not in owner/name form")

    shas = event.commit_shas or ([event.after_sha] if event.after_sha else [])
    if not shas:
        raise CommitEventError("no commit SHAs to process")

    # Resolve enrichable projects once (injectable for tests).
    known_projects = project_ids if project_ids is not None else _get_all_project_ids()

    results: list[PipelineResult] = []
    for sha in shas:
        try:
            record = _build_and_enrich(owner, repo_name, sha, event.branch)
        except Exception as exc:
            # Raw fetch failed — nothing durable to enrich. Record FAILED per
            # known project so the PR gate can classify it (no event: the
            # CommitWorker poison-pill path still acks 404s to discard fakes).
            _mark_failed_for_projects(
                event, sha, known_projects, exc, status_store=status_store
            )
            raise
        doc_id = store.save_change_record(record)
        logger.info(
            "Pipeline complete for %s@%s [%s] -> %s",
            event.repository,
            sha[:10],
            record.change_type,
            doc_id,
        )
        # --- Enriched record for Work Journey (push → enriched) — single-repo test mode ---
        # Keep same Pub/Sub worker, no new topic. Failure must NOT break change_records.
        # Readiness gate: per-project status lifecycle + completion event AFTER
        # the enriched record is durable (COMPLETED -> publish).
        _enrich_with_gate(
            record,
            known_projects,
            status_store=status_store,
            outbox_store=outbox_store,
            publish_fn=publish_fn,
        )
        results.append(
            PipelineResult(
                commit_sha=record.commit_sha,
                document_id=doc_id,
                change_type=record.change_type,
                components=record.components,
                changed_files=len(record.changed_files),
            )
        )
    return results


def _get_all_project_ids() -> list[str]:
    """One-repo test: enrich for every project_context so Work Journey works whichever project_id the UI queries (campuscart, authentication-redesign, etc.)."""
    try:
        store = get_change_store()
        client = store.client
        docs = list(client.collection("project_contexts").stream())
        if docs:
            return [d.id for d in docs]
        logger.warning("Enrichment skipped — no project_contexts found. Create one via POST /api/projects/context first.")
        return []
    except Exception as exc:  # pragma: no cover
        logger.warning("Enrichment skipped — could not list project_contexts: %s", exc)
        return []


def _try_create_enriched(record: ChangeRecord) -> None:
    """Backward-compatible wrapper: enrich for all known projects with gate."""
    _enrich_with_gate(record, _get_all_project_ids())


def _enrich_with_gate(
    record: ChangeRecord,
    project_ids: list[str],
    *,
    status_store: object = None,
    outbox_store: object = None,
    publish_fn: object = None,
) -> None:
    """Create enriched_changes/{sha} under projects/{project_id} without breaking pipeline — one-repo test writes to all projects.

    Gate ordering per (project, sha):
      ensure PENDING -> mark PROCESSING -> persist enriched ->
      mark COMPLETED -> publish completion event (outbox on failure).
    """
    if not project_ids:
        return
    # Lazy import once
    try:
        try:
            from agents.change_analysis_agent import ChangeAnalysisAgent
        except ImportError:
            from backend.agents.change_analysis_agent import ChangeAnalysisAgent  # type: ignore
    except ImportError as exc:
        logger.warning("Enrichment skipped — ChangeAnalysisAgent not importable: %s", exc)
        return

    for project_id in project_ids:
        _mark_processing(record, project_id, status_store=status_store)
        try:
            from telemetry.spans import set_span_attrs as _set_span_attrs
            from telemetry.spans import workflow_span as _workflow_span

            with _workflow_span(
                "commit.enrich",
                {
                    "project_id": project_id,
                    "repo": record.repository,
                    "commit.sha": record.commit_sha[:12],
                },
            ) as _espan:
                agent = ChangeAnalysisAgent(
                    project=settings.gcp_project,
                    database=os.getenv("FIRESTORE_DATABASE", "(default)"),
                )
                enriched, path, saved_by_tool = agent.analyze(project_id, record)
                _set_span_attrs(
                    _espan,
                    {
                        "change.type": getattr(enriched, "change_type", ""),
                        "output.confidence": float(getattr(enriched, "confidence", 0.0) or 0.0),
                        "commit.status": "completed",
                    },
                )
            logger.info(
                "Enriched record created for %s@%s -> %s (saved_by_tool=%s)",
                record.repository,
                record.commit_sha[:10],
                path,
                saved_by_tool,
            )
        except ValueError as exc:
            logger.warning("Enrichment skipped (validation) project=%s: %s", project_id, exc)
            _mark_failed(record, project_id, exc, status_store=status_store)
            _publish_status_event(
                record, project_id, "FAILED", status_store=status_store,
                outbox_store=outbox_store, publish_fn=publish_fn,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("Enrichment failed for %s@%s project=%s: %s", record.repository, record.commit_sha[:10], project_id, exc, exc_info=True)
            # continue to next project_id — one failure must not block others or change_records
            _mark_failed(record, project_id, exc, status_store=status_store)
            _publish_status_event(
                record, project_id, "FAILED", status_store=status_store,
                outbox_store=outbox_store, publish_fn=publish_fn,
            )
        else:
            # Enriched record is durable (agent persisted) — now COMPLETED, then event.
            _mark_completed(record, project_id, status_store=status_store)
            _publish_status_event(
                record, project_id, "COMPLETED", status_store=status_store,
                outbox_store=outbox_store, publish_fn=publish_fn,
            )


def _build_and_enrich(
    owner: str,
    repo_name: str,
    sha: str,
    branch: str,
) -> ChangeRecord:
    record = build_change_record(owner, repo_name, sha, branch=branch)

    if smart_extract_components is not None:
        enriched = smart_extract_components(record)
        if enriched:
            record.components = enriched

    reclassified = classify_change_type(record)
    if reclassified != "unknown":
        record.change_type = reclassified

    return record


# ---------------------------------------------------------------------------
# Readiness-gate status helpers (Firestore-backed, gateway-authorized)
# ---------------------------------------------------------------------------

def _get_status_store() -> object:
    if not _GATE_AVAILABLE:
        return None
    try:
        return FirestoreCommitStatusStore(project=settings.gcp_project)
    except Exception:
        logger.warning("Commit status store unavailable — gate checks will treat commits as missing")
        return None


def _get_outbox_store() -> object:
    if not _GATE_AVAILABLE:
        return None
    try:
        return FirestoreOutboxStore(project=settings.gcp_project)
    except Exception:
        return None


def _authorize_gate(action: str, resource: str, project_id: str, workflow_id: str) -> bool:
    """Gateway check for gate operations. Fail-open with warning when the
    gateway itself is unavailable (never block change persistence on audit)."""
    try:
        from security.gateway import get_permission_gateway
        from security.identities import Action, AgentIdentity, Resource
        from security.permissions import AuthorizationRequest

        gateway = get_permission_gateway()
        decision = gateway.authorize(
            AuthorizationRequest(
                agent_id=AgentIdentity.COMMIT_WORKER.value,
                action=Action(action),
                resource=Resource(resource),
                project_id=project_id,
                workflow_id=workflow_id,
            )
        )
        if not decision.allowed:
            logger.warning(
                "Gate operation denied action=%s resource=%s project=%s: %s",
                action, resource, project_id, decision.reason,
            )
        return decision.allowed
    except Exception:
        logger.warning("Gate authorization unavailable — proceeding (audit best-effort)")
        return True


def _audit_gate_transition(project_id: str, workflow_id: str, reason: str, status: str) -> None:
    try:
        from security.audit import AuditEvent, AuditLogger

        AuditLogger().record(
            AuditEvent(
                agent_id="commit_worker",
                action="write",
                resource="commit_status",
                project_id=project_id,
                workflow_id=workflow_id,
                decision=status,
                reason=reason,
                policy="commit_readiness_gate",
                event_type="commit.status.transition",
            )
        )
    except Exception:
        pass


def _mark_processing(record: ChangeRecord, project_id: str, *, status_store: object = None) -> None:
    if not _GATE_AVAILABLE:
        return
    store = status_store if status_store is not None else _get_status_store()
    if store is None:
        return
    if not _authorize_gate("write", "commit_status", project_id, f"commit:{project_id}:{record.commit_sha[:8]}"):
        return
    try:
        existing = store.get(project_id, record.repository, record.commit_sha)  # type: ignore[attr-defined]
        attempt = int(getattr(existing, "attempt_count", 0) or 0) + 1
        if existing is not None and getattr(existing, "status", "") == "COMPLETED":
            return  # reprocessing a completed commit reuses output — keep COMPLETED
        store.put(CommitProcessingRecord(  # type: ignore[attr-defined]
            project_id=project_id,
            repository=record.repository,
            commit_sha=record.commit_sha,
            status="PROCESSING",
            stage="enrich",
            attempt_count=attempt,
        ))
        _audit_gate_transition(project_id, record.commit_sha, f"PROCESSING attempt={attempt}", "executed")
    except Exception:
        logger.exception("Failed to mark PROCESSING for %s", record.commit_sha[:8])


def _mark_completed(record: ChangeRecord, project_id: str, *, status_store: object = None) -> None:
    if not _GATE_AVAILABLE:
        return
    store = status_store if status_store is not None else _get_status_store()
    if store is None:
        return
    if not _authorize_gate("write", "commit_status", project_id, f"commit:{project_id}:{record.commit_sha[:8]}"):
        return
    try:
        existing = store.get(project_id, record.repository, record.commit_sha)  # type: ignore[attr-defined]
        attempt = int(getattr(existing, "attempt_count", 0) or 1)
        store.put(CommitProcessingRecord(  # type: ignore[attr-defined]
            project_id=project_id,
            repository=record.repository,
            commit_sha=record.commit_sha,
            status="COMPLETED",
            stage="enrich",
            attempt_count=attempt,
        ))
        _audit_gate_transition(project_id, record.commit_sha, "COMPLETED enriched durable", "executed")
    except Exception:
        logger.exception("Failed to mark COMPLETED for %s", record.commit_sha[:8])


def _mark_failed(
    record: ChangeRecord, project_id: str, exc: BaseException, *, status_store: object = None
) -> None:
    if not _GATE_AVAILABLE:
        return
    store = status_store if status_store is not None else _get_status_store()
    if store is None:
        return
    if not _authorize_gate("write", "commit_status", project_id, f"commit:{project_id}:{record.commit_sha[:8]}"):
        return
    try:
        kind, summary = classify_commit_error(exc)
        existing = store.get(project_id, record.repository, record.commit_sha)  # type: ignore[attr-defined]
        attempt = int(getattr(existing, "attempt_count", 0) or 1)
        store.put(CommitProcessingRecord(  # type: ignore[attr-defined]
            project_id=project_id,
            repository=record.repository,
            commit_sha=record.commit_sha,
            status="FAILED",
            stage="enrich",
            attempt_count=attempt,
            error=summary,
            error_kind=kind,  # type: ignore[arg-type]
        ))
        _audit_gate_transition(project_id, record.commit_sha, f"FAILED kind={kind}", "executed")
    except Exception:
        logger.exception("Failed to mark FAILED for %s", record.commit_sha[:8])


def _mark_failed_for_projects(
    event: CommitEvent, sha: str, project_ids: list[str], exc: BaseException, *, status_store: object = None
) -> None:
    """Raw fetch failed before any enriched record existed."""
    if not _GATE_AVAILABLE or not project_ids:
        return
    from .change_records import ChangeRecord as _CR

    for project_id in project_ids:
        try:
            pseudo = _CR(
                commit_sha=sha,
                repository=event.repository,
                owner=event.owner,
                branch=event.branch,
            )
            _mark_failed(pseudo, project_id, exc, status_store=status_store)
        except Exception:
            continue


def _default_publish_fn(message: dict, attributes: dict) -> str | None:
    from .publisher import get_publisher

    return get_publisher().publish(message, attributes)


def _publish_status_event(
    record: ChangeRecord,
    project_id: str,
    status: str,
    *,
    status_store: object = None,
    outbox_store: object = None,
    publish_fn: object = None,
) -> None:
    if not _GATE_AVAILABLE:
        return
    if not _authorize_gate("execute", "completion_events", project_id, f"commit:{project_id}:{record.commit_sha[:8]}"):
        return
    try:
        owner, _, _ = record.repository.partition("/")
        existing = None
        if status_store is not None:
            try:
                existing = status_store.get(project_id, record.repository, record.commit_sha)  # type: ignore[attr-defined]
            except Exception:
                existing = None
        event = build_completion_event(
            repository=record.repository,
            owner=owner,
            project_id=project_id,
            commit_sha=record.commit_sha,
            status=status,
            attempt_count=int(getattr(existing, "attempt_count", 0) or 0),
        )
        fn = publish_fn if publish_fn is not None else _default_publish_fn
        ob = outbox_store if outbox_store is not None else _get_outbox_store()
        key = CommitProcessingRecord.doc_id(project_id, record.repository, record.commit_sha)
        publish_completion_event(event, fn, outbox=ob, outbox_key=key)  # type: ignore[arg-type]
    except Exception:
        logger.exception("Failed to publish completion event for %s", record.commit_sha[:8])


def enriched_exists(project_id: str, commit_sha: str) -> bool:
    """Authoritative check: canonical EnrichedChangeRecord durable in Firestore."""
    try:
        store = get_change_store()
        snap = (
            store.client.collection("projects")
            .document(project_id)
            .collection("enriched_changes")
            .document(commit_sha)
            .get()
        )
        return bool(snap.exists)
    except Exception:
        logger.warning("enriched_exists check failed for %s/%s", project_id, commit_sha[:8])
        return False


def flush_completion_outbox(publish_fn: object = None, *, outbox_store: object = None) -> int:
    """Operational helper: publish queued completion events (reconciliation)."""
    if not _GATE_AVAILABLE:
        return 0
    from coordination.events import flush_outbox

    ob = outbox_store if outbox_store is not None else _get_outbox_store()
    if ob is None:
        return 0
    fn = publish_fn if publish_fn is not None else _default_publish_fn
    try:
        return flush_outbox(ob, fn)  # type: ignore[arg-type]
    except Exception:
        logger.exception("Outbox flush failed")
        return 0
