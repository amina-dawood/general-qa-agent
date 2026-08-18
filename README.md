# General QA Agent

A documentation-driven QA platform for testing conversational assistants and webhook-based systems.

> **Portfolio edition:** this public repository intentionally excludes production databases, API keys, real endpoints, client documents, run history, and proprietary project data. The included Appointment Assistant is a synthetic demo target.

## What it does

General QA Agent turns product documentation into executable conversational QA:

1. Create a project and upload requirement documents.
2. Extract and index requirements for retrieval.
3. Generate test suites from documentation plus a developer-written generation prompt.
4. Simulate natural users across multi-turn conversations.
5. Execute tests against mock, generic webhook, or Twilio-style webhook targets.
6. Pause safely when a real human action is required, then resume the same test session.
7. Evaluate conversations, surface failures, and retain run-level analytics.

## Why I built it

Conversational systems are difficult to test with fixed request/response scripts. Real users answer partially, correct themselves, provide information early, skip steps, ask unrelated questions, and trigger external actions. This project explores a hybrid QA architecture where deterministic guards protect state/resources while AI handles natural language variation.

## Architecture

```text
Product documentation
        |
        v
Requirement extraction + indexing
        |
        v
Developer generation prompt
        |
        v
AI test-case generator
        |
        v
Canonical scenario facts / runtime guards
        |
        v
AI user simulator <----> Target webhook / assistant
        |
        v
Human-action pause/resume when required
        |
        v
Evaluation + diagnostics + analytics
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for more detail.

## Tech stack

- **Backend:** Python, FastAPI, SQLite, HTTPX
- **AI / evaluation:** LLM-based generation, simulation, diagnosis, embeddings, optional DeepEval
- **Frontend:** React, TypeScript, Vite
- **Integrations:** Generic JSON/form webhooks, Twilio-style inbound webhook simulation
- **Automation demo:** n8n
- **Deployment:** Local Windows scripts or Docker Compose

## Public demo included

The repository contains a fully synthetic Appointment Assistant example:

```text
examples/appointment-assistant/
├── requirements.pdf
├── n8n-workflow.json
└── README.md
```

Use it to demonstrate that the QA Agent can test a project it was not built around.

## Quick start on Windows

### Requirements

- Python 3.12
- Node.js / npm
- Git

### Setup

```bat
SETUP_WINDOWS.bat
```

Then open `.env` and set your local LLM API configuration.

Start the application:

```bat
START_LOCAL.bat
```

Open:

```text
http://127.0.0.1:8000
```

Stop it with:

```bat
STOP_LOCAL.bat
```

## Docker

```bat
START_DOCKER.bat
```

The public repository starts with an empty `data/` directory. Your local projects, uploads, databases, and reports stay under `data/` and are ignored by Git.

## Example QA workflow

1. Import `examples/appointment-assistant/n8n-workflow.json` into n8n.
2. Activate the workflow and copy its production webhook URL.
3. Create a new QA Agent project.
4. Configure a **Generic webhook** target:
   - message field: `message`
   - session field: `session_id`
   - response path: `reply`
5. Upload `examples/appointment-assistant/requirements.pdf`.
6. Generate a suite, approve it, and run a few cases.

A sample generation prompt is included in the example README.

## Core engineering ideas demonstrated

- Documentation-grounded test generation
- Developer-controlled test-suite prompts and exact requested counts
- Natural AI user simulation with deterministic safety guards
- Canonical runtime fact normalization and backward-compatible scenario handling
- Multi-entity state isolation
- Controlled external resources instead of invented URLs/addresses
- Human-in-the-loop pause/resume for OAuth/browser actions
- Generic target adapters and session isolation
- Conversational evaluation and failure diagnostics
- Full-stack dashboard for project, suite, and run management

## Privacy and repository safety

Before every push, run:

```bat
python scripts\public_repo_check.py
```

The checker rejects common secret files, database files, likely API tokens, email addresses, and known private-project identifiers.

See [`SECURITY.md`](SECURITY.md).

## Tests

The public edition includes generic unit tests only. Client-specific regression fixtures are intentionally not published.

```bat
set PYTHONPATH=%CD%\backend
python -m pytest -q backend\tests
```

Frontend:

```bat
cd dashboard
npm ci
npm run build
```

## Screenshots / demo video

Add sanitized screenshots under `docs/screenshots/` and link your demo video here later. Do not capture real endpoints, phone numbers, emails, API keys, or client data.

## Repository note

This repository is a sanitized portfolio copy. Production/client workspaces and proprietary configuration are deliberately kept outside Git.
