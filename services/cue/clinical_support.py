"""
services/cue/clinical_support.py
---------------------------------
Shared clinical DECISION-SUPPORT engine (Phase 24 — Cue clinical support surface).

NAMING / LEGAL (Hector, 2026-06-29): this is a doctor-support tool. NOTHING here
— the tool name, the card, the labels, or the model's framing — may be named or
inferred as an "(official) diagnosis." It produces a ranked list of clinical
CONSIDERATIONS for a licensed physician to weigh; the only place the word
"diagnosis" appears is the disclaimer's explicit DENIAL ("not a diagnosis").

SINGLE SOURCE of the structured generation, used by BOTH:
  - services/cue/tools/executors.clinical_decision_support  (the Cue conversational tool)
  - routes/ai_routes.py                                     (the legacy HTTP endpoint, on adoption)

Runs on the Opus reasoning tier via the provider-neutral `anthropic_complete()`
wrapper (CUE-09) — switching providers stays a wrapper swap, and no provider-SDK
types appear here (the D1 leak guard scans this file).

STATELESS / NO-PHI — callers MUST pass a DE-IDENTIFIED clinical presentation. The
input-side anonymization notice (frontend) trains physicians never to include
identifiers; this module never logs the presentation and stores nothing.

LEGAL COPY STATUS — `CLINICAL_SUPPORT_DISCLAIMER` is DRAFT, written conservatively
to be safe to ship and clean for counsel (Luis Ignacio) to review/localize (US vs.
MX). Framing is jurisdiction-neutral and LFPDPPP-aware.
"""

from __future__ import annotations

import logging
from typing import Optional

from utils.anthropic_client import anthropic_complete

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical on-card legal disclaimer (DRAFT — pending counsel review)
# ---------------------------------------------------------------------------

CLINICAL_SUPPORT_DISCLAIMER = (
    "Clinical decision support only — not a diagnosis. This AI-generated decision "
    "support assists a licensed physician's reasoning. It does not establish a "
    "diagnosis, does not constitute medical advice, and does not replace clinical "
    "examination, your independent professional judgment, or the standard of care. "
    "Verify all findings independently; responsibility for every clinical decision "
    "remains solely with the treating physician."
    "\n\n"
    "Solo apoyo a la decisión clínica — no es un diagnóstico. Este apoyo a la "
    "decisión clínica generado por IA asiste el razonamiento de un médico con "
    "licencia. No establece un diagnóstico, no constituye consejo médico y no "
    "sustituye la exploración clínica, su juicio profesional independiente ni el "
    "estándar de atención. Verifique todos los hallazgos de forma independiente; la "
    "responsabilidad de cada decisión clínica recae exclusivamente en el médico "
    "tratante."
)


# ---------------------------------------------------------------------------
# Model routing + prompt
# ---------------------------------------------------------------------------

CLINICAL_SUPPORT_TIER = "opus"        # highest-stakes clinical tier (tier→model lives in the adapter)
CLINICAL_SUPPORT_MAX_TOKENS = 1200

CLINICAL_SUPPORT_SYSTEM_PROMPT = """\
You are a clinical decision support tool for licensed physicians on the Medikah \
telehealth platform. You assist physicians by generating a ranked list of clinical \
CONSIDERATIONS (possible conditions to weigh) based on reported symptoms.

IMPORTANT RULES:
1. You are assisting a LICENSED PHYSICIAN, not a patient. Use professional \
clinical language appropriate for a medical professional.
2. Provide a ranked list of clinical considerations — possible conditions to \
weigh, most likely to least likely. These are considerations to SUPPORT the \
physician's reasoning, NOT findings, conclusions, or a diagnosis.
3. For each consideration, include:
   - The condition name
   - A brief clinical rationale (1-2 sentences)
   - A confidence indicator: HIGH, MODERATE, or LOW
   - Key distinguishing features or tests that would confirm/rule out
4. Always include a "Red Flags" section at the end highlighting any symptoms \
that warrant urgent evaluation or immediate action.
5. This is CLINICAL DECISION SUPPORT only. Never state or imply that this is the \
patient's diagnosis. Remind the physician that clinical correlation and \
examination are required and that the decision is theirs.
6. If the symptoms are vague or insufficient, say so and suggest what \
additional history or examination findings would help narrow the considerations.
7. Limit your response to 5-8 considerations maximum.
8. Do NOT store or reference any patient-identifying information. \
Work only with the clinical presentation provided.
9. Respond in the same language as the input (English or Spanish).
"""

_FORMAT_SUFFIX = (
    "\n\nProvide a ranked list of clinical considerations with confidence levels "
    "and red flags. Format each consideration as:\n"
    "1. **Condition** (CONFIDENCE)\n"
    "   Rationale: ...\n"
    "   Distinguishing factors: ...\n\n"
    "End with a Red Flags section."
)


class ClinicalSupportUnavailable(RuntimeError):
    """Raised when the support provider is unconfigured or returns an empty
    response. Callers map this to a 503 (HTTP) or an is_error tool_result (Cue)."""


# ---------------------------------------------------------------------------
# generate_clinical_support — the shared generation entrypoint
# ---------------------------------------------------------------------------


async def generate_clinical_support(
    presentation: str,
    age_range: Optional[str] = None,
    sex: Optional[str] = None,
) -> dict:
    """Generate ranked clinical considerations for a DE-IDENTIFIED presentation.

    Returns a JSON-serializable dict:
        {
          "considerations": [{condition, rationale, confidence, distinguishing_factors}, ...],
          "red_flags":      ["...", ...],
          "disclaimer":     CLINICAL_SUPPORT_DISCLAIMER,
          "summary":        <readable LLM prose>,   # fed to the model for its walkthrough
        }

    Raises ClinicalSupportUnavailable when the provider is unconfigured / empty.
    Stateless: the presentation is never logged or stored.
    """
    user_parts = [f"Clinical presentation: {presentation}"]
    if age_range:
        user_parts.append(f"Age range: {age_range}")
    if sex:
        user_parts.append(f"Biological sex: {sex}")
    user_prompt = "\n".join(user_parts) + _FORMAT_SUFFIX

    raw_text = await anthropic_complete(
        system_prompt=CLINICAL_SUPPORT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        tier=CLINICAL_SUPPORT_TIER,
        max_tokens=CLINICAL_SUPPORT_MAX_TOKENS,
    )
    if raw_text is None:
        raise ClinicalSupportUnavailable(
            "clinical support provider unconfigured or returned an empty response"
        )

    considerations, red_flags = parse_support_response(raw_text)
    return {
        "considerations": considerations,
        "red_flags": red_flags,
        "disclaimer": CLINICAL_SUPPORT_DISCLAIMER,
        "summary": raw_text,
    }


# ---------------------------------------------------------------------------
# parse_support_response — best-effort structuring of the model prose
# ---------------------------------------------------------------------------


def parse_support_response(raw_text: str) -> tuple[list[dict], list[str]]:
    """Parse the model prose into structured consideration dicts + red-flag strings.

    Best-effort: if the format isn't exactly as requested we still return useful
    data. Each consideration is a plain dict {condition, rationale, confidence,
    distinguishing_factors} so the result is directly JSON-serializable for the
    Cue card payload.
    """
    considerations: list[dict] = []
    red_flags: list[str] = []
    in_red_flags = False

    current_condition = ""
    current_confidence = "MODERATE"
    current_rationale = ""
    current_distinguishing = ""

    def _flush() -> None:
        nonlocal current_condition, current_rationale, current_confidence, current_distinguishing
        if current_condition:
            considerations.append(
                {
                    "condition": current_condition,
                    "rationale": current_rationale or "See clinical details above.",
                    "confidence": current_confidence,
                    "distinguishing_factors": current_distinguishing or "Clinical correlation required.",
                }
            )
        current_condition = ""
        current_rationale = ""
        current_confidence = "MODERATE"
        current_distinguishing = ""

    for line in raw_text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        lower = stripped.lower()
        if "red flag" in lower and (":" in stripped or "##" in stripped or "**" in stripped):
            _flush()
            in_red_flags = True
            continue

        if in_red_flags:
            clean = stripped.lstrip("-*0123456789.) ").strip()
            if clean:
                red_flags.append(clean)
            continue

        # Numbered consideration entry, e.g. "1. **Condition** (HIGH)"
        if len(stripped) > 2 and stripped[0].isdigit() and ("." in stripped[:4] or ")" in stripped[:4]):
            _flush()
            entry = (
                stripped.split(".", 1)[-1].strip()
                if "." in stripped[:4]
                else stripped.split(")", 1)[-1].strip()
            )
            for conf in ("HIGH", "MODERATE", "LOW"):
                if conf in entry.upper():
                    current_confidence = conf
                    break
            clean_entry = entry.replace("**", "").strip()
            for conf in ("(HIGH)", "(MODERATE)", "(LOW)", "(high)", "(moderate)", "(low)"):
                clean_entry = clean_entry.replace(conf, "").strip()
            current_condition = clean_entry.rstrip("-:").strip()
            continue

        lower_stripped = stripped.lower()
        if lower_stripped.startswith("rationale:") or lower_stripped.startswith("- rationale:"):
            current_rationale = stripped.split(":", 1)[-1].strip()
        elif lower_stripped.startswith("distinguishing") or lower_stripped.startswith("- distinguishing"):
            current_distinguishing = stripped.split(":", 1)[-1].strip()
        elif current_condition:
            if not current_rationale:
                current_rationale = stripped.lstrip("-* ").strip()
            elif not current_distinguishing:
                current_distinguishing = stripped.lstrip("-* ").strip()

    _flush()
    return considerations, red_flags
