-- Add physician_id foreign key to appointments table
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS physician_id UUID REFERENCES physicians(id);

CREATE INDEX IF NOT EXISTS idx_appointments_physician_id ON appointments(physician_id);
