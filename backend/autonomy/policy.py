"""Deterministic autonomy policy — pure functions, no I/O, no LLM.

Authoritative over the Supervisor's advisory recommendation: the final
decision is the MORE restrictive of the two (BLOCK wins over everything;
policy can only downgrade AUTO_SEND, never upgrade a BLOCK/SKIP).

Restrictiveness order: AUTO_SEND < HUMAN_REVIEW < SKIP < BLOCK.
"""

from __future__ import annotations

import logging
from typing import Any

from . import config as aconfig
from .models import AutonomyDecision, AutonomyDecisionResult

logger = logging.getLogger("autonomy.policy")

_ORDER = {"AUTO_SEND": 0, "HUMAN_REVIEW": 1, "SKIP": 2, "BLOCK": 3}


def _more_restrictive(first: str, second: str) -> str:
    return first if _ORDER.get(first, 1) >= _ORDER.get(second, 1) else second


def _security_hit(*texts: str) -> str | None:
    joined = " ".join(t for t in texts if t).lower()
    for keyword in aconfig.SECURITY_KEYWORDS:
        if keyword in joined:
            return keyword
    return None


def evaluate(
    message: dict[str, Any],
    *,
    impact: dict[str, Any] | None = None,
    task: dict[str, Any] | None = None,
    supervisor_recommendation: str = "HUMAN_REVIEW",
    supervisor_reasons: list[str] | None = None,
    already_delivered: bool = False,
    auto_sent_this_pr: int = 0,
    dry_run: bool = False,
    requires_approval: bool = False,
    canonical_status: str = "needs_review",
    consistency_issues: list[str] | None = None,
    decision_result: AutonomyDecisionResult | None = None,
    shadow_mode: bool = False,
) -> AutonomyDecision:
    """Evaluate one resolved SlackMessage. Never raises (fails to HUMAN_REVIEW)."""
    impact = impact or {}
    task = task or {}
    mid = str(message.get("id", "") or "")
    reasons: list[str] = []
    try:
        return _evaluate(
            message, impact=impact, task=task,
            supervisor_recommendation=supervisor_recommendation,
            supervisor_reasons=supervisor_reasons or [],
            already_delivered=already_delivered,
            auto_sent_this_pr=auto_sent_this_pr,
            dry_run=dry_run, reasons=reasons,
            requires_approval=requires_approval,
            canonical_status=canonical_status,
            consistency_issues=consistency_issues or [],
            decision_result=decision_result,
            shadow_mode=shadow_mode,
        )
    except Exception as exc:  # never block the pipeline on policy bugs
        logger.exception("Autonomy policy failed for %s", mid)
        return AutonomyDecision(
            message_id=mid, decision="HUMAN_REVIEW",
            reasons=[f"policy error, defaulting to review: {exc}"[:300]],
            supervisor_recommendation=supervisor_recommendation,
            dry_run=dry_run,
            decision_model_result=decision_result,
            shadow_mode=shadow_mode,
        )


def _evaluate(message, *, impact, task, supervisor_recommendation,
              supervisor_reasons, already_delivered, auto_sent_this_pr,
              dry_run, reasons, requires_approval, canonical_status,
              consistency_issues, decision_result=None, shadow_mode=False) -> AutonomyDecision:
    mid = str(message.get("id", "") or "")
    # When Decision-1.1 is active, its structured decision is the primary recommendation.
    # When in shadow mode or Decision-1.1 is disabled, fallback to supervisor's recommendation.
    if decision_result is not None and not shadow_mode:
        effective_advisory = decision_result.recommended_decision
    else:
        effective_advisory = supervisor_recommendation

    sup_rec = effective_advisory if effective_advisory in _ORDER else "HUMAN_REVIEW"

    def decide(decision: str, risk: str, extra: list[str]) -> AutonomyDecision:
        final = _more_restrictive(decision, sup_rec)
        all_reasons = list(extra)
        if final != decision:
            all_reasons.append(f"advisory recommended {sup_rec} (more restrictive)")
        if supervisor_reasons and final != "AUTO_SEND":
            all_reasons.extend([f"supervisor: {r}"[:300] for r in supervisor_reasons[:3]])
        if decision_result is not None and decision_result.reason_code:
            all_reasons.append(f"decision_model:{decision_result.reason_code}")
        return AutonomyDecision(
            message_id=mid, decision=final,  # type: ignore[arg-type]
            reasons=all_reasons[:8], risk_level=risk,
            confidence=float(impact.get("confidence", 0.0) or 0.0),
            evidence_count=int(len(message.get("evidence", []) or []) + len(impact.get("evidence", []) or [])),
            supervisor_recommendation=supervisor_recommendation, dry_run=dry_run,
            decision_model_result=decision_result, shadow_mode=shadow_mode,
        )

    # --- BLOCK: invalid data / unmapped / prohibited (never sendable) ---
    text = str(message.get("text", "") or "").strip()
    if not text:
        return decide("BLOCK", "high", ["empty message text"])
    if message.get("unmapped") or not str(message.get("slack_user_id", "") or "").strip():
        return decide("BLOCK", "high", ["no valid Slack user mapping for recipient"])
    if not str(message.get("channel", "") or "").strip():
        return decide("BLOCK", "high", ["no delivery channel resolved"])
    from services.slack_mentions import count_mentions

    mentions = count_mentions(message.get("text", ""))
    if mentions != 1:
        return decide("BLOCK", "high", [f"expected exactly 1 mention, found {mentions}"])

    # --- SKIP: nothing to notify ---
    if already_delivered:
        return decide("SKIP", "low", ["already delivered — not resent"])
    impact_type = str(task.get("impact_type", "") or message.get("impact_type", "") or "").strip().lower()
    if impact_type in ("", "none"):
        return decide("SKIP", "low", ["no notifiable impact for recipient"])

    # Decision model specific guards
    if decision_result is not None and not shadow_mode:
        if decision_result.recommended_decision == "BLOCK":
            return decide("BLOCK", "high", [f"decision model flagged invalid communication: {decision_result.reason_code}"])
        if decision_result.creates_unapproved_commitment is True:
            return decide("HUMAN_REVIEW", "high", ["message creates an unapproved commitment or obligation — human review required"])
        if decision_result.communication_consequence_score is not None and decision_result.communication_consequence_score >= 1.5:
            return decide("HUMAN_REVIEW", "high", [f"communication consequence score {decision_result.communication_consequence_score:.2f} — human review required"])
        if decision_result.message_intent in ("new_commitment", "organizational_decision"):
            return decide("HUMAN_REVIEW", "high", [f"message intent '{decision_result.message_intent}' requires human review"])

    # --- risk signals (security-sensitive vs communication risk) ---
    risks = impact.get("risks", []) or []
    risk_text = " ".join(str(r) for r in risks)
    hit = _security_hit(
        risk_text,
        " ".join(str(c) for c in impact.get("affected_components", []) or []),
        str(task.get("component", "") or ""),
        str(message.get("text", "") or "")[:1000],
    )

    # When Decision-1.1 specifically evaluates the message consequence as low and recommends AUTO_SEND,
    # safe grounded awareness notifications about security modules are allowed.
    decision_allows_safe_security = (
        decision_result is not None
        and not shadow_mode
        and decision_result.recommended_decision == "AUTO_SEND"
        and (decision_result.communication_consequence_score or 0.0) < 1.0
        and not decision_result.creates_unapproved_commitment
    )

    if hit and not decision_allows_safe_security:
        return decide("HUMAN_REVIEW", "high", [f"security-sensitive keyword {hit!r} — human approval required"])

    if impact_type == "approval":
        return decide("HUMAN_REVIEW", "high", ["approval impact — human sign-off required regardless of confidence"])
    if requires_approval:
        return decide("HUMAN_REVIEW", "high", ["coordination plan requires approval — human sign-off required"])
    if canonical_status == "conflict":
        return decide("HUMAN_REVIEW", "high", ["unresolved canonical change-type conflict — explicit review required"])
    if canonical_status != "validated":
        return decide("HUMAN_REVIEW", "medium", [f"canonical context {canonical_status} — autonomous delivery blocked"])
    if consistency_issues:
        first = str(consistency_issues[0])[:200]
        extra = f" (+{len(consistency_issues) - 1} more)" if len(consistency_issues) > 1 else ""
        return decide("HUMAN_REVIEW", "high", [f"consistency check failed: {first}{extra}"])
    if str(task.get("priority", "") or "").strip().lower() == "critical":
        return decide("HUMAN_REVIEW", "high", ["critical priority — human approval required"])

    confidence = float(impact.get("confidence", 0.0) or 0.0)
    evidence_count = len(message.get("evidence", []) or []) + len(impact.get("evidence", []) or [])
    if confidence < aconfig.min_confidence():
        return decide("HUMAN_REVIEW", "medium",
                      [f"confidence {confidence:.2f} below threshold {aconfig.min_confidence():.2f}"])
    if evidence_count < aconfig.min_evidence():
        return decide("HUMAN_REVIEW", "medium",
                      [f"evidence items {evidence_count} below minimum {aconfig.min_evidence()}"])
    if impact_type == "action" and not aconfig.allow_action() and not (decision_result and decision_result.recommended_decision == "AUTO_SEND" and not shadow_mode):
        return decide("HUMAN_REVIEW", "medium", ["action impact requires human approval (AUTONOMY_ALLOW_ACTION off)"])
    if auto_sent_this_pr >= aconfig.max_auto_per_pr():
        return decide("HUMAN_REVIEW", "medium",
                      [f"per-PR auto-send cap reached ({aconfig.max_auto_per_pr()})"])

    risk = "low" if impact_type == "awareness" else "medium"
    return decide("AUTO_SEND", risk, [
        f"low-risk {impact_type} message, confidence {confidence:.2f}, evidence x{evidence_count}",
    ])
