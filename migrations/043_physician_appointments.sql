-- =====================================================
-- Appointments vertical — Migration 043: physician_appointments
-- =====================================================
--
-- ⚠️  THIS FILE IS NOT IN THE CANONICAL MIGRATIONS DIRECTORY AND HAS NOT BEEN
--     APPLIED ANYWHERE.
--
--     The canonical migrations directory is:
--         medikah-chat-frontend/supabase/migrations/
--
--     Before this can be applied, a human must copy this file there:
--         cp migrations/043_physician_appointments.sql \
--            ../medikah-chat-frontend/supabase/migrations/043_physician_appointments.sql
--
--     and then apply it through the normal Supabase migration path. Nothing in
--     this repo applies it, and the backend code that reads/writes
--     physician_appointments will fail closed (bilingual "could not read" line
--     on the read tool, HTTP 500 on confirm-write) until it is applied.
--
-- NUMBERING NOTE (deviation from the original request, which said 038):
--     038 is already taken in the canonical directory by
--     038_admin_credential_review.sql, and the highest applied number there is
--     042_isabel_agent.sql. This migration therefore takes 043. If another
--     unmerged branch has already claimed 043, renumber this file at copy time
--     — nothing in the SQL depends on the number.
--
-- WHAT THIS ADDS
--     physician_appointments — the appointment object behind Cue's doctor-facing
--     appointment vertical (create / move / cancel). One row per appointment.
--     Every Cue-created row is MIRRORED into the doctor's SOGo calendar as an
--     X-CUE-MANAGED VEVENT; caldav_uid is that mirror's UID.
--
-- PHI DISCIPLINE (the hard rule for this table)
--     patient_name holds FIRST NAME + LAST INITIAL ONLY (e.g. 'María G.').
--     The backend minimizes the name before it is ever written — see
--     services/cue/appointments_store.py minimize_patient_name(). A full legal
--     name must never land here, because this column is read back into the
--     model's context by the appointment_list tool.
--
--     patient_contact is the ONE field that may hold a raw identifier (email or
--     phone). It exists ONLY for the LATER patient-notification build; nothing
--     in this build reads it, and it is NEVER placed in model context, in a
--     confirm-card summary, in an audit row, or in the idempotency ledger.
--
-- THIS MIGRATION IS PURELY ADDITIVE: CREATE TABLE / CREATE INDEX / CREATE POLICY
-- with IF NOT EXISTS. No existing table or column is altered or dropped.
--
-- RLS pattern mirrors 031_cue_hands.sql: service_role-only. The FastAPI backend
-- reads and writes with the service-role key and scopes every query to the
-- session-derived physician_id (CUE-11); physicians never touch this table
-- directly from the browser.
-- =====================================================


CREATE TABLE IF NOT EXISTS physician_appointments (
  id                  UUID        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,

  -- FK to the canonical physician record (027 identity spine). Always the
  -- session-derived physician_id at the route layer — never a body value.
  physician_id        UUID        NOT NULL REFERENCES physicians(id) ON DELETE CASCADE,

  -- FIRST NAME + LAST INITIAL ONLY (e.g. 'María G.'). See PHI DISCIPLINE above.
  -- Minimized by the backend before insert; this column is read into model context.
  patient_name        TEXT        NOT NULL,

  -- Email or phone, for the LATER notification build. Nullable, and deliberately
  -- unused by this build. NEVER enters model context or an audit row.
  patient_contact     TEXT        NULL,

  starts_at           TIMESTAMPTZ NOT NULL,
  ends_at             TIMESTAMPTZ NOT NULL,

  -- 'scheduled' — created and not yet rescheduled.
  -- 'moved'     — rescheduled at least once; STILL ACTIVE and still upcoming.
  --               (It is a history marker, not a terminal state — appointment_list
  --               returns both 'scheduled' and 'moved'.)
  -- 'cancelled' — terminal; the CalDAV mirror has been (or should be) deleted.
  status              TEXT        NOT NULL DEFAULT 'scheduled'
                                  CHECK (status IN ('scheduled', 'moved', 'cancelled')),

  -- 'cue'    — Cue created this row through the confirm-write route.
  -- 'manual' — entered outside Cue (dashboard/receptionist; no such path yet).
  -- BLAST-RADIUS GUARD, DB SIDE: Cue may move/cancel ONLY source='cue' rows,
  -- the table-level analogue of the X-CUE-MANAGED guard on the calendar. Cue
  -- mutates only what Cue created.
  source              TEXT        NOT NULL DEFAULT 'cue'
                                  CHECK (source IN ('cue', 'manual')),

  -- UID of the mirrored X-CUE-MANAGED VEVENT in the doctor's SOGo calendar.
  -- NULL when the mirror has not been written yet (or was never written because
  -- CalDAV was unreachable — see needs_sync).
  caldav_uid          TEXT        NULL,

  -- TRUE when the DB row and the CalDAV mirror are known to disagree: the row
  -- was written but the calendar write/delete failed. The route sets this
  -- instead of failing the whole operation, so a CalDAV outage never loses an
  -- appointment the doctor just confirmed. A future reconciliation sweep reads
  -- WHERE needs_sync = true.
  needs_sync          BOOLEAN     NOT NULL DEFAULT false,

  -- Audit trail for a move: where the appointment sat before the last move.
  -- NULL on an appointment that has never been moved.
  previous_starts_at  TIMESTAMPTZ NULL,
  previous_ends_at    TIMESTAMPTZ NULL,

  cancelled_at        TIMESTAMPTZ NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- An appointment that ends before it starts is always a bug upstream.
  CONSTRAINT physician_appointments_time_order CHECK (ends_at > starts_at)
);

COMMENT ON TABLE physician_appointments IS
  'Doctor-facing appointment object behind Cue''s appointment vertical. Every '
  'source=''cue'' row is mirrored into the physician''s SOGo calendar as an '
  'X-CUE-MANAGED VEVENT (caldav_uid). PHI-minimized: patient_name is first name '
  '+ last initial only; patient_contact is reserved for the later notification '
  'build and never enters model context.';

COMMENT ON COLUMN physician_appointments.patient_name IS
  'FIRST NAME + LAST INITIAL ONLY (e.g. ''María G.''). Minimized by the backend '
  'before insert. This column is read back into the model context by the '
  'appointment_list tool — a full legal name must never be stored here.';

COMMENT ON COLUMN physician_appointments.patient_contact IS
  'Email or phone for the LATER patient-notification build. Unused by the '
  'current build. NEVER placed in model context, confirm-card summaries, audit '
  'rows, or the cue_write_idempotency ledger.';

COMMENT ON COLUMN physician_appointments.source IS
  'DB-side blast-radius guard: Cue may move or cancel ONLY source=''cue'' rows, '
  'mirroring the X-CUE-MANAGED guard on the calendar side.';

COMMENT ON COLUMN physician_appointments.needs_sync IS
  'TRUE when the CalDAV mirror could not be written/deleted after the DB row '
  'changed. The confirm-write route sets this rather than failing, so a CalDAV '
  'outage never loses a confirmed appointment.';


-- The hot read: upcoming appointments for one physician, in time order
-- (appointment_list, and the future reminder sweep).
CREATE INDEX IF NOT EXISTS idx_physician_appointments_physician_starts
  ON physician_appointments (physician_id, starts_at);

-- Status filtering within a physician's book (active vs cancelled).
CREATE INDEX IF NOT EXISTS idx_physician_appointments_physician_status
  ON physician_appointments (physician_id, status);

-- Reverse lookup from a calendar event back to its appointment row (used by
-- reconciliation, not by the request path).
CREATE INDEX IF NOT EXISTS idx_physician_appointments_caldav_uid
  ON physician_appointments (caldav_uid)
  WHERE caldav_uid IS NOT NULL;

-- Reconciliation sweep target.
CREATE INDEX IF NOT EXISTS idx_physician_appointments_needs_sync
  ON physician_appointments (needs_sync)
  WHERE needs_sync = true;


ALTER TABLE physician_appointments ENABLE ROW LEVEL SECURITY;

-- Service role only — the FastAPI backend is the sole reader/writer and scopes
-- every query to the session-derived physician_id (CUE-11).
CREATE POLICY "Service role can select physician appointments"
  ON physician_appointments
  FOR SELECT
  USING (auth.role() = 'service_role');

CREATE POLICY "Service role can insert physician appointments"
  ON physician_appointments
  FOR INSERT
  WITH CHECK (auth.role() = 'service_role');

CREATE POLICY "Service role can update physician appointments"
  ON physician_appointments
  FOR UPDATE
  USING (auth.role() = 'service_role')
  WITH CHECK (auth.role() = 'service_role');

-- No DELETE policy on purpose: cancellation is a status transition, never a row
-- delete. An appointment's history has to survive its cancellation.
