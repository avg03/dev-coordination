"""Commit readiness gate coordination package.

Event-driven dependency coordination between CommitWorker (push) and
PRWorker/PRWorkflow (pull_request), reusing existing Firestore, Pub/Sub,
PermissionGateway, IdempotencyManager, and audit infrastructure.

Same-topic strategy: commit-completion events are published to the existing
webhook topic (settings.pubsub_topic) with attribute event=commit.completed
(or commit.processing.failed). Existing CommitWorker/PRWorker filter on
event==push / event==pull_request and skip+ack anything else, so no new
topic is required and no existing consumer breaks.
"""

from __future__ import annotations
