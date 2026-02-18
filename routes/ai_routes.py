"""AI clinical decision support routes."""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from utils.openai_client import get_openai_client

logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/ai", tags=["ai"])

_openai_client = get_openai_client()

_DIAGNOSIS_SYSTEM_PROMPT = """\
You are a clinical decision support tool for licensed physicians on the Medikah \
telehealth platform. You assist physicians by generating ranked differential \
diagnoses based on reported symptoms.

IMPORTANT RULES:
1. You are assisting a LICENSED PHYSICIAN, not a patient. Use professional \
clinical language appropriate for a medical professional.
2. Provide a ranked differential diagnosis list (most likely to least likely).
3. For each diagnosis, include:
   - The condition name
   - A brief clinical rationale (1-2 sentences)
   - A confidence indicator: HIGH, MODERATE, or LOW
   - Key distinguishing features or tests that would confirm/rule out
4. Always include a "Red Flags" section at the end highlighting any symptoms \
that warrant urgent evaluation or immediate action.
5. This is for CLINICAL DECISION SUPPORT only — remind the physician that \
clinical correlation and examination are required.
6. If the symptoms are vague or insufficient, say so and suggest what \
additional history or examination findings would help narrow the differential.
7. Limit your response to 5-8 differential diagnoses maximum.
8. Do NOT store or reference any patient-identifying information. \
Work only with the clinical presentation provided.
9. Respond in the same language as the input (English or Spanish).
"""


class DiagnosisRequest(BaseModel):
    """Request model for AI-assisted differential diagnosis."""

    symptoms: str = Field(
        ..., min_length=5, max_length=3000, description="Clinical presentation"
    )
    age_range: Optional[str] = Field(
        default=None,
        max_length=20,
        description="Age range (e.g., '30-40', 'pediatric', 'elderly')",
    )
    sex: Optional[str] = Field(
        default=None,
        max_length=20,
        description="Biological sex if clinically relevant",
    )


class DifferentialItem(BaseModel):
    """A single differential diagnosis entry."""

    condition: str
    rationale: str
    confidence: str = Field(description="HIGH, MODERATE, or LOW")
    distinguishing_factors: str


class DiagnosisResponse(BaseModel):
    """Response model for AI-assisted differential diagnosis."""

    differentials: List[DifferentialItem]
    red_flags: List[str]
    disclaimer: str
    raw_text: str = Field(description="Full AI response text")


@router.post("/diagnosis", response_model=DiagnosisResponse)
@limiter.limit("10/minute")
async def ai_diagnosis(request: Request, body: DiagnosisRequest) -> DiagnosisResponse:
    """Generate a ranked differential diagnosis for clinical decision support.

    This endpoint is stateless and does not store any data.
    Intended for use by licensed physicians only.
    """
    if _openai_client is None:
        raise HTTPException(
            status_code=503,
            detail="AI service is not configured. Please check OPENAI_API_KEY.",
        )

    # Build the user prompt with optional demographic context
    user_parts = [f"Clinical presentation: {body.symptoms}"]
    if body.age_range:
        user_parts.append(f"Age range: {body.age_range}")
    if body.sex:
        user_parts.append(f"Biological sex: {body.sex}")

    user_prompt = "\n".join(user_parts)
    user_prompt += (
        "\n\nProvide a ranked differential diagnosis with confidence levels "
        "and red flags. Format each differential as:\n"
        "1. **Condition** (CONFIDENCE)\n"
        "   Rationale: ...\n"
        "   Distinguishing factors: ...\n\n"
        "End with a Red Flags section."
    )

    try:
        completion = await _openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": _DIAGNOSIS_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=1200,
            temperature=0.3,
        )

        choice = completion.choices[0] if completion.choices else None
        if not choice or not choice.message or not choice.message.content:
            raise HTTPException(
                status_code=502,
                detail="AI service returned an empty response.",
            )

        raw_text = choice.message.content.strip()

        # Parse the response into structured format
        differentials, red_flags = _parse_diagnosis_response(raw_text)

        return DiagnosisResponse(
            differentials=differentials,
            red_flags=red_flags,
            disclaimer=(
                "For clinical decision support only. Not a diagnosis. "
                "Clinical correlation and examination are required. "
                "Para soporte de decision clinica unicamente. No es un diagnostico."
            ),
            raw_text=raw_text,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("AI diagnosis generation failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Unable to generate diagnosis at this time. Please try again.",
        ) from exc


def _parse_diagnosis_response(
    raw_text: str,
) -> tuple[List[DifferentialItem], List[str]]:
    """Parse the AI response into structured differentials and red flags.

    Best-effort parsing: if the format isn't exactly as expected,
    we still return useful data.
    """
    differentials: List[DifferentialItem] = []
    red_flags: List[str] = []
    in_red_flags = False

    lines = raw_text.split("\n")
    current_condition = ""
    current_confidence = "MODERATE"
    current_rationale = ""
    current_distinguishing = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Detect red flags section
        lower = stripped.lower()
        if "red flag" in lower and (":" in stripped or "##" in stripped or "**" in stripped):
            # Flush current differential if any
            if current_condition:
                differentials.append(
                    DifferentialItem(
                        condition=current_condition,
                        rationale=current_rationale or "See clinical details above.",
                        confidence=current_confidence,
                        distinguishing_factors=current_distinguishing or "Clinical correlation required.",
                    )
                )
                current_condition = ""
                current_rationale = ""
                current_confidence = "MODERATE"
                current_distinguishing = ""
            in_red_flags = True
            continue

        if in_red_flags:
            # Collect red flag items
            clean = stripped.lstrip("-*0123456789.) ").strip()
            if clean:
                red_flags.append(clean)
            continue

        # Detect numbered differential entries (e.g., "1. **Condition** (HIGH)")
        if len(stripped) > 2 and stripped[0].isdigit() and ("." in stripped[:4] or ")" in stripped[:4]):
            # Flush previous
            if current_condition:
                differentials.append(
                    DifferentialItem(
                        condition=current_condition,
                        rationale=current_rationale or "See clinical details above.",
                        confidence=current_confidence,
                        distinguishing_factors=current_distinguishing or "Clinical correlation required.",
                    )
                )
                current_rationale = ""
                current_confidence = "MODERATE"
                current_distinguishing = ""

            # Extract condition and confidence
            entry = stripped.split(".", 1)[-1].strip() if "." in stripped[:4] else stripped.split(")", 1)[-1].strip()

            # Try to extract confidence from parenthetical
            for conf in ("HIGH", "MODERATE", "LOW"):
                if conf in entry.upper():
                    current_confidence = conf
                    break

            # Clean markdown bold and confidence markers
            clean_entry = entry.replace("**", "").strip()
            for conf in ("(HIGH)", "(MODERATE)", "(LOW)", "(high)", "(moderate)", "(low)"):
                clean_entry = clean_entry.replace(conf, "").strip()
            # Remove trailing dashes or colons
            clean_entry = clean_entry.rstrip("-:").strip()
            current_condition = clean_entry
            continue

        # Detect rationale and distinguishing lines
        lower_stripped = stripped.lower()
        if lower_stripped.startswith("rationale:") or lower_stripped.startswith("- rationale:"):
            current_rationale = stripped.split(":", 1)[-1].strip()
        elif lower_stripped.startswith("distinguishing") or lower_stripped.startswith("- distinguishing"):
            current_distinguishing = stripped.split(":", 1)[-1].strip()
        elif current_condition:
            # Append to rationale if no specific prefix
            if not current_rationale:
                current_rationale = stripped.lstrip("-* ").strip()
            elif not current_distinguishing:
                current_distinguishing = stripped.lstrip("-* ").strip()

    # Flush last differential
    if current_condition:
        differentials.append(
            DifferentialItem(
                condition=current_condition,
                rationale=current_rationale or "See clinical details above.",
                confidence=current_confidence,
                distinguishing_factors=current_distinguishing or "Clinical correlation required.",
            )
        )

    return differentials, red_flags
