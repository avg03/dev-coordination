"""Autonomy batch evaluation + auto-send dispatch.

Pure evaluation (evaluate_batch) plus a thin dispatch layer that reuses the
EXISTING PermissionGateway, IdempotencyManager, ChannelThrottler, and
SlackExecutor. No new delivery pipeline. Only a real `"sent"` result counts
as delivered — mock/failed/denied never do.
"""

from __future__ import annotations

import logging
from typing import Any

from . import config as aconfig
from .models import AutonomyBatch, AutonomyDecision
from .policy import evaluate

logger = logging.getLogger("autonomy.runner")


def tasks_by_team(plan_dict: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for task in (plan_dict or {}).get("tasks") or []:
        if not isinstance(task, dict):
            continue
        key = str(task.get("team", "") or "").strip().lower()
        if key and key not in index:
            index[key] = task
    return index


def evaluate_batch(
    *,
    project_id: str,
    pr_number: int,
    head_sha: str,
    messages: list[dict[str, Any]],
    impact: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    supervisor_autonomy: list[dict[str, Any]] | None = None,
    delivered_ids: set[str] | None = None,
    dry_run: bool = False,
) -> AutonomyBatch:
    """Deterministic per-message decisions. Never raises (fails closed)."""
    impact = impact or {}
    plan = plan or {}
    delivered_ids = delivered_ids or set()
    sup_by_id = {
        str(s.get("message_id", "")): s
        for s in (supervisor_autonomy or [])
        if isinstance(s, dict) and s.get("message_id")
    }
    team_tasks = tasks_by_team(plan)
    decisions: list[AutonomyDecision] = []
    auto_count = 0
    for raw in messages or []:
        msg = dict(raw) if isinstance(raw, dict) else {}
        mid = str(msg.get("id", "") or "")
        sup = sup_by_id.get(mid, {})
        team = str(msg.get("team", "") or "").strip().lower()
        decision = evaluate(
            msg,
            impact=impact,
            task=team_tasks.get(team, {}),
            supervisor_recommendation=str(sup.get("recommendation", "HUMAN_REVIEW") or "HUMAN_REVIEW"),
            supervisor_reasons=list(sup.get("reasons", []) or []),
            already_delivered=mid in delivered_ids,
            auto_sent_this_pr=auto_count,
            dry_run=dry_run,
        )
        if decision.decision == "AUTO_SEND":
            auto_count += 1
        decisions.append(decision)
        audit_decision(project_id, f"pr:{project_id}:{pr_number}:{head_sha or 'na'}", decision, dry_run)
    return AutonomyBatch(
        project_id=project_id, pr_number=pr_number, head_sha=head_sha or "",
        decisions=decisions, dry_run=dry_run,
    )


def dispatch_auto_sends(
    batch: AutonomyBatch,
    messages_by_id: dict[str, dict[str, Any]],
    *,
    gateway: Any = None,
    idempotency: Any = None,
    delivery_store: Any = None,
    sender: Any = None,
    audit_sink: Any = None,
    dry_run: bool = False,
) -> dict[str, dict[str, Any]]:
    """Send AUTO_SEND messages via the existing executor. Returns per-message
    outcomes {message_id: {status, delivered, ...}}. Respects dry-run (no
    sends) and per-channel throttling. Never raises."""
    from security.execution import SlackExecutor
    from services.slack_service import ChannelThrottler, send_delay_seconds

    outcomes: dict[str, dict[str, Any]] = {}
    if dry_run or batch.dry_run:
        for decision in batch.decisions:
            if decision.decision == "AUTO_SEND":
                outcomes[decision.message_id] = {"status": "dry_run", "delivered": False}
                audit_dispatch(batch, decision, "dry_run", False, None)
        return outcomes

    executor = SlackExecutor(
        gateway=gateway, idempotency=idempotency,
        delivery_store=delivery_store, sender=sender, audit_sink=audit_sink,
    )
    throttler = ChannelThrottler(send_delay_seconds())
    workflow_id = f"pr:{batch.project_id}:{batch.pr_number}"
    for decision in batch.decisions:
        if decision.decision != "AUTO_SEND":
            continue
        mid = decision.message_id
        msg = messages_by_id.get(mid)
        if msg is None:
            outcomes[mid] = {"status": "failed", "delivered": False, "reason": "message missing from bundle"}
            continue
        approval_id = f"auto:{batch.project_id}:{batch.pr_number}:{mid}"
        try:
            throttler.wait(str(msg.get("channel", "") or ""))
            result = executor.execute_send(batch.project_id, batch.pr_number, msg, approval_id)
        except Exception as exc:
            logger.exception("Autonomous send failed for %s", mid)
            outcomes[mid] = {"status": "failed", "delivered": False, "reason": str(exc)[:300]}
            audit_dispatch(batch, decision, "failed", False, None)
            continue
        delivered = bool(result.status == "sent" and result.ok)
        outcomes[mid] = {
            "status": result.status,
            "delivered": delivered,
            "channel": result.channel,
            "slack_ts": result.slack_ts,
            "reused": result.reused,
            "reason": result.reason,
            "idempotency_key": result.idempotency_key,
        }
        audit_dispatch(batch, decision, result.status, delivered, result.idempotency_key)
    return outcomes


def load_decisions(project_id: str, pr_number: int, client: Any) -> dict[str, dict[str, Any]]:
    """Best-effort load of persisted autonomy decisions {message_id: {...}}.

    Returns {} when autonomy never ran (manual path unaffected). Never raises.
    """
    try:
        snap = (
            client.collection("projects").document(project_id)
            .collection("autonomy").document(str(pr_number)).get()
        )
        if not snap.exists:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for decision in (snap.to_dict() or {}).get("decisions") or []:
            if isinstance(decision, dict) and decision.get("message_id"):
                out[str(decision["message_id"])] = decision
        return out
    except Exception:
        logger.exception("Autonomy decision load failed for %s/%s", project_id, pr_number)
        return {}


def audit_decision(project_id: str, workflow_id: str, decision: AutonomyDecision, dry_run: bool) -> None:
    try:
        from security.audit import AuditEvent, AuditLogger

        AuditLogger().record(AuditEvent(
            agent_id="autonomy_policy", action="evaluate", resource="communications",
            project_id=project_id, workflow_id=workflow_id,
            decision=decision.decision.lower(),
            reason="; ".join(decision.reasons)[:500] or decision.decision,
            policy=f"autonomy_{decision.policy_version}",
            message_id=decision.message_id, event_type="autonomy.decision",
        ))
    except Exception:
        pass


def audit_dispatch(batch: AutonomyBatch, decision: AutonomyDecision,
                   status: str, delivered: bool, idempotency_key: str | None) -> None:
    try:
        from security.audit import AuditEvent, AuditLogger

        AuditLogger().record(AuditEvent(
            agent_id="autonomy_policy", action="send", resource="slack",
            project_id=batch.project_id,
            workflow_id=f"pr:{batch.project_id}:{batch.pr_number}:{batch.head_sha or 'na'}",
            idempotency_key=idempotency_key or "",
            decision="executed" if delivered else status,
            reason=f"autonomy={decision.decision} dry_run={batch.dry_run} delivered={delivered}",
            policy=f"autonomy_{decision.policy_version}",
            message_id=decision.message_id, event_type="autonomy.dispatch",
        ))
    except Exception:
        pass
