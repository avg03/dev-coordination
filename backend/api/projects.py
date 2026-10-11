from __future__ import annotations

import logging

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from agents.communication_agent import CommunicationResult
from agents.context_agent import ContextAgent
from agents.coordination_agent import CoordinationPlan
from agents.impact_analysis_agent import ImpactReport
from project_context.models import ProjectContextOutput
from workflows.pr_workflow import CommunicationBundle

logger = logging.getLogger("api.projects")

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectContextRequest(BaseModel):
    project_id: str = Field(
        ..., min_length=1, description="Client-provided project identifier used as Firestore document ID"
    )
    project_description: str = Field(
        ..., min_length=1, description="Raw unstructured project description text"
    )


class ProjectContextResponse(BaseModel):
    success: bool = Field(default=True, description="Whether creation succeeded")
    project_context: ProjectContextOutput = Field(description="Validated ProjectContext model")


class CommitTimelineItem(BaseModel):
    commit_sha: str = Field(description="Commit SHA")
    summary: str = Field(default="", description="Change summary")
    components: list[str] = Field(default_factory=list, description="Affected components")
    timestamp: str = Field(default="", description="Commit timestamp ISO string")


class CommitTimelineResponse(BaseModel):
    commits: list[CommitTimelineItem] = Field(default_factory=list, description="Ordered commit timeline")


class PRReviewResponse(BaseModel):
    project_id: str = Field(description="Project identifier")
    pr_number: int = Field(description="Pull request number")
    impact_report: ImpactReport | None = Field(default=None, description="PR-level impact analysis")
    coordination_plan: CoordinationPlan | None = Field(default=None, description="PR-level coordination plan")
    communication_bundle: CommunicationBundle | CommunicationResult | None = Field(
        default=None, description="PR-level communication bundle"
    )
    workflow: dict | None = Field(
        default=None,
        description="Readiness-gate workflow state (WAITING_FOR_COMMITS, READY, PROCESSING, COMPLETED, PARTIAL, FAILED) — lets the UI distinguish waiting/partial/failed from success",
    )


def get_context_agent() -> ContextAgent:
    try:
        return ContextAgent()
    except ValueError as exc:
        logger.error("Failed to initialize ContextAgent: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Context agent is not configured",
        ) from exc


def get_firestore_client():
    try:
        from google.cloud import firestore
        from github_infrastructure.config import settings

        project = settings.gcp_project
        database = os.getenv("FIRESTORE_DATABASE", "(default)")
        if not project:
            raise ValueError("GCP project is not configured")
        kwargs: dict = {"project": project}
        if database and database != "(default)":
            kwargs["database"] = database
        return firestore.Client(**kwargs)
    except ValueError as exc:
        logger.error("Failed to initialize Firestore client: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Firestore is not configured",
        ) from exc
    except Exception as exc:
        logger.exception("Failed to create Firestore client")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to connect to Firestore",
        ) from exc


@router.post(
    "/context",
    response_model=ProjectContextResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create project context from description",
)
def create_project_context(
    payload: ProjectContextRequest,
    agent: ContextAgent = Depends(get_context_agent),
) -> ProjectContextResponse:
    try:
        output, document_id, saved_by_tool = agent.analyze_and_save(
            payload.project_description,
            document_id=payload.project_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("Context agent failed to produce output")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Agent failed: {exc}"
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected error during project context creation")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error"
        ) from exc

    logger.info(
        "Project context created project_id=%s document_id=%s saved_by_tool=%s",
        payload.project_id,
        document_id,
        saved_by_tool,
    )
    return ProjectContextResponse(success=True, project_context=output)


@router.get(
    "/{project_id}/commits",
    response_model=CommitTimelineResponse,
    summary="Get commit timeline for project",
)
def get_commit_timeline(
    project_id: str,
    limit: Optional[int] = Query(None, ge=1, le=100, description="Max commits to return"),
    client=Depends(get_firestore_client),
) -> CommitTimelineResponse:
    if not project_id or not project_id.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="project_id cannot be empty")

    try:
        col_ref = client.collection("projects").document(project_id).collection("enriched_changes")
        query = col_ref.order_by("timestamp", direction="DESCENDING")
        if limit is not None:
            query = query.limit(limit)
        docs = list(query.stream())
    except Exception as exc:
        logger.exception("Failed to fetch commits for project_id=%s", project_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to fetch commits"
        ) from exc

    commits: list[CommitTimelineItem] = []
    for doc in docs:
        data = doc.to_dict() or {}
        # EnrichedChangeRecord stores timestamp as ISO string via model_dump(mode="json")
        ts = data.get("timestamp", "")
        # Firestore may store timestamp as datetime object if written via SDK; normalize to ISO string
        if hasattr(ts, "isoformat"):
            try:
                ts = ts.isoformat()
            except Exception:
                ts = str(ts)
        commits.append(
            CommitTimelineItem(
                commit_sha=data.get("commit_sha", doc.id),
                summary=data.get("summary", ""),
                components=data.get("components", []),
                timestamp=ts or data.get("saved_at", ""),
            )
        )

    return CommitTimelineResponse(commits=commits)


@router.get(
    "/{project_id}/prs/{pr_number}",
    response_model=PRReviewResponse,
    summary="Get PR review artifacts",
)
def get_pr_review(
    project_id: str,
    pr_number: int,
    client=Depends(get_firestore_client),
) -> PRReviewResponse:
    if not project_id or not project_id.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="project_id cannot be empty")
    if pr_number <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="pr_number must be positive")

    pr_id = str(pr_number)
    base = client.collection("projects").document(project_id)

    def _fetch_doc(collection: str, doc_id: str):
        try:
            snap = base.collection(collection).document(doc_id).get()
            if snap.exists:
                return snap.to_dict()
            return None
        except Exception as exc:
            logger.exception("Failed to fetch %s/%s for %s", collection, doc_id, project_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch {collection}",
            ) from exc

    impact_data = _fetch_doc("impact_reports", pr_id)
    coord_data = _fetch_doc("coordination_plans", pr_id)
    comm_data = _fetch_doc("communications", pr_id)

    if not any([impact_data, coord_data, comm_data]):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PR {pr_number} not found for project {project_id}",
        )

    impact_report = None
    coordination_plan = None
    communication_bundle = None

    if impact_data:
        try:
            impact_report = ImpactReport.model_validate(impact_data)
        except Exception:
            logger.warning("Invalid ImpactReport shape for %s/%s", project_id, pr_id)

    if coord_data:
        try:
            coordination_plan = CoordinationPlan.model_validate(coord_data)
        except Exception:
            logger.warning("Invalid CoordinationPlan shape for %s/%s", project_id, pr_id)

    if comm_data:
        # PR workflow persists CommunicationBundle; fallback to CommunicationResult for backward compat
        try:
            communication_bundle = CommunicationBundle.model_validate(comm_data)
        except Exception:
            try:
                communication_bundle = CommunicationResult.model_validate(comm_data)  # type: ignore
            except Exception:
                logger.warning("Invalid CommunicationBundle shape for %s/%s", project_id, pr_id)

    # Readiness-gate workflow state (best-effort, additive — never breaks review).
    workflow_state: dict | None = None
    try:
        from coordination.stores import FirestorePRWorkflowStore
        from github_infrastructure.config import settings as _settings

        if _settings.gcp_project:
            _prs = FirestorePRWorkflowStore(project=_settings.gcp_project)
            _head = ""
            if isinstance(comm_data, dict):
                _head = str(comm_data.get("head_sha", "") or "")
            if _head:
                _rec = _prs.get(project_id, pr_number, _head)
            else:
                _candidates = _prs.list_for_pr(project_id, pr_number)
                _rec = max(_candidates, key=lambda r: r.updated_at) if _candidates else None
            if _rec is not None:
                workflow_state = _rec.model_dump(mode="json")
    except Exception:
        logger.warning("PR workflow state lookup failed for %s/%s", project_id, pr_id)

    return PRReviewResponse(
        project_id=project_id,
        pr_number=pr_number,
        impact_report=impact_report,
        coordination_plan=coordination_plan,
        communication_bundle=communication_bundle,
        workflow=workflow_state,
    )


class ApprovePRRequest(BaseModel):
    approved: bool = Field(description="Whether user approved Slack delivery")
    selected_messages: list[str] = Field(
        default_factory=list, description="List of SlackMessage ids to send, e.g., [\"msg_1\",\"msg_3\"]. Empty means all when approved=true"
    )


class ApprovePRResponse(BaseModel):
    success: bool = Field(description="Whether approval was processed")
    delivery_status: str = Field(description="Delivery status: sent, rejected, failed, mock_sent")
    selected_messages: list[str] = Field(description="Ids that were processed")
    sent_results: list[dict] = Field(default_factory=list, description="Per-message Slack results")
    message: str = Field(default="", description="Human readable summary")


@router.post(
    "/{project_id}/prs/{pr_number}/approve",
    response_model=ApprovePRResponse,
    summary="Human-in-the-loop approval for Slack delivery",
)
def approve_pr(
    project_id: str,
    pr_number: int,
    payload: ApprovePRRequest,
    client=Depends(get_firestore_client),
) -> ApprovePRResponse:
    if not project_id or not project_id.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="project_id cannot be empty")
    if pr_number <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="pr_number must be positive")

    pr_id = str(pr_number)
    doc_ref = client.collection("projects").document(project_id).collection("communications").document(pr_id)

    try:
        snap = doc_ref.get()
        if not snap.exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Communication bundle not found for PR {pr_number} in project {project_id}",
            )
        data = snap.to_dict() or {}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to fetch communication bundle for approve %s/%s", project_id, pr_number)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to fetch bundle") from exc

    # Determine messages in bundle (support both CommunicationBundle and CommunicationResult shapes)
    raw_messages = data.get("messages") or []
    # Build id -> message map, ensuring ids exist
    id_to_msg: dict[str, dict] = {}
    for idx, msg in enumerate(raw_messages, start=1):
        mid = msg.get("id") or f"msg_{idx}"
        # Normalize missing id in stored data
        if not msg.get("id"):
            msg["id"] = mid
        id_to_msg[mid] = msg

    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()

    if not payload.approved:
        # Rejected path - update status, do not send
        try:
            doc_ref.set(
                {
                    "approved": False,
                    "delivery_status": "rejected",
                    "selected_messages": payload.selected_messages,
                    "approved_at": now_iso,
                    "updated_at": now_iso,
                },
                merge=True,
            )
        except Exception as exc:
            logger.exception("Failed to update delivery status to rejected for %s/%s", project_id, pr_number)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update status") from exc
        return ApprovePRResponse(
            success=True,
            delivery_status="rejected",
            selected_messages=payload.selected_messages,
            sent_results=[],
            message="PR delivery rejected by user",
        )

    # approved == True
    target_ids = payload.selected_messages
    if not target_ids:
        # Empty means all
        target_ids = list(id_to_msg.keys())

    # Validate requested ids exist
    invalid = [mid for mid in target_ids if mid not in id_to_msg]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid message ids: {invalid}. Available: {sorted(id_to_msg.keys())}",
        )

    # Approval binding + authorization + idempotent execution.
    # The approve endpoint is the authoritative human approval; the gateway
    # then authorizes each send and the SlackExecutor guarantees exactly-once
    # delivery (retries return the stored result, never a duplicate message).
    from security.approval import ApprovalValidationError, build_approval, validate_approval_for_message
    from security.execution import get_slack_executor, new_approval_id

    approval_id = new_approval_id()
    approval = build_approval(
        approval_id=approval_id,
        project_id=project_id,
        pr_number=pr_number,
        approved=True,
        approved_at=now_iso,
        selected_message_ids=target_ids,
    )

    executor = get_slack_executor()
    sent_results: list[dict] = []
    denied_any = False

    for mid in target_ids:
        msg = id_to_msg[mid]
        channel = msg.get("channel") or ""
        try:
            validate_approval_for_message(approval, project_id, str(pr_number), mid, msg)
        except ApprovalValidationError as exc:
            logger.warning("Approval binding rejected message %s: %s", mid, exc)
            sent_results.append(
                {"id": mid, "channel": channel, "result": {"status": "denied", "ok": False, "error": str(exc)}}
            )
            denied_any = True
            continue
        delivery = executor.execute_send(project_id, pr_number, msg, approval_id)
        sent_results.append(
            {
                "id": mid,
                "channel": delivery.channel or channel,
                "result": {
                    "status": delivery.status,
                    "ok": delivery.ok,
                    "error": delivery.reason or None,
                    "ts": delivery.slack_ts,
                    "reused": delivery.reused,
                },
            }
        )
        if delivery.status in ("denied", "in_progress"):
            denied_any = True

    # Determine overall delivery status
    has_real = any(r["result"].get("status") == "sent" for r in sent_results)
    has_mock = any(r["result"].get("status") == "mock_sent" for r in sent_results)
    has_error = any(not r["result"].get("ok") for r in sent_results)
    if denied_any or has_error:
        delivery_status = "failed"
    elif has_real and not has_mock:
        delivery_status = "sent"
    elif has_mock and not has_real:
        delivery_status = "mock_sent"
    else:
        delivery_status = "sent" if has_real else "mock_sent"

    # Update Firestore same doc
    try:
        doc_ref.set(
            {
                "approved": True,
                "delivery_status": delivery_status,
                "selected_messages": target_ids,
                "sent_results": sent_results,
                "sent_at": now_iso,
                "approved_at": now_iso,
                "updated_at": now_iso,
            },
            merge=True,
        )
    except Exception as exc:
        logger.exception("Failed to update delivery status to %s for %s/%s", delivery_status, project_id, pr_number)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update delivery status") from exc

    return ApprovePRResponse(
        success=True,
        delivery_status=delivery_status,
        selected_messages=target_ids,
        sent_results=sent_results,
        message=f"Delivered {len(sent_results)} message(s) via Slack ({delivery_status})",
    )
