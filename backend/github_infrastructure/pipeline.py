from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .change_records import (
    ChangeRecord,
    build_change_record,
    classify_change_type,
)
from .commit_events import CommitEvent, CommitEventError
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
