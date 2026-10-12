from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from agents.communication_agent import CommunicationAgent, CommunicationResult, SlackMessage
from agents.coordination_agent import CoordinationAgent, CoordinationPlan
from agents.impact_analysis_agent import ImpactReport
from agents.supervisor_agent import SupervisionResult, SupervisorAgent
from blast_radius.blast_radius_service import BlastRadiusService
from blast_radius.diff_analyzer import parse_pr_files
from blast_radius.models import ANALYSIS_VERSION, BlastRadiusReport, ChangedFileInfo
from github_infrastructure.github_service import GitHubService
from project_context.models import ProjectContextOutput
from security.idempotency import ClaimStatus, IdempotencyManager, StepInProgressError

try:
    from agents.change_analysis_agent import EnrichedChangeRecord
except ImportError:
    from agents.change_analysis_agent import EnrichedChangeRecord

logger = logging.getLogger("workflows.pr_workflow")


class PREvent(BaseModel):
    project_id: str = Field(description="Project identifier")
    owner: str = Field(description="GitHub owner/org")
    repository: str = Field(description="Repository name")
    pr_number: int = Field(description="Pull request number")
    action: str = Field(default="opened", description="PR action: opened, synchronize, reopened, closed")
    head_sha: str = Field(default="", description="PR head SHA — changes the idempotency key so updated PRs re-analyze")


class PRAnalysisInput(BaseModel):
    project_id: str
    pr_number: int
    project_context: ProjectContextOutput
    enriched_changes: List[EnrichedChangeRecord] = Field(default_factory=list)


class CommunicationBundle(BaseModel):
    project_id: str
    pr_number: int
    owner: str
    repository: str
    impact_report: Optional[ImpactReport] = None
    coordination_plan: Optional[CoordinationPlan] = None
    communication_result: Optional[CommunicationResult] = None
    messages: List[SlackMessage] = Field(default_factory=list)
    summary: str = Field(default="")


class PRWorkflow:
    def __init__(
        self,
        github_service: Optional[GitHubService] = None,
        impact_agent: Optional[Any] = None,
        coordinator_agent: Optional[Any] = None,
        communication_agent: Optional[Any] = None,
        supervisor_agent: Optional[Any] = None,
        idempotency_manager: Optional[IdempotencyManager] = None,
        blast_radius_service: Optional[Any] = None,
        project: Optional[str] = None,
        database: Optional[str] = None,
        pr_store: Optional[Any] = None,
        commit_status_store: Optional[Any] = None,
        enriched_exists_fn: Optional[Any] = None,
    ) -> None:
        from github_infrastructure.config import settings

        self.github_service = github_service or GitHubService()
        self.project = project or settings.gcp_project
        self.database = database or os.getenv("FIRESTORE_DATABASE", "(default)")
        # Lazy import to avoid circular deps, use existing agents
        self.impact_agent = impact_agent
        self.coordinator_agent = coordinator_agent
        self.communication_agent = communication_agent
        self.supervisor_agent = supervisor_agent
        self.blast_radius_service = blast_radius_service
        self._idempotency = idempotency_manager
        self._firestore = None
        # Readiness-gate stores (injectable for tests; Firestore otherwise).
        self._pr_store = pr_store
        self._commit_status_store = commit_status_store
        self._enriched_exists_fn = enriched_exists_fn

    @property
    def firestore_client(self):
        if self._firestore is None:
            from google.cloud import firestore

            kwargs: dict = {"project": self.project}
            if self.database and self.database != "(default)":
                kwargs["database"] = self.database
            self._firestore = firestore.Client(**kwargs)
        return self._firestore

    async def _load_existing_bundle(self, pr_event: PREvent) -> CommunicationBundle | None:
        """Fast path: if this PR head already has a communications doc, skip LLM re-runs.

        A changed head SHA means new work — the workflow re-runs (idempotency
        keys incorporate the head SHA, so each head analyzes exactly once).
        """
        import asyncio

        def _sync_check():
            base = self.firestore_client.collection("projects").document(pr_event.project_id)
            comm_snap = base.collection("communications").document(str(pr_event.pr_number)).get()
            if not comm_snap.exists:
                return None
            data = comm_snap.to_dict() or {}
            stored_head = str(data.get("head_sha", "") or "")
            if pr_event.head_sha and stored_head and stored_head != pr_event.head_sha:
                logger.info(
                    "PR %s head changed (%s -> %s) — re-running workflow",
                    pr_event.pr_number,
                    stored_head[:10],
                    pr_event.head_sha[:10],
                )
                return None
            try:
                return CommunicationBundle.model_validate(data)
            except Exception:
                return None

        return await asyncio.to_thread(_sync_check)

    def _get_idempotency_manager(self) -> IdempotencyManager:
        if self._idempotency is None:
            from security.idempotency import get_idempotency_manager

            self._idempotency = get_idempotency_manager()
        return self._idempotency

    # -- readiness gate stores --------------------------------------------

    def _get_pr_store(self) -> Optional[Any]:
        if self._pr_store is not None:
            return self._pr_store
        try:
            from coordination.stores import FirestorePRWorkflowStore

            self._pr_store = FirestorePRWorkflowStore(project=self.project, database=self.database)
            return self._pr_store
        except Exception:
            logger.warning("PR workflow store unavailable — gate will fail open to legacy behavior")
            return None

    def _get_commit_status_store(self) -> Optional[Any]:
        if self._commit_status_store is not None:
            return self._commit_status_store
        try:
            from coordination.stores import FirestoreCommitStatusStore

            self._commit_status_store = FirestoreCommitStatusStore(project=self.project, database=self.database)
            return self._commit_status_store
        except Exception:
            return None

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
            # Fallback: direct Firestore read (same semantics as pipeline helper).
            try:
                doc = (
                    self.firestore_client.collection("projects")
                    .document(project_id)
                    .collection("enriched_changes")
                    .document(sha)
                    .get()
                )
                return bool(doc.exists)
            except Exception:
                return False

    async def _gate_check(self, pr_event: PREvent, workflow_id: str, commits: List[str]) -> dict[str, Any]:
        """Persist WAITING_FOR_COMMITS before checking (race-safe), recheck
        after registration, enforce timeout/fallback. Never runs PR agents."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        from coordination.config import pr_wait_timeout_seconds

        pr_store = await asyncio.to_thread(self._get_pr_store)
        status_store = await asyncio.to_thread(self._get_commit_status_store)
        repo_full = f"{pr_event.owner}/{pr_event.repository}" if pr_event.owner else pr_event.repository

        # Gate unavailable (no Firestore in some test contexts) — legacy pass-through.
        if pr_store is None or status_store is None:
            return {"decision": "proceed"}

        def _sync_gate() -> dict[str, Any]:
            from coordination.gate import check_readiness
            from coordination.models import PRWorkflowRecord
            from telemetry.spans import current_span_ids

            timeout = pr_wait_timeout_seconds()
            now = datetime.now(timezone.utc)
            # 1. Load or create state for THIS revision (doc id includes head SHA).
            state = pr_store.get(pr_event.project_id, pr_event.pr_number, pr_event.head_sha or "")
            # Resume linkage: if this revision previously went WAITING, the new
            # run links back to that trace instead of holding spans open.
            prior_trace: tuple[Any, Any] = (None, None)
            if state is not None and state.status == "WAITING_FOR_COMMITS" and state.trace_id:
                prior_trace = (state.trace_id, state.span_id)
            if state is None:
                state = PRWorkflowRecord(
                    project_id=pr_event.project_id,
                    owner=pr_event.owner,
                    repository=pr_event.repository,
                    pr_number=pr_event.pr_number,
                    head_sha=pr_event.head_sha or "",
                    action=pr_event.action,
                    required_shas=list(commits),
                    status="PENDING",
                    wait_started_at=now.isoformat(),
                    deadline_at=(now + timedelta(seconds=timeout)).isoformat(),
                )
            else:
                # Revision change is a new doc; same-doc required-set refresh.
                state.required_shas = list(commits)
                state.action = pr_event.action
            # 2. Supersede stale WAITING docs for the same PR number / other heads.
            try:
                for other in pr_store.list_for_pr(pr_event.project_id, pr_event.pr_number):
                    if other.head_sha != (pr_event.head_sha or "") and other.status in ("PENDING", "WAITING_FOR_COMMITS", "READY", "PROCESSING"):
                        other.status = "SUPERSEDED"
                        other.error = f"superseded by head {(pr_event.head_sha or '')[:8]}"
                        pr_store.put(other)
            except Exception:
                logger.exception("Gate: supersede check failed for PR#%s", pr_event.pr_number)
            # 3. Persist WAITING state BEFORE the authoritative check (race-safe:
            # a commit finishing right now will find this record).
            if state.status in ("PENDING", "READY", "PROCESSING", "COMPLETED", "PARTIAL", "FAILED", "SUPERSEDED") and state.status not in ("PENDING",):
                pass  # terminal/processing states are left intact except required-set refresh above
            else:
                state.status = "WAITING_FOR_COMMITS"
            if not state.wait_started_at:
                state.wait_started_at = now.isoformat()
            if not state.deadline_at:
                state.deadline_at = (now + timedelta(seconds=timeout)).isoformat()
            pr_store.put(state)
            # 4. Authoritative recheck AFTER registration (closes lost-event race).
            ready, missing, failed = check_readiness(
                status_store, self._enriched_exists,
                project_id=pr_event.project_id, repository=repo_full, required_shas=list(commits),
            )
            if ready:
                state.status = "READY"
                state.missing_shas = []
                state.failed_shas = []
                pr_store.put(state)
                self._audit_gate(pr_event, workflow_id, "READY", f"{len(commits)} commits ready")
                return {"decision": "proceed", "state": state.model_dump(mode="json"), "prior_trace": prior_trace}
            # 5. Timeout path: rechecked already; partial when meaningful else FAILED.
            try:
                deadline = datetime.fromisoformat(str(state.deadline_at)) if state.deadline_at else None
            except ValueError:
                deadline = None
            if deadline is not None and now >= deadline:
                from coordination.gate import can_produce_partial

                available = len(commits) - len(missing)
                if can_produce_partial(available, len(commits)):
                    state.status = "READY"
                    state.partial = True
                    state.missing_shas = list(missing)
                    state.failed_shas = list(failed)
                    state.error = f"wait timeout with {available}/{len(commits)} commits; partial analysis"
                    pr_store.put(state)
                    self._audit_gate(pr_event, workflow_id, "READY", "timeout partial")
                    return {"decision": "proceed", "partial": True, "state": state.model_dump(mode="json"), "prior_trace": prior_trace}
                state.status = "FAILED"
                state.missing_shas = list(missing)
                state.failed_shas = list(failed)
                state.error = f"wait timeout: missing=[{','.join(s[:8] for s in missing[:5])}]"
                pr_store.put(state)
                self._audit_gate(pr_event, workflow_id, "FAILED", state.error)
                return {"decision": "failed", "missing": missing, "failed_shas": failed}
            # 6. Still waiting — record missing set, return without running agents.
            state.status = "WAITING_FOR_COMMITS"
            state.missing_shas = list(missing)
            state.failed_shas = list(failed)
            # Span ends here (waiting); the resume run links back via these ids.
            trace_id, span_id = current_span_ids()
            if trace_id and span_id:
                state.trace_id = trace_id
                state.span_id = span_id
            pr_store.put(state)
            self._audit_gate(pr_event, workflow_id, "WAITING_FOR_COMMITS", f"missing={len(missing)}")
            return {"decision": "wait", "missing": missing, "failed_shas": failed}

        from telemetry.spans import set_span_attrs, workflow_span

        with workflow_span(
            "commit.readiness_check",
            {
                "project_id": pr_event.project_id,
                "pr.number": pr_event.pr_number,
                "pr.head_sha": (pr_event.head_sha or "")[:12],
                "gate.required_commits": len(commits),
            },
        ) as gspan:
            gate_result = await asyncio.to_thread(_sync_gate)
            set_span_attrs(
                gspan,
                {
                    "pending_commits": len(gate_result.get("missing") or []),
                    "failed_commits": len(gate_result.get("failed_shas") or []),
                    "pr.status": gate_result.get("decision", ""),
                    "pr.partial": bool(gate_result.get("partial", False)),
                },
            )
            return gate_result

    def _audit_gate(self, pr_event: PREvent, workflow_id: str, decision: str, reason: str) -> None:
        try:
            from security.audit import AuditEvent, AuditLogger

            AuditLogger().record(
                AuditEvent(
                    agent_id="completion_consumer",
                    action="write",
                    resource="pr_workflows",
                    project_id=pr_event.project_id,
                    workflow_id=workflow_id,
                    decision=decision,
                    reason=reason[:500],
                    policy="pr_readiness_gate",
                    event_type="pr.workflow.transition",
                )
            )
        except Exception:
            pass

    def _waiting_bundle(self, pr_event: PREvent, gate: dict[str, Any]) -> CommunicationBundle:
        missing = gate.get("missing") or []
        return CommunicationBundle(
            project_id=pr_event.project_id,
            pr_number=pr_event.pr_number,
            owner=pr_event.owner,
            repository=pr_event.repository,
            messages=[],
            summary=(
                f"PR #{pr_event.pr_number} waiting for {len(missing)} commit(s) "
                f"({','.join(s[:8] for s in missing[:5])}). Status=WAITING_FOR_COMMITS."
            ),
        )

    def _failed_bundle(self, pr_event: PREvent, gate: dict[str, Any]) -> CommunicationBundle:
        missing = gate.get("missing") or []
        return CommunicationBundle(
            project_id=pr_event.project_id,
            pr_number=pr_event.pr_number,
            owner=pr_event.owner,
            repository=pr_event.repository,
            messages=[],
            summary=(
                f"PR #{pr_event.pr_number} failed readiness: missing=[{','.join(s[:8] for s in missing[:5])}]. "
                "Status=FAILED."
            ),
        )

    async def _claim_dispatch(self, pr_event: PREvent, workflow_id: str, commits: List[str]) -> tuple[str, Optional[dict]]:
        """Atomically claim PR execution.

        Returns (status, info): status is EXECUTE|COMPLETED|IN_PROGRESS; info
        identifies OUR claim (key/attempt/lease) so a fast failure can release
        exactly this claim without touching a peer's.
        """
        import asyncio

        from coordination.config import pr_dispatch_step_name

        idem = self._get_idempotency_manager()
        fingerprint = {"head_sha": pr_event.head_sha, "commit_shas": sorted(commits)}
        key = idem.build_key(pr_event.project_id, workflow_id, pr_dispatch_step_name(), fingerprint)
        outcome = await asyncio.to_thread(
            idem.claim, key,
            workflow_id=workflow_id, project_id=pr_event.project_id,
            step_name=pr_dispatch_step_name(), input_payload=fingerprint,
        )
        if outcome.status == ClaimStatus.COMPLETED:
            return "completed", None
        if outcome.status == ClaimStatus.IN_PROGRESS:
            return "in_progress", None
        # Mark state PROCESSING under the same claim (crash recovers via lease).
        await self._mark_state_processing(pr_event)
        rec = outcome.record
        info = {
            "key": key,
            "attempt": rec.attempt_count if rec is not None else 1,
            "lease": rec.lease_until if rec is not None else None,
        }
        return "execute", info

    async def _fail_owned_dispatch(self, dispatch_info: Optional[dict], error: str) -> None:
        """Release ONLY our own dispatch claim after a fast failure, so the
        next redelivery retries immediately instead of waiting out the lease.
        A peer's newer/reclaimed claim is never touched (single-flight kept)."""
        if not dispatch_info:
            return
        import asyncio

        def _sync() -> None:
            try:
                idem = self._get_idempotency_manager()
                rec = idem.store.get(dispatch_info["key"])
                if rec is None or rec.status != "IN_PROGRESS":
                    return
                if (rec.attempt_count != dispatch_info["attempt"]
                        or rec.lease_until != dispatch_info["lease"]):
                    logger.warning("Dispatch claim changed hands — leaving peer's claim alone")
                    return
                idem.fail(dispatch_info["key"], f"owner run failed: {error[:300]}")
                logger.info("Released owned dispatch claim %s", dispatch_info["key"])
            except Exception:
                logger.exception("Failed to release dispatch claim")

        await asyncio.to_thread(_sync)

    async def _mark_state_processing(self, pr_event: PREvent) -> None:
        import asyncio

        def _sync() -> None:
            store = self._get_pr_store()
            if store is None:
                return
            state = store.get(pr_event.project_id, pr_event.pr_number, pr_event.head_sha or "")
            if state is None:
                return
            state.status = "PROCESSING"
            store.put(state)

        await asyncio.to_thread(_sync)

    async def _mark_state_terminal(self, pr_event: PREvent, status: str, partial: bool = False) -> None:
        import asyncio

        def _sync() -> None:
            store = self._get_pr_store()
            if store is None:
                return
            state = store.get(pr_event.project_id, pr_event.pr_number, pr_event.head_sha or "")
            if state is None:
                return
            state.status = status  # type: ignore[assignment]
            state.partial = partial
            if status in ("COMPLETED", "PARTIAL"):
                try:
                    from security.idempotency import get_idempotency_manager

                    from coordination.config import pr_dispatch_step_name

                    # Close the dispatch claim so completion-event redeliveries
                    # reuse instead of re-executing (crash-after-complete safe).
                    idem = self._idempotency or get_idempotency_manager()
                    commits = list(state.required_shas or [])
                    fingerprint = {"head_sha": state.head_sha, "commit_shas": sorted(commits)}
                    key = idem.build_key(state.project_id, state.workflow_id, pr_dispatch_step_name(), fingerprint)
                    idem.complete(key, {"status": status, "pr_number": state.pr_number})
                except Exception:
                    pass
            store.put(state)
            self._audit_gate(pr_event, state.workflow_id, status, "terminal")

        await asyncio.to_thread(_sync)

    async def _mark_state_completed_if_exists(self, pr_event: PREvent) -> None:
        import asyncio

        def _sync() -> None:
            store = self._get_pr_store()
            if store is None:
                return
            state = store.get(pr_event.project_id, pr_event.pr_number, pr_event.head_sha or "")
            if state is None or state.status in ("COMPLETED", "PARTIAL", "FAILED", "SUPERSEDED"):
                return
            state.status = "COMPLETED"
            store.put(state)

        await asyncio.to_thread(_sync)

    def get_workflow_state(self, project_id: str, pr_number: int, head_sha: str) -> Optional[Any]:
        store = self._get_pr_store()
        if store is None:
            return None
        try:
            return store.get(project_id, pr_number, head_sha or "")
        except Exception:
            return None

    async def _run_step(
        self,
        step_name: str,
        pr_event: PREvent,
        workflow_id: str,
        input_fingerprint: dict[str, Any],
        execute,
        model_cls,
    ):
        """Claim -> execute -> persist result -> mark COMPLETED (spec §8 ordering).

        The full result is stored inside the idempotency record before it is
        marked COMPLETED, so a retry after a crash returns the stored result
        instead of re-executing the (LLM) step.
        """
        import asyncio

        idem = self._get_idempotency_manager()
        key = idem.build_key(pr_event.project_id, workflow_id, step_name, input_fingerprint)
        outcome = await asyncio.to_thread(
            idem.claim,
            key,
            workflow_id=workflow_id,
            project_id=pr_event.project_id,
            step_name=step_name,
            input_payload=input_fingerprint,
        )
        if outcome.status == ClaimStatus.COMPLETED:
            logger.info("Step %s already completed for %s — reusing stored result", step_name, workflow_id)
            return model_cls.model_validate(outcome.result or {})
        if outcome.status == ClaimStatus.IN_PROGRESS:
            raise StepInProgressError(f"{step_name} is in progress for {workflow_id}")

        try:
            result = await execute()
        except Exception as exc:
            await asyncio.to_thread(idem.fail, key, str(exc))
            raise

        await asyncio.to_thread(idem.complete, key, result.model_dump(mode="json"))
        return result

    async def run(self, pr_event: PREvent) -> CommunicationBundle:
        """Traced entry point: root `pr.workflow` span (latency automatic).

        WAITING runs end here with `pr.status=waiting`; the resume run after
        a completion event links back to the waiting trace (see _gate_check).
        """
        from telemetry.spans import annotate_current_span, workflow_span

        with workflow_span(
            "pr.workflow",
            {
                "project_id": pr_event.project_id,
                "pr.number": pr_event.pr_number,
                "repo": f"{pr_event.owner}/{pr_event.repository}" if pr_event.owner else pr_event.repository,
                "pr.head_sha": (pr_event.head_sha or "")[:12],
                "pr.action": pr_event.action,
            },
        ):
            bundle = await self._run_inner(pr_event)
            annotate_current_span({"pr.summary": (bundle.summary or "")[:500]})
            return bundle

    async def _run_inner(self, pr_event: PREvent) -> CommunicationBundle:
        # Fast path: same head SHA already analyzed -> skip LLM calls (prevents 429 loops)
        existing = await self._load_existing_bundle(pr_event)
        if existing is not None:
            logger.info("PR %s already has communications bundle for %s — skipping LLM re-run", pr_event.pr_number, pr_event.project_id)
            await self._mark_state_completed_if_exists(pr_event)
            return existing

        workflow_id = f"pr:{pr_event.project_id}:{pr_event.pr_number}:{pr_event.head_sha or 'na'}"
        commits = await self._fetch_pr_commits(pr_event)
        project_context = await self._load_project_context(pr_event.project_id)

        # --- Readiness gate: never run downstream PR agents until every
        # required commit is COMPLETED with a durable EnrichedChangeRecord.
        gate = await self._gate_check(pr_event, workflow_id, commits)
        from telemetry.spans import annotate_current_span, link_prior_trace

        if gate["decision"] == "wait":
            annotate_current_span(
                {"pr.status": "waiting", "pending_commits": len(gate.get("missing") or [])}
            )
            return self._waiting_bundle(pr_event, gate)
        if gate["decision"] == "failed":
            annotate_current_span(
                {"pr.status": "failed", "pending_commits": len(gate.get("missing") or [])}
            )
            return self._failed_bundle(pr_event, gate)
        prior = gate.get("prior_trace") or (None, None)
        if prior[0] and prior[1]:
            # Resume after wait: link to the waiting trace, don't hold spans open.
            link_prior_trace(prior[0], prior[1])
            annotate_current_span({"pr.resumed": True})
        partial = bool(gate.get("partial", False))
        annotate_current_span({"pr.status": "partial" if partial else "ready"})

        # Atomic dispatch claim: concurrent completion events must not launch
        # the same PR workflow twice; crash-after-claim recovers via lease.
        claimed, dispatch_info = await self._claim_dispatch(pr_event, workflow_id, commits)
        if claimed == "completed":
            reloaded = await self._load_existing_bundle(pr_event)
            if reloaded is not None:
                return reloaded
        elif claimed == "in_progress":
            raise StepInProgressError(f"pr:dispatch is in progress for {workflow_id}")

        try:
            return await self._run_claimed(pr_event, workflow_id, commits, project_context, partial)
        except StepInProgressError:
            raise
        except Exception as exc:
            # We owned the claim and failed fast: release it so the next
            # redelivery retries immediately (peer's claims are never touched).
            await self._fail_owned_dispatch(dispatch_info, str(exc))
            raise

    async def _run_claimed(
        self,
        pr_event: PREvent,
        workflow_id: str,
        commits: List[str],
        project_context: ProjectContextOutput,
        partial: bool,
    ) -> CommunicationBundle:
        """Post-claim pipeline: enriched load, blast radius, agents, persist."""
        from telemetry.spans import annotate_current_span

        enriched_changes = await self._load_enriched_changes(pr_event.project_id, commits)
        pr_input = self._build_pr_analysis_input(project_context, enriched_changes, pr_event)

        # Input fingerprint: any change (new head, new commits, updated context)
        # produces different idempotency keys, so the new input is analyzed.
        input_fingerprint = {
            "pr_number": pr_event.pr_number,
            "head_sha": pr_event.head_sha,
            "action": pr_event.action,
            "commit_shas": commits,
            "context_updated_at": project_context.metadata.updated_at,
            "enriched_shas": [c.commit_sha for c in enriched_changes],
        }

        # Deterministic blast radius FIRST: sandboxed static analysis produces
        # the evidence the Impact Agent reasons over (never the reverse).
        # Keyed by PR head SHA + context version + analysis version.
        blast_radius_fingerprint = {
            "pr_number": pr_event.pr_number,
            "head_sha": pr_event.head_sha,
            "context_updated_at": project_context.metadata.updated_at,
            "analysis_version": ANALYSIS_VERSION,
        }
        from telemetry.spans import set_span_attrs as _set_span_attrs
        from telemetry.spans import workflow_span as _workflow_span

        with _workflow_span(
            "pr.blast_radius",
            {
                "project_id": pr_event.project_id,
                "pr.number": pr_event.pr_number,
                "pr.head_sha": (pr_event.head_sha or "")[:12],
            },
        ) as _bspan:
            blast_report = await self._run_blast_radius_step(
                pr_event, workflow_id, blast_radius_fingerprint, project_context
            )
            _set_span_attrs(
                _bspan,
                {
                    "output.entries": len(getattr(blast_report, "entries", None) or []),
                    "output.changed_files": len(getattr(blast_report, "changed_files", None) or []),
                    "output.incomplete": bool(getattr(blast_report, "incomplete", False)),
                },
            )

        # Execute agents sequentially - workflow orchestrates, agents don't call each other.
        # Agents reason and persist drafts; nothing here sends Slack.
        impact_report = await self._run_step(
            "impact:analyze", pr_event, workflow_id, input_fingerprint,
            lambda: self._run_impact_agent(pr_input, blast_report), ImpactReport,
        )
        self._reconcile_blast_radius(impact_report, blast_report)
        coordination_plan = await self._run_step(
            "coordination:create", pr_event, workflow_id, input_fingerprint,
            lambda: self._run_coordinator_agent(pr_input, impact_report), CoordinationPlan,
        )
        # PR-level reports now (not after supervision): the supervisor's tool
        # and the UI read coordination_plans/{pr_number}. Abort if superseded.
        if not await self._persist_pr_reports(pr_event, impact_report, coordination_plan):
            return self._stale_bundle(pr_event)
        communication_result = await self._run_step(
            "communication:generate", pr_event, workflow_id, input_fingerprint,
            lambda: self._run_communication_agent(pr_input, coordination_plan), CommunicationResult,
        )
        supervision = await self._run_step(
            "communication:supervise", pr_event, workflow_id, input_fingerprint,
            lambda: self._run_supervisor_agent(pr_input, coordination_plan, communication_result, impact_report), SupervisionResult,
        )

        final_messages = supervision.final_messages or communication_result.messages
        final_messages, mention_issues = self._resolve_mentions(pr_input, final_messages, coordination_plan)
        final_messages = await self._run_autonomy_stage(
            pr_event, workflow_id, impact_report, coordination_plan, final_messages,
            supervisor_autonomy=getattr(supervision, "autonomy", None) or [],
        )
        communication_bundle = CommunicationBundle(
            project_id=pr_event.project_id,
            pr_number=pr_event.pr_number,
            owner=pr_event.owner,
            repository=pr_event.repository,
            coordination_plan=coordination_plan,
            communication_result=communication_result,
            messages=list(final_messages),
            summary=supervision.summary or communication_result.summary,
        )
        if mention_issues:
            names = ", ".join(i.get("recipient", "?") for i in mention_issues[:5])
            communication_bundle.summary = (
                f"{communication_bundle.summary} [Slack mapping needed for: {names}]"
            ).strip()

        await self._persist_outputs(pr_event, impact_report, coordination_plan, communication_bundle)
        terminal = "PARTIAL" if partial else "COMPLETED"
        annotate_current_span(
            {"pr.status": terminal.lower(), "output.messages": len(list(final_messages))}
        )
        await self._mark_state_terminal(pr_event, terminal, partial=partial)

        return communication_bundle

    async def _fetch_pr_commits(self, pr_event: PREvent) -> List[str]:
        return await self.github_service.fetch_pr_commits(
            pr_event.owner, pr_event.repository, pr_event.pr_number
        )

    async def _load_project_context(self, project_id: str) -> ProjectContextOutput:
        # Use Firestore service if available, otherwise direct client via repository pattern
        # No dedicated ProjectContextService exists, so we encapsulate Firestore access here
        # to keep workflow free of raw queries - this method *is* the service boundary
        import asyncio

        def _sync_load() -> ProjectContextOutput:
            doc = self.firestore_client.collection("project_contexts").document(project_id).get()
            if not doc.exists:
                raise ValueError(f"ProjectContext not found for project_id={project_id}")
            return ProjectContextOutput.model_validate(doc.to_dict())

        return await asyncio.to_thread(_sync_load)

    async def _load_enriched_changes(self, project_id: str, commit_shas: List[str]) -> List[EnrichedChangeRecord]:
        import asyncio

        def _sync_load() -> List[EnrichedChangeRecord]:
            records: List[EnrichedChangeRecord] = []
            for sha in commit_shas:
                doc = (
                    self.firestore_client.collection("projects")
                    .document(project_id)
                    .collection("enriched_changes")
                    .document(sha)
                    .get()
                )
                if not doc.exists:
                    logger.warning("EnrichedChange not found for %s %s", project_id, sha)
                    continue
                records.append(EnrichedChangeRecord.model_validate(doc.to_dict()))
            return records

        return await asyncio.to_thread(_sync_load)

    def _build_pr_analysis_input(
        self,
        project_context: ProjectContextOutput,
        enriched_changes: List[EnrichedChangeRecord],
        pr_event: PREvent,
    ) -> PRAnalysisInput:
        return PRAnalysisInput(
            project_id=pr_event.project_id,
            pr_number=pr_event.pr_number,
            project_context=project_context,
            enriched_changes=enriched_changes,
        )

    def _get_blast_radius_service(self) -> BlastRadiusService:
        if self.blast_radius_service is None:
            self.blast_radius_service = BlastRadiusService()
        return self.blast_radius_service

    async def _run_blast_radius_step(
        self,
        pr_event: PREvent,
        workflow_id: str,
        fingerprint: dict[str, Any],
        project_context: ProjectContextOutput,
    ) -> BlastRadiusReport:
        """Idempotent blast_radius:analyze step (spec: keyed by PR head SHA +
        project/context version + analysis version). Result is persisted to
        projects/{id}/blast_radius/{pr_number} BEFORE the record is marked
        COMPLETED, so retries return the stored report and the sandbox is
        never re-provisioned for the same key."""
        import asyncio

        async def _execute() -> BlastRadiusReport:
            raw_files = await self.github_service.fetch_pr_files(
                pr_event.owner, pr_event.repository, pr_event.pr_number
            )

            def _sync() -> BlastRadiusReport:
                changed_files = parse_pr_files(raw_files)
                patches = {
                    str(f.get("filename")): str(f.get("patch") or "")
                    for f in raw_files
                    if f.get("filename") and f.get("patch")
                }
                service = self._get_blast_radius_service()
                return service.analyze(
                    project_id=pr_event.project_id,
                    pr_number=pr_event.pr_number,
                    repo=f"{pr_event.owner}/{pr_event.repository}",
                    head_sha=pr_event.head_sha,
                    changed_files=changed_files,
                    project_context=project_context.model_dump(mode="json"),
                    patches=patches,
                )

            report = await asyncio.to_thread(_sync)
            await asyncio.to_thread(self._persist_blast_radius, pr_event, report)
            return report

        return await self._run_step(
            "blast_radius:analyze",
            pr_event,
            workflow_id,
            fingerprint,
            _execute,
            BlastRadiusReport,
        )

    def _persist_blast_radius(self, pr_event: PREvent, report: BlastRadiusReport) -> None:
        from datetime import datetime, timezone

        self.firestore_client.collection("projects").document(pr_event.project_id).collection(
            "blast_radius"
        ).document(str(pr_event.pr_number)).set(
            {**report.model_dump(mode="json"), "persisted_at": datetime.now(timezone.utc).isoformat()}
        )

    def _reconcile_blast_radius(self, impact_report: ImpactReport, blast_report: Optional[BlastRadiusReport]) -> None:
        """Evidence integrity: the Impact Agent must not drop deterministic
        blast-radius entries — union any missing ones back from the stored report."""
        if blast_report is None:
            return
        present_ids = {entry.entity_id for entry in impact_report.blast_radius}
        missing = [entry for entry in blast_report.entries if entry.entity_id not in present_ids]
        if missing:
            logger.info(
                "Re-attaching %d blast radius entries dropped by the Impact Agent",
                len(missing),
            )
        impact_report.blast_radius = list(impact_report.blast_radius) + missing
        if not impact_report.changed_components:
            impact_report.changed_components = list(blast_report.changed_components)

    async def _run_impact_agent(self, pr_input: PRAnalysisInput, blast_report: Optional[BlastRadiusReport] = None) -> ImpactReport:
        # Delegate to ImpactAgent - workflow does not contain prompt engineering
        agent = self.impact_agent or self._get_impact_agent()
        # For PR, we create a synthetic aggregated impact via the agent's PR-aware method if available,
        # otherwise fall back to single-commit handling. Keep orchestration, not business logic.
        if hasattr(agent, "analyze_pr"):
            return await agent.analyze_pr(pr_input)  # type: ignore
        # Fallback: use first enriched change to drive existing single-commit agent, then adapt IDs to PR
        if not pr_input.enriched_changes:
            raise ValueError("No enriched changes to analyze for PR")
        # Use first commit as representative for PR-level analysis (workflow-level aggregation is minimal)
        first = pr_input.enriched_changes[0]
        # Ensure enriched change is persisted so tool fetch succeeds
        await self._ensure_enriched_persisted(pr_input.project_id, first)
        report, _, _ = await self._to_thread(
            agent.analyze, pr_input.project_id, first.commit_sha, pr_input.pr_number
        )
        # Re-key report to PR number for persistence
        report.project_id = pr_input.project_id
        # keep original commit_sha for traceability, but workflow will persist under pr_number
        return report

    async def _run_coordinator_agent(
        self, pr_input: PRAnalysisInput, impact_report: ImpactReport
    ) -> CoordinationPlan:
        agent = self.coordinator_agent or self._get_coordinator_agent()
        if hasattr(agent, "analyze_pr"):
            return await agent.analyze_pr(pr_input, impact_report)  # type: ignore
        # Fallback: ensure impact report is persisted so coordinator's tool can fetch it
        await self._ensure_impact_persisted(pr_input.project_id, impact_report)
        # Coordinator expects project_id + commit_sha; use first commit's sha as surrogate for PR
        surrogate_sha = pr_input.enriched_changes[0].commit_sha if pr_input.enriched_changes else str(pr_input.pr_number)
        plan, _, _ = await self._to_thread(agent.analyze, pr_input.project_id, surrogate_sha)
        plan.project_id = pr_input.project_id
        return plan

    async def _run_communication_agent(
        self, pr_input: PRAnalysisInput, coordination_plan: CoordinationPlan
    ) -> CommunicationResult:
        """Draft messages only — never sends. Delivery goes through the approval endpoint."""
        agent = self.communication_agent or self._get_communication_agent()
        if hasattr(agent, "analyze_pr"):
            return await agent.analyze_pr(pr_input, coordination_plan)  # type: ignore
        await self._ensure_coordination_persisted(pr_input.project_id, coordination_plan)
        surrogate_sha = pr_input.enriched_changes[0].commit_sha if pr_input.enriched_changes else str(pr_input.pr_number)
        result = await self._to_thread(agent.analyze, pr_input.project_id, surrogate_sha)
        if not isinstance(result, CommunicationResult):
            raise ValueError("CommunicationAgent did not return a CommunicationResult")
        return result

    async def _run_autonomy_stage(
        self,
        pr_event: PREvent,
        workflow_id: str,
        impact_report: ImpactReport,
        coordination_plan: CoordinationPlan,
        final_messages: list,
        supervisor_autonomy: list | None = None,
    ) -> list:
        """Risk-based autonomous routing (no-op unless AUTONOMY_ENABLED).

        Idempotent evaluate step -> persist batch -> stamp messages ->
        dispatch AUTO_SEND via existing gateway/idempotency/executor.
        HUMAN_REVIEW flows into the normal approval bundle; BLOCK/SKIP are
        stamped and enforced at approve time. Dry-run evaluates only.
        """
        from telemetry.spans import annotate_current_span, workflow_span

        from autonomy import config as autonomy_config
        from autonomy.models import AutonomyBatch
        from autonomy.runner import dispatch_auto_sends, evaluate_batch

        if not autonomy_config.enabled():
            return list(final_messages)

        with workflow_span(
            "pr.autonomy",
            {"project_id": pr_event.project_id, "pr.number": pr_event.pr_number,
             "dry_run": autonomy_config.dry_run()},
        ):
            fingerprint = {
                "head_sha": pr_event.head_sha,
                "messages": [
                    {"id": getattr(m, "id", ""), "text": getattr(m, "text", "")}
                    for m in (final_messages or [])
                ],
                "impact_confidence": getattr(impact_report, "confidence", 0.0),
                "policy_version": autonomy_config.POLICY_VERSION,
                "thresholds": {
                    "min_confidence": autonomy_config.min_confidence(),
                    "min_evidence": autonomy_config.min_evidence(),
                    "allow_action": autonomy_config.allow_action(),
                },
            }

            async def _execute() -> AutonomyBatch:
                import asyncio

                def _sync() -> AutonomyBatch:
                    return self._evaluate_autonomy_sync(
                        pr_event, impact_report, coordination_plan, final_messages,
                        supervisor_autonomy or [],
                    )

                return await asyncio.to_thread(_sync)

            batch = await self._run_step(
                "autonomy:evaluate", pr_event, workflow_id, fingerprint, _execute, AutonomyBatch,
            )
            await self._persist_autonomy(pr_event, batch)

            stamped = self._stamp_autonomy(final_messages, batch)
            messages_by_id = {
                (m.id if hasattr(m, "id") else ""): (
                    m.model_dump(mode="json") if hasattr(m, "model_dump") else dict(m)
                )
                for m in stamped
            }
            outcomes = dispatch_auto_sends(
                batch, messages_by_id, dry_run=autonomy_config.dry_run(),
            )
            sent = sum(1 for o in outcomes.values() if o.get("delivered"))
            annotate_current_span(
                {
                    "autonomy.auto_send": sum(1 for d in batch.decisions if d.decision == "AUTO_SEND"),
                    "autonomy.human_review": sum(1 for d in batch.decisions if d.decision == "HUMAN_REVIEW"),
                    "autonomy.blocked": sum(1 for d in batch.decisions if d.decision == "BLOCK"),
                    "autonomy.skipped": sum(1 for d in batch.decisions if d.decision == "SKIP"),
                    "autonomy.delivered": sent,
                    "autonomy.dry_run": batch.dry_run,
                }
            )
            return stamped

    def _evaluate_autonomy_sync(
        self,
        pr_event: PREvent,
        impact_report: ImpactReport,
        coordination_plan: CoordinationPlan,
        final_messages: list,
        supervisor_autonomy: list | None = None,
    ):
        """Synchronous batch evaluation (runs in a worker thread)."""
        from autonomy import config as autonomy_config
        from autonomy.runner import evaluate_batch

        try:
            impact_dict = impact_report.model_dump(mode="json")
        except Exception:
            impact_dict = {}
        try:
            plan_dict = coordination_plan.model_dump(mode="json")
        except Exception:
            plan_dict = {}
        messages = [
            m.model_dump(mode="json") if hasattr(m, "model_dump") else dict(m)
            for m in (final_messages or [])
        ]
        delivered: set[str] = set()
        try:
            from security.execution import FirestoreDeliveryStore

            delivered = set(
                FirestoreDeliveryStore(project=self.project, database=self.database)
                .read_delivered(pr_event.project_id, pr_event.pr_number)
                .keys()
            )
        except Exception:
            pass
        return evaluate_batch(
            project_id=pr_event.project_id, pr_number=pr_event.pr_number,
            head_sha=pr_event.head_sha or "", messages=messages,
            impact=impact_dict, plan=plan_dict,
            supervisor_autonomy=[
                dict(s) if isinstance(s, dict) else (
                    s.model_dump(mode="json") if hasattr(s, "model_dump") else {}
                )
                for s in (supervisor_autonomy or [])
            ],
            delivered_ids=delivered, dry_run=autonomy_config.dry_run(),
        )

    @staticmethod
    def _stamp_autonomy(final_messages: list, batch) -> list:
        by_id = {d.message_id: d for d in (batch.decisions or [])}
        stamped = []
        for m in final_messages or []:
            decision = by_id.get(getattr(m, "id", "")) if hasattr(m, "id") else None
            if decision is None:
                stamped.append(m)
                continue
            try:
                m.autonomy = decision.decision
                m.autonomy_reasons = list(decision.reasons or [])
            except Exception:
                pass
            stamped.append(m)
        return stamped

    async def _persist_autonomy(self, pr_event: PREvent, batch) -> None:
        import asyncio

        def _sync() -> None:
            self.firestore_client.collection("projects").document(pr_event.project_id).collection(
                "autonomy"
            ).document(str(pr_event.pr_number)).set(
                {**batch.model_dump(mode="json"), "persisted_at": pr_payload_time()}
            )

        await asyncio.to_thread(_sync)

    async def _run_supervisor_agent(
        self,
        pr_input: PRAnalysisInput,
        coordination_plan: CoordinationPlan,
        communication_result: CommunicationResult,
        impact_report: ImpactReport | None = None,
    ) -> SupervisionResult:
        """Supervisor critiques/validates drafts. No send capability."""
        agent = self.supervisor_agent
        if agent is None:
            agent = SupervisorAgent(project=self.project, database=self.database)
        if hasattr(agent, "analyze_pr"):
            return await agent.analyze_pr(pr_input, coordination_plan, communication_result)  # type: ignore
        drafts = [m.model_dump(mode="json") for m in communication_result.messages]
        autonomy_context: dict[str, Any] = {}
        try:
            if impact_report is not None:
                autonomy_context = {
                    "impact_confidence": float(getattr(impact_report, "confidence", 0.0) or 0.0),
                    "risks": list(getattr(impact_report, "risks", None) or []),
                    "affected_components": list(getattr(impact_report, "affected_components", None) or []),
                    "evidence_count": len(getattr(impact_report, "evidence", None) or []),
                    "stakeholders": [
                        {"team": getattr(s, "team", ""), "impact_type": getattr(s, "impact_type", "")}
                        for s in (getattr(impact_report, "stakeholders", None) or [])
                    ],
                }
        except Exception:
            autonomy_context = {}
        return await self._to_thread(
            agent.analyze,
            pr_input.project_id,
            pr_input.pr_number,
            coordination_plan.summary,
            drafts,
            autonomy_context,
        )

    def _resolve_mentions(self, pr_input: PRAnalysisInput, messages: list, coordination_plan: Any = None) -> tuple[list, list[dict]]:
        """Deterministic role→Slack ID resolution (V1 mentions).

        The agents produce content + recipient identity only. Here every
        draft is forced into the single configured channel with exactly one
        <@USER_ID> mention; unmapped recipients are flagged, never silently
        sent untagged. Preference signals come from the plan task per team.
        """
        from telemetry.spans import annotate_current_span

        from services.slack_mentions import resolve_message_mentions

        try:
            context_dict = pr_input.project_context.model_dump(mode="json")
        except Exception:
            context_dict = {}
        try:
            plan_dict = coordination_plan.model_dump(mode="json") if hasattr(coordination_plan, "model_dump") else dict(coordination_plan or {})
        except Exception:
            plan_dict = {}
        default_channel = str(context_dict.get("default_slack_channel_id", "") or "")
        raw = [m.model_dump(mode="json") if hasattr(m, "model_dump") else dict(m) for m in (messages or [])]
        resolved_dicts, issues = resolve_message_mentions(
            raw, context_dict, default_channel, list(plan_dict.get("tasks") or [])
        )
        resolved = []
        for d in resolved_dicts:
            try:
                from agents.communication_agent import SlackMessage

                resolved.append(SlackMessage.model_validate(d))
            except Exception:
                logger.warning("Dropping unresolvable draft message %s", d.get("id"))
        annotate_current_span(
            {
                "mention.messages": len(resolved),
                "mention.unmapped": len(issues),
                "mention.channel": default_channel,
            }
        )
        if issues:
            logger.warning("PR#%s has %d unmapped Slack recipient(s)", pr_input.pr_number, len(issues))
        return resolved, issues

    async def _persist_outputs(
        self,
        pr_event: PREvent,
        impact_report: ImpactReport,
        coordination_plan: CoordinationPlan,
        communication_bundle: CommunicationBundle,
    ) -> None:
        import asyncio

        pr_id = str(pr_event.pr_number)

        def _sync_persist() -> None:
            # Stale-revision guard: abort only when OUR OWN workflow state was
            # superseded (the gate marks old heads SUPERSEDED when a newer
            # head registers, so direction is handled there — unlike a raw
            # head comparison, this never blocks a legitimate head update).
            if self._is_superseded(pr_event):
                logger.warning(
                    "Stale persist blocked for PR#%s head %s (superseded)",
                    pr_event.pr_number, (pr_event.head_sha or "")[:8],
                )
                return
            base = self.firestore_client.collection("projects").document(pr_event.project_id)
            # Persist under projects/{project_id}/<collection>/{pr_number} as per spec
            # ImpactReport
            impact_payload = impact_report.model_dump(mode="json")
            # Re-key to PR number for PR-level doc
            impact_doc = base.collection("impact_reports").document(pr_id)
            impact_doc.set({**impact_payload, "pr_number": pr_event.pr_number, "persisted_at": pr_payload_time()})

            # CoordinationPlan
            coord_payload = coordination_plan.model_dump(mode="json")
            coord_doc = base.collection("coordination_plans").document(pr_id)
            coord_doc.set({**coord_payload, "pr_number": pr_event.pr_number, "persisted_at": pr_payload_time()})

            # CommunicationBundle
            comm_payload = communication_bundle.model_dump(mode="json")
            comm_doc = base.collection("communications").document(pr_id)
            comm_doc.set({**comm_payload, "pr_number": pr_event.pr_number, "head_sha": pr_event.head_sha, "persisted_at": pr_payload_time()})

        await asyncio.to_thread(_sync_persist)

    def _is_superseded(self, pr_event: PREvent) -> bool:
        """True only when our own head-revision state was superseded by a
        newer head. Legacy path (no store) returns False — old behavior."""
        try:
            store = self._get_pr_store()
            if store is None:
                return False
            mine = store.get(pr_event.project_id, pr_event.pr_number, pr_event.head_sha or "")
            return mine is not None and mine.status == "SUPERSEDED"
        except Exception:
            logger.exception("Supersede check failed for PR#%s — proceeding", pr_event.pr_number)
            return False

    def _stale_bundle(self, pr_event: PREvent) -> CommunicationBundle:
        return CommunicationBundle(
            project_id=pr_event.project_id,
            pr_number=pr_event.pr_number,
            owner=pr_event.owner,
            repository=pr_event.repository,
            messages=[],
            summary=(
                f"PR #{pr_event.pr_number} superseded by a newer revision — "
                "this run stopped before overwriting newer results."
            ),
        )

    async def _persist_pr_reports(
        self,
        pr_event: PREvent,
        impact_report: ImpactReport,
        coordination_plan: CoordinationPlan,
    ) -> bool:
        """Persist PR-level impact + coordination BEFORE supervision, so the
        supervisor's tool (and the UI) finds the coordination plan instead of
        warning. Returns False when superseded (caller should abort)."""
        import asyncio

        pr_id = str(pr_event.pr_number)

        def _sync_persist() -> bool:
            if self._is_superseded(pr_event):
                logger.warning(
                    "Stale run aborted for PR#%s head %s (superseded)",
                    pr_event.pr_number, (pr_event.head_sha or "")[:8],
                )
                return False
            base = self.firestore_client.collection("projects").document(pr_event.project_id)
            base.collection("impact_reports").document(pr_id).set(
                {**impact_report.model_dump(mode="json"), "pr_number": pr_event.pr_number,
                 "persisted_at": pr_payload_time()}
            )
            base.collection("coordination_plans").document(pr_id).set(
                {**coordination_plan.model_dump(mode="json"), "pr_number": pr_event.pr_number,
                 "persisted_at": pr_payload_time()}
            )
            return True

        return await asyncio.to_thread(_sync_persist)

    # --- internal helpers to avoid business logic leakage ---

    def _get_impact_agent(self):
        from agents.impact_analysis_agent import ImpactAnalysisAgent

        return ImpactAnalysisAgent(project=self.project, database=self.database)

    def _get_coordinator_agent(self):
        from agents.coordination_agent import CoordinationAgent

        return CoordinationAgent(project=self.project, database=self.database)

    def _get_communication_agent(self):
        from agents.communication_agent import CommunicationAgent

        return CommunicationAgent(project=self.project, database=self.database)

    async def _ensure_enriched_persisted(self, project_id: str, enriched: EnrichedChangeRecord) -> None:
        import asyncio

        def _sync() -> None:
            doc_ref = (
                self.firestore_client.collection("projects")
                .document(project_id)
                .collection("enriched_changes")
                .document(enriched.commit_sha)
            )
            if not doc_ref.get().exists:
                doc_ref.set(enriched.model_dump(mode="json"))

        await asyncio.to_thread(_sync)

    async def _ensure_impact_persisted(self, project_id: str, report: ImpactReport) -> None:
        import asyncio

        def _sync() -> None:
            doc_ref = (
                self.firestore_client.collection("projects")
                .document(project_id)
                .collection("impact_reports")
                .document(report.commit_sha)
            )
            if not doc_ref.get().exists:
                from datetime import datetime, timezone

                payload = report.model_dump(mode="json")
                payload["saved_at"] = datetime.now(timezone.utc).isoformat()
                doc_ref.set(payload)

        await asyncio.to_thread(_sync)

    async def _ensure_coordination_persisted(self, project_id: str, plan: CoordinationPlan) -> None:
        import asyncio

        def _sync() -> None:
            doc_ref = (
                self.firestore_client.collection("projects")
                .document(project_id)
                .collection("coordination_plans")
                .document(plan.commit_sha)
            )
            if not doc_ref.get().exists:
                from datetime import datetime, timezone

                payload = plan.model_dump(mode="json")
                payload["saved_at"] = datetime.now(timezone.utc).isoformat()
                doc_ref.set(payload)

        await asyncio.to_thread(_sync)

    async def _to_thread(self, func, *args, **kwargs):
        import asyncio

        return await asyncio.to_thread(func, *args, **kwargs)


def pr_payload_time() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
