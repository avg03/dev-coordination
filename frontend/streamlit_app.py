"""Minimal Streamlit frontend for the Impact-Aware Engineering Change Coordinator.

Presentation/client layer only. No business logic lives here — all backend
communication goes through ``api_client.py``.

Sidebar:
  - Backend URL / status
  - Project ID input
  - Navigation: Project Context, Work Journey, PR Review
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=False)
load_dotenv(override=False)

from api_client import (  # noqa: E402
    BackendApiError,
    approve_pr,
    check_backend_health,
    create_project_context,
    get_backend_url,
    get_pr_review,
    get_project_commits,
    get_project_context,
    get_project_id,
    update_slack_mappings,
)

st.set_page_config(
    page_title="Impact-Aware Engineering Change Coordinator",
    page_icon="◉",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render_error(exc: Exception) -> None:
    if isinstance(exc, BackendApiError):
        detail = f" — {exc.detail}" if exc.detail else ""
        code = f" [{exc.status_code}]" if exc.status_code else ""
        st.error(f"{exc}{code}{detail}")
        if exc.url:
            st.caption(f"URL: {exc.url}")
        # Actionable hint for the 404 that users hit on /api/projects/context when backend not yet patched
        if exc.status_code == 404:
            url = (exc.url or "").lower()
            if "/api/projects/context" in url or "not found" in (exc.detail or "").lower():
                st.warning(
                    "404 on `/api/projects/context` usually means the backend is running without the projects router. "
                    "Make sure `backend/github_infrastructure/main.py` includes the projects router (it now does) and **restart** the backend: "
                    "`uvicorn github_infrastructure.main:app --reload` from `backend/` (or `uvicorn backend.github_infrastructure.main:app` from repo root). "
                    "Then check `GET /openapi.json` contains `/api/projects/context`."
                )
            elif "/prs/" in url:
                st.caption("This PR has no bundle yet (404 is expected until the PR workflow writes `impact_reports`/`coordination_plans`/`communications`).")
            else:
                st.caption("Check that `BACKEND_URL` is correct and the backend is reachable.")
        elif exc.status_code is None and exc.url:
            st.caption("Backend unavailable — is it running? Check `BACKEND_URL` and that the backend process is up.")
    elif isinstance(exc, ValueError):
        st.warning(str(exc))
    else:
        st.error(f"Unexpected error: {exc}")


def _json_view(data: object, expanded: bool = False) -> None:
    try:
        st.json(data, expanded=expanded)
    except Exception:
        st.code(json.dumps(data, indent=2, default=str), language="json")


def _format_timestamp(value: str) -> str:
    if not value:
        return "—"
    try:
        # Normalize ISO strings
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return value


def _short_sha(sha: str) -> str:
    if not sha:
        return "—"
    return sha[:7]


def _extract_messages(bundle: dict | None) -> list[dict]:
    """Handle both CommunicationBundle and CommunicationResult shapes."""
    if not isinstance(bundle, dict):
        return []
    msgs = bundle.get("messages") or []
    if msgs:
        return [m for m in msgs if isinstance(m, dict)]
    # Fallback: nested communication_result.messages
    nested = bundle.get("communication_result")
    if isinstance(nested, dict):
        nested_msgs = nested.get("messages") or []
        return [m for m in nested_msgs if isinstance(m, dict)]
    # Fallback: total shape where bundle is CommunicationResult directly
    return []


def _recommendation(msg: dict) -> tuple[bool, str]:
    """Notify-first preference: mapped + must-act beats FYI.

    Recommended when the recipient is mapped AND (action_required OR
    impact is action/approval OR priority high/critical). Unmapped and
    pure-awareness messages are never preselected. Human still approves.
    """
    if not isinstance(msg, dict) or msg.get("unmapped"):
        return False, ""
    reasons: list[str] = []
    if msg.get("action_required"):
        reasons.append("action required")
    itype = str(msg.get("impact_type") or "").lower()
    if itype in ("action", "approval"):
        reasons.append(f"impact: {itype}")
    prio = str(msg.get("priority") or "").lower()
    if prio in ("high", "critical"):
        reasons.append(f"priority: {prio}")
    if not reasons:
        return False, ""
    return True, "; ".join(reasons)


def _render_project_context_structured(ctx: dict) -> None:
    """Render ProjectContextOutput in readable expandable sections."""
    if not isinstance(ctx, dict):
        _json_view(ctx)
        return

    project = ctx.get("project", {})
    work_scope = ctx.get("work_scope", {})
    architecture = ctx.get("architecture", {})
    stakeholders = ctx.get("stakeholders", [])
    team_context = ctx.get("team_context", [])
    planned_dependencies = ctx.get("planned_dependencies", [])
    success_criteria = ctx.get("success_criteria", [])
    raw_context = ctx.get("raw_context", {})
    metadata = ctx.get("metadata", {})

    # Summary header
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Project", project.get("name", "—") or "—")
    with c2:
        st.metric("Status", project.get("status", "—") or "—")
    with c3:
        st.metric("Domain", project.get("domain", "—") or "—")

    if project.get("objective"):
        st.info(f"**Objective:** {project.get('objective')}")
    if project.get("description"):
        st.caption(project.get("description"))

    # Work scope
    with st.expander("Work Scope", expanded=True):
        if work_scope.get("current_goal"):
            st.markdown(f"**Current goal:** {work_scope['current_goal']}")
        tasks = work_scope.get("planned_tasks", []) or []
        if tasks:
            st.markdown(f"**Planned tasks ({len(tasks)}):**")
            for t in tasks:
                tid = t.get("id", "")
                title = t.get("title", "")
                priority = t.get("priority", "")
                status = t.get("status", "")
                desc = t.get("description", "")
                st.markdown(f"- `{tid}` **{title}** — {desc}  \n  `priority:{priority}` `status:{status}`")
        else:
            st.caption("No planned tasks.")
        if work_scope.get("constraints"):
            st.markdown("**Constraints:**")
            for con in work_scope["constraints"]:
                st.markdown(f"- {con}")
        if work_scope.get("important_decisions"):
            st.markdown("**Important decisions:**")
            for d in work_scope["important_decisions"]:
                st.markdown(f"- {d}")

    # Architecture
    with st.expander("Architecture", expanded=False):
        comps = architecture.get("components", []) or []
        deps = architecture.get("dependencies", []) or []
        if comps:
            st.markdown(f"**Components ({len(comps)}):**")
            for comp in comps:
                name = comp.get("name") or comp.get("id") or "—"
                ctype = comp.get("type", "")
                desc = comp.get("description", "")
                paths = ", ".join(comp.get("repository_paths", []) or [])
                teams = ", ".join(comp.get("responsible_teams", []) or [])
                st.markdown(f"- **{name}** `{ctype}` — {desc}")
                if paths:
                    st.caption(f"  paths: {paths}")
                if teams:
                    st.caption(f"  teams: {teams}")
        else:
            st.caption("No components.")
        if deps:
            st.markdown(f"**Dependencies ({len(deps)}):**")
            for d in deps:
                st.markdown(
                    f"- `{d.get('source_component','')}` → `{d.get('target_component','')}` "
                    f"({d.get('relationship','')}) — {d.get('description','')}"
                )
        else:
            st.caption("No dependencies.")

    # Stakeholders
    with st.expander(f"Stakeholders ({len(stakeholders)})", expanded=False):
        if stakeholders:
            for s in stakeholders:
                name = s.get("name", "—")
                role = s.get("role", "")
                team = s.get("team", "")
                pref = s.get("communication_preference", "")
                slack_u = s.get("slack_user_id") or "—"
                slack_c = s.get("slack_channel_id") or "—"
                st.markdown(f"- **{name}** — {role} (`{team}`) · pref: `{pref}`")
                st.caption(f"  slack_user: {slack_u} · slack_channel: {slack_c}")
        else:
            st.caption("No stakeholders.")

    # Team context
    with st.expander(f"Teams ({len(team_context)})", expanded=False):
        if team_context:
            for t in team_context:
                name = t.get("name", "—")
                owned = ", ".join(t.get("owned_components", []) or [])
                chan = t.get("slack_channel_id", "")
                resp = t.get("responsibilities", []) or []
                st.markdown(f"- **{name}**  · channel: `{chan or '—'}` · owned: `{owned or '—'}`")
                if resp:
                    for r in resp:
                        st.markdown(f"  - {r}")
        else:
            st.caption("No teams.")

    # Planned dependencies
    if planned_dependencies:
        with st.expander(f"Planned Dependencies ({len(planned_dependencies)})", expanded=False):
            for pd in planned_dependencies:
                st.markdown(
                    f"- {pd.get('description','')} — `{pd.get('source_task','')}` → `{pd.get('dependent_task','')}` "
                    f"(team: {pd.get('affected_team','')})"
                )

    # Success criteria
    if success_criteria:
        with st.expander("Success Criteria", expanded=False):
            for crit in success_criteria:
                st.markdown(f"- {crit}")

    # Raw + metadata
    with st.expander("Raw Context & Metadata", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Raw context**")
            st.write(f"Source type: `{raw_context.get('source_type','')}`")
            if raw_context.get("original_description"):
                st.caption(raw_context["original_description"][:1000])
        with c2:
            st.markdown("**Metadata**")
            st.write(f"Created: {_format_timestamp(metadata.get('created_at',''))}")
            st.write(f"Updated: {_format_timestamp(metadata.get('updated_at',''))}")
            st.write(f"Version: {metadata.get('version','')}")


def _render_impact_report(impact: dict | None) -> None:
    if not impact:
        st.caption("No impact report for this PR yet.")
        return
    # Confidence header
    conf = impact.get("confidence", 0.0)
    affected = impact.get("affected_components", []) or []
    stakeholders = impact.get("stakeholders", []) or []
    downstream = impact.get("downstream_workflows", []) or []
    risks = impact.get("risks", []) or []
    evidence = impact.get("evidence", []) or []

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Confidence", f"{conf:.2f}" if isinstance(conf, (int, float)) else str(conf))
    with c2:
        st.metric("Affected components", str(len(affected)))
    with c3:
        st.metric("Stakeholders", str(len(stakeholders)))

    if affected:
        st.markdown("**Affected components**")
        st.write(" · ".join(f"`{c}`" for c in affected))
    else:
        st.caption("No affected components.")

    if stakeholders:
        st.markdown("**Stakeholders**")
        for s in stakeholders:
            team = s.get("team", "—")
            itype = s.get("impact_type", "—")
            reason = s.get("reason", "")
            badge = {"awareness": "🔵", "action": "🟡", "approval": "🔴", "none": "⚪"}.get(itype, "•")
            st.markdown(f"{badge} **{team}** — `{itype}`")
            if reason:
                st.caption(reason)
    else:
        st.caption("No stakeholders.")

    if downstream:
        st.markdown("**Downstream workflows**")
        for w in downstream:
            st.markdown(f"- {w}")
    if risks:
        st.markdown("**Risks**")
        for r in risks:
            st.markdown(f"- {r}")
    if evidence:
        st.markdown("**Evidence**")
        for ev in evidence:
            etype = ev.get("type", "")
            src = ev.get("source", "")
            detail = ev.get("detail", "")
            st.markdown(f"- `{etype}` · `{src}` — {detail}")
    else:
        st.caption("No evidence.")


def _render_blast_radius(impact: dict | None) -> None:
    """Blast Radius section — deterministic static-analysis evidence attached
    to the impact report. Shows who/what is affected and WHY."""
    if not isinstance(impact, dict):
        return
    entries = impact.get("blast_radius") or []
    changed_components = impact.get("changed_components") or []
    if not entries and not changed_components:
        return

    st.divider()
    st.markdown("#### 💥 Blast Radius")
    incomplete = impact.get("incomplete")
    if isinstance(incomplete, bool) and incomplete:
        st.caption("⚠️ Static analysis incomplete — evidence may be partial.")

    if changed_components:
        st.markdown("**Affected components**")
        st.write(" · ".join(f"`{c}`" for c in changed_components))

    level_order = [
        ("direct", "Direct impact"),
        ("dependency", "Dependency impact"),
        ("indirect", "Indirect impact"),
        ("workflow", "Workflow impact"),
    ]
    level_badge = {
        "direct": "🔴",
        "dependency": "🟠",
        "indirect": "🟡",
        "workflow": "🔵",
    }
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        if isinstance(entry, dict):
            grouped.setdefault(entry.get("impact_level", "indirect"), []).append(entry)

    for level, label in level_order:
        group = grouped.pop(level, None)
        if not group:
            continue
        st.markdown(f"**{level_badge.get(level, '•')} {label}**")
        for entry in group:
            entity_type = entry.get("entity_type", "")
            icon = {"person": "👤", "team": "👥", "component": "🧩", "workflow": "⚙️"}.get(entity_type, "•")
            name = entry.get("name") or entry.get("entity_id", "—")
            itype = entry.get("impact_type", "none")
            conf = entry.get("confidence")
            conf_str = f" · confidence {conf:.2f}" if isinstance(conf, (int, float)) else ""
            line = f"{icon} **{name}** `{entity_type}` — `{itype}`{conf_str}"
            st.markdown(line)
            reason = entry.get("reason", "")
            if reason:
                st.caption(reason)
            evidence = entry.get("evidence") or []
            if evidence:
                with st.expander("Why? — evidence", expanded=False):
                    for ev in evidence:
                        if isinstance(ev, dict):
                            st.markdown(
                                f"- `{ev.get('type', '')}` · `{ev.get('source', '')}` — {ev.get('detail', '')}"
                            )

    for level, group in grouped.items():  # any unexpected levels still shown
        for entry in group:
            name = entry.get("name") or entry.get("entity_id", "—")
            st.markdown(f"• **{name}** — `{entry.get('impact_type', '')}`")


def _render_coordination_plan(plan: dict | None) -> None:
    if not plan:
        st.caption("No coordination plan for this PR yet.")
        return
    summary = plan.get("summary", "")
    requires = plan.get("requires_approval", False)
    tasks = plan.get("tasks", []) or []

    if summary:
        st.info(summary)
    st.write(f"**Requires approval:** {'Yes 🔒' if requires else 'No'}  ·  Tasks: {len(tasks)}")

    if not tasks:
        st.caption("No coordination tasks.")
        return

    for idx, task in enumerate(tasks, start=1):
        team = task.get("team", "—")
        itype = task.get("impact_type", "—")
        component = task.get("component", "—")
        change_type = task.get("change_type", "—")
        reason = task.get("reason", "")
        channel = task.get("channel", "")
        mode = task.get("mode", "")
        priority = task.get("priority", "")
        confidence = task.get("confidence", 0.0)
        action_required = task.get("action_required", False)
        evidence = task.get("evidence", []) or []

        icon = {"awareness": "🔵", "action": "🟠", "approval": "🔴"}.get(itype, "•")
        with st.expander(f"{icon} {idx}. {team} — {itype} ({component})", expanded=False):
            c1, c2, c3 = st.columns(3)
            with c1:
                st.markdown(f"**Team:** {team}")
                st.markdown(f"**Impact:** `{itype}`")
                st.markdown(f"**Priority:** `{priority}`")
            with c2:
                st.markdown(f"**Component:** `{component}`")
                st.markdown(f"**Change type:** `{change_type}`")
                st.markdown(f"**Mode:** `{mode}`")
            with c3:
                st.markdown(f"**Channel:** `{channel or '—'}`")
                st.markdown(f"**Action required:** {'Yes' if action_required else 'No'}")
                st.markdown(f"**Confidence:** {confidence:.2f}" if isinstance(confidence, (int, float)) else f"**Confidence:** {confidence}")
            if reason:
                st.markdown("**Reason**")
                st.caption(reason)
            if evidence:
                st.markdown("**Evidence**")
                for ev in evidence:
                    st.markdown(f"- `{ev.get('type','')}` · `{ev.get('source','')}` — {ev.get('detail','')}")
            else:
                st.caption("No evidence.")


def _render_approval_result(ar: dict | None) -> None:
    """Display ApprovePRResponse fields with reliability handling. Never crashes on missing nullable fields."""
    if not isinstance(ar, dict):
        _json_view(ar)
        return
    delivery_status = ar.get("delivery_status", "") or "—"
    selected = ar.get("selected_messages", []) or []
    sent_results = ar.get("sent_results", []) or []
    message = ar.get("message", "") or ""
    success = ar.get("success", None)

    # Delivery status badge — use status elements only
    if delivery_status == "rejected":
        st.warning(f"Delivery status: `{delivery_status}` — delivery blocked by user.")
    elif delivery_status == "failed":
        st.error(f"Delivery status: `{delivery_status}` — partial or failed delivery.")
        st.caption("Some messages may not have been delivered. See per-message results below.")
    elif delivery_status in ("sent", "mock_sent"):
        # mock_sent is expected in local dev without real Slack token
        st.success(f"Delivery status: `{delivery_status}` — {message or 'messages delivered'}")
    elif delivery_status in ("", "—", None):
        st.info("Delivery status: — (missing)")
    else:
        st.info(f"Delivery status: `{delivery_status}` — {message}")

    if success is False:
        st.error("`success: false` — backend reported failure.")

    st.markdown("**Selected messages**")
    if selected:
        st.write(" · ".join(f"`{m}`" for m in selected))
        st.caption(f"{len(selected)} message(s) selected")
    else:
        st.caption("`[]` — none (for `Approve & Send All` empty means all stored messages)")

    st.markdown("**Sent results**")
    if not sent_results:
        st.caption("No per-message results (empty `sent_results`). This is expected for `rejected` or failed bundle fetch.")
    else:
        # Detect partial delivery
        has_error = any(not (r.get("result") or {}).get("ok", True) for r in sent_results if isinstance(r, dict))
        if has_error or delivery_status == "failed":
            st.warning("Partial delivery detected — at least one message failed.")
        for r in sent_results:
            if not isinstance(r, dict):
                st.markdown(f"- {r}")
                continue
            rid = r.get("id", "—") or "—"
            chan = r.get("channel", "—") or "—"
            result = r.get("result") if isinstance(r.get("result"), dict) else {}
            status = result.get("status", "—") if isinstance(result, dict) else "—"
            ok = result.get("ok", "") if isinstance(result, dict) else ""
            err = result.get("error") or result.get("fallback_error") or ""
            line = f"`{rid}` → `{chan}` — `{status}` · ok=`{ok}`"
            if status == "sent":
                st.success(line)
            elif status == "mock_sent":
                st.info(line + " · mock (no real Slack token)")
            elif not ok or status == "error":
                st.error(line + (f" — {err}" if err else ""))
            else:
                st.caption(line)
            if err:
                st.caption(f"  error: {err}")

    if message:
        st.markdown("**Message**")
        st.caption(message)

    with st.expander("Raw approval response", expanded=False):
        _json_view(ar)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("Change Coordinator")
    st.caption("Impact-Aware Engineering Change Coordinator")
    st.divider()
    st.subheader("Backend")
    try:
        backend_url = get_backend_url()
        st.code(backend_url, language="text")
        health = check_backend_health(timeout=5.0)
        if health["ok"]:
            body = health["body"]
            label = body.get("status") if isinstance(body, dict) else str(body)[:80]
            st.success(f"● Connected — {label or 'ok'} ({health['status_code']})")
        else:
            msg = health["error"] or f"HTTP {health['status_code']}"
            st.error(f"○ Unreachable — {msg}")
            if health["url"]:
                st.caption(health["url"])
    except BackendApiError as exc:
        backend_url = ""
        st.warning(str(exc))
        st.caption("Set BACKEND_URL env var to your FastAPI backend base URL.")

    st.divider()
    if "project_id" not in st.session_state:
        st.session_state["project_id"] = ""
    st.subheader("Project")
    # Single-project mode: fixed ID from frontend/.env PROJECT_ID (one repo -> one project).
    try:
        fixed_project_id = get_project_id()
    except BackendApiError as exc:
        fixed_project_id = ""
        st.warning(str(exc))
        st.caption("Set PROJECT_ID in frontend/.env, then rerun (`streamlit run streamlit_app.py`).")
    st.session_state["project_id"] = fixed_project_id
    if fixed_project_id:
        st.code(fixed_project_id, language="text")
        st.caption("Single-project mode — all pages use this ID.")
    st.divider()
    st.subheader("Navigation")
    page = st.radio(
        "Go to",
        options=["Project Context", "Work Journey", "PR Review", "Slack Mapping"],
        index=0,
        label_visibility="collapsed",
        key="nav_radio",
    )
    st.divider()
    st.caption("Frontend is a thin client. All logic lives in the backend via `api_client.py`.")

project_id: str = st.session_state.get("project_id", "").strip()
if not project_id:
    st.warning("PROJECT_ID is not configured — set it in frontend/.env and rerun.")
    st.stop()

st.header("Impact-Aware Engineering Change Coordinator")
st.caption("Understand the evolution → reason about impact → coordinate the organization.")
st.divider()

# ── Project Context ───────────────────────────────────────────────────────
if page == "Project Context":
    st.subheader("Project Context")
    st.write(
        "Create a structured project context from an unstructured description. "
        "The backend Context Agent extracts objective, architecture, teams and tasks."
    )

    # Persist last input so switching pages doesn't lose draft
    if "pc_description" not in st.session_state:
        st.session_state["pc_description"] = ""

    with st.form("project_context_form", clear_on_submit=False):
        st.code(project_id, language="text")
        st.caption("Fixed project ID from frontend/.env — context is created/updated for this project only.")
        project_description = st.text_area(
            "Project description",
            value=st.session_state["pc_description"],
            height=260,
            placeholder="Describe your project: objectives, components, teams, planned work...",
            help="Raw text. The Context Agent will structure it.",
        )
        submitted = st.form_submit_button("Create / Update Project Context", use_container_width=True)

    # Keep draft in state
    st.session_state["pc_description"] = project_description

    if submitted:
        if not project_description.strip():
            st.warning("Project description is required.")
        else:
            with st.spinner("Creating project context..."):
                try:
                    result = create_project_context(project_id, project_description.strip())
                    st.session_state["project_context_result"] = result
                    st.session_state["project_context_error"] = None
                except Exception as exc:  # noqa: BLE001
                    st.session_state["project_context_result"] = None
                    st.session_state["project_context_error"] = exc

    # Render result / error from session_state (persists across reruns)
    err = st.session_state.get("project_context_error")
    res = st.session_state.get("project_context_result")
    if err is not None:
        _render_error(err)
    if res is not None:
        st.success("Project context created.")
        ctx = res.get("project_context") if isinstance(res, dict) else None
        if isinstance(ctx, dict):
            st.markdown("### Structured Project Context")
            _render_project_context_structured(ctx)
            with st.expander("Raw JSON", expanded=False):
                _json_view(res)
        else:
            _json_view(res)

    with st.expander("How this calls the backend", expanded=False):
        st.code("POST /api/projects/context  { project_id, project_description }", language="text")
        st.caption("via api_client.create_project_context(project_id, project_description) with the fixed PROJECT_ID")

# ── Work Journey ──────────────────────────────────────────────────────────
elif page == "Work Journey":
    st.subheader("Work Journey")
    st.write(
        "On-demand visibility into how work evolved. Each card is a commit enriched "
        "by the Change Analysis Agent. Reverse chronological order, newest first."
    )

    col1, col2 = st.columns([3, 1])
    with col1:
        st.code(project_id, language="text")
        st.caption("Fixed project — timeline for this project only.")
    with col2:
        limit_raw = st.selectbox(
            "Limit",
            options=["20", "10", "50", "100", "All"],
            index=0,
            help="Max commits to return (backend caps at 100).",
        )
        limit_val = None if limit_raw == "All" else int(limit_raw)

    fetch_journey = st.button("Fetch Commits", key="fetch_journey_btn")

    if "journey_result" not in st.session_state:
        st.session_state["journey_result"] = None
    if "journey_error" not in st.session_state:
        st.session_state["journey_error"] = None

    if fetch_journey:
        with st.spinner("Fetching commit timeline..."):
            try:
                result = get_project_commits(project_id, limit=limit_val)
                st.session_state["journey_result"] = result
                st.session_state["journey_error"] = None
            except Exception as exc:  # noqa: BLE001
                st.session_state["journey_result"] = None
                st.session_state["journey_error"] = exc

    err = st.session_state.get("journey_error")
    if err is not None:
        _render_error(err)

    result = st.session_state.get("journey_result")
    if result is not None:
        commits = result.get("commits", []) if isinstance(result, dict) else []
        if not commits:
            st.info("No commits found for this project yet.")
            with st.expander("Raw response", expanded=False):
                _json_view(result)
        else:
            st.caption(f"{len(commits)} commit(s) — newest first · reverse chronological order")
            for item in commits:
                if not isinstance(item, dict):
                    continue
                sha = item.get("commit_sha", "") or item.get("sha", "") or ""
                short = _short_sha(sha)
                summary = item.get("summary", "") or ""
                components = item.get("components", []) or []
                timestamp = item.get("timestamp", "") or item.get("saved_at", "") or ""
                # Optional enriched fields — may not be present in current API, but display if available
                author = item.get("author", "") or item.get("author_name", "") or ""
                commit_msg = item.get("commit_message", "") or item.get("message", "") or ""
                change_type = item.get("change_type", "") or ""
                changed_files = item.get("changed_files", []) or []
                branch = item.get("branch", "") or ""
                repository = item.get("repository", "") or ""

                header = f"{short} — {summary[:60]}" if summary else f"{short} — {commit_msg[:60] if commit_msg else 'no summary'}"
                # Card as expander (timeline)
                with st.expander(header, expanded=False):
                    # Top row: key metadata
                    m1, m2, m3 = st.columns(3)
                    with m1:
                        st.markdown(f"**SHA:** `{sha}`  ·  `{short}`")
                        if author:
                            st.markdown(f"**Author:** {author}")
                        else:
                            st.caption("Author: — (not in API response)")
                        if branch:
                            st.markdown(f"**Branch:** `{branch}`")
                    with m2:
                        st.markdown(f"**Timestamp:**  \n{_format_timestamp(timestamp) if timestamp else '—'}")
                        if repository:
                            st.markdown(f"**Repository:** `{repository}`")
                    with m3:
                        if change_type:
                            st.markdown(f"**Change type:** `{change_type}`")
                        else:
                            st.caption("Change type: — (not in current API)")
                        if components:
                            st.markdown("**Components:**")
                            st.write(" ".join(f"`{c}`" for c in components))
                        else:
                            st.caption("Components: —")

                    if summary:
                        st.markdown("**Summary**")
                        st.write(summary)
                    if commit_msg and commit_msg != summary:
                        st.markdown("**Commit message**")
                        st.code(commit_msg, language="text")
                    elif not summary and not commit_msg:
                        st.caption("No summary or commit message.")

                    # Changed files
                    if changed_files:
                        st.markdown(f"**Changed files ({len(changed_files)})**")
                        for f in changed_files:
                            if not isinstance(f, dict):
                                st.markdown(f"- {f}")
                                continue
                            path = f.get("path", "") or f.get("filename", "") or "—"
                            status = f.get("status", "")
                            adds = f.get("additions", "")
                            dels = f.get("deletions", "")
                            funcs = f.get("functions", []) or []
                            st.markdown(f"- `{path}`  `{status}`  +{adds}/-{dels}")
                            if funcs:
                                st.caption(f"  functions: {', '.join(funcs)}")
                            patch = f.get("patch")
                            if patch:
                                with st.expander(f"Patch: {path}", expanded=False):
                                    st.code(patch[:4000], language="diff")
                    else:
                        st.caption("Changed files: — (not in current API response; only summary/components/timestamp are returned)")

                    # Extra details if any other keys exist
                    known = {"commit_sha", "sha", "summary", "components", "timestamp", "saved_at", "author", "author_name", "commit_message", "message", "change_type", "changed_files", "branch", "repository"}
                    extra = {k: v for k, v in item.items() if k not in known}
                    if extra:
                        with st.expander("More details", expanded=False):
                            _json_view(extra)

                    with st.expander("Raw commit JSON", expanded=False):
                        _json_view(item)

            with st.expander("Raw timeline response", expanded=False):
                _json_view(result)

    with st.expander("How this calls the backend", expanded=False):
        st.code("GET /api/projects/{project_id}/commits?limit={limit}", language="text")
        st.caption("via api_client.get_project_commits(project_id, limit)")

# ── PR Review ─────────────────────────────────────────────────────────────
elif page == "PR Review":
    st.subheader("PR Review")
    st.write(
        "Review PR-level impact, coordination plan and drafted Slack messages. "
        "Nothing is sent on page load — you choose what to send."
    )

    col1, col2 = st.columns([2, 1])
    with col1:
        st.code(project_id, language="text")
        st.caption("Fixed project — PR review for this project only.")
    with col2:
        pr_number_input = st.number_input(
            "PR number",
            min_value=1,
            step=1,
            value=int(st.session_state.get("pr_number", 1)),
            key="pr_number_input",
        )

    pr_number = int(pr_number_input)
    st.session_state["pr_number"] = pr_number

    col_a, col_b = st.columns([1, 1])
    with col_a:
        fetch_pr = st.button("Fetch PR Review", use_container_width=True, key="fetch_pr_btn")
    with col_b:
        clear_pr = st.button("Clear", use_container_width=True, key="clear_pr_btn")
        if clear_pr:
            for k in ["pr_review_result", "pr_review_error", "pr_approve_result", "pr_approve_error", "editable_messages", "selected_msg_ids"]:
                st.session_state.pop(k, None)
            # also clear per-message widget keys (old and new prefixes)
            for k in list(st.session_state.keys()):
                if k.startswith("cb_msg_") or k.startswith("edit_msg_") or k.startswith("preview_msg_"):
                    st.session_state.pop(k, None)
            st.rerun()

    if "pr_review_result" not in st.session_state:
        st.session_state["pr_review_result"] = None
    if "pr_review_error" not in st.session_state:
        st.session_state["pr_review_error"] = None
    if "pr_approve_result" not in st.session_state:
        st.session_state["pr_approve_result"] = None
    if "pr_approve_error" not in st.session_state:
        st.session_state["pr_approve_error"] = None

    if fetch_pr:
        if pr_number <= 0:
            st.warning("PR number must be positive.")
        else:
            with st.spinner(f"Fetching PR #{pr_number} review..."):
                try:
                    review = get_pr_review(project_id, pr_number)
                    st.session_state["pr_review_result"] = review
                    st.session_state["pr_review_error"] = None
                    st.session_state["pr_approve_result"] = None
                    st.session_state["pr_approve_error"] = None
                    # Reset per-message selection state for new fetch
                    st.session_state.pop("editable_messages", None)
                    for k in list(st.session_state.keys()):
                        if k.startswith("cb_msg_") or k.startswith("edit_msg_") or k.startswith("preview_msg_"):
                            st.session_state.pop(k, None)
                except Exception as exc:  # noqa: BLE001
                    st.session_state["pr_review_result"] = None
                    st.session_state["pr_review_error"] = exc

    err = st.session_state.get("pr_review_error")
    if err is not None:
        _render_error(err)

    review = st.session_state.get("pr_review_result")
    if review is not None:
        if not isinstance(review, dict):
            _json_view(review)
        else:
            st.divider()
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("Project", review.get("project_id", project_id))
            with c2:
                st.metric("PR", f"#{review.get('pr_number', pr_number)}")
            with c3:
                has_impact = review.get("impact_report") is not None
                has_coord = review.get("coordination_plan") is not None
                has_comm = review.get("communication_bundle") is not None
                st.caption("Artifacts")
                st.write(f"Impact: {'✓' if has_impact else '—'}  ·  Coordination: {'✓' if has_coord else '—'}  ·  Messages: {'✓' if has_comm else '—'}")

            # A. Impact Report
            st.markdown("### A. Impact Report")
            with st.container(border=True):
                _render_impact_report(review.get("impact_report"))
                _render_blast_radius(review.get("impact_report"))
                with st.expander("Raw Impact Report JSON", expanded=False):
                    _json_view(review.get("impact_report"))

            # B. Coordination Plan
            st.markdown("### B. Coordination Plan")
            with st.container(border=True):
                _render_coordination_plan(review.get("coordination_plan"))
                with st.expander("Raw Coordination Plan JSON", expanded=False):
                    _json_view(review.get("coordination_plan"))

            # C. Communication Bundle
            st.markdown("### C. Communication Bundle")
            # Integration note: backend approve sends stored bundle messages — inspect backend/api/projects.py:357-377
            # ApprovePRRequest only has `approved` + `selected_messages`; no edited body fields. Stored `text` is sent.
            st.info(
                "Messages below are **previews of stored CommunicationBundle contents**. "
                "Editing is currently disabled — the backend approval API (`POST /api/projects/{project_id}/prs/{pr_number}/approve`) "
                "sends the **stored** `text`/`channel` for the selected IDs and ignores any frontend edits. "
                "Check the boxes to choose which stored messages to send."
            )
            bundle = review.get("communication_bundle") if isinstance(review, dict) else None
            if bundle is None:
                with st.container(border=True):
                    st.caption("No communication bundle for this PR yet.")
                    st.caption("Approval is unavailable until a bundle exists. Fetch a different PR or wait for the workflow to generate one.")
                    with st.expander("Raw Communication Bundle JSON", expanded=False):
                        _json_view(bundle)
                # Still render approval result if prior call existed
                ae = st.session_state.get("pr_approve_error")
                if ae is not None:
                    _render_error(ae)
                ar = st.session_state.get("pr_approve_result")
                if ar is not None:
                    st.divider()
                    st.markdown("#### Approval result")
                    _render_approval_result(ar)
            else:
                summary = bundle.get("summary", "") if isinstance(bundle, dict) else ""
                if summary:
                    st.caption(summary)
                messages = _extract_messages(bundle) if isinstance(bundle, dict) else []
                if not messages:
                    with st.container(border=True):
                        st.warning("Empty communication bundle — no draft messages to approve.")
                        st.caption("`messages: []` — nothing to send. The backend will return an empty delivery or error.")
                        with st.expander("Raw Communication Bundle JSON", expanded=False):
                            _json_view(bundle)
                    # Disable approval actions but still show last result if any
                    ae = st.session_state.get("pr_approve_error")
                    if ae is not None:
                        _render_error(ae)
                    ar = st.session_state.get("pr_approve_result")
                    if ar is not None:
                        st.divider()
                        st.markdown("#### Approval result")
                        _render_approval_result(ar)
                else:
                    st.caption(f"{len(messages)} draft message(s) — check the boxes to select which stored messages to send. Nothing is sent until you approve.")

                    # Render each message as preview + checkbox (editing disabled per backend contract)
                    for idx, msg in enumerate(messages):
                        if not isinstance(msg, dict):
                            continue
                        mid = (msg.get("id") or f"msg_{idx+1}").strip() or f"msg_{idx+1}"
                        recipient = (msg.get("recipient") or "—").strip() or "—"
                        team = (msg.get("team") or "—").strip() or "—"
                        channel = (msg.get("channel") or "—").strip() or "—"
                        mode = (msg.get("mode") or "—").strip() or "—"
                        title = (msg.get("title") or msg.get("subject") or "").strip()
                        priority = (msg.get("priority") or "").strip()
                        component = (msg.get("component") or "").strip()
                        evidence = msg.get("evidence", []) or []
                        confidence = msg.get("confidence", None)
                        body = msg.get("text") or msg.get("body") or ""

                        cb_key = f"cb_msg_{pr_number}_{mid}"

                        with st.container(border=True):
                            col_check, col_meta = st.columns([1, 5])
                            _rec, _why = _recommendation(msg)
                            with col_check:
                                # Preference preselected, human decides: recommended boxes
                                # start checked (first render only); anything can be toggled.
                                if cb_key not in st.session_state:
                                    st.session_state[cb_key] = _rec
                                st.checkbox("Send", key=cb_key, help=f"Include {mid} when you click Approve & Send Selected")
                            with col_meta:
                                _rec_badge = " ⭐ Recommended" if _rec else ""
                                st.markdown(f"**{mid}** — **{recipient}** (`{team}`) · `{mode}` · channel `{channel}`{_rec_badge}")
                                if _rec:
                                    st.caption(f"Prefer: {_why}")
                                _msg_uid = (msg.get("slack_user_id") or "").strip() if isinstance(msg, dict) else ""
                                if msg.get("unmapped"):
                                    st.warning("⚠️ No Slack mapping — will be blocked at approval. Map this person on the Slack Mapping page, then re-approve.")
                                elif _msg_uid:
                                    st.caption(f"Mention: `<@{_msg_uid}>` → **{recipient}**")
                                _auto = (msg.get("autonomy") or "").strip().upper() if isinstance(msg, dict) else ""
                                if _auto == "AUTO_SEND":
                                    st.caption("🤖 Autonomy: AUTO_SEND (policy-approved, may deliver without review)")
                                elif _auto == "HUMAN_REVIEW":
                                    st.caption("👁 Autonomy: HUMAN_REVIEW (needs your approval)")
                                elif _auto == "BLOCK":
                                    _br = "; ".join(msg.get("autonomy_reasons") or [])[:300]
                                    st.error(f"⛔ Autonomy BLOCKED{_br and ': ' + _br}. Cannot be approved.")
                                elif _auto == "SKIP":
                                    st.caption("⏭ Autonomy SKIP (no notification needed)")
                                if title:
                                    st.markdown(f"**Title:** {title}")
                                meta_parts = []
                                if priority:
                                    meta_parts.append(f"priority: `{priority}`")
                                if component:
                                    meta_parts.append(f"component: `{component}`")
                                if confidence is not None:
                                    try:
                                        meta_parts.append(f"confidence: `{float(confidence):.2f}`")
                                    except Exception:
                                        meta_parts.append(f"confidence: `{confidence}`")
                                if meta_parts:
                                    st.caption(" · ".join(meta_parts))

                            # Preview body — disabled editing (backend ignores edits). Show as read-only text_area + code fallback.
                            st.markdown(f"**Body — {mid}** · preview of stored `text` (read-only)")
                            st.text_area(
                                f"Body preview — {mid}",
                                value=body if isinstance(body, str) else str(body),
                                height=160,
                                key=f"preview_msg_{pr_number}_{mid}",
                                disabled=True,
                                label_visibility="collapsed",
                                placeholder="— no body —",
                            )
                            # Also show as code for faithful whitespace
                            if body:
                                with st.expander(f"Raw body text — {mid}", expanded=False):
                                    st.code(body, language="text")

                            if evidence:
                                with st.expander("Evidence", expanded=False):
                                    for ev in evidence:
                                        if isinstance(ev, dict):
                                            st.markdown(f"- `{ev.get('type','')}` · `{ev.get('source','')}` — {ev.get('detail','')}")
                                        else:
                                            st.markdown(f"- {ev}")
                            with st.expander("Message metadata (read-only)", expanded=False):
                                _json_view(msg)

                    # Collect selected IDs — explicit interaction required
                    selected_ids = [
                        (m.get("id") or f"msg_{i+1}").strip() or f"msg_{i+1}"
                        for i, m in enumerate(messages)
                        if isinstance(m, dict) and st.session_state.get(f"cb_msg_{pr_number}_{(m.get('id') or f'msg_{i+1}').strip() or f'msg_{i+1}'}", False)
                    ]
                    all_ids = [
                        (m.get("id") or f"msg_{i+1}").strip() or f"msg_{i+1}"
                        for i, m in enumerate(messages)
                        if isinstance(m, dict)
                    ]

                    st.divider()
                    st.markdown("#### Human-in-the-loop approval")
                    st.caption("Requires explicit click. `Approve & Send Selected` sends only checked IDs. `Approve & Send All` sends all stored messages (`selected_messages: []`). `Reject` blocks delivery. ⭐ = recommended (mapped + must-act); toggle anything before approving.")

                    _recommended_ids = [
                        (m.get("id") or f"msg_{i+1}").strip() or f"msg_{i+1}"
                        for i, m in enumerate(messages)
                        if isinstance(m, dict) and _recommendation(m)[0]
                    ]
                    if _recommended_ids:
                        st.info(f"Recommended to notify first: {', '.join(_recommended_ids)} (mapped + action/approval/high-priority)")
                    if selected_ids:
                        st.success(f"Selected: {', '.join(selected_ids)} ({len(selected_ids)}/{len(messages)})")
                    else:
                        st.caption(f"Selected: — (none checked) · All available: {', '.join(all_ids)}")

                    c1, c2, c3 = st.columns(3)
                    with c1:
                        approve_selected_btn = st.button(
                            "Approve & Send Selected",
                            type="primary",
                            use_container_width=True,
                            key=f"approve_selected_{pr_number}",
                            help="POST {approved:true, selected_messages:selected_ids}",
                        )
                    with c2:
                        approve_all_btn = st.button(
                            "Approve & Send All",
                            use_container_width=True,
                            key=f"approve_all_{pr_number}",
                            help="POST {approved:true, selected_messages:[]}",
                        )
                    with c3:
                        reject_btn = st.button(
                            "Reject",
                            use_container_width=True,
                            key=f"reject_{pr_number}",
                            help="POST {approved:false, selected_messages:[]}",
                        )

                    if approve_selected_btn:
                        if not selected_ids:
                            st.warning("No messages checked. Check at least one message or use Approve & Send All / Reject.")
                        else:
                            with st.spinner("Approving & sending selected messages..."):
                                try:
                                    resp = approve_pr(project_id, pr_number, True, selected_ids)
                                    st.session_state["pr_approve_result"] = resp
                                    st.session_state["pr_approve_error"] = None
                                except Exception as exc:  # noqa: BLE001
                                    st.session_state["pr_approve_result"] = None
                                    st.session_state["pr_approve_error"] = exc
                            st.rerun()

                    if approve_all_btn:
                        with st.spinner("Approving & sending all messages..."):
                            try:
                                resp = approve_pr(project_id, pr_number, True, [])
                                st.session_state["pr_approve_result"] = resp
                                st.session_state["pr_approve_error"] = None
                            except Exception as exc:  # noqa: BLE001
                                st.session_state["pr_approve_result"] = None
                                st.session_state["pr_approve_error"] = exc
                        st.rerun()

                    if reject_btn:
                        with st.spinner("Rejecting delivery..."):
                            try:
                                resp = approve_pr(project_id, pr_number, False, [])
                                st.session_state["pr_approve_result"] = resp
                                st.session_state["pr_approve_error"] = None
                            except Exception as exc:  # noqa: BLE001
                                st.session_state["pr_approve_result"] = None
                                st.session_state["pr_approve_error"] = exc
                        st.rerun()

                    # Render approval results — reliability: handle all statuses, nullable fields, partial delivery
                    ae = st.session_state.get("pr_approve_error")
                    if ae is not None:
                        _render_error(ae)
                    ar = st.session_state.get("pr_approve_result")
                    if ar is not None:
                        st.divider()
                        st.markdown("#### Approval result")
                        _render_approval_result(ar)

                    with st.expander("Raw Communication Bundle JSON", expanded=False):
                        _json_view(bundle)

    with st.expander("How this calls the backend", expanded=False):
        st.code("GET  /api/projects/{project_id}/prs/{pr_number}", language="text")
        st.code("POST /api/projects/{project_id}/prs/{pr_number}/approve  { approved, selected_messages }", language="text")
        st.caption("via api_client.get_pr_review(...) and api_client.approve_pr(...)")

# ── Slack Mapping ─────────────────────────────────────────────────────────
elif page == "Slack Mapping":
    import re as _re

    st.subheader("Slack Mapping")
    st.write(
        "Map team roles/persons to Slack user IDs. Every approved PR message goes to the "
        "single channel below with exactly one `<@USER_ID>` mention per affected person. "
        "Saved into the existing project context — no new project is created."
    )
    st.code(project_id, language="text")

    if "mapping_ctx" not in st.session_state:
        st.session_state["mapping_ctx"] = None
    if "mapping_error" not in st.session_state:
        st.session_state["mapping_error"] = None
    if "mapping_saved" not in st.session_state:
        st.session_state["mapping_saved"] = None

    if st.button("Load Current Mappings", key="mapping_load_btn"):
        try:
            st.session_state["mapping_ctx"] = get_project_context(project_id)
            st.session_state["mapping_error"] = None
            st.session_state["mapping_saved"] = None
        except Exception as exc:  # noqa: BLE001
            st.session_state["mapping_ctx"] = None
            st.session_state["mapping_error"] = exc

    if st.session_state.get("mapping_error") is not None:
        _render_error(st.session_state["mapping_error"])

    _mctx = st.session_state.get("mapping_ctx")
    _pctx = (_mctx.get("project_context") if isinstance(_mctx, dict) else None) or {}
    _stakeholders = [s for s in (_pctx.get("stakeholders") or []) if isinstance(s, dict)]
    _cur_channel = str(_pctx.get("default_slack_channel_id") or "")

    if _mctx is not None:
        st.markdown("### 1. Notification channel")
        new_channel = st.text_input(
            "Default Slack channel ID",
            value=_cur_channel,
            placeholder="C012345",
            help="All per-person PR messages are posted here. Channel IDs look like C/G/D + letters/digits.",
            key="mapping_channel_input",
        )

        st.markdown("### 2. People mappings")
        st.caption("One Slack user ID per person. Duplicates are rejected on save.")
        edited: list[dict] = []
        for idx, person in enumerate(_stakeholders):
            name = str(person.get("name", "") or "")
            team = str(person.get("team", "") or "")
            role = str(person.get("role", "") or "")
            uid = st.text_input(
                f"{name or '—'} (`{team or '—'}`{f' · {role}' if role else ''})",
                value=str(person.get("slack_user_id") or ""),
                placeholder="U012345",
                key=f"mapping_uid_{idx}",
            ).strip()
            edited.append({"name": name, "team": team, "role": role, "slack_user_id": uid})

        st.markdown("### 3. Add a person")
        with st.form("mapping_add_form", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                add_name = st.text_input("Name", key="mapping_add_name")
                add_team = st.text_input("Team", key="mapping_add_team")
            with c2:
                add_role = st.text_input("Role", key="mapping_add_role")
                add_uid = st.text_input("Slack user ID", placeholder="U012345", key="mapping_add_uid")
            add_go = st.form_submit_button("Add To List")
        if add_go:
            if not (add_name or "").strip() or not (add_team or "").strip() or not (add_uid or "").strip():
                st.warning("Name, team and Slack user ID are all required to add a person.")
            else:
                _stakeholders.append({
                    "name": add_name.strip(), "team": add_team.strip(),
                    "role": (add_role or "").strip(), "slack_user_id": add_uid.strip(),
                })
                _pctx["stakeholders"] = _stakeholders
                st.session_state["mapping_ctx"] = _mctx
                st.rerun()

        st.divider()
        if st.button("Save Mappings", type="primary", key="mapping_save_btn"):
            problems: list[str] = []
            chan = (new_channel or "").strip()
            if chan and not _re.fullmatch(r"[CGD][A-Z0-9]{5,}", chan):
                problems.append(f"Channel ID {chan!r} looks invalid (expect C/G/D + letters/digits, e.g. C012345).")
            payload_people: list[dict] = []
            seen_uids: set[str] = set()
            seen_keys: set[str] = set()
            for person in edited:
                uid = (person["slack_user_id"] or "").strip()
                if not person["name"] or not person["team"]:
                    problems.append("Every row needs a name and team.")
                    continue
                if not uid:
                    continue  # unmapped rows are kept as-is (person stays without mention)
                if not _re.fullmatch(r"[UWB][A-Z0-9]{5,}", uid):
                    problems.append(f"User ID {uid!r} for {person['name']} looks invalid (expect U/W/B + letters/digits).")
                    continue
                if uid in seen_uids:
                    problems.append(f"Duplicate Slack user ID {uid!r} — one user maps to one person.")
                    continue
                seen_uids.add(uid)
                key = (person["name"].strip().lower(), person["team"].strip().lower())
                if key in seen_keys:
                    problems.append(f"Duplicate row for {person['name']} ({person['team']}).")
                    continue
                seen_keys.add(key)
                payload_people.append({
                    "name": person["name"].strip(), "team": person["team"].strip(),
                    "role": person["role"].strip(), "slack_user_id": uid,
                })
            if problems:
                for problem in problems:
                    st.warning(problem)
            else:
                try:
                    saved = update_slack_mappings(project_id, chan, payload_people)
                    st.session_state["mapping_saved"] = saved
                    st.session_state["mapping_ctx"] = saved
                    st.success(f"Saved: channel `{chan or '—'}` · {len(payload_people)} mapped person(s).")
                except Exception as exc:  # noqa: BLE001
                    st.session_state["mapping_saved"] = None
                    _render_error(exc)
    else:
        st.info("Click **Load Current Mappings** to edit. (Create the project context first if none exists.)")

    with st.expander("How this calls the backend", expanded=False):
        st.code("GET /api/projects/{project_id}/context", language="text")
        st.code("PUT /api/projects/{project_id}/slack-mappings  { default_slack_channel_id, stakeholders[] }", language="text")
        st.caption("via api_client.get_project_context(...) and api_client.update_slack_mappings(...)")

st.divider()
st.caption("Frontend is a thin client. No business logic lives here — see `frontend/api_client.py` for backend communication.")
