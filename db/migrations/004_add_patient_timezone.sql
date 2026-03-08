-- Add patient_timezone column to conversation_sessions
-- This column is already used by ConversationStateStore._state_to_row()
-- but was missing from the base schema definition.

ALTER TABLE conversation_sessions
ADD COLUMN IF NOT EXISTS patient_timezone TEXT;
