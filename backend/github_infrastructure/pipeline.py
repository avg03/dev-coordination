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
) -> list[PipelineResult]:
    store = store or get_change_store()
    owner, _, repo_name = event.repository.partition("/")
    if not repo_name:
        raise CommitEventError(f"repository '{event.repository}' is not in owner/name form")

    shas = event.commit_shas or ([event.after_sha] if event.after_sha else [])
    if not shas:
        raise CommitEventError("no commit SHAs to process")

    results: list[PipelineResult] = []
    for sha in shas:
        record = _build_and_enrich(owner, repo_name, sha, event.branch)
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
        _try_create_enriched(record)
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
    """Create enriched_changes/{sha} under projects/{project_id} without breaking pipeline — one-repo test writes to all projects."""
    project_ids = _get_all_project_ids()
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
        try:
            agent = ChangeAnalysisAgent(
                project=settings.gcp_project,
                database=os.getenv("FIRESTORE_DATABASE", "(default)"),
            )
            enriched, path, saved_by_tool = agent.analyze(project_id, record)
            logger.info(
                "Enriched record created for %s@%s -> %s (saved_by_tool=%s)",
                record.repository,
                record.commit_sha[:10],
                path,
                saved_by_tool,
            )
        except ValueError as exc:
            logger.warning("Enrichment skipped (validation) project=%s: %s", project_id, exc)
        except Exception as exc:  # pragma: no cover
            logger.warning("Enrichment failed for %s@%s project=%s: %s", record.repository, record.commit_sha[:10], project_id, exc, exc_info=True)
            # continue to next project_id — one failure must not block others or change_records


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
