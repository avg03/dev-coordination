"""Role-based Slack mentions V1 — one message per person, one channel, one mention.

Run: python backend/tests/test_slack_mentions.py (no GCP, no Slack needed)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_context.models import is_valid_slack_channel_id, is_valid_slack_user_id
from services.slack_mentions import count_mentions, resolve_message_mentions, strip_invented_mentions

CTX = {
    "default_slack_channel_id": "C012345",
    "stakeholders": [
        {"name": "Rahul", "role": "Mobile Dev", "team": "mobile", "slack_user_id": "U111111"},
        {"name": "Priya", "role": "QA", "team": "qa", "slack_user_id": "U222222"},
    ],
}


def _draft(mid, recipient, team, text="Auth API response changed."):
    return {"id": mid, "recipient": recipient, "team": team, "channel": "", "mode": "group", "text": text}


# 1. One affected person -> exactly one message with exactly one mention -----
def test_01_single_person_single_mention():
    resolved, issues = resolve_message_mentions(
        [_draft("msg_1", "Rahul", "mobile")], CTX, CTX["default_slack_channel_id"]
    )
    assert issues == [], issues
    assert len(resolved) == 1
    msg = resolved[0]
    assert msg["channel"] == "C012345", msg
    assert count_mentions(msg["text"]) == 1, msg["text"]
    assert msg["text"].startswith("<@U111111>"), msg["text"]
    assert msg["slack_user_id"] == "U111111" and msg["unmapped"] is False


# 2. Two people -> two messages, same channel, one correct mention each ------
def test_02_two_people_two_mentions_same_channel():
    resolved, issues = resolve_message_mentions(
        [_draft("msg_1", "Rahul", "mobile"), _draft("msg_2", "Priya", "qa")],
        CTX, CTX["default_slack_channel_id"],
    )
    assert issues == [] and len(resolved) == 2
    assert {m["channel"] for m in resolved} == {"C012345"}
    assert resolved[0]["text"].startswith("<@U111111>")
    assert resolved[1]["text"].startswith("<@U222222>")
    assert all(count_mentions(m["text"]) == 1 for m in resolved)
    assert "<@U222222>" not in resolved[0]["text"] and "<@U111111>" not in resolved[1]["text"]


# 2b. Agent-invented mentions are stripped, never trusted --------------------
# 2a. Preference signals joined deterministically from the plan task -------
def test_02a_preference_signals_from_plan():
    tasks = [
        {"team": "mobile", "priority": "high", "impact_type": "action",
         "action_required": True, "component": "auth-service"},
        {"team": "qa", "priority": "low", "impact_type": "awareness",
         "action_required": False, "component": "auth-service"},
    ]
    resolved, issues = resolve_message_mentions(
        [_draft("msg_1", "Rahul", "mobile"), _draft("msg_2", "Priya", "qa")],
        CTX, CTX["default_slack_channel_id"], tasks,
    )
    assert issues == []
    mobile, qa = resolved
    assert (mobile["priority"], mobile["impact_type"], mobile["action_required"], mobile["component"]) == (
        "high", "action", True, "auth-service"), mobile
    assert (qa["priority"], qa["impact_type"], qa["action_required"]) == ("low", "awareness", False), qa
    # Mentions still exact after enrichment.
    assert mobile["text"].startswith("<@U111111>") and count_mentions(mobile["text"]) == 1
    # Unknown team -> neutral defaults, still delivered.
    resolved2, _ = resolve_message_mentions(
        [_draft("msg_9", "Rahul", "mobile")], CTX, CTX["default_slack_channel_id"], [],
    )
    assert resolved2[0]["priority"] == "" and resolved2[0]["action_required"] is False


def test_02b_invented_mentions_stripped():
    draft = _draft("msg_1", "Rahul", "mobile", text="<@U999999> please review <@UEVIL>.")
    resolved, _ = resolve_message_mentions([draft], CTX, CTX["default_slack_channel_id"])
    assert count_mentions(resolved[0]["text"]) == 1
    assert "<@U999999>" not in resolved[0]["text"] and resolved[0]["text"].startswith("<@U111111>")
    assert strip_invented_mentions("<@U1> hi <@U2>") == " hi "


# 3. Missing/invalid mappings handled safely ---------------------------------
def test_03_unmapped_never_sends_untagged():
    resolved, issues = resolve_message_mentions(
        [_draft("msg_1", "Ghost", "mobile"), _draft("msg_2", "Rahul", "mobile")],
        CTX, CTX["default_slack_channel_id"],
    )
    ghost = resolved[0]
    assert ghost["unmapped"] is True and ghost["channel"] == ""
    assert count_mentions(ghost["text"]) == 0, "no mention without a mapping"
    assert len(issues) == 1 and issues[0]["message_id"] == "msg_1"

    # Known person but no user id configured -> also flagged.
    ctx2 = {"default_slack_channel_id": "C012345",
            "stakeholders": [{"name": "Rahul", "team": "mobile", "slack_user_id": ""}]}
    resolved2, issues2 = resolve_message_mentions([_draft("msg_1", "Rahul", "mobile")], ctx2, "C012345")
    assert resolved2[0]["unmapped"] is True and issues2[0]["reason"].startswith("stakeholder")

    # No channel configured -> mapped user still flagged, never sent bare.
    resolved3, issues3 = resolve_message_mentions([_draft("msg_1", "Rahul", "mobile")], CTX, "")
    assert resolved3[0]["unmapped"] is True and resolved3[0]["channel"] == ""


def test_03b_id_validation():
    assert is_valid_slack_user_id("U012345") and is_valid_slack_user_id("W012345")
    assert not is_valid_slack_user_id("rahul") and not is_valid_slack_user_id("U12")
    assert is_valid_slack_channel_id("C012345") and is_valid_slack_channel_id("G012345")
    assert not is_valid_slack_channel_id("#general") and not is_valid_slack_channel_id("")


# 4. Duplicate processing does not resend ------------------------------------
def test_04_retry_reuses_delivery():
    from security.audit import InMemoryAuditSink
    from security.execution import InMemoryDeliveryStore, SlackExecutor
    from security.gateway import PermissionGateway
    from security.idempotency import IdempotencyManager, InMemoryIdempotencyStore

    calls: list = []
    ex = SlackExecutor(
        gateway=PermissionGateway(audit_sink=InMemoryAuditSink()),
        idempotency=IdempotencyManager(InMemoryIdempotencyStore()),
        delivery_store=InMemoryDeliveryStore(),
        sender=lambda channel, text: (calls.append((channel, text)), {"status": "sent", "ok": True, "ts": "1.0"})[1],
    )
    resolved, _ = resolve_message_mentions([_draft("msg_1", "Rahul", "mobile")], CTX, "C012345")
    msg = resolved[0]
    r1 = ex.execute_send("proj-1", 7, msg, approval_id="appr_1")
    assert r1.status == "sent" and r1.channel == "C012345" and len(calls) == 1
    r2 = ex.execute_send("proj-1", 7, msg, approval_id="appr_1")  # retry/redelivery
    assert r2.reused is True and len(calls) == 1, "duplicate processing must not resend"


# 5. Approval + permission checks remain enforced -----------------------------
def test_05_approval_and_unmapped_denied():
    from security.audit import InMemoryAuditSink
    from security.execution import InMemoryDeliveryStore, SlackExecutor
    from security.gateway import PermissionGateway
    from security.idempotency import IdempotencyManager, InMemoryIdempotencyStore

    def make_exec():
        return SlackExecutor(
            gateway=PermissionGateway(audit_sink=InMemoryAuditSink()),
            idempotency=IdempotencyManager(InMemoryIdempotencyStore()),
            delivery_store=InMemoryDeliveryStore(),
            sender=lambda channel, text: {"status": "sent", "ok": True, "ts": "1.0"},
        )

    resolved, _ = resolve_message_mentions([_draft("msg_1", "Rahul", "mobile")], CTX, "C012345")
    # No approval id -> gateway denies (existing policy intact).
    denied = make_exec().execute_send("proj-1", 7, resolved[0], approval_id="")
    assert denied.status == "denied" and denied.ok is False, denied

    # Unmapped message (empty channel) -> gateway recipient policy denies.
    ghost, _ = resolve_message_mentions([_draft("msg_9", "Ghost", "mobile")], CTX, "C012345")
    denied2 = make_exec().execute_send("proj-1", 7, ghost[0], approval_id="appr_1")
    assert denied2.status == "denied" and denied2.ok is False, denied2


# 6. Sequential sends respect per-channel rate limits -------------------------
def test_06_channel_throttle_paces_same_channel():
    from services.slack_service import ChannelThrottler

    th = ChannelThrottler(delay_seconds=0.05)
    stamps: list[float] = []
    for _ in range(3):
        th.wait("C012345")
        stamps.append(time.monotonic())
    assert stamps[1] - stamps[0] >= 0.04, stamps
    assert stamps[2] - stamps[1] >= 0.04, stamps

    # Different channels are independent — no artificial delay.
    th2 = ChannelThrottler(delay_seconds=60.0)
    th2.wait("C000001")
    start = time.monotonic()
    th2.wait("C000002")
    assert time.monotonic() - start < 1.0


if __name__ == "__main__":
    test_01_single_person_single_mention()
    test_02_two_people_two_mentions_same_channel()
    test_02a_preference_signals_from_plan()
    test_02b_invented_mentions_stripped()
    test_03_unmapped_never_sends_untagged()
    test_03b_id_validation()
    test_04_retry_reuses_delivery()
    test_05_approval_and_unmapped_denied()
    test_06_channel_throttle_paces_same_channel()
    print("=== ALL SLACK MENTION TESTS PASSED ===")
