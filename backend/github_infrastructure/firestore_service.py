from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore

from .change_records import ChangeRecord
from .config import settings

logger = logging.getLogger("github.firestore")

CHANGE_RECORDS_COLLECTION = "change_records"


class FirestoreChangeStore:
    def __init__(
        self,
        project: Optional[str] = None,
        database: Optional[str] = None,
    ) -> None:
        self.project = project or settings.gcp_project
        if not self.project:
            raise ValueError("GCP project is not configured")
        self.database = database or os.getenv("FIRESTORE_DATABASE", "(default)")
        self._client: Optional[firestore.Client] = None

    @property
    def client(self) -> firestore.Client:
        if self._client is None:
            kwargs: dict = {"project": self.project}
            if self.database and self.database != "(default)":
                kwargs["database"] = self.database
            self._client = firestore.Client(**kwargs)
        return self._client

    @staticmethod
    def document_id(repository: str, commit_sha: str) -> str:
        repo_key = repository.replace("/", "__")
        return f"{repo_key}#{commit_sha}"

    def save_change_record(self, record: ChangeRecord) -> str:
        doc_id = self.document_id(record.repository, record.commit_sha)
        doc_ref = self.client.collection(CHANGE_RECORDS_COLLECTION).document(doc_id)
        payload = record.model_dump(mode="json")
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        existing = doc_ref.get().exists
        doc_ref.set(payload)
        logger.info("%s change record %s", "Updated" if existing else "Saved", doc_id)
        return doc_id

    def get_change_record(self, repository: str, commit_sha: str) -> Optional[dict]:
        doc_id = self.document_id(repository, commit_sha)
        snapshot = self.client.collection(CHANGE_RECORDS_COLLECTION).document(doc_id).get()
        return snapshot.to_dict() if snapshot.exists else None


_store: Optional[FirestoreChangeStore] = None


def get_change_store() -> FirestoreChangeStore:
    global _store
    if _store is None:
        _store = FirestoreChangeStore()
    return _store
