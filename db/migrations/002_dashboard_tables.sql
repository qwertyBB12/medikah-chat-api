-- Migration: 002_dashboard_tables
-- Description: Creates tables for patient inquiries and physician availability
--              to support the physician dashboard features.
-- Date: 2026-02-06

-- Patient inquiries table
CREATE TABLE IF NOT EXISTS patient_inquiries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    physician_id    UUID NOT NULL REFERENCES physicians(id) ON DELETE CASCADE,
    patient_name    TEXT NOT NULL,
    patient_email   TEXT,
    symptoms        TEXT,
    preferred_time  TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'accepted', 'declined')),
    decline_reason  TEXT,
    locale          TEXT DEFAULT 'en',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for patient_inquiries
CREATE INDEX idx_patient_inquiries_physician_id ON patient_inquiries (physician_id);
CREATE INDEX idx_patient_inquiries_status ON patient_inquiries (status);
CREATE INDEX idx_patient_inquiries_created_at ON patient_inquiries (created_at DESC);

-- Auto-update updated_at on patient_inquiries
CREATE OR REPLACE FUNCTION update_patient_inquiries_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_patient_inquiries_updated_at
    BEFORE UPDATE ON patient_inquiries
    FOR EACH ROW
    EXECUTE FUNCTION update_patient_inquiries_updated_at();

-- Physician availability table
CREATE TABLE IF NOT EXISTS physician_availability (
    physician_id    UUID PRIMARY KEY REFERENCES physicians(id) ON DELETE CASCADE,
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    schedule        JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Auto-update updated_at on physician_availability
CREATE OR REPLACE FUNCTION update_physician_availability_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_physician_availability_updated_at
    BEFORE UPDATE ON physician_availability
    FOR EACH ROW
    EXECUTE FUNCTION update_physician_availability_updated_at();

-- Enable Row Level Security
ALTER TABLE patient_inquiries ENABLE ROW LEVEL SECURITY;
ALTER TABLE physician_availability ENABLE ROW LEVEL SECURITY;

-- RLS Policies for patient_inquiries

-- Service role has full access
CREATE POLICY "Service role full access on patient_inquiries"
    ON patient_inquiries
    FOR ALL
    USING (true)
    WITH CHECK (true);

-- Physicians can view their own inquiries
CREATE POLICY "Physicians can view own inquiries"
    ON patient_inquiries
    FOR SELECT
    USING (
        physician_id IN (
            SELECT id FROM physicians WHERE auth_user_id = auth.uid()
        )
    );

-- Physicians can update their own inquiries (accept/decline)
CREATE POLICY "Physicians can update own inquiries"
    ON patient_inquiries
    FOR UPDATE
    USING (
        physician_id IN (
            SELECT id FROM physicians WHERE auth_user_id = auth.uid()
        )
    )
    WITH CHECK (
        physician_id IN (
            SELECT id FROM physicians WHERE auth_user_id = auth.uid()
        )
    );

-- RLS Policies for physician_availability

-- Service role has full access
CREATE POLICY "Service role full access on physician_availability"
    ON physician_availability
    FOR ALL
    USING (true)
    WITH CHECK (true);

-- Physicians can view their own availability
CREATE POLICY "Physicians can view own availability"
    ON physician_availability
    FOR SELECT
    USING (
        physician_id IN (
            SELECT id FROM physicians WHERE auth_user_id = auth.uid()
        )
    );

-- Physicians can manage their own availability
CREATE POLICY "Physicians can manage own availability"
    ON physician_availability
    FOR ALL
    USING (
        physician_id IN (
            SELECT id FROM physicians WHERE auth_user_id = auth.uid()
        )
    )
    WITH CHECK (
        physician_id IN (
            SELECT id FROM physicians WHERE auth_user_id = auth.uid()
        )
    );

-- Public can view verified physician availability (for booking pages)
CREATE POLICY "Public can view verified physician availability"
    ON physician_availability
    FOR SELECT
    USING (
        physician_id IN (
            SELECT id FROM physicians WHERE verification_status = 'verified'
        )
    );
