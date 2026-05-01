-- Migration: 001_create_physicians_table
-- Description: Creates the physicians table for storing physician profiles,
--              credentials, licenses, and availability in the Medikah platform.
-- Date: 2026-02-06

-- Create verification status enum
CREATE TYPE physician_verification_status AS ENUM (
    'pending',
    'in_review',
    'verified',
    'rejected'
);

-- Create physicians table
CREATE TABLE IF NOT EXISTS physicians (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_user_id    UUID UNIQUE REFERENCES auth.users(id) ON DELETE SET NULL,

    -- Identity
    full_name       TEXT NOT NULL,
    slug            TEXT NOT NULL,
    photo_url       TEXT,
    bio             TEXT,
    email           TEXT NOT NULL,
    phone           TEXT,

    -- Professional info
    specialty       TEXT,
    sub_specialties TEXT[] DEFAULT '{}',
    languages       TEXT[] DEFAULT '{}',
    timezone        TEXT,

    -- Complex structured data stored as JSONB
    -- Each entry: { name, issuing_body, year, expiry }
    board_certifications JSONB DEFAULT '[]'::jsonb,

    -- Each entry: { jurisdiction, country, license_type, license_number, expiry_date, verified }
    medical_licenses     JSONB DEFAULT '[]'::jsonb,

    -- Each entry: { institution, degree, field, graduation_year }
    education            JSONB DEFAULT '[]'::jsonb,

    -- Each entry: { title, journal, year, url, doi }
    publications         JSONB DEFAULT '[]'::jsonb,

    -- Schedule by day: { monday: [{ start, end }], tuesday: [...], ... }
    availability         JSONB DEFAULT '{}'::jsonb,

    -- Each entry: { name, role, location }
    practice_affiliations JSONB DEFAULT '[]'::jsonb,

    -- Verification
    verification_status  physician_verification_status NOT NULL DEFAULT 'pending',

    -- External links
    linkedin_url    TEXT,

    -- Timestamps
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE UNIQUE INDEX idx_physicians_email ON physicians (email);
CREATE UNIQUE INDEX idx_physicians_slug ON physicians (slug);
CREATE INDEX idx_physicians_verification_status ON physicians (verification_status);
CREATE INDEX idx_physicians_specialty ON physicians (specialty);
CREATE INDEX idx_physicians_auth_user_id ON physicians (auth_user_id);

-- Auto-update updated_at on row modification
CREATE OR REPLACE FUNCTION update_physicians_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_physicians_updated_at
    BEFORE UPDATE ON physicians
    FOR EACH ROW
    EXECUTE FUNCTION update_physicians_updated_at();

-- Enable Row Level Security
ALTER TABLE physicians ENABLE ROW LEVEL SECURITY;

-- RLS Policies

-- Service role has full access (backend API uses service_role key)
CREATE POLICY "Service role full access on physicians"
    ON physicians
    FOR ALL
    USING (true)
    WITH CHECK (true);

-- Physicians can read their own row via auth
CREATE POLICY "Physicians can view own profile"
    ON physicians
    FOR SELECT
    USING (auth.uid() = auth_user_id);

-- Physicians can update their own row via auth
CREATE POLICY "Physicians can update own profile"
    ON physicians
    FOR UPDATE
    USING (auth.uid() = auth_user_id)
    WITH CHECK (auth.uid() = auth_user_id);

-- Public can read verified physician profiles (for /dr/[slug] pages)
CREATE POLICY "Public can view verified physicians"
    ON physicians
    FOR SELECT
    USING (verification_status = 'verified');
