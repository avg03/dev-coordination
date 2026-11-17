# Dev Coordination - Impact-Aware Engineering Change Coordinator

An agentic, end-to-end workflow that tracks how engineering work evolves through GitHub, reasons about the downstream impact of changes, and coordinates targeted communication to affected stakeholders. It is not a PR summarizer or a notification bot: the system maintains a living project memory, produces evidence-backed impact analysis, and keeps a human in control of every external action.

Built for the Gemini / Google ADK stack: Gemini via Google ADK 2.5.0, FastAPI, Cloud Pub/Sub, Firestore, GitHub API + Webhooks, and Slack.

---

## 1. Core Concept

```
Project Context + Continuous Change Memory + Repository Evidence
        |
        v
Change Intelligence -> Impact -> Stakeholder Coordination -> Action -> Verification
```

The guiding principle: agents reason, deterministic services execute. Webhooks, Pub/Sub, Firestore, GitHub calls, authorization, and idempotency are plain Python. Only semantic understanding (context extraction, change enrichment, impact reasoning, coordination, message drafting, supervision) is delegated to Gemini agents.

---

## 2. High Level Architecture

```
                    +---------------------------+
                    |         GitHub            |
                    |  push / pull_request      |
                    +-------------+-------------+
                                  |  webhook (HMAC verified)
                                  v
+------------------+    +-------------------------+
|      React       |--->|      FastAPI Backend    |
|  (frontend/)     |    | github_infrastructure/  |
+------------------+    +------------+------------+
        ^        REST                |
        |        /api/projects/*     v
        |                  +------------------+
        |                  |     Pub/Sub      |
        |                  | github-webhook-  |
        |                  |     events       |
        |                  +---+----------+---+
        |                      |          |
        |          push events |          | pull_request events
        |                      v          v
        |            +-------------+  +-------------+
        |            | CommitWorker|  |  PRWorker   |
        |            +------+------+  +------+------+
        |                   |                |
        |                   v                |
        |      GitHub API -> ChangeRecord    |
        |                   |                |
        |                   v                v
        |      ChangeAnalysisAgent    PRWorkflow
        |      (enriched changes)           |
        |                          +--------+---------+
        |                          |  BlastRadius V1  |
        |                          |  sandbox + static|
        |                          |  analysis        |
        |                          +--------+---------+
        |                                   |
        |                     Impact -> Coordination -> Communication -> Supervisor
        |                     (all agent tool calls pass the Permission Gateway)
        |                                   |
        |                                   v
        +---- GET /prs/{n} --------- Firestore (projects, change_records, audit, idempotency)
                                    |
                           human approval (React)
                                    |
                     PermissionGateway -> Idempotency -> SlackExecutor -> Slack
```

---

## 3. Repository Layout

```
dev-coordination/
├── backend/
│   ├── api/                    FastAPI routes: project context, commits, PR review, approval
│   ├── agents/                 Gemini (ADK) agents, one class per agent
│   ├── blast_radius/           Sandbox + static dependency/ownership analysis (V1)
│   ├── github_infrastructure/  Webhook receiver, Pub/Sub, change records, Firestore store
│   ├── project_context/        Pydantic models for the Project Context schema
│   ├── security/               Permission gateway, idempotency, audit, Slack executor
│   ├── services/               Slack delivery service (real send with mock fallback)
│   ├── tests/                  Offline unit tests + live integration scripts
│   ├── utils/                  Deterministic component extraction from file paths
│   ├── workers/                Pub/Sub consumers (commit worker, PR worker)
│   └── workflows/              PRWorkflow orchestration (idempotent steps)
├── frontend/
│   ├── src/                    React views, components, API client, and styles
│   ├── server.mjs              Static server, runtime config, and backend proxy
│   └── package.json            Vite build and frontend dependencies
└── docs/                       Architecture and agent coding rules
```

---

## 4. Agents

All agents are Google ADK `Agent` instances with structured Pydantic output schemas and Firestore-backed tools. Every tool call is intercepted by a `before_tool_callback` guard that routes through the Permission Gateway.

| Agent | Responsibility | Reads | Writes |
|---|---|---|---|
| ContextAgent | Converts an unstructured project description into a structured ProjectContext | - | project_context |
| ChangeAnalysisAgent | Enriches a deterministic ChangeRecord with summary, behavior before/after, impact scope, related tasks | project_context, change_records | enriched_changes |
| ImpactAnalysisAgent | Reasons over evidence (including the blast radius report) to classify stakeholder impact | project_context, enriched_changes, blast_radius | impact_reports |
| CoordinationAgent | Turns an ImpactReport into per-team coordination tasks with priority and channel | project_context, impact_reports | coordination_plans |
| CommunicationAgent | Drafts role-specific Slack messages. Pure drafting: it has no send capability | project_context, coordination_plans | communications |
| SupervisorAgent | Critiques and finalizes drafts (keep / revise / reject) before approval. No send capability | impact_reports, coordination_plans, communications | communications |
| SlackExecutor | Not an agent. Deterministic executor: the only component allowed to call Slack, after gateway + idempotency | approved communications | delivery_state |
| BlastRadiusAnalyzer | Not an agent. Deterministic sandbox + static analysis service, gateway-gated | repository snapshot (sandboxed) | blast_radius |

Default model for all agents: `gemini-3.5-flash`, overridable per agent via environment variables (`CONTEXT_AGENT_MODEL`, `CHANGE_ANALYSIS_MODEL`, `IMPACT_ANALYSIS_MODEL`, `COORDINATION_MODEL`, `COMMUNICATION_MODEL`, `SUPERVISOR_MODEL`).

---

## 5. Security Layer (`backend/security/`)

The pipeline is governed by a deterministic, deny-by-default authorization architecture. No LLM participates in any authorization decision.

### 5.1 Permission Gateway

```
Agent / Service
      |
      v
AuthorizationRequest (identity, action, resource, project scope, workflow id)
      |
      v
PermissionGateway.authorize()   (pure Python)
  1. identity known?
  2. capability granted in the matrix?
  3. project scope valid?
  4. external side effects require human approval?
  5. recipient allowed?  already delivered?
      |
      v
PolicyDecision (allowed: true/false, policy name, reason) -> audit_logs
```

### 5.2 Capability Matrix (deny by default)

| Identity | Granted capabilities |
|---|---|
| context_agent | read + write project_context |
| change_analysis_agent | read project_context, read change_records, write enriched_changes |
| impact_analysis_agent | read project_context, read enriched_changes, read blast_radius, write impact_reports |
| coordination_agent | read project_context, read impact_reports, write coordination_plans |
| communication_agent | read project_context, read coordination_plans, write communications. No slack.send |
| supervisor_agent | read impact_reports / coordination_plans / communications, write communications. No slack.send |
| slack_executor | read communications, write delivery_state, execute + send slack (approval-gated) |
| blast_radius_analyzer | sandbox.create, sandbox.read_repo, sandbox.write_temp, static_analysis.execute, write blast_radius. No slack, no external side effects |

Unknown identities, unknown actions, unknown resources, and unregistered tools are always denied.

### 5.3 Tool Guard

Each ADK agent is built with `before_tool_callback=make_before_tool_guard(identity)`. The guard maps the tool name to an (action, resource) pair via a central `TOOL_RESOURCE_MAP` and calls the gateway. A denied tool call never executes; the model receives a structured permission error instead.

### 5.4 Step Level Idempotency

Every meaningful workflow step (blast_radius:analyze, impact:analyze, coordination:create, communication:generate, communication:supervise, slack:send) is wrapped by the IdempotencyManager:

```
claim (atomic Firestore create-if-absent with lease)
   -> execute
   -> persist result
   -> mark COMPLETED
   -> return
```

Key = sha256(project_id, workflow_id, step_name, canonical input fingerprint, step_version). A changed input (new PR head SHA, new commits, updated project context, new analysis version) always produces a new key, so updated work is re-analyzed while retries and Pub/Sub redeliveries return the stored result. Expired leases are reclaimed with an incremented attempt count; FAILED steps are retryable; COMPLETED steps never re-execute.

### 5.5 Slack Execution Boundary

```
Human approval (POST /api/projects/{id}/prs/{n}/approve)
   -> approval binding validated (project, bundle, selected message ids)
   -> PermissionGateway (approval present, recipient allowed, not already delivered)
   -> IdempotencyManager (key: project + pr + message id + text hash)
   -> SlackExecutor -> services/slack_service.py -> Slack API
   -> delivery state persisted, record marked COMPLETED
```

A retried approval returns the stored delivery result instead of messaging again. Exactly one send per approved message, guaranteed.

### 5.6 Audit

Every authorization decision and every execution is written to the `audit_logs` Firestore collection (and structured logs): timestamp, agent id, action, resource, project, workflow, idempotency key, decision, policy, reason, approval id. Secrets are never logged.

---

## 6. Blast Radius V1 (`backend/blast_radius/`)

A deterministic evidence generator, not an LLM predictor. It establishes factual relationships from the repository; the Impact Agent reasons about their significance.

```
PR files + project context
   -> sandbox.create        (repo snapshot at head SHA via GitHub tarball API)
   -> static analysis       (Python AST: imports, symbols; no code execution)
   -> component resolution  (ProjectContext repository_paths, longest prefix match)
   -> dependency traversal  (bounded BFS over importers, default depth 2)
   -> ownership mapping     (ProjectContext + CODEOWNERS, never invented)
   -> BlastRadiusReport     (entries with evidence + confidence, persisted to Firestore)
   -> Impact Agent reasons over the report (read-only access)
```

Sandbox constraints: the GitHub token travels only in the HTTP request header and never touches the workspace; zero network after provisioning; zero code execution; file reads are policy checked (workspace containment plus a secret deny list covering .env files, keys, certificates, service account files, .git and .ssh paths). Sandbox failures degrade gracefully to an incomplete report so the impact analysis still runs.

The repository graph is a frozen, read-only model. Agents cannot modify graph relationships.

---

## 7. Data Flows

### Push flow

```
git push -> GitHub webhook -> Pub/Sub -> CommitWorker
   -> GitHub API (commit + diff)
   -> ChangeRecord (change type classification, evidence, components)
   -> Firestore change_records (idempotent key repo#sha)
   -> ChangeAnalysisAgent -> projects/{id}/enriched_changes/{sha}
```

### PR flow

```
PR opened/updated -> webhook -> Pub/Sub -> PRWorker
   -> PRWorkflow (workflow id pr:{project}:{pr}:{head_sha})
   -> fetch PR files + commits
   -> blast_radius:analyze   (idempotent)
   -> impact:analyze         (idempotent, agent reads blast radius)
   -> coordination:create    (idempotent)
   -> communication:generate (idempotent, drafts only)
   -> communication:supervise (idempotent, verdicts + final messages)
   -> persist impact_reports, coordination_plans, communications
```

### Approval flow

```
React PR Review page -> POST .../approve
   -> validate selected message ids
   -> PermissionGateway + IdempotencyManager + SlackExecutor
   -> Slack (sent, mock_sent, denied, or failed)
   -> delivery status persisted on the communications document
```

Re-analysis: a new head SHA changes the workflow id and every idempotency key, so updated PRs are fully re-analyzed. Redeliveries of the same head return stored artifacts.

---

## 8. Firestore Data Model

| Path | Contents |
|---|---|
| `change_records/{repo__name#sha}` | Deterministic ChangeRecord per commit |
| `project_contexts/{project_id}` | Structured ProjectContext |
| `projects/{id}/enriched_changes/{sha}` | EnrichedChangeRecord (summary, behavior, impact scope) |
| `projects/{id}/blast_radius/{pr}` | BlastRadiusReport (changed components, graph, entries, evidence) |
| `projects/{id}/impact_reports/{pr}` | ImpactReport (includes blast radius entries) |
| `projects/{id}/coordination_plans/{pr}` | CoordinationPlan (per-team tasks) |
| `projects/{id}/communications/{pr}` | CommunicationBundle (drafts, supervision, delivery state) |
| `idempotency_keys/{key}` | IdempotencyRecord (status, lease, result, attempts) |
| `audit_logs/{auto id}` | Authorization + execution audit records |

---

## 9. API Reference

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/projects/context` | Create project context from a text description |
| GET | `/api/projects/{project_id}/commits` | Commit timeline (Work Journey view) |
| GET | `/api/projects/{project_id}/prs/{pr_number}` | PR review artifacts (impact, coordination, communications) |
| POST | `/api/projects/{project_id}/prs/{pr_number}/approve` | Human approval for Slack delivery |
| POST | `/github/webhook` | GitHub webhook receiver (push, pull_request, ping) |
| GET | `/health` | Liveness probe |

---

## 10. Frontend

React dashboard with four workflows. It remains a thin client; all business logic stays in FastAPI:

- Project Context: create and inspect the structured project model
- Work Journey: commit timeline derived from enriched changes
- PR Review: impact report, blast radius section (grouped by impact level with per-entry evidence), coordination plan, drafted messages with selection, approve / reject
- Slack Mapping: maintain the notification channel and per-person Slack user IDs

The Node frontend server reads `BACKEND_URL`, proxies `/api/*` and `/health`, and exposes `PROJECT_ID` to the browser through runtime configuration. This keeps internal Docker hostnames out of browser code.

---

## 11. Running Locally

Prerequisites: Python 3.12+, a GCP project with Firestore and Pub/Sub enabled, a GitHub token, a GitHub webhook on the target repo, and optionally a Slack bot token.

```powershell
# Terminal 1: API + webhook receiver
cd backend
.\venv\Scripts\python.exe -m uvicorn github_infrastructure.main:app --host 0.0.0.0 --port 8000

# Terminal 2: commit (push event) consumer
.\venv\Scripts\python.exe -c "from github_infrastructure.commit_worker import CommitWorker; CommitWorker().start()"

# Terminal 3: PR consumer
.\venv\Scripts\python.exe -c "from workers.pr_worker import PRWorker; PRWorker().start()"

# Terminal 4: completion-event consumer
.\venv\Scripts\python.exe -c "from workers.completion_consumer import CompletionConsumer; CompletionConsumer().start()"

# Terminal 5: React UI
cd ../frontend
npm install
npm run dev

# Terminal 6: public webhook tunnel (optional)
ngrok http 8000
```

For local webhook testing, copy the HTTPS forwarding URL printed by ngrok and
configure the GitHub webhook target as
`https://<ngrok-host>/github/webhook`. Enable Push and Pull requests events and
use the secret from `GITHUB_WEBHOOK_SECRET`. Keep the API running on port 8000
while ngrok is running.

Workers self-create the topic and subscriptions on first run. Pub/Sub and Slack both degrade gracefully when unconfigured.

### Docker

The [Dockerfile](./Dockerfile) has two targets and [compose.yaml](./compose.yaml)
builds and runs the complete project:

- `backend`: FastAPI API and GitHub webhook receiver
- `commit-worker`: push-event consumer
- `pr-worker`: pull-request consumer
- `completion-consumer`: commit-completion consumer
- `frontend`: React UI and backend proxy
- `ngrok`: optional public webhook tunnel, enabled through the `tunnel` profile

All services use one process per container. Compose waits for the backend health
check before starting the workers and frontend.

#### Build and start the complete application

Run these commands from the repository root:

```powershell
docker compose build
docker compose up -d
docker compose ps
```

The application is then available at:

| Component | URL |
|---|---|
| FastAPI health | http://localhost:8000/health |
| FastAPI OpenAPI | http://localhost:8000/docs |
| React UI | http://localhost:8501 |

`backend/.env` is loaded into the backend and worker services. `frontend/.env`
provides `PROJECT_ID`; Compose overrides `BACKEND_URL` with the internal address
`http://backend:8000` for the frontend proxy.

The backend and all three worker services receive the Langfuse variables from
`backend/.env`: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and optionally
`LANGFUSE_HOST` or `LANGFUSE_BASE_URL`. A standalone `docker run` validation
command does not receive those variables unless `--env-file backend/.env` is
explicitly supplied; use Compose for the normal runtime.

#### Start with the ngrok GitHub webhook tunnel

Build and start the complete stack with the tunnel profile:

```powershell
docker compose --profile tunnel up -d --build
docker compose ps
```

The ngrok inspector is available at http://localhost:4040. Open it to find the
public HTTPS forwarding URL, then configure the GitHub webhook as:

```text
https://<ngrok-host>/github/webhook
```

Enable the GitHub **Push** and **Pull requests** events and use the value of
`GITHUB_WEBHOOK_SECRET` from `backend/.env`. The tunnel forwards to the internal
Compose service `backend:8000`; no host-side `ngrok http 8000` command is needed.

This configuration does not provide or require an ngrok token. If the installed
ngrok version requires account authentication, use the host-based command below
with an authenticated ngrok installation, or use another tunnel such as
Cloudflare Tunnel.

If you prefer to run ngrok on the host instead of Compose, start the application
without the profile and run:

```powershell
docker compose up -d
ngrok http 8000
```

#### Monitor and operate the services

```powershell
# Follow all service logs
docker compose logs -f

# Follow one service
docker compose logs -f backend
docker compose logs -f commit-worker
docker compose logs -f pr-worker
docker compose logs -f completion-consumer
docker compose logs -f frontend
docker compose logs -f ngrok

# Restart one service after configuration changes
docker compose restart backend

# Stop services but keep containers
docker compose stop

# Stop and remove containers and the Compose network
docker compose down

# Remove containers, network, and built images
docker compose down --rmi local
```

If `GOOGLE_APPLICATION_CREDENTIALS` points to a service-account JSON file, mount
that file into each backend/worker container read-only and set the variable to the
container path (for example `/var/secrets/google/key.json`). Prefer Application
Default Credentials or workload identity in managed environments. For local
service-account authentication, add the following entries to `backend`,
`commit-worker`, `pr-worker`, and `completion-consumer` in
[compose.yaml](./compose.yaml):

```yaml
environment:
  GOOGLE_APPLICATION_CREDENTIALS: /var/secrets/google/key.json
volumes:
  - ./backend/service-account.json:/var/secrets/google/key.json:ro
```

Replace `backend/service-account.json` with the path to your local key file.
Never commit that file; it is excluded by [.dockerignore](./.dockerignore) and
the repository `.gitignore`.

---

## 12. Configuration

All secrets live in `backend/.env` (gitignored; never committed).

| Variable | Purpose |
|---|---|
| GOOGLE_CLOUD_PROJECT | GCP project for Firestore + Pub/Sub |
| GOOGLE_APPLICATION_CREDENTIALS | Service account key path |
| FIRESTORE_DATABASE | Firestore database id |
| GITHUB_EVENTS_TOPIC | Pub/Sub topic for webhook events (default github-webhook-events) |
| GITHUB_WEBHOOK_SECRET | HMAC secret for webhook signature verification |
| GITHUB_TOKEN | GitHub API access (commits, PR files, sandbox snapshot) |
| GOOGLE_API_KEY | Gemini API key for the agents |
| SLACK_BOT_TOKEN | Slack delivery (omit for mock mode) |
| SLACK_TEST_CHANNEL | Channel used by the live Slack test script |
| SLACK_STRICT | `true` = raise instead of mock fallback on send failure (default false) |
| SLACK_SEND_DELAY_SECONDS | Per-channel send pacing (default 1.0) |
| PROVIDER | LLM provider: `google` (default) or `azure` (via LiteLLM, needs `litellm` package) |
| AZURE_API_KEY / AZURE_API_BASE / AZURE_API_VERSION / AZURE_DEPLOYMENT | Azure OpenAI credentials + deployment (only when PROVIDER=azure) |
| AUTONOMY_ENABLED | Risk-based auto-send master switch (default false = manual approval only) |
| AUTONOMY_DRY_RUN | Evaluate + persist + audit autonomy without sending (default false) |
| AUTONOMY_MIN_CONFIDENCE | Min impact confidence for AUTO_SEND (default 0.8 — requires labeled eval before relaxing) |
| AUTONOMY_MIN_EVIDENCE | Min evidence items for AUTO_SEND (default 2) |
| AUTONOMY_ALLOW_ACTION | Allow action-impact auto-send (default false) |
| AUTONOMY_MAX_AUTO_PER_PR | Cap on auto-sends per PR (default 10) |
| BACKEND_URL | Frontend to backend base URL |
| BLAST_RADIUS_MAX_DEPTH | Dependency traversal depth (default 2) |

---

## 13. Testing

Offline unit suites (no GCP, no Slack, no network; the pre-push gate):

```bash
cd backend
for t in test_permissions_gateway test_idempotency test_slack_execution test_crash_retry \
         test_blast_radius_permissions test_sandbox_policy test_symbol_dependency \
         test_ownership_resolver test_blast_radius_service; do
  ./venv/Scripts/python.exe tests/$t.py > /dev/null 2>&1 && echo "PASS $t" || echo "FAIL $t"
done
```

Live integration scripts (run manually; they use real services): `test_full_pipeline.py`, `test_push_pipeline.py`, `test_firestore.py`, `test_pubsub.py`, `test_slack.py` (sends a real Slack message when a token is configured), `test_github_private.py` (requires CLI arguments).

---

## 14. Current Status

Implemented and connected:

- Webhook ingestion with signature verification, Pub/Sub fan-out, poison-pill handling
- Deterministic change records with classification, evidence, and components
- All six agents with guarded tools and structured outputs
- Permission gateway, audit logging, step-level idempotency, exactly-once Slack delivery
- Blast Radius V1: sandboxed static analysis, dependency traversal, ownership mapping
- React frontend for context, work journey, PR review/approval, and Slack mapping

Not yet implemented (planned):

- Jira integration, governance policy engine beyond the approval gate, verification and follow-up loops
- Merge-specific coordination flow, PDF and image context ingestion
- Cloud Run deployment, Dockerfile, CI/CD, Cloud Logging integration
- Work Journey living project state beyond the commit timeline

Known limitations:

- Approver identity is trusted to the approve endpoint (no authentication layer yet); every approval is audited
- The sandbox snapshot has no git history, so reviewer and contributor evidence is out of scope for V1
- The import graph parses all Python files in the snapshot; very large monorepos may need caps
- CODEOWNERS team references are matched verbatim against ProjectContext team names

---

## 15. Design Principles

1. Agents reason, deterministic code executes. Nothing that touches money, messages, or infrastructure is decided by an LLM.
2. Deny by default. Every identity, action, resource, tool, and path must be explicitly granted.
3. Evidence first. Every impact inference carries traceable evidence; missing ownership is never invented.
4. Exactly once. Every external side effect is idempotent across retries, redeliveries, and crashes.
5. Human in control. No Slack message leaves the system without an explicit, audited approval.
6. Degrade gracefully. Sandbox or analysis failures produce incomplete, honest artifacts rather than fabricated ones.
