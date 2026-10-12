"""Role-based Slack mention resolution — deterministic, no LLM.

Contract with the Communication Agent: the agent produces message content
plus recipient identity (name + team) and must NOT invent Slack IDs or
channels. This module resolves identities to admin-configured
`slack_user_id`s, forces every message into the single configured channel,
and guarantees exactly one `<@USER_ID>` mention per message.

Unmapped recipients are flagged (`unmapped=True`, empty channel) — never
silently sent untagged. The PermissionGateway already denies empty-channel
sends, so flagged messages surface as denied in the approval results.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("services.slack_mentions")

# Matches Slack user mentions like <@U012345>. Anything the agent emits in
# this shape is untrusted (invented) and gets stripped before resolution.
MENTION_RE = re.compile(r"<@[A-Z0-9]+>")


def strip_invented_mentions(text: str) -> str:
    """Remove any <@...> mentions — only resolver-issued mentions may remain."""
    return MENTION_RE.sub("", text or "")


def count_mentions(text: str) -> int:
    return len(MENTION_RE.findall(text or ""))


def _stakeholder_index(project_context: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for person in project_context.get("stakeholders") or []:
        if not isinstance(person, dict):
            continue
        key = (
            str(person.get("name", "")).strip().lower(),
            str(person.get("team", "")).strip().lower(),
        )
        if key[0]:
            index.setdefault(key, person)
    return index


def resolve_message_mentions(
    messages: list[dict[str, Any]],
    project_context: dict[str, Any],
    default_channel: str = "",
    coordination_tasks: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve one message per person to a single mention in one channel.

    Returns (resolved_messages, issues). Each resolved message keeps its
    stable id; exactly one <@SLACK_USER_ID> mention is (pre)pended for
    mapped recipients. Unmapped recipients get unmapped=True + empty
    channel + an issue entry {message_id, recipient, reason}.

    Preference signals (priority/impact_type/action_required/component) are
    joined deterministically from the CoordinationPlan task for the
    message's team so the UI can recommend whom to notify first.
    """
    channel = (default_channel or "").strip()
    index = _stakeholder_index(project_context)
    task_by_team: dict[str, dict[str, Any]] = {}
    for task in coordination_tasks or []:
        if not isinstance(task, dict):
            continue
        team_key = str(task.get("team", "") or "").strip().lower()
        if team_key and team_key not in task_by_team:
            task_by_team[team_key] = task
    resolved: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for position, raw in enumerate(messages or []):
        msg = dict(raw) if isinstance(raw, dict) else {}
        mid = str(msg.get("id") or f"msg_{position + 1}").strip() or f"msg_{position + 1}"
        msg["id"] = mid
        recipient = str(msg.get("recipient", "") or "").strip()
        team = str(msg.get("team", "") or "").strip()
        task = task_by_team.get(team.lower(), {})
        msg["priority"] = str(task.get("priority", "") or "").strip().lower()
        msg["impact_type"] = str(task.get("impact_type", "") or "").strip().lower()
        msg["action_required"] = bool(task.get("action_required", False))
        msg["component"] = str(task.get("component", "") or "").strip()
        person = index.get((recipient.lower(), team.lower()))
        if person is None and recipient:
            # Fall back to name-only match when the team differs/empty.
            for (name, _team), candidate in index.items():
                if name == recipient.lower():
                    person = candidate
                    break

        body = strip_invented_mentions(str(msg.get("text", "") or "")).strip()
        user_id = str((person or {}).get("slack_user_id", "") or "").strip()

        if not person or not user_id:
            msg["text"] = body
            msg["channel"] = ""
            msg["mode"] = "group"
            msg["slack_user_id"] = ""
            msg["unmapped"] = True
            reason = (
                f"no stakeholder match for '{recipient}'" if not person
                else f"stakeholder '{recipient}' has no slack_user_id mapping"
            )
            issues.append({"message_id": mid, "recipient": recipient or mid, "reason": reason})
            logger.warning("Unmapped Slack recipient %s (%s)", mid, reason)
        elif not channel:
            msg["text"] = body
            msg["channel"] = ""
            msg["mode"] = "group"
            msg["slack_user_id"] = user_id
            msg["unmapped"] = True
            issues.append({
                "message_id": mid,
                "recipient": recipient or mid,
                "reason": "no default Slack channel configured — set it on the Slack Mapping page",
            })
            logger.warning("Unmapped Slack channel for %s (no default channel)", mid)
        else:
            mention = f"<@{user_id}>"
            msg["text"] = f"{mention} {body}".strip()
            msg["channel"] = channel
            msg["mode"] = "group"
            msg["slack_user_id"] = user_id
            msg["unmapped"] = False

        if mid in seen_ids:
            logger.warning("Duplicate message id %s — keeping first occurrence order", mid)
        seen_ids.add(mid)
        resolved.append(msg)

    return resolved, issues
