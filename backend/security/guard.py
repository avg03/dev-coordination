"""Central ADK tool guard.

`make_before_tool_guard(agent_id)` produces a Google ADK `before_tool_callback`
that routes every tool call through the PermissionGateway. Returning a dict
short-circuits the tool (denied); returning None lets it proceed.

Attach once per agent — security checks are not duplicated inside agent code.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .gateway import PermissionGateway, get_permission_gateway
from .permissions import (
    POLICY_UNKNOWN_TOOL,
    TOOL_RESOURCE_MAP,
    AuthorizationRequest,
    PolicyDecision,
)

logger = logging.getLogger("security.guard")

_DENY_RESULT = {"status": "error"}


def make_before_tool_guard(agent_id: str, gateway: Optional[PermissionGateway] = None):
    """Build an ADK-compatible before_tool_callback bound to one agent identity."""

    def _guard(tool: Any, args: dict[str, Any], tool_context: Any) -> Optional[dict[str, Any]]:
        gw = gateway or get_permission_gateway()
        tool_name = getattr(tool, "name", "") or getattr(tool, "__name__", "")

        mapped = TOOL_RESOURCE_MAP.get((agent_id, tool_name))
        if mapped is None:
            decision = PolicyDecision(
                allowed=False,
                reason=f"Tool '{tool_name}' is not registered for agent '{agent_id}'",
                policy=POLICY_UNKNOWN_TOOL,
                agent_id=agent_id,
            )
        else:
            action, resource = mapped
            decision = gw.authorize(
                AuthorizationRequest(
                    agent_id=agent_id,
                    action=action,
                    resource=resource,
                    project_id=_extract_project_id(args),
                    tool_name=tool_name,
                )
            )

        if not decision.allowed:
            logger.warning(
                "tool.execution denied agent=%s tool=%s policy=%s reason=%s",
                agent_id,
                tool_name,
                decision.policy,
                decision.reason,
            )
            return {
                **_DENY_RESULT,
                "error": f"permission denied [{decision.policy}]: {decision.reason}",
            }

        logger.info("tool.execution allowed agent=%s tool=%s", agent_id, tool_name)
        return None

    return _guard


def _extract_project_id(args: dict[str, Any]) -> str:
    return str(args.get("project_id", "") or "")
