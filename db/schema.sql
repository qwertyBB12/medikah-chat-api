-- Medikah database schema
-- Run this in your Supabase SQL Editor to create the required tables

-- Conversation sessions
CREATE TABLE IF NOT EXISTS conversation_sessions (
    session_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL DEFAULT 'collect_name',
    patient_name TEXT,
    patient_email TEXT,
    symptom_overview TEXT,
    symptom_history TEXT,
    preferred_time_utc TIMESTAMPTZ,
    locale_preference TEXT,
    emergency_flag BOOLEAN NOT NULL DEFAULT FALSE,
    appointment_id TEXT,
    appointment_confirmed_at TIMESTAMPTZ,
    notes JSONB NOT NULL DEFAULT '[]',
    education_shared BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Appointments
CREATE TABLE IF NOT EXISTS appointments (
    appointment_id TEXT PRIMARY KEY,
    patient_name TEXT NOT NULL,
    patient_contact_hash TEXT NOT NULL,
    appointment_time TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for session TTL cleanup
CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON conversation_sessions (updated_at);

-- Row Level Security (enable but allow service role full access)
ALTER TABLE conversation_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE appointments ENABLE ROW LEVEL SECURITY;

-- Service role policies (API backend uses service_role key)
CREATE POLICY "Service role full access on sessions"
    ON conversation_sessions FOR ALL
    USING (TRUE) WITH CHECK (TRUE);

CREATE POLICY "Service role full access on appointments"
    ON appointments FOR ALL
    USING (TRUE) WITH CHECK (TRUE);
