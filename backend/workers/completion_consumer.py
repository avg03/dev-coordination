"""Completion-event consumer: resumes WAITING_FOR_COMMITS PRs when commits finish.

Shares the existing webhook topic (settings.pubsub_topic); filters
event==commit.completed / commit.failed. Existing CommitWorker/PRWorker
skip+ack these attributes, so no existing consumer breaks.

For each valid event:
  validate -> dedupe (idempotency) -> find waiting PRs containing the SHA ->
  reload authoritative state -> check EVERY required commit (status COMPLETED
  + enriched durable) -> if ready, atomically claim via pr:dispatch and
  dispatch to the existing PRWorkflow.run -> ack only after the claim or a
  durable waiting update is recorded.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Optional

from google.api_core.exceptions import AlreadyExists
from google.cloud import pubsub_v1

from github_infrastructure.config import settings

logger = logging.getLogger("workers.completion_consumer")

COMPLETION_EVENTS = {"commit.completed", "commit.failed"}


class CompletionConsumer:
    def __init__(
        self,
        project: Optional[str] = None,
        subscription_id: Optional[str] = None,
        topic_id: Optional[str] = None,
        pr_store: Optional[Any] = None,
        commit_status_store: Optional[Any] = None,
        idempotency_manager: Optional[Any] = None,
        enriched_exists_fn: Optional[Callable[[str, str], bool]] = None,
        dispatch_fn: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self.project = project or settings.gcp_project
        if not self.project:
            raise ValueError("GCP project is not configured")
        default_topic = settings.pubsub_topic
        from coordination.config import completion_subscription_suffix

        default_sub = f"{default_topic}{completion_subscription_suffix()}"
        self.subscription_id = subscription_id or os.getenv("COMPLETION_SUBSCRIPTION") or default_sub
        self.topic_id = topic_id or default_topic
        self._pr_store = pr_store
        self._commit_status_store = commit_status_store
        self._idempotency = idempotency_manager
        self._enriched_exists_fn = enriched_exists_fn
        self._dispatch_fn = dispatch_fn
        self._subscriber: Optional[pubsub_v1.SubscriberClient] = None
        self._publisher: Optional[pubsub_v1.PublisherClient] = None

    # -- wiring -----------------------------------------------------------

    @property
    def subscriber(self) -> pubsub_v1.SubscriberClient:
        if self._subscriber is None:
            self._subscriber = pubsub_v1.SubscriberClient()
        return self._subscriber

    @property
    def subscription_path(self) -> str:
        return self.subscriber.subscription_path(self.project, self.subscription_id)

    @property
    def topic_path(self) -> str:
        return self.subscriber.topic_path(self.project, self.topic_id)

    def ensure_subscription(self) -> None:
        if self._publisher is None:
            self._publisher = pubsub_v1.PublisherClient()
        try:
            self._publisher.create_topic(name=self._publisher.topic_path(self.project, self.topic_id))
        except AlreadyExists:
            pass
        try:
            self.subscriber.create_subscription(name=self.subscription_path, topic=self.topic_path)
            logger.info("Created subscription %s on topic %s", self.subscription_id, self.topic_id)
        except AlreadyExists:
            pass

    def _get_pr_store(self) -> Any:
        if self._pr_store is None:
            from coordination.stores import FirestorePRWorkflowStore

            self._pr_store = FirestorePRWorkflowStore(project=self.project)
        return self._pr_store

    def _get_status_store(self) -> Any:
        if self._commit_status_store is None:
            from coordination.stores import FirestoreCommitStatusStore

            self._commit_status_store = FirestoreCommitStatusStore(project=self.project)
        return self._commit_status_store

    def _get_idempotency(self) -> Any:
        if self._idempotency is None:
            from security.idempotency import get_idempotency_manager

            self._idempotency = get_idempotency_manager()
        return self._idempotency

    def _enriched_exists(self, project_id: str, sha: str) -> bool:
        if self._enriched_exists_fn is not None:
            try:
                return bool(self._enriched_exists_fn(project_id, sha))
            except Exception:
                return False
        try:
            from github_infrastructure.pipeline import enriched_exists

            return enriched_exists(project_id, sha)
        except Exception:
            return False

    # -- event handling ----------------------------------------------------

    def _decode(self, data: bytes) -> dict[str, Any]:
        try:
            return json.loads(data.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"completion message is not valid JSON: {exc}") from exc

    def _authorize(self, project_id: str, workflow_id: str) -> bool:
        try:
            from security.gateway import get_permission_gateway
            from security.identities import Action, AgentIdentity, Resource
            from security.permissions import AuthorizationRequest

            decision = get_permission_gateway().authorize(
                AuthorizationRequest(
                    agent_id=AgentIdentity.COMPLETION_CONSUMER.value,
                    action=Action.READ,
                    resource=Resource.PR_WORKFLOWS,
                    project_id=project_id,
                    workflow_id=workflow_id,
                )
            )
            return decision.allowed
        except Exception:
            return True

    def handle_event_dict(self, message: dict[str, Any]) -> int:
        """Process one decoded completion message. Returns dispatched count."""
        from coordination.config import completion_dedupe_step_name, pr_dispatch_step_name
        from coordination.gate import check_readiness
        from coordination.models import CommitCompletionEvent
        from security.idempotency import ClaimStatus

        try:
            event = CommitCompletionEvent.from_envelope(message)
        except Exception as exc:
            logger.warning("Discarding invalid completion event: %s", exc)
            return 0

        if not self._authorize(event.project_id, f"commit:{event.project_id}:{event.commit_sha[:8]}"):
            logger.warning("Completion consumer not authorized for project %s", event.project_id)
            return 0

        # Dedupe duplicate deliveries (same event redelivered is harmless).
        idem = self._get_idempotency()
        dedupe_key = idem.build_key(
            event.project_id,
            f"commit:{event.project_id}:{event.commit_sha}",
            completion_dedupe_step_name(),
            {"sha": event.commit_sha, "status": event.status, "schema": event.schema_version},
        )
        outcome = idem.claim(
            dedupe_key,
            workflow_id=f"commit:{event.project_id}:{event.commit_sha}",
            project_id=event.project_id,
            step_name=completion_dedupe_step_name(),
            input_payload={"sha": event.commit_sha, "status": event.status},
        )
        if outcome.status == ClaimStatus.COMPLETED:
            return 0  # already handled this event
        if outcome.status == ClaimStatus.IN_PROGRESS:
            return 0  # concurrent delivery is handling it

        pr_store = self._get_pr_store()
        status_store = self._get_status_store()
        try:
            candidates = pr_store.list_waiting_for_commit(event.commit_sha)
        except Exception:
            logger.exception("list_waiting_for_commit failed")
            idem.fail(dedupe_key, "list_waiting failed")
            return 0

        dispatched = 0
        for record in candidates:
            # Tenant isolation: only PRs in the same project/repo can depend
            # on this commit (plus the event's project when set).
            if event.project_id and record.project_id != event.project_id:
                continue
            if event.repository and record.repo_full and record.repo_full != event.repository:
                continue
            # Reload authoritative state (candidate may be stale).
            try:
                fresh = pr_store.get(record.project_id, record.pr_number, record.head_sha)
            except Exception:
                continue
            if fresh is None or fresh.status != "WAITING_FOR_COMMITS":
                continue
            ready, _, _ = check_readiness(
                status_store, self._enriched_exists,
                project_id=fresh.project_id, repository=fresh.repo_full,
                required_shas=list(fresh.required_shas or []),
            )
            if not ready:
                continue
            # Atomically claim before dispatch (concurrent events: one wins).
            workflow_id = fresh.workflow_id
            fingerprint = {"head_sha": fresh.head_sha, "commit_shas": sorted(fresh.required_shas or [])}
            claim_key = idem.build_key(fresh.project_id, workflow_id, pr_dispatch_step_name(), fingerprint)
            claim = idem.claim(
                claim_key, workflow_id=workflow_id, project_id=fresh.project_id,
                step_name=pr_dispatch_step_name(), input_payload=fingerprint,
            )
            if claim.status != "EXECUTE":
                continue  # COMPLETED (already ran) or IN_PROGRESS (peer won)
            try:
                self._dispatch(fresh)
                idem.complete(claim_key, {"status": "dispatched", "pr_number": fresh.pr_number})
                dispatched += 1
            except Exception:
                logger.exception("Dispatch failed for PR#%s — lease allows recovery", fresh.pr_number)
                idem.fail(claim_key, "dispatch failed")
                continue

        idem.complete(dedupe_key, {"dispatched": dispatched, "sha": event.commit_sha})
        return dispatched

    def _dispatch(self, record: Any) -> None:
        """Dispatch to the existing PR processing mechanism (no duplication)."""
        if self._dispatch_fn is not None:
            result = self._dispatch_fn(record)
            if asyncio.iscoroutine(result):
                asyncio.run(result)
            return
        # Default: reconstruct PREvent and run the existing PRWorkflow.run.
        # The workflow re-checks the gate authoritatively, so dispatch-then-
        # crash cannot cause unguarded agent execution.
        from workflows.pr_workflow import PREvent, PRWorkflow

        pr_event = PREvent(
            project_id=record.project_id,
            owner=record.owner,
            repository=record.repository,
            pr_number=record.pr_number,
            action=record.action or "opened",
            head_sha=record.head_sha or "",
        )
        asyncio.run(PRWorkflow(project=self.project).run(pr_event))

    # -- Pub/Sub loops (same conventions as PRWorker) -----------------------

    def run_once(self, max_messages: int = 10, timeout: float = 30.0) -> int:
        self.ensure_subscription()
        response = self.subscriber.pull(subscription=self.subscription_path, max_messages=max_messages, timeout=timeout)
        ack_ids: list[str] = []
        total_dispatched = 0
        for received in response.received_messages:
            attrs = dict(received.message.attributes or {})
            if attrs.get("event") not in COMPLETION_EVENTS:
                ack_ids.append(received.ack_id)  # not ours — skip without poisoning
                continue
            try:
                message = self._decode(received.message.data)
                total_dispatched += self.handle_event_dict(message)
                ack_ids.append(received.ack_id)  # ack after durable claim/dispatch record
            except Exception:
                logger.exception("Completion message %s failed; redelivering", received.message.message_id)
                continue
        if ack_ids:
            self.subscriber.acknowledge(subscription=self.subscription_path, ack_ids=ack_ids)
        return total_dispatched

    def start(self) -> None:
        self.ensure_subscription()

        def _callback(message: pubsub_v1.subscriber.message.Message) -> None:
            try:
                attrs = dict(message.attributes or {})
                if attrs.get("event") not in COMPLETION_EVENTS:
                    message.ack()
                    return
                self.handle_event_dict(self._decode(message.data))
                message.ack()  # ack only after durable claim/dispatch bookkeeping
            except Exception:
                logger.exception("Completion callback failed for %s; nacking", message.message_id)
                message.nack()

        future = self.subscriber.subscribe(self.subscription_path, callback=_callback)
        logger.info("Listening on %s (topic %s)", self.subscription_path, self.topic_path)
        try:
            future.result()
        except KeyboardInterrupt:
            future.cancel()
            logger.info("Completion consumer stopped by interrupt")


def reconcile_waiting_prs(
    *,
    project: Optional[str] = None,
    pr_store: Optional[Any] = None,
    commit_status_store: Optional[Any] = None,
    idempotency_manager: Optional[Any] = None,
    enriched_exists_fn: Optional[Callable[[str, str], bool]] = None,
    dispatch_fn: Optional[Callable[[Any], Any]] = None,
    timeout_seconds: Optional[int] = None,
) -> dict[str, int]:
    """Operational entry point: recover lost events + interrupted dispatches.

    Uses default dispatch (PRWorkflow.run) when no dispatch_fn is supplied.
    Safe to run periodically (cron) or on demand; idempotent.
    """
    from coordination.reconciler import reconcile_waiting

    proj = project or settings.gcp_project
    consumer = CompletionConsumer(
        project=proj,
        pr_store=pr_store,
        commit_status_store=commit_status_store,
        idempotency_manager=idempotency_manager,
        enriched_exists_fn=enriched_exists_fn,
        dispatch_fn=dispatch_fn,
    )

    def _dispatch(record: Any) -> bool:
        from coordination.config import pr_dispatch_step_name
        from security.idempotency import ClaimStatus

        idem = consumer._get_idempotency()
        fingerprint = {"head_sha": record.head_sha, "commit_shas": sorted(record.required_shas or [])}
        key = idem.build_key(record.project_id, record.workflow_id, pr_dispatch_step_name(), fingerprint)
        claim = idem.claim(
            key, workflow_id=record.workflow_id, project_id=record.project_id,
            step_name=pr_dispatch_step_name(), input_payload=fingerprint,
        )
        if claim.status != "EXECUTE":
            return False
        try:
            consumer._dispatch(record)
            idem.complete(key, {"status": "dispatched", "pr_number": record.pr_number})
            return True
        except Exception:
            idem.fail(key, "reconcile dispatch failed")
            return False

    return reconcile_waiting(
        consumer._get_pr_store(),
        consumer._get_status_store(),
        consumer._enriched_exists,
        _dispatch,
        timeout_seconds=timeout_seconds,
    )
