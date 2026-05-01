## Governance Integration

@extends /.governance/guides/06-medikah.md

**Always read first:**
1. `/.governance/CLAUDE.md` (global instructions)
2. `/.governance/quick-ref/forbidden.md` (absolute rules)
3. `/MedikahHub/CLAUDE.md` (parent project)
4. This file (backend-specific)

---

## Governance Notes (Backend)

@project-specific

The backend is a Python/FastAPI service with no direct UI. Governance design rules (colors, typography, radius) do not apply here. However:

1. **Voice in API responses** — AI triage responses and email templates must follow Medikah voice: clinical but warm, compliance-visible. Never sound like a startup or give medical advice.
2. **Forbidden language** — All patient-facing text (email templates, AI prompts, error messages) must avoid forbidden terms from `/.governance/quick-ref/forbidden.md`.
3. **Compliance** — All API responses that touch patient data must include or reference the Medikah compliance disclaimer. Never claim Medikah provides medical care.
4. **Bilingual** — AI responses auto-detect language (EN/ES). Email templates must support both locales.

---

# CLAUDE.md - Medikah Chat API

> Backend service for the Medikah telehealth platform. This file provides context for Claude Code sessions working on this repository.

## Project Overview

**Medikah Chat API** is a FastAPI-based backend service handling patient intake conversations, AI-powered triage, appointment scheduling, and email notifications for the Medikah Pan-American telehealth platform.

**Repository:** `medikah-chat-api`
**Type:** Python/FastAPI REST API
**Related:** `medikah-chat-frontend` (Next.js web app)

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Language | Python 3.13 |
| Framework | FastAPI |
| Server | Uvicorn (ASGI) |
| AI | OpenAI GPT-4o |
| Database | Supabase (PostgreSQL) |
| Email | Resend |
| Rate Limiting | SlowAPI |
| Deployment | Render / Netlify Functions |

---

## Project Structure

```
medikah-chat-api/
├── main.py                    # FastAPI app, endpoints, orchestration
├── requirements.txt           # Python dependencies
├── .env.example              # Environment variable template
├── check_env_vars.py         # Diagnostic script
├── README.md                 # Documentation
│
├── routes/
│   ├── physician_routes.py    # Physician dashboard, inquiries, availability
│   └── ai_routes.py          # AI clinical decision support (/ai/diagnosis)
│
├── models/
│   └── physician.py          # Pydantic schemas (profile, inquiries, availability)
│
├── services/
│   ├── triage.py             # TriageConversationEngine (state machine)
│   ├── conversation_state.py # Session storage, IntakeHistory
│   ├── ai_triage.py          # GPT-4o response generator
│   ├── appointments.py       # SecureAppointmentStore (HMAC hashed)
│   ├── notifications.py      # Resend email service (patient)
│   ├── physician_dashboard.py # Physician profile, inquiries, availability logic
│   ├── physician_notifications.py # Physician inquiry accepted/declined emails
│   └── physician_calendar.py # Physician calendar generation
│
├── utils/
│   └── scheduling.py         # Calendar generation (ICS, Google)
│
├── db/
│   ├── client.py             # Supabase singleton
│   ├── schema.sql            # Database DDL
│   └── migrations/
│       └── 002_dashboard_tables.sql  # patient_inquiries + physician_availability
│
└── netlify/                  # Alternative serverless deployment
    └── functions/api.ts
```

---

## API Endpoints

### Patient Endpoints

| Endpoint | Method | Rate Limit | Purpose |
|----------|--------|------------|---------|
| `/chat` | POST | 30/min | Triage intake conversation |
| `/schedule` | POST | 10/min | Direct appointment booking |
| `/` | GET | - | Root health check |
| `/ping` | GET | - | Lightweight ping |
| `/health` | GET | - | Detailed diagnostics |

### Physician Endpoints (prefix: `/physicians`)

| Endpoint | Method | Rate Limit | Purpose |
|----------|--------|------------|---------|
| `/physicians/{id}/dashboard` | GET | 30/min | Profile + stats |
| `/physicians/{id}/inquiries` | GET | 30/min | List patient inquiries (paginated) |
| `/physicians/{id}/inquiries/{iid}/accept` | POST | 10/min | Accept inquiry |
| `/physicians/{id}/inquiries/{iid}/decline` | POST | 10/min | Decline inquiry |
| `/physicians/{id}/availability` | GET | 30/min | Get schedule |
| `/physicians/{id}/availability` | PUT | 10/min | Update schedule |

### AI Endpoints (prefix: `/ai`)

| Endpoint | Method | Rate Limit | Purpose |
|----------|--------|------------|---------|
| `/ai/diagnosis` | POST | 10/min | Clinical decision support (differential diagnosis) |

### POST /chat

Main conversation endpoint. Handles the full intake flow.

**Request:**
```json
{
  "message": "I have a headache",
  "session_id": "optional_existing_session",
  "locale": "en",
  "timezone": "America/Mexico_City"
}
```

**Response:**
```json
{
  "reply": "AI-generated response",
  "session_id": "unique_token",
  "stage": "collect_symptoms",
  "actions": [{"label": "Join visit", "url": "https://doxy.me/..."}],
  "appointment_confirmed": false,
  "emergency_noted": false
}
```

### POST /schedule

Direct scheduling (bypasses chat flow).

**Request:**
```json
{
  "patient_name": "John Doe",
  "patient_contact": "john@example.com",
  "appointment_time": "2025-02-10T14:00:00-06:00",
  "symptoms": "Headache for 3 days",
  "locale_preference": "en",
  "patient_timezone": "America/Mexico_City"
}
```

---

## Conversation State Machine

The triage engine (`services/triage.py`) drives conversations through these stages:

```
WELCOME → COLLECT_SYMPTOMS → COLLECT_HISTORY → COLLECT_NAME
        → COLLECT_EMAIL → COLLECT_TIMING → CONFIRM_SUMMARY → SCHEDULED
                                                          ↘ FOLLOW_UP
        ↳ EMERGENCY_ESCALATED (if keywords detected)
```

### Emergency Detection

Keyword-based safety net triggers immediate escalation:

**English:** "chest pain", "shortness of breath", "unconscious", "suicidal", "overdose", "stroke", "heart attack", "severe pain"

**Spanish:** "dolor de pecho", "falta de aire", "sangrado", "inconsciente", "sobredosis", "derrame", "infarto"

---

## AI Response Generation

`services/ai_triage.py` wraps OpenAI GPT-4o with:

- Stage-specific system prompts
- Bilingual support (auto-detects English/Spanish)
- Conversation history (last 10 turns)
- Graceful fallback to hardcoded responses
- Rules: Never diagnose, never mention "Doxy.me", always say "Medikah"

**Model Config:**
- Model: `gpt-4o`
- Max tokens: 400
- Temperature: 0.8

---

## Appointment Storage

`services/appointments.py` uses cryptographic hashing for privacy:

- Patient email is HMAC-SHA256 hashed before storage
- Only patient name stored in plain text
- Supports Supabase persistence or in-memory fallback

---

## Email Notifications

`services/notifications.py` sends via Resend:

**Patient Email:**
- Appointment confirmation with local timezone
- ICS calendar attachment
- "Join Your Visit" button with Doxy.me link
- Professional HTML template (Medikah branding)

**Doctor Email:**
- Patient name and symptoms
- Intake notes from conversation
- Doxy.me link

---

## Environment Variables

### Required

```bash
# Email (Resend)
RESEND_API_KEY=re_xxxxx
RESEND_SENDER_EMAIL=Medikah <noreply@medikah.health>

# Appointments
APPOINTMENT_HASH_KEY=<openssl rand -hex 32>
DOCTOR_NOTIFICATION_EMAIL=oncall@medikah.health

# Telemedicine
DOXY_ROOM_URL=https://doxy.me/medikahhealth/
```

### Recommended

```bash
# AI
OPENAI_API_KEY=sk-xxxxx

# Doctor Display
ON_CALL_DOCTOR_NAME=Dr. Smith

# Database (optional - falls back to in-memory)
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...
```

### Optional

```bash
# CORS
ALLOWED_ORIGINS=https://medikah.health,http://localhost:3000

# Appointment
APPOINTMENT_DURATION_MINUTES=30

# Debug
EMAIL_SANDBOX_MODE=false
LOG_LEVEL=INFO
```

---

## Database Schema

### conversation_sessions

Stores active intake sessions:
- `session_id` (PK)
- `stage`, `patient_name`, `patient_email`
- `symptom_overview`, `symptom_history`
- `preferred_time_utc`, `patient_timezone`
- `notes` (JSONB), `message_history` (JSONB)
- `created_at`, `updated_at`

### appointments

Stores scheduled appointments:
- `appointment_id` (PK, UUID)
- `patient_name`
- `patient_contact_hash` (HMAC-SHA256)
- `appointment_time` (UTC)
- `created_at`

### patient_inquiries

Patient requests to physicians:
- `id` (PK, UUID)
- `physician_id` (FK → physicians)
- `patient_name`, `patient_email`, `symptoms`
- `preferred_time` (TIMESTAMPTZ)
- `status` ('pending'|'accepted'|'declined')
- `decline_reason`, `locale`
- `created_at`, `updated_at`

### physician_availability

Physician weekly schedule:
- `physician_id` (PK, FK → physicians)
- `timezone` (default 'UTC')
- `schedule` (JSONB — array of day/time slot objects)
- `updated_at`

---

## Time Handling

**Critical:** All times stored in UTC. Display in patient's local timezone.

**Parsing supports:**
- Relative: "tomorrow", "today", "next week", "mañana"
- Absolute: "February 10 at 3pm", "2025-02-10T15:00"
- Timezone-aware conversion using `zoneinfo`

**Display:** Convert back to patient timezone before showing in chat/email.

---

## Local Development

```bash
# Setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your keys

# Run
uvicorn main:app --reload --port 8000

# Test
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello"}'
```

---

## Deployment

### Render (Primary)

- Deployed as Python web service
- Environment variables set in Render dashboard
- Auto-deploys on push to main

### Netlify (Alternative)

- Uses `netlify/functions/api.ts` Express wrapper
- Limited functionality compared to main.py

---

## Design System Reference

When generating emails or responses, use these brand colors:

| Color | Hex | Usage |
|-------|-----|-------|
| Institutional Navy | `#1B2A41` | Headers, buttons |
| Clinical Teal | `#2C7A8C` | Accents, links |
| Body text | `#4A5568` | Paragraphs |

Font: **Mulish** (referenced in email templates)

---

## AI Clinical Decision Support

`routes/ai_routes.py` provides a stateless differential diagnosis tool for physicians:

- **Endpoint:** `POST /ai/diagnosis`
- **Input:** symptoms (required), age_range, sex (optional)
- **Output:** ranked differentials with confidence levels (HIGH/MODERATE/LOW), red flags
- **System prompt:** Clinical decision support framing — never definitive diagnoses
- **Privacy:** No PII stored, stateless, anonymous
- **Rate limit:** 10/min
- **Requires:** `OPENAI_API_KEY` env var (disabled if not set)

---

## Security Notes

1. **Contact Hashing:** Patient emails HMAC-SHA256 hashed
2. **Service Role:** Supabase uses admin key, not exposed to clients
3. **Rate Limiting:** 30/min on /chat and dashboard, 10/min on /schedule and actions
4. **CORS:** Whitelist of allowed origins
5. **No Diagnosis (Patient):** Patient-facing AI explicitly forbidden from medical advice
6. **Clinical Support (Physician):** AI diagnosis tool framed as decision support only, with disclaimers
7. **RLS Policies:** patient_inquiries and physician_availability use row-level security

---

## Common Issues

| Issue | Solution |
|-------|----------|
| `DOXY_ROOM_URL` showing "(not set)" | Add to environment variables |
| Emails not sending | Check `RESEND_API_KEY` and verified domain |
| AI responses empty | Verify `OPENAI_API_KEY` is set |
| Timezone showing UTC | Ensure `patient_timezone` passed from frontend |

---

## Related Repository

**Frontend:** `medikah-chat-frontend`
- Next.js web application
- Calls `/chat` and `/schedule` endpoints for patient flow
- Calls `/physicians/*` endpoints for dashboard (inquiries, availability)
- Calls `/ai/diagnosis` for physician clinical decision support tool
- Handles physician onboarding and public profiles separately
