"""Tests for Microsoft Decision-1.1 System One Decision Service and Autonomy Policy Integration."""

import json
from unittest.mock import MagicMock, patch
import pytest

from autonomy.models import AutonomyDecisionResult
from autonomy.policy import evaluate
from autonomy.runner import evaluate_batch
from services.decision_service import DecisionService


def test_build_request_payload():
    service = DecisionService(
        endpoint="https://test.services.ai.azure.com",
        deployment="Decision-1.1",
        api_key="test-key",
    )
    message = {
        "id": "msg_1",
        "text": "• Changed auth check\n• Impact: awareness",
        "recipient": "Alice",
        "team": "Security",
        "channel": "C012345",
        "slack_user_id": "U012345",
        "evidence": ["PR #10 diff"],
    }
    payload = service.build_request_payload(message)
    assert payload["model"] == "Decision-1.1"
    assert payload["state"]["action_type"] == "slack.send"
    assert "Changed auth check" in payload["state"]["message_text"]
    assert "questions" in payload
    assert "autonomy_decision" in payload["questions"]
    assert "creates_unapproved_commitment" in payload["questions"]
    assert "communication_consequence" in payload["questions"]
    assert "message_intent" in payload["questions"]


def test_parse_response_auto_send():
    service = DecisionService(
        endpoint="https://test.services.ai.azure.com",
        deployment="Decision-1.1",
        api_key="test-key",
    )
    mock_data = {
        "model": "microsoft-decision-1.1",
        "answers": {
            "autonomy_decision": {
                "type": "choice",
                "choice": "AUTO_SEND",
                "confidence": 0.95,
                "probabilities": {
                    "AUTO_SEND": 0.95,
                    "HUMAN_REVIEW": 0.05,
                },
            },
            "communication_consequence": {
                "type": "score",
                "score": 0.12,
            },
            "creates_unapproved_commitment": {
                "type": "noul",
                "noul": 0.02,
            },
            "message_intent": {
                "type": "choice",
                "choice": "routine_coordination",
            },
        },
    }
    result = service.parse_response("msg_1", mock_data)
    assert result.message_id == "msg_1"
    assert result.recommended_decision == "AUTO_SEND"
    assert result.creates_unapproved_commitment is False
    assert result.communication_consequence_score == 0.12
    assert result.message_intent == "routine_coordination"
    assert result.decision_probability == 0.95


def test_parse_response_uncertain_maps_to_human_review():
    service = DecisionService(
        endpoint="https://test.services.ai.azure.com",
        deployment="Decision-1.1",
        api_key="test-key",
    )
    mock_data = {
        "model": "microsoft-decision-1.1",
        "answers": {
            "autonomy_decision": {
                "type": "choice",
                "choice": "UNCERTAIN",
                "probabilities": {"UNCERTAIN": 0.70, "HUMAN_REVIEW": 0.30},
            },
        },
    }
    result = service.parse_response("msg_1", mock_data)
    assert result.recommended_decision == "HUMAN_REVIEW"


def test_malformed_response_fallback():
    service = DecisionService(
        endpoint="https://test.services.ai.azure.com",
        deployment="Decision-1.1",
        api_key="test-key",
    )
    result = service.parse_response("msg_1", {"bad": "data"})
    assert result.recommended_decision == "HUMAN_REVIEW"
    assert "fallback" in result.reason_code


def test_http_error_safe_fallback():
    service = DecisionService(
        endpoint="https://test.services.ai.azure.com",
        deployment="Decision-1.1",
        api_key="test-key",
    )
    with patch("urllib.request.urlopen", side_effect=Exception("Connection timed out")):
        result = service.evaluate_message({"id": "msg_1", "text": "test"})
        assert result.recommended_decision == "HUMAN_REVIEW"
        assert "fallback" in result.reason_code


def test_security_change_grounded_awareness_auto_send():
    """An auth/security PR with a grounded awareness notification can be AUTO_SEND

    when Decision-1.1 determines communication consequence is low.
    """
    message = {
        "id": "msg_1",
        "text": "<@U012345> • Auth module updated\n• Evidence: verified token schema",
        "channel": "C012345",
        "slack_user_id": "U012345",
        "unmapped": False,
        "impact_type": "awareness",
        "evidence": ["item 1", "item 2"],
    }
    impact = {
        "confidence": 0.95,
        "evidence": ["ev1", "ev2"],
        "risks": ["security authentication logic updated"],
        "affected_components": ["auth"],
    }
    task = {"component": "auth", "impact_type": "awareness", "priority": "normal"}

    dm_result = AutonomyDecisionResult(
        message_id="msg_1",
        message_intent="awareness",
        creates_unapproved_commitment=False,
        communication_consequence_score=0.1,
        recommended_decision="AUTO_SEND",
        decision_probability=0.92,
        reason_code="decision_model:auto_send:awareness",
    )

    decision = evaluate(
        message,
        impact=impact,
        task=task,
        supervisor_recommendation="AUTO_SEND",
        canonical_status="validated",
        decision_result=dm_result,
        shadow_mode=False,
    )
    assert decision.decision == "AUTO_SEND"


def test_unapproved_commitment_forces_human_review():
    """When the message creates an unapproved commitment or deadline,

    it must be downgraded to HUMAN_REVIEW.
    """
    message = {
        "id": "msg_1",
        "text": "<@U012345> • You are assigned to complete the database migration by Friday",
        "channel": "C012345",
        "slack_user_id": "U012345",
        "unmapped": False,
        "impact_type": "action",
        "evidence": ["ev1", "ev2"],
    }
    impact = {"confidence": 0.95, "evidence": ["ev1", "ev2"]}
    task = {"component": "database", "impact_type": "action", "priority": "normal"}

    dm_result = AutonomyDecisionResult(
        message_id="msg_1",
        message_intent="new_commitment",
        creates_unapproved_commitment=True,
        communication_consequence_score=2.0,
        recommended_decision="HUMAN_REVIEW",
        decision_probability=0.88,
        reason_code="decision_model:human_review:new_commitment",
    )

    decision = evaluate(
        message,
        impact=impact,
        task=task,
        supervisor_recommendation="AUTO_SEND",
        canonical_status="validated",
        decision_result=dm_result,
        shadow_mode=False,
    )
    assert decision.decision == "HUMAN_REVIEW"
    assert any("unapproved commitment" in r or "new_commitment" in r for r in decision.reasons)


def test_permission_gateway_hard_policy_unmapped_recipient_blocks():
    """Deterministic policy wins: even if Decision-1.1 recommends AUTO_SEND,

    an unmapped recipient MUST be BLOCK.
    """
    message = {
        "id": "msg_1",
        "text": "• Notification text without recipient mapping",
        "channel": "C012345",
        "slack_user_id": "",
        "unmapped": True,
        "evidence": ["ev1", "ev2"],
    }
    impact = {"confidence": 0.95, "evidence": ["ev1", "ev2"]}

    dm_result = AutonomyDecisionResult(
        message_id="msg_1",
        message_intent="awareness",
        creates_unapproved_commitment=False,
        communication_consequence_score=0.1,
        recommended_decision="AUTO_SEND",
        decision_probability=0.99,
        reason_code="decision_model:auto_send:awareness",
    )

    decision = evaluate(
        message,
        impact=impact,
        supervisor_recommendation="AUTO_SEND",
        canonical_status="validated",
        decision_result=dm_result,
        shadow_mode=False,
    )
    assert decision.decision == "BLOCK"
    assert any("no valid Slack user mapping" in r for r in decision.reasons)


def test_permission_gateway_missing_channel_blocks():
    """Deterministic policy wins: missing channel MUST be BLOCK."""
    message = {
        "id": "msg_1",
        "text": "<@U012345> • Message text",
        "channel": "",
        "slack_user_id": "U012345",
        "unmapped": False,
        "evidence": ["ev1", "ev2"],
    }
    dm_result = AutonomyDecisionResult(
        message_id="msg_1",
        message_intent="awareness",
        creates_unapproved_commitment=False,
        communication_consequence_score=0.1,
        recommended_decision="AUTO_SEND",
        decision_probability=0.99,
        reason_code="decision_model:auto_send:awareness",
    )
    decision = evaluate(
        message,
        impact={"confidence": 0.95, "evidence": ["ev1", "ev2"]},
        supervisor_recommendation="AUTO_SEND",
        canonical_status="validated",
        decision_result=dm_result,
        shadow_mode=False,
    )
    assert decision.decision == "BLOCK"
    assert any("no delivery channel" in r for r in decision.reasons)


def test_permission_gateway_already_delivered_skips():
    """Already delivered message MUST be SKIP."""
    message = {
        "id": "msg_1",
        "text": "<@U012345> • Message text",
        "channel": "C012345",
        "slack_user_id": "U012345",
        "evidence": ["ev1", "ev2"],
    }
    dm_result = AutonomyDecisionResult(
        message_id="msg_1",
        recommended_decision="AUTO_SEND",
        reason_code="decision_model:auto_send",
    )
    decision = evaluate(
        message,
        impact={"confidence": 0.95, "evidence": ["ev1", "ev2"]},
        supervisor_recommendation="AUTO_SEND",
        already_delivered=True,
        canonical_status="validated",
        decision_result=dm_result,
        shadow_mode=False,
    )
    assert decision.decision == "SKIP"
    assert any("already delivered" in r for r in decision.reasons)


def test_evaluate_batch_with_mock_decision_service():
    mock_service = MagicMock()
    mock_service.is_configured = True
    mock_service.evaluate_message.return_value = AutonomyDecisionResult(
        message_id="msg_1",
        message_intent="awareness",
        creates_unapproved_commitment=False,
        communication_consequence_score=0.2,
        recommended_decision="AUTO_SEND",
        decision_probability=0.94,
        reason_code="decision_model:auto_send:awareness",
    )

    messages = [{
        "id": "msg_1",
        "recipient": "Alice",
        "text": "<@U012345> • Routine notification",
        "team": "frontend",
        "channel": "C012345",
        "slack_user_id": "U012345",
        "impact_type": "awareness",
        "evidence": ["ev1", "ev2"],
    }]

    batch = evaluate_batch(
        project_id="test-proj",
        pr_number=10,
        head_sha="abc1234",
        messages=messages,
        impact={"confidence": 0.9, "evidence": ["ev1", "ev2"]},
        plan={"tasks": [{"team": "frontend", "impact_type": "awareness", "priority": "normal"}]},
        canonical={"classification_status": "validated"},
        decision_service=mock_service,
    )
    assert len(batch.decisions) == 1
    assert batch.decisions[0].decision == "AUTO_SEND"
    assert batch.decisions[0].decision_model_result is not None
    assert batch.decisions[0].decision_model_result.recommended_decision == "AUTO_SEND"
