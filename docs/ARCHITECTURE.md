# Architecture

## Design goal

The QA engine should learn expected behavior from each project's documentation instead of requiring a separate hard-coded test runner for every assistant.

## Major components

### 1. Project + document layer

Projects store target configuration, controlled test resources, and indexed requirement documents. Runtime state lives in SQLite locally and is intentionally excluded from the public repository.

### 2. Requirement retrieval

Uploaded PDF, DOCX, TXT, and Markdown files are parsed, chunked, embedded, and retrieved as grounding context for generation and evaluation.

### 3. Test generation

A developer can enter a normal-language suite prompt in the dashboard. The generation pipeline combines that request with indexed requirements, validates runnable structure, preserves intentional negative/recovery states, and enforces explicit requested test counts.

### 4. Runtime scenario normalization

Generated/legacy scenario facts are normalized into canonical runtime facts. Deterministic code handles exact identities, resources, indexing, and safety constraints; AI remains responsible for natural conversational behavior.

### 5. User simulator

The simulator acts like a natural user: short answers, partial answers, corrections, interruptions, recovery, and context-sensitive responses. Deterministic guards prevent cross-slot mistakes and accidental resource leakage.

### 6. Target adapters

- Mock target for installation checks
- Generic JSON/form webhook
- Twilio-style inbound SMS webhook simulation

Each case gets a session identity so stateful targets can isolate conversations.

### 7. Human action

When a real browser/OAuth/external action cannot be safely simulated, the run pauses and stores the test state. After the tester completes the action, the same session resumes without replaying an already-completed action.

### 8. Evaluation

Completed conversations are evaluated against documented expectations, deterministic checks, latency signals, and optional DeepEval metrics. Failures can be diagnosed and summarized for review.

## Safety boundary

The system distinguishes between:

- **invalid user behavior**, which is often exactly what a QA scenario should test; and
- **invalid test structure**, which makes execution impossible.

External resources such as real URLs, addresses, or authorization actions should come from tester-controlled configuration or human input rather than model invention.
