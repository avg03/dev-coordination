# Agent Coding Rules

## Google ADK Version

This project MUST use Google ADK 2.5.0.

- Do NOT use ADK 1.x APIs.
- Do NOT copy examples written for ADK 1.x.
- Before implementing ADK functionality, consult the current ADK 2.x API/documentation.
- If an older example conflicts with the installed package, prefer the installed ADK 2.5.0 API.
- Do not downgrade google-adk to solve compatibility issues.
- Keep requirements pinned to:
  google-adk==2.5.0

## Project Architecture

This project uses:
- Gemini 3.5+
- Google ADK 2.5.0
- FastAPI
- Cloud Run
- Pub/Sub
- Firestore
- GitHub API/Webhooks
- Slack API
- Jira API

## Agent Architecture

Context Agent
→ Change Analysis Agent
→ Impact Agent
→ Stakeholder Coordinator
→ Communication Agent

Use deterministic Python services for:
- GitHub API
- Webhooks
- Pub/Sub
- Firestore
- Slack/Jira
- Authentication
- State transitions
- Governance rules

Use ADK/Gemini for:
- Semantic understanding
- Context extraction
- Change reasoning
- Impact reasoning
- Stakeholder reasoning
- Message generation