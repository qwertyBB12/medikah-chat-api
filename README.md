# Medikah Chat API

This repository contains the FastAPI backend for the Medikah triage and scheduling
experience. The `/chat` endpoint now runs a guided intake conversation that
collects patient details, captures symptom history, offers a telemedicine slot with
Medikah's on-call doctor, and—when approved—books the appointment through SendGrid
notifications. A standalone `/schedule` endpoint remains available for direct API
integration.

## Setup

1. Create and activate a Python virtual environment (optional but recommended):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Run the development server:

   ```bash
   uvicorn main:app --reload --port 8000
   ```

4. Copy `.env` (or set environment variables) with the required credentials. A
   quick way to verify everything is configured is:

   ```bash
   python3 check_env_vars.py
   ```

5. Start the API server. The default port is 8000:

   ```bash
   uvicorn main:app --reload --port 8000
   ```

6. Test the chat interaction:

   ```bash
   curl -X POST http://localhost:8000/chat \
     -H "Content-Type: application/json" \
     -d '{"message": "Hi, I need help"}'
   ```

   The response includes the agent's reply, the `session_id` token for continuing
   the conversation, and optional `actions` you can surface in a client UI.

## Environment Variables

The triage flow and scheduler expect the following variables to be present:

| Variable | Required | Purpose |
| --- | --- | --- |
| `SENDGRID_API_KEY` | ✅ | SendGrid API key with Mail Send permission |
| `SENDGRID_SENDER_EMAIL` | ✅ | Verified “from” address for SendGrid |
| `APPOINTMENT_HASH_KEY` | ✅ | Secret used by `SecureAppointmentStore` |
| `DOCTOR_NOTIFICATION_EMAIL` | ✅ | Recipient for on-call doctor alerts |
| `DOXY_ROOM_URL` | ✅ (recommended) | Direct link to the on-call Doxy.me room |
| `DOXY_BASE_URL` | ➖ fallback | Base URL used if `DOXY_ROOM_URL` is not supplied |
| `ON_CALL_DOCTOR_NAME` | ✅ | Display name for the telemedicine doctor |
| `SENDGRID_SANDBOX_MODE` | ➖ | Set to `true` to skip email delivery while testing |
| `OPENAI_API_KEY` | ➖ | Enables the educational follow-up summary in the triage flow |
| `LOG_LEVEL` | ➖ | Controls logging verbosity (defaults to `INFO`) |

Populate these in Render (or locally via `.env`) before deploying. After updating
variables in Render, redeploy with the build cache cleared, then confirm inside the
container:

```bash
python3 check_env_vars.py
env | grep DOXY
```

## Guided Triage Flow

1. **Greeting & Intake** – collects full name, email, symptom overview, and a brief
   history. Emergency phrases trigger immediate escalation and halt scheduling.
2. **Education snapshot** – after symptoms are captured, the assistant provides a
   short, non-diagnostic summary powered by OpenAI to orient the patient.
3. **Appointment offer** – the agent proposes a telemedicine visit with the
   configured on-call doctor, captures the preferred time, then confirms the intake
   summary for accuracy.
4. **Scheduling** – when the patient consents, the backend stores a hashed record,
   generates the calendar link, and sends SendGrid notifications (or skips them in
   sandbox mode). The Doxy room link is returned in the API response so a client UI
   can surface a “Join visit” action.

Conversation state is kept in-memory for roughly 90 minutes. Each session is keyed
by the `session_id` returned from `/chat`.

## Local Diagnostics

- `python3 check_env_vars.py` – quick pass/fail check for required environment keys.
- `uvicorn main:app --reload` – run the API locally.
- `python3 -m compileall main.py services` – sanity-compile the code (useful in CI).
