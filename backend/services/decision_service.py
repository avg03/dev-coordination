"""Microsoft Decision-1.1 System One Decision Service.

Calls Microsoft Foundry's System One decision endpoint:
    POST <FOUNDRY_RESOURCE_ENDPOINT>/providers/microsoft/v1/systemone

Evaluates the communication consequence and autonomy level of a generated
Slack message using typed System One questions (choice, noul, score).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from typing import Any, Optional

from autonomy import config as aconfig
from autonomy.models import AutonomyDecisionResult

logger = logging.getLogger("services.decision_service")

# Static question definitions matching the Microsoft System One contract
SYSTEM_ONE_QUESTIONS: dict[str, Any] = {
    "message_intent": {
        "type": "choice",
        "instructions": (
            "Classify the primary intent of the proposed Slack message. Consider what "
            "the message actually communicates or requests, not merely the topic of the "
            "underlying code change."
        ),
        "criteria": {
            "awareness": "Communicates factual information without assigning work",
            "routine_coordination": "Requests review or verification within existing responsibilities",
            "new_commitment": "Creates a new responsibility, deadline, or obligation",
            "organizational_decision": "Announces or makes a consequential organizational decision",
            "unclear": "Cannot reliably determine the message intent",
        },
    },
    "creates_unapproved_commitment": {
        "type": "noul",
        "instructions": (
            "Does the message itself create a new responsibility, deadline, commitment, or "
            "consequential instruction not already authorized by the supplied policy? Do not "
            "treat a routine request for PR review as a new commitment."
        ),
        "criteria": {
            "true": "The message creates an unauthorized new obligation or commitment",
            "false": "The message only provides awareness or requests authorized routine coordination",
        },
    },
    "communication_consequence": {
        "type": "score",
        "instructions": (
            "Evaluate the consequences of sending this message itself. Do not equate "
            "code-change sensitivity with communication risk."
        ),
        "criteria": [
            "Low: factual awareness or routine authorized coordination",
            "Moderate: possible confusion or nontrivial follow-up",
            "High: significant operational or organizational consequences",
            "Critical: severe disruption or prohibited consequences",
        ],
    },
    "autonomy_decision": {
        "type": "choice",
        "instructions": (
            "Recommend the appropriate communication autonomy level using the message, "
            "supporting evidence, and provided policy context. Security-sensitive code "
            "changes can still generate safe automatic notifications. Prefer human review "
            "for unapproved commitments, material uncertainty, or consequential communication. "
            "Select uncertain when there is insufficient information. Do not treat the model "
            "recommendation as permission to execute."
        ),
        "criteria": {
            "AUTO_SEND": "Grounded awareness or authorized routine coordination with low communication risk",
            "HUMAN_REVIEW": "Requires human judgment because of material uncertainty, unclear authority, or consequential communication",
            "BLOCK": "The available information indicates invalid, prohibited, or unauthorized communication",
            "SKIP": "No legitimate notification is needed",
            "UNCERTAIN": "Insufficient information to classify reliably",
        },
    },
}


class DecisionService:
    """Invokes Microsoft Decision-1.1 via System One API."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        deployment: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.endpoint = (endpoint or aconfig.decision_foundry_endpoint()).rstrip("/")
        self.deployment = deployment or aconfig.decision_model_deployment()
        self.api_key = api_key or aconfig.decision_api_key()
        self.timeout_seconds = timeout_seconds

    @property
    def is_configured(self) -> bool:
        return bool(self.endpoint and self.deployment and self.api_key)

    def build_request_payload(
        self,
        message: dict[str, Any],
        *,
        message_context: Optional[dict[str, Any]] = None,
        supervisor_assessment: Optional[dict[str, Any]] = None,
        trusted_policy_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Constructs a typed System One request payload."""
        state = {
            "action_type": "slack.send",
            "message_text": str(message.get("text", "") or message.get("body", "")),
            "message_context": message_context or {
                "recipient": message.get("recipient", ""),
                "team": message.get("team", ""),
                "impact_type": message.get("impact_type", "awareness"),
                "priority": message.get("priority", "normal"),
                "notification_only": message.get("impact_type") != "approval",
            },
            "supervisor_assessment": supervisor_assessment or {
                "message_intent": "routine_coordination" if message.get("impact_type") in ("action", "approval") else "awareness",
                "action_burden": "low" if message.get("priority") != "critical" else "high",
                "creates_new_commitment": False,
                "claims_organizational_authority": False,
                "evidence_quality": "grounded" if bool(message.get("evidence")) else "sparse",
                "communication_consequence": "low",
            },
            "trusted_policy_context": trusted_policy_context or {
                "recipient_authorized": not bool(message.get("unmapped")),
                "channel_authorized": bool(message.get("channel")),
                "existing_responsibility": True,
                "routine_review_request_allowed": True,
                "contains_prohibited_secrets": False,
            },
        }

        return {
            "model": self.deployment,
            "state": state,
            "questions": SYSTEM_ONE_QUESTIONS,
        }

    def evaluate_message(
        self,
        message: dict[str, Any],
        *,
        message_context: Optional[dict[str, Any]] = None,
        supervisor_assessment: Optional[dict[str, Any]] = None,
        trusted_policy_context: Optional[dict[str, Any]] = None,
    ) -> AutonomyDecisionResult:
        """Synchronously calls Microsoft Decision-1.1 and returns normalized result.

        Never raises: on any network, timeout, parse, or HTTP error, safely falls
        back to HUMAN_REVIEW.
        """
        mid = str(message.get("id", "") or "msg_unknown")
        if not self.is_configured:
            logger.warning(
                "DecisionService not configured (endpoint=%s, deployment=%s, key_set=%s) — fallback to HUMAN_REVIEW",
                bool(self.endpoint),
                bool(self.deployment),
                bool(self.api_key),
            )
            return self._fallback_result(mid, "service_not_configured")

        url = f"{self.endpoint}/providers/microsoft/v1/systemone"
        payload = self.build_request_payload(
            message,
            message_context=message_context,
            supervisor_assessment=supervisor_assessment,
            trusted_policy_context=trusted_policy_context,
        )

        headers = {
            "Content-Type": "application/json",
            "api-key": self.api_key,
        }

        started_at = time.perf_counter()
        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=req_data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                status_code = resp.status
                raw_body = resp.read().decode("utf-8")
                elapsed_ms = (time.perf_counter() - started_at) * 1000.0

            if status_code != 200:
                logger.error("Decision API returned status %s: %s (in %.1fms)", status_code, raw_body[:200], elapsed_ms)
                return self._fallback_result(mid, f"http_status_{status_code}")

            data = json.loads(raw_body)
            result = self.parse_response(mid, data)
            logger.info(
                "Decision-1.1 evaluated %s -> %s (intent=%s, prob=%.3f, elapsed=%.1fms)",
                mid,
                result.recommended_decision,
                result.message_intent,
                result.decision_probability or 0.0,
                elapsed_ms,
            )
            return result

        except urllib.error.HTTPError as exc:
            elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8")
            except Exception:
                pass
            logger.error("Decision API HTTPError %s: %s (in %.1fms)", exc.code, error_body[:200], elapsed_ms)
            return self._fallback_result(mid, f"http_error_{exc.code}")
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            logger.exception("Decision API call failed for message %s after %.1fms", mid, elapsed_ms)
            return self._fallback_result(mid, f"exception_{type(exc).__name__}")

    def parse_response(self, message_id: str, data: dict[str, Any]) -> AutonomyDecisionResult:
        """Maps System One answers to the normalized AutonomyDecisionResult."""
        answers = data.get("answers")
        if not isinstance(answers, dict):
            return self._fallback_result(message_id, "missing_answers_dict")

        # 1. message_intent (choice)
        intent_info = answers.get("message_intent") or {}
        message_intent = str(intent_info.get("choice", "unclear") or "unclear").lower()

        # 2. creates_unapproved_commitment (noul)
        commitment_info = answers.get("creates_unapproved_commitment") or {}
        noul_val = commitment_info.get("noul")
        creates_commitment: Optional[bool] = None
        if isinstance(noul_val, (int, float)):
            # Evaluate threshold: > 0.5 indicates affirmative commitment
            creates_commitment = float(noul_val) > 0.5

        # 3. communication_consequence (score)
        consequence_info = answers.get("communication_consequence") or {}
        score_val = consequence_info.get("score")
        consequence_score: Optional[float] = None
        if isinstance(score_val, (int, float)):
            consequence_score = float(score_val)

        # 4. autonomy_decision (choice)
        decision_info = answers.get("autonomy_decision") or {}
        raw_choice = str(decision_info.get("choice", "HUMAN_REVIEW") or "HUMAN_REVIEW").strip().upper()
        probs = decision_info.get("probabilities") if isinstance(decision_info.get("probabilities"), dict) else {}
        conf = decision_info.get("confidence")
        if conf is None and probs and raw_choice in probs:
            conf = probs[raw_choice]

        # Allowed decisions mapping: UNCERTAIN and invalid map to HUMAN_REVIEW
        valid_decisions = {"AUTO_SEND", "HUMAN_REVIEW", "BLOCK", "SKIP"}
        recommended: Any = raw_choice
        if raw_choice not in valid_decisions or raw_choice == "UNCERTAIN":
            recommended = "HUMAN_REVIEW"

        reason_code = f"decision_model:{recommended.lower()}:{message_intent}"
        if creates_commitment:
            reason_code += ":commitment_flagged"

        return AutonomyDecisionResult(
            message_id=message_id,
            message_intent=message_intent,
            creates_unapproved_commitment=creates_commitment,
            communication_consequence_score=consequence_score,
            recommended_decision=recommended,
            decision_probability=float(conf) if isinstance(conf, (int, float)) else None,
            option_probabilities={str(k): float(v) for k, v in probs.items() if isinstance(v, (int, float))},
            provider=aconfig.decision_provider(),
            model=str(data.get("model") or self.deployment),
            reason_code=reason_code,
        )

    def _fallback_result(self, message_id: str, reason_code: str) -> AutonomyDecisionResult:
        """Safe fallback: defaults to HUMAN_REVIEW on any error or missing config."""
        return AutonomyDecisionResult(
            message_id=message_id,
            message_intent="unclear",
            creates_unapproved_commitment=None,
            communication_consequence_score=None,
            recommended_decision="HUMAN_REVIEW",
            decision_probability=0.0,
            option_probabilities=None,
            provider=aconfig.decision_provider(),
            model=self.deployment,
            reason_code=f"fallback:{reason_code}",
        )
