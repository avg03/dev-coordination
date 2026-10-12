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
from .models import AutonomyDecision

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
        )
    except Exception as exc:  # never block the pipeline on policy bugs
        logger.exception("Autonomy policy failed for %s", mid)
        return AutonomyDecision(
            message_id=mid, decision="HUMAN_REVIEW",
            reasons=[f"policy error, defaulting to review: {exc}"[:300]],
            supervisor_recommendation=supervisor_recommendation,
            dry_run=dry_run,
        )


def _evaluate(message, *, impact, task, supervisor_recommendation,
              supervisor_reasons, already_delivered, auto_sent_this_pr,
              dry_run, reasons) -> AutonomyDecision:
    mid = str(message.get("id", "") or "")
    sup_rec = supervisor_recommendation if supervisor_recommendation in _ORDER else "HUMAN_REVIEW"

    def decide(decision: str, risk: str, extra: list[str]) -> AutonomyDecision:
        final = _more_restrictive(decision, sup_rec)
        all_reasons = list(extra)
        if final != decision:
            all_reasons.append(f"supervisor recommended {sup_rec} (more restrictive)")
        elif sup_rec != "AUTO_SEND" and decision == "AUTO_SEND":
            all_reasons.append(f"supervisor concurred ({sup_rec})" if sup_rec == "HUMAN_REVIEW" else "")
            all_reasons = [r for r in all_reasons if r]
        if supervisor_reasons and final != "AUTO_SEND":
            all_reasons.extend([f"supervisor: {r}"[:300] for r in supervisor_reasons[:3]])
        return AutonomyDecision(
            message_id=mid, decision=final,  # type: ignore[arg-type]
            reasons=all_reasons[:8], risk_level=risk,
            confidence=float(impact.get("confidence", 0.0) or 0.0),
            evidence_count=int(len(message.get("evidence", []) or []) + len(impact.get("evidence", []) or [])),
            supervisor_recommendation=sup_rec, dry_run=dry_run,
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

    # --- risk signals (security-sensitive => HUMAN_REVIEW regardless) ---
    risks = impact.get("risks", []) or []
    risk_text = " ".join(str(r) for r in risks)
    hit = _security_hit(
        risk_text,
        " ".join(str(c) for c in impact.get("affected_components", []) or []),
        str(task.get("component", "") or ""),
        str(message.get("text", "") or "")[:1000],
    )
    if hit:
        return decide("HUMAN_REVIEW", "high", [f"security-sensitive keyword {hit!r} — human approval required"])
    if impact_type == "approval":
        return decide("HUMAN_REVIEW", "high", ["approval impact — human sign-off required regardless of confidence"])
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
    if impact_type == "action" and not aconfig.allow_action():
        return decide("HUMAN_REVIEW", "medium", ["action impact requires human approval (AUTONOMY_ALLOW_ACTION off)"])
    if auto_sent_this_pr >= aconfig.max_auto_per_pr():
        return decide("HUMAN_REVIEW", "medium",
                      [f"per-PR auto-send cap reached ({aconfig.max_auto_per_pr()})"])

    risk = "low" if impact_type == "awareness" else "medium"
    return decide("AUTO_SEND", risk, [
        f"low-risk {impact_type} message, confidence {confidence:.2f}, evidence x{evidence_count}",
    ])
