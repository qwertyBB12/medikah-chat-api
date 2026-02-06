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
├── services/
│   ├── triage.py             # TriageConversationEngine (state machine)
│   ├── conversation_state.py # Session storage, IntakeHistory
│   ├── ai_triage.py          # GPT-4o response generator
│   ├── appointments.py       # SecureAppointmentStore (HMAC hashed)
│   └── notifications.py      # Resend email service
│
├── utils/
│   └── scheduling.py         # Calendar generation (ICS, Google)
│
├── db/
│   ├── client.py             # Supabase singleton
│   └── schema.sql            # Database DDL
│
└── netlify/                  # Alternative serverless deployment
    └── functions/api.ts
```

---

## API Endpoints

| Endpoint | Method | Rate Limit | Purpose |
|----------|--------|------------|---------|
| `/chat` | POST | 30/min | Triage intake conversation |
| `/schedule` | POST | 10/min | Direct appointment booking |
| `/` | GET | - | Root health check |
| `/ping` | GET | - | Lightweight ping |
| `/health` | GET | - | Detailed diagnostics |

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

## Security Notes

1. **Contact Hashing:** Patient emails HMAC-SHA256 hashed
2. **Service Role:** Supabase uses admin key, not exposed to clients
3. **Rate Limiting:** 30/min on /chat, 10/min on /schedule
4. **CORS:** Whitelist of allowed origins
5. **No Diagnosis:** AI explicitly forbidden from medical advice

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
- Calls `/chat` and `/schedule` endpoints
- Handles physician onboarding separately
