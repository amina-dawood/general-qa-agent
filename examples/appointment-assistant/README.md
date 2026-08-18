# Appointment Assistant Demo

This synthetic n8n project is included to demonstrate that General QA Agent can test a new system from documentation rather than a preconfigured client workspace.

## Import into n8n

Import `n8n-workflow.json`, activate it, and copy the production webhook URL.

The target accepts:

```json
{
  "message": "Hi, I want to book an appointment",
  "session_id": "demo-001"
}
```

and returns JSON containing:

```json
{
  "reply": "Sure! What's your name?"
}
```

## QA Agent connection

Create a project and choose **Generic webhook**.

- Payload: JSON
- Message field: `message`
- Session field: `session_id`
- Response path: `reply`
- Reset URL: leave blank

Upload `requirements.pdf` as the project documentation.

## Example generation prompt

```text
Generate exactly 10 independent test cases for this appointment assistant.

Include happy-path booking, different names and dates, Morning/Afternoon choices,
service-information questions, cancellation, vague-date recovery, invalid time
preference followed by correction, booking again, and session isolation.

Use natural wording, keep cases independent, and generate only behavior grounded
in the uploaded documentation. Return exactly 10 distinct runnable test cases.
```
