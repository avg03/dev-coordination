# Impact-Aware Engineering Change Coordinator

## 1. Project Overview

### One-line concept

An autonomous agentic workflow layer that continuously understands how
an engineering project evolves through GitHub commits, connects those
changes to project context and historical work, analyzes downstream
impact, identifies affected stakeholders, and coordinates
communication/actions when a PR is raised or merged.

### Core product philosophy

**Understand the evolution → reason about impact → coordinate the
organization.**

This is **not** a PR summarizer or AI notification bot.

The system maintains a **living project memory** and turns:

> Work Context + Continuous Change Memory + Repository Evidence\
> → Change Intelligence → Impact → Stakeholder Coordination → Action →
> Verification

### Hackathon Track

**The Taskmaster**

The agent takes a meaningful engineering change from detection through
analysis, communication, action, and follow-up with minimal
hand-holding.

------------------------------------------------------------------------

# 2. Core User Journey

``` text
1. User creates a project
        ↓
2. User provides project/work context
   (text / PDF / image)
        ↓
3. System builds Project Context Model
        ↓
4. Developer pushes commits
        ↓
5. GitHub push webhook fires
        ↓
6. System fetches commit/diff details
        ↓
7. Meaningful changes are analyzed
        ↓
8. Change Memory is updated
        ↓
9. Relevant previous changes + project context are linked
        ↓
10. PR is opened/updated
        ↓
11. Deep Change + Impact Analysis runs
        ↓
12. System identifies:
       - Awareness
       - Action
       - Approval
        ↓
13. Coordinator drafts stakeholder-specific messages
        ↓
14. User reviews/approves
        ↓
15. Slack/Jira actions execute
        ↓
16. System verifies actions
        ↓
17. Project Memory is updated
        ↓
18. Teammates can inspect Work Journey on demand
```

------------------------------------------------------------------------

# 3. High-Level Architecture

``` text
                         ┌───────────────────────┐
                         │         USER          │
                         │ Developer / PM / QA   │
                         └───────────┬───────────┘
                                     │
                                     ▼
                         ┌───────────────────────┐
                         │    React Dashboard    │
                         │                       │
                         │ • Project Context     │
                         │ • Change Analysis     │
                         │ • Work Journey        │
                         │ • Impact Report       │
                         │ • Coordination        │
                         │ • Approvals           │
                         └───────────┬───────────┘
                                     │
                                     ▼
                         ┌───────────────────────┐
                         │   FastAPI Backend     │
                         │       Cloud Run       │
                         └───────────┬───────────┘
                                     │
             ┌───────────────────────┼───────────────────────┐
             │                       │                       │
             ▼                       ▼                       ▼
      GitHub Webhooks              Pub/Sub              User Uploads
      push / PR events           async jobs            PDF / Text / Image
             │                       │                       │
             └───────────────────────┼───────────────────────┘
                                     ▼
                         ┌───────────────────────┐
                         │     Google ADK        │
                         │    Agent Runtime      │
                         └───────────┬───────────┘
                                     │
          ┌──────────────────────────┼──────────────────────────┐
          │                          │                          │
          ▼                          ▼                          ▼
   Context Agent             Change Analysis Agent        Impact Agent
          │                          │                          │
          └──────────────────────────┼──────────────────────────┘
                                     ▼
                         ┌───────────────────────┐
                         │ Stakeholder /         │
                         │ Coordination Agent    │
                         └───────────┬───────────┘
                                     │
                         ┌───────────┴───────────┐
                         ▼                       ▼
                    Slack API               Jira API
                         │                       │
                         └───────────┬───────────┘
                                     ▼
                              Verification
```

------------------------------------------------------------------------

# 4. Technology Stack

## Required Hackathon Technologies

  ---------------------------------------------------------------------------
  Layer                   Technology              Purpose
  ----------------------- ----------------------- ---------------------------
  Model                   Gemini 3.5+ / Vertex AI Reasoning, semantic
                                                  analysis, generation

  Agent framework         Google ADK              Agent orchestration, tools,
                                                  sessions/state

  Cloud                   Google Cloud            Production
                                                  deployment/infrastructure
  ---------------------------------------------------------------------------

## Application Stack

  Layer                        Technology
  ---------------------------- ------------------------------
  Frontend                     React + TypeScript
  Backend                      Python + FastAPI
  Agent framework              Google ADK
  Model SDK                    Google GenAI / Vertex AI
  Deployment                   Cloud Run
  Async processing             Pub/Sub
  Database / memory            Firestore
  File storage                 Cloud Storage
  Source control integration   GitHub API + GitHub Webhooks
  Communication                Slack API
  Action management            Jira API
  Containerization             Docker
  CI/CD                        GitHub Actions
  Logging                      Cloud Logging

### MVP integrations

Start with:

**GitHub + Gemini + ADK + Cloud Run + Firestore + Pub/Sub + Slack**

Add Jira after the core workflow works.

Discord, Teams, Linear, Notion, email, etc. are future integrations and
should not be built initially.

------------------------------------------------------------------------

# 5. Major System Modules

## Module A --- Project Context

### Purpose

Give the system an understanding of what the user is actually working on
before analyzing repository changes.

### Inputs

-   Text
-   PDF
-   Image
-   Architecture diagrams
-   Requirements
-   Task descriptions
-   Project plans

### Initial MVP

Start with text.

Then add PDF/image processing.

### Context Agent

Converts unstructured input into a structured Project Context Model.

### Example

``` json
{
  "project": "Authentication Redesign",
  "objective": "Migrate legacy authentication to OAuth",
  "components": [
    "auth-service",
    "api-gateway",
    "mobile-client"
  ],
  "teams": {
    "backend": ["auth-service"],
    "mobile": ["mobile-client"],
    "qa": ["authentication testing"],
    "product": ["requirements"]
  },
  "planned_work": [
    "OAuth migration",
    "API update",
    "Mobile integration",
    "QA regression"
  ],
  "dependencies": []
}
```

### Important principle

This is not merely a document RAG store.

It is a **structured representation of the intended project state**.

------------------------------------------------------------------------

# 6. Module B --- GitHub Event Ingestion

## GitHub Webhook

Use one webhook endpoint capable of receiving multiple event types.

### `push`

Purpose:

**Continuous change tracking**

``` text
Developer
   ↓ git push
GitHub
   ↓ push event
FastAPI
   ↓
Pub/Sub
   ↓
Commit Worker
```

### `pull_request`

Purpose:

**Deep PR analysis and coordination**

Relevant actions:

-   opened
-   synchronize / updated
-   reopened
-   closed / merged

### Important distinction

**Webhook = event notification**

**GitHub API = detailed information retrieval**

The webhook tells us that something happened. The GitHub API gives us
the detailed commit/PR/diff data required for analysis.

------------------------------------------------------------------------

# 7. Module C --- Commit Processing

After a push event:

``` text
GitHub Push Event
       ↓
FastAPI Webhook
       ↓
Pub/Sub
       ↓
Commit Worker
       ↓
GitHub API
       ↓
Commit + Diff + Repository Context
```

### GitHub information to retrieve

-   Repository
-   Branch
-   Commit SHA
-   Parent SHA
-   Author
-   Timestamp
-   Commit message
-   Files changed
-   Added/modified/deleted files
-   Additions
-   Deletions
-   Patch/diff
-   Relevant PR/issue references
-   Repository structure when needed

------------------------------------------------------------------------

# 8. Module D --- Meaningful Change Filter

Do not run expensive agent reasoning on every tiny commit.

### Example

``` text
README typo
   ↓
TRIVIAL
   ↓
Store basic event
```

``` text
API response schema changed
   ↓
SIGNIFICANT
   ↓
Deep analysis
```

Possible categories:

-   TRIVIAL
-   LOW
-   MEDIUM
-   HIGH
-   CRITICAL

Use lightweight deterministic rules and/or a lightweight model step.

------------------------------------------------------------------------

# 9. Module E --- Change Analysis Agent

For every meaningful change, determine:

## What changed?

-   Files
-   Functions
-   Classes
-   Modules
-   Behavior

## What does the changed code do?

Example:

``` text
src/auth/token.py
→ manages token lifecycle
```

## What behavior changed?

``` text
Before:
JWT refresh mechanism

After:
OAuth refresh-token mechanism
```

## Why did it change?

Use:

-   commit message
-   PR description
-   linked issue
-   project context
-   historical changes

Do not blindly assume intent when evidence is insufficient.

------------------------------------------------------------------------

# 10. Module F --- Relevant History Retrieval

The system should NOT retrieve all project history for every commit.

Retrieve relevant previous changes based on:

-   same files
-   same functions/classes
-   same component
-   same branch
-   same issue/ticket
-   dependency relationships
-   semantic similarity
-   temporal relevance

Example:

``` text
Current Commit C118
       │
       ├── auth/token.py
       └── OAuth refresh
              │
              ▼
Relevant History
              │
       ┌──────┼──────┐
       ▼      ▼      ▼
      C064   C087   C102
```

------------------------------------------------------------------------

# 11. Module G --- Change Timeline

The system should build a meaningful timeline.

Example:

``` text
C064
Initial OAuth architecture
       ↓
C087
Token refresh introduced
       ↓
C102
Mobile API dependency added
       ↓
C118
Refresh mechanism modified
```

This gives the agent **temporal context**.

The system should understand:

> How the current change evolved from previous work.

------------------------------------------------------------------------

# 12. Module H --- Living Change Memory

The Change Memory is the core persistent intelligence layer.

For each meaningful change, store:

## 1. Commit Identity

-   SHA
-   Author
-   Timestamp
-   Branch
-   Commit message

## 2. Raw Change

-   Files
-   Additions
-   Deletions
-   Diff
-   Functions/classes
-   File status

## 3. Semantic Meaning

-   What changed?
-   Why?
-   Previous behavior
-   New behavior
-   Change type

## 4. Architecture

-   Component
-   Service
-   Module
-   Architecture layer
-   Dependencies

## 5. Project Relationship

-   Related task
-   Related feature
-   Original plan
-   Previous decisions
-   Related commits

## 6. Impact

-   Affected systems
-   Affected workflows
-   Affected teams
-   Potential downstream work

## 7. Stakeholder-Specific Impact

Example:

``` text
Backend → Awareness
Mobile → Action
QA → Action
PM → Awareness
Design → No Impact
External Client → Approval
```

## 8. Evidence

Every inference must have supporting evidence.

Example:

``` text
Inference:
Mobile team is affected.

Evidence:
- auth.py response schema changed
- mobile client consumes /auth/refresh
- corresponding mobile consumer found
```

## 9. Confidence

Example:

``` text
impact_confidence: 0.91
```

------------------------------------------------------------------------

# 13. Evidence Model

Evidence must be a first-class data object.

``` text
Change
  ↓
Inference
  ↓
Evidence
  ↓
Impact
  ↓
Stakeholder
  ↓
Required Action
```

Example:

``` json
{
  "inference": "Mobile team is affected",
  "impact": "Mobile integration may need an update",
  "confidence": 0.91,
  "evidence": [
    {
      "type": "file_change",
      "source": "src/api/auth.py",
      "detail": "Response schema changed"
    },
    {
      "type": "dependency",
      "source": "mobile/src/auth.ts",
      "detail": "Consumes /auth/refresh"
    }
  ]
}
```

### Why evidence matters

-   Trust
-   Explainability
-   Debugging
-   Evaluation
-   Reducing arbitrary stakeholder guesses

------------------------------------------------------------------------

# 14. Module I --- Living Project State

After processing a meaningful change, update the current project
understanding.

Example:

### Before

``` text
Auth API
 ↓
OAuth v1
 ↓
Mobile
```

### After

``` text
Auth API
 ↓
OAuth v2
 ↓
Mobile dependency
```

The memory should represent both:

-   historical evolution
-   current known project state

------------------------------------------------------------------------

# 15. Module J --- PR Analysis

## PR opened/updated

Trigger:

``` text
pull_request webhook
```

Retrieve:

-   PR description
-   final/current diff
-   changed files
-   commit SHAs
-   linked issues
-   relevant Change Memory
-   Project Context
-   repository/dependency information

Then:

``` text
PR
+
Change Memory
+
Project Context
+
Architecture
+
Relevant History
       ↓
Gemini + ADK
       ↓
Change Intelligence
```

------------------------------------------------------------------------

# 16. PR Change Intelligence Output

The system should produce:

### What changed?

### Why did it change?

### What is the current architecture?

### What architectural component changed?

### What previous work is related?

### Which components are affected?

### Which workflows are affected?

### What future work may be required?

### Who is affected?

### What evidence supports each conclusion?

### Confidence

------------------------------------------------------------------------

# 17. Module K --- Impact Analysis Agent

The Impact Agent constructs an impact graph.

``` text
                 CHANGE
                    │
                    ▼
              Auth Service
              /     |     \
             /      |      \
            ▼       ▼       ▼
          API     Session   Token
           │
           ▼
        Mobile
           │
           ▼
      Mobile Team
```

Analyze:

### Direct impact

Directly changed component.

### Dependency impact

Components consuming the changed component.

### Workflow impact

Processes relying on changed behavior.

### Future impact

Work likely required downstream.

------------------------------------------------------------------------

# 18. Module L --- Stakeholder Intelligence

Classify each relevant stakeholder as:

### Awareness

Needs information.

### Action

Needs to perform work.

### Approval

Must approve before external/sensitive action.

### No Impact

Should not be notified.

Example:

``` text
Backend → Awareness
Mobile → Action
QA → Action
PM → Awareness
External Client → Approval
Design → No Impact
```

------------------------------------------------------------------------

# 19. Team / Ownership Context

For MVP, create a simple team registry.

Example:

``` json
{
  "backend": {
    "components": ["auth-service", "api"],
    "members": ["..."]
  },
  "mobile": {
    "components": ["mobile-client"],
    "members": ["..."]
  },
  "qa": {
    "areas": ["authentication", "regression"],
    "members": ["..."]
  },
  "product": {
    "areas": ["requirements"],
    "members": ["..."]
  }
}
```

Later, ownership can be derived from:

-   GitHub CODEOWNERS
-   Jira ownership
-   repository structure
-   manually configured project context

------------------------------------------------------------------------

# 20. Module M --- Coordinator

Input:

``` text
Change Intelligence
+
Impact Graph
+
Stakeholder Registry
+
Governance Policies
```

Output:

``` text
Who?
Why?
Awareness / Action / Approval?
Channel?
Message?
Required follow-up?
```

------------------------------------------------------------------------

# 21. Communication Agent

Generate role-specific messages from the same underlying Change
Intelligence.

### Developer

Technical details, files, dependencies, risks.

### QA

Regression focus, changed behavior, testing areas.

### PM

Product/business impact and user-facing implications.

### Team Group

Common context + actions for everyone.

### Personal DM

Only information/action relevant to that individual.

------------------------------------------------------------------------

# 22. Group Communication

Example:

``` text
🔔 Change Impact Update

Change:
OAuth refresh mechanism updated.

Affected components:
• Auth Service
• API authentication

Required actions:
• Mobile → update integration
• QA → regression-test authentication

Related PR:
#142
```

------------------------------------------------------------------------

# 23. Personal Communication

Example:

``` text
Hey Rahul,

PR #142 changed the authentication API response.

Since the mobile client consumes this endpoint,
your integration may need to be updated.

Required:
→ Update refresh-token handling

Evidence:
auth.py response schema changed.
```

------------------------------------------------------------------------

# 24. Module N --- Governance

Before execution:

``` text
Proposed Action
      ↓
Governance Check
      │
 ┌────┼────┐
 ▼    ▼    ▼
SAFE REVIEW BLOCK
 │     │     │
 ▼     ▼     ▼
Send  Human Reject
      Approval
```

Example policies:

``` text
Internal Slack → Auto-send
Internal Jira task → Auto-create
External communication → Approval
Sensitive security change → Approval
Restricted information → Block
```

------------------------------------------------------------------------

# 25. Human Approval

Dashboard should show:

``` text
COORDINATION PLAN

Backend
✓ Awareness

Mobile
⚠ Action Required

QA
⚠ Action Required

PM
✓ Awareness

External
🔒 Approval Required

[View Message]
[Edit]
[Approve]
[Reject]
```

Human remains in control.

------------------------------------------------------------------------

# 26. Module O --- Execution

After approval:

``` text
Coordinator
   │
   ├──────────────► Slack API
   │
   └──────────────► Jira API
```

### Slack

-   Team channel
-   Personal DM

### Jira

-   Create task
-   Assign task
-   Link PR
-   Add context
-   Set priority
-   Track status

------------------------------------------------------------------------

# 27. Module P --- Verification

After execution:

``` text
Message sent?
      ↓
Correct recipient?
      ↓
Task created?
      ↓
Task completed?
      ↓
Follow-up required?
```

Example:

``` text
✓ QA notified
✓ Mobile Jira task created
✓ PM notified
🟡 Mobile action pending
```

------------------------------------------------------------------------

# 28. Module Q --- Follow-up

If an action remains incomplete:

``` text
PR merged
    ↓
Mobile task created
    ↓
Task remains pending
    ↓
Agent checks state
    ↓
Follow-up required
    ↓
Notify relevant stakeholder
```

Follow-up must be policy-controlled; do not spam.

------------------------------------------------------------------------

# 29. Module R --- Work Journey

This is **separate from notification**.

It is user-initiated.

A teammate can open:

``` text
Rahul → Work Journey
```

and see:

``` text
Authentication Redesign

Work started
      ↓
OAuth architecture
      ↓
Token refresh implementation
      ↓
API contract change
      ↓
Mobile dependency introduced
      ↓
QA regression pending

Current status:
🟡 In Progress
```

Clicking a change should show:

-   What changed
-   Why
-   Files/functions
-   Previous related work
-   Project context
-   Impact
-   Evidence
-   Current downstream status

------------------------------------------------------------------------

# 30. Work Journey Principle

This is **not employee surveillance**.

Do NOT expose:

-   keystrokes
-   productivity scores
-   idle time
-   exact coding time
-   developer rankings

The feature is for:

> **Understanding how a piece of work evolved and where it currently
> stands.**

------------------------------------------------------------------------

# 31. Backend Architecture

``` text
FastAPI
│
├── /health
├── /projects
├── /projects/{id}/context
├── /github/webhook
├── /projects/{id}/changes
├── /projects/{id}/journey
├── /projects/{id}/impact
├── /coordination
├── /approvals
└── /integrations
```

Use service modules behind these endpoints rather than placing business
logic directly in route handlers.

------------------------------------------------------------------------

# 32. Suggested Backend Structure

``` text
backend/
├── app/
│   ├── main.py
│   │
│   ├── api/
│   │   ├── projects.py
│   │   ├── github.py
│   │   ├── changes.py
│   │   ├── impact.py
│   │   ├── coordination.py
│   │   └── approvals.py
│   │
│   ├── agents/
│   │   ├── context_agent.py
│   │   ├── change_agent.py
│   │   ├── impact_agent.py
│   │   ├── coordinator_agent.py
│   │   └── communication_agent.py
│   │
│   ├── services/
│   │   ├── github_service.py
│   │   ├── firestore_service.py
│   │   ├── pubsub_service.py
│   │   ├── slack_service.py
│   │   └── jira_service.py
│   │
│   ├── workers/
│   │   ├── commit_worker.py
│   │   └── pr_worker.py
│   │
│   ├── models/
│   │   ├── project.py
│   │   ├── change.py
│   │   ├── impact.py
│   │   └── coordination.py
│   │
│   └── core/
│       ├── config.py
│       ├── security.py
│       └── logging.py
│
├── tests/
├── Dockerfile
├── requirements.txt
└── .env.example
```

Keep the exact structure flexible as implementation evolves.

------------------------------------------------------------------------

# 33. Data Flow

## Push Flow

``` text
GitHub Push
    ↓
Webhook
    ↓
Pub/Sub
    ↓
Commit Worker
    ↓
GitHub API
    ↓
Change Extraction
    ↓
Meaningful Change Filter
    ↓
Relevant Context + History Retrieval
    ↓
Change Analysis Agent
    ↓
Evidence-backed Change Record
    ↓
Firestore
    ↓
Living Project State
```

## PR Flow

``` text
PR Opened/Updated
       ↓
Webhook
       ↓
Pub/Sub
       ↓
PR Worker
       ↓
Retrieve:
  - PR
  - Final diff
  - Commit memory
  - Project context
  - Architecture/dependencies
       ↓
Change Analysis
       ↓
Impact Agent
       ↓
Stakeholder Agent
       ↓
Impact Report
       ↓
Dashboard
```

## Merge/Coordination Flow

``` text
PR Merged
    ↓
Final Impact Confirmation
    ↓
Coordinator
    ↓
Governance
    ↓
Draft Messages + Actions
    ↓
Human Approval
    ↓
Slack / Jira
    ↓
Verification
    ↓
Follow-up
    ↓
Project Memory Update
```

------------------------------------------------------------------------

# 34. Important Design Principle: Deterministic vs Agentic

Do NOT make everything an LLM call.

Use deterministic code for:

-   webhook handling
-   authentication
-   GitHub API calls
-   Pub/Sub
-   database operations
-   state transitions
-   policy enforcement
-   Slack/Jira API calls
-   idempotency
-   retries

Use agents/models for:

-   interpreting project context
-   semantic change understanding
-   connecting historical changes
-   impact reasoning
-   stakeholder reasoning
-   message generation
-   ambiguous decisions

This keeps the system cheaper, more reliable, and easier to debug.

------------------------------------------------------------------------

# 35. State Machine

Use explicit task states:

``` text
RECEIVED
   ↓
PROCESSING
   ↓
ANALYZED
   ↓
IMPACT_READY
   ↓
WAITING_APPROVAL
   ↓
APPROVED
   ↓
EXECUTING
   ↓
EXECUTED
   ↓
VERIFIED
   ↓
COMPLETED
```

Failure path:

``` text
PROCESSING
   ↓
FAILED
   ↓
RETRY
   ↓
PROCESSING
```

Use idempotency keys such as:

``` text
github_event_id
commit_sha
pr_id + head_sha
```

to prevent duplicate processing.

------------------------------------------------------------------------

# 36. Security / Governance Principles

Minimum requirements:

-   Verify GitHub webhook signatures.
-   Never expose GitHub/Slack/Jira secrets to the frontend.
-   Store secrets in environment/secret management, not source code.
-   Apply least-privilege OAuth/API permissions.
-   Separate read tools from action tools.
-   Require approval for external/sensitive actions.
-   Maintain audit records for agent decisions and actions.
-   Treat repository contents as untrusted input.
-   Do not let repository text override system/policy instructions.

------------------------------------------------------------------------

# 37. Evaluation Strategy

Create 20--50 representative scenarios.

Example:

### Case 1

API contract changed.

Expected:

``` text
Mobile → Action
QA → Action
PM → Awareness
```

### Case 2

Frontend styling changed.

Expected:

``` text
Design → Awareness
PM → Awareness
QA → Possible Action
Backend → No impact
```

### Case 3

Backend internal refactor with unchanged API.

Expected:

``` text
Backend → Awareness
External → No communication
```

Measure:

-   Change classification accuracy
-   Impact accuracy
-   Stakeholder accuracy
-   Evidence correctness
-   False notifications
-   Action correctness
-   Completion rate
-   Latency
-   Model/tool errors
-   Estimated cost per task

------------------------------------------------------------------------

# 38. MVP Scope

## Must Have

``` text
✓ Project context
✓ GitHub integration
✓ Push webhook
✓ Commit memory
✓ Relevant history retrieval
✓ PR webhook
✓ Change analysis
✓ Impact analysis
✓ Stakeholder identification
✓ Evidence
✓ Slack integration
✓ Human approval
✓ Cloud Run deployment
```

## Strong MVP+

``` text
✓ Pub/Sub async processing
✓ Work Journey
✓ Jira integration
✓ Governance policies
✓ Verification
✓ Follow-up
✓ Evaluation dashboard
```

## Later / Optional

``` text
○ Discord
○ Microsoft Teams
○ Linear
○ Notion
○ Email
○ Automatic documentation updates
○ Learned routing from user corrections
○ Advanced dependency graph
```

------------------------------------------------------------------------

# 39. Recommended Build Order

Do NOT build all agents first.

Build in these milestones:

### Milestone 1 --- System sees

``` text
GitHub
 ↓
Webhook
 ↓
Commit
 ↓
Firestore
```

### Milestone 2 --- System understands

``` text
Commit
+
Project Context
+
History
 ↓
Gemini / ADK
 ↓
Meaningful Change
```

### Milestone 3 --- System reasons

``` text
Change
 ↓
Impact
 ↓
Stakeholders
 ↓
Evidence
```

### Milestone 4 --- System acts

``` text
Impact
 ↓
Message
 ↓
Approval
 ↓
Slack
 ↓
Jira
```

### Milestone 5 --- System remembers

``` text
All changes
 ↓
Living Project Memory
 ↓
Work Journey
```

### Milestone 6 --- System is production-grade

``` text
Async
+
Retries
+
Idempotency
+
Governance
+
Evaluation
+
Observability
```

------------------------------------------------------------------------

# 40. Immediate Development Goal

Start with only:

``` text
Developer
    ↓
git push
    ↓
GitHub
    ↓
push webhook
    ↓
Cloud Run / FastAPI
    ↓
Pub/Sub
    ↓
Commit Worker
    ↓
GitHub API
    ↓
Commit + Diff
```

Do not build the sophisticated agentic workflow until this pipeline
works reliably.

The first success condition is:

> **I push a real commit, and my backend receives it, retrieves the
> correct diff, and stores a structured Change Record.**

Everything else builds on this foundation.

------------------------------------------------------------------------

# 41. Core Product Narrative

When explaining the project, keep returning to this:

### Traditional workflow

``` text
PR
 ↓
Diff
 ↓
Summary
 ↓
Notification
```

### Our workflow

``` text
Project Context
      +
Continuous Change Memory
      +
Historical Evolution
      +
Repository Evidence
      ↓
What changed?
      ↓
What does it mean?
      ↓
What does it affect?
      ↓
Who needs to know?
      ↓
Who needs to act?
      ↓
What should each person receive?
      ↓
Did the required action happen?
```

## Final product definition

> **An evidence-backed, continuously learning agentic coordination layer
> that understands how engineering work evolves and turns meaningful
> changes into targeted stakeholder actions.**
