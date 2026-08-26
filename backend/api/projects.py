from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from agents.context_agent import ContextAgent
from project_context.models import ProjectContextOutput

logger = logging.getLogger("api.projects")

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectContextRequest(BaseModel):
    project_id: str = Field(
        ..., min_length=1, description="Client-provided project identifier used as Firestore document ID"
    )
    project_description: str = Field(
        ..., min_length=1, description="Raw unstructured project description text"
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


@router.post(
    "/context",
    response_model=ProjectContextOutput,
    status_code=status.HTTP_201_CREATED,
    summary="Create project context from description",
)
def create_project_context(
    payload: ProjectContextRequest,
    agent: ContextAgent = Depends(get_context_agent),
) -> ProjectContextOutput:
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
    return output
