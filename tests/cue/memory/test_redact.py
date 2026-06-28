"""
tests/cue/memory/test_redact.py — PATCH-01 free-text redaction.

BeNeXT's strip-pii is key-name-only; the judge writes a free-text sentence,
so we must scrub the body. These assert the load-bearing no-BAA control.
"""
from services.cue.memory.redact import redact_free_text


class TestRedactFreeText:
    def test_email_is_redacted(self):
        out = redact_free_text("Follow up with maria.lopez@gmail.com about scheduling")
        assert "maria.lopez@gmail.com" not in out
        assert "[correo]" in out

    def test_mx_phone_is_redacted(self):
        out = redact_free_text("Patient callback at +52 55 1234 5678 next week")
        assert "1234" not in out
        assert "[teléfono]" in out

    def test_long_digit_run_is_redacted(self):
        out = redact_free_text("Record number 0123456789 was updated")
        assert "0123456789" not in out
        assert "[id]" in out

    def test_honorific_name_is_redacted(self):
        out = redact_free_text("The doctor will see Sr. Juan Pérez on Tuesday")
        assert "Juan Pérez" not in out
        assert "[paciente]" in out

    def test_benign_operational_text_untouched(self):
        text = "The doctor is preparing the CDMX launch and reviewing the schedule"
        assert redact_free_text(text) == text

    def test_empty_and_none_safe(self):
        assert redact_free_text("") == ""
        assert redact_free_text(None) == ""
