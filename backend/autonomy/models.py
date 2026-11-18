"""Risk-based autonomous delivery — decision models.

Decisions are per recipient message. The deterministic policy
(autonomy/policy.py) is authoritative; the Supervisor's recommendation is
advisory and can only be downgraded, never upgraded.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

AutonomyDecisionValue = Literal["AUTO_SEND", "HUMAN_REVIEW", "BLOCK", "SKIP"]


class MessageAutonomy(BaseModel):
    """Supervisor's per-message autonomy recommendation (advisory)."""

    message_id: str = Field(description="Stable SlackMessage id, e.g. msg_1")
    recommendation: AutonomyDecisionValue = Field(
        default="HUMAN_REVIEW", description="Supervisor's delivery recommendation"
    )
    reasons: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"] = Field(default="medium")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class AutonomyDecisionResult(BaseModel):
    """Normalized application model for Microsoft Decision-1.1 System One output."""

    message_id: str
    message_intent: str = "unclear"
    creates_unapproved_commitment: bool | None = None
    communication_consequence_score: float | None = None

    recommended_decision: Literal[
        "AUTO_SEND",
        "HUMAN_REVIEW",
        "BLOCK",
        "SKIP"
    ] = "HUMAN_REVIEW"

    decision_probability: float | None = None
    option_probabilities: dict[str, float] | None = None

    provider: str = "microsoft"
    model: str = "Decision-1.1"
    reason_code: str = "default_fallback"


class AutonomyDecision(BaseModel):
    """Final deterministic decision for one message (persisted + audited)."""

    message_id: str = ""
    decision: AutonomyDecisionValue = "HUMAN_REVIEW"
    reasons: list[str] = Field(default_factory=list)
    risk_level: str = "medium"
    confidence: float = 0.0
    evidence_count: int = 0
    supervisor_recommendation: str = "HUMAN_REVIEW"
    policy_version: str = "v1"
    dry_run: bool = False
    decision_model_result: Optional[AutonomyDecisionResult] = None
    shadow_mode: bool = False


class AutonomyBatch(BaseModel):
    """Persisted under projects/{project_id}/autonomy/{pr_number}."""

    project_id: str = ""
    pr_number: int = 0
    head_sha: str = ""
    decisions: list[AutonomyDecision] = Field(default_factory=list)
    dry_run: bool = False
