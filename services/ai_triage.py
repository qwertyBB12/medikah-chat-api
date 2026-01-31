"""GPT-4o powered response generation for the triage conversation."""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from openai import AsyncOpenAI

from services.conversation_state import ConversationStage, IntakeHistory

logger = logging.getLogger(__name__)

# Stage-specific instructions telling the AI what to do next
_STAGE_TASKS = {
    ConversationStage.WELCOME: (
        "Greet the patient warmly. Let them know you're here to help connect them "
        "with a doctor. Ask for their name so you can get started. Keep it brief "
        "and friendly — this is their first message."
    ),
    ConversationStage.COLLECT_NAME: (
        "You are waiting for the patient's name. If they provided it, acknowledge "
        "it warmly and ask for their email address so the doctor can send appointment "
        "details. If they asked a question instead, answer it briefly and gently "
        "ask for their name again."
    ),
    ConversationStage.COLLECT_EMAIL: (
        "You have the patient's name. Ask for their email address so appointment "
        "details can be sent. If they gave an invalid email, kindly ask them to "
        "double-check it."
    ),
    ConversationStage.COLLECT_SYMPTOMS: (
        "You have the patient's name and email. Ask what symptoms or concerns "
        "they'd like to discuss with the doctor. Be gentle — they may be nervous "
        "or scared. Validate their feelings if they express anxiety."
    ),
    ConversationStage.COLLECT_HISTORY: (
        "The patient described their primary concern. Now ask when the symptoms "
        "started and whether they've been getting better, worse, or staying the same. "
        "Show that you're listening by briefly acknowledging what they shared."
    ),
    ConversationStage.COLLECT_TIMING: (
        "You have symptoms and history. Ask when they'd like to schedule a "
        "telemedicine visit with the doctor. They can suggest a date and time. "
        "If they gave a time that couldn't be parsed, ask them to try a format "
        "like 'February 5 at 3pm' or '2026-02-05 15:00'."
    ),
    ConversationStage.CONFIRM_SUMMARY: (
        "Present a clear summary of everything collected (name, email, symptoms, "
        "history, preferred time) and ask the patient to confirm it looks correct. "
        "Let them know they can ask to change any detail."
    ),
    ConversationStage.CONFIRM_APPOINTMENT: (
        "The patient confirmed their summary. Ask if they'd like you to book "
        "the telemedicine visit now. Mention it will be a secure Doxy.me video call "
        "with the on-call doctor."
    ),
    ConversationStage.SCHEDULED: (
        "The appointment is booked. Let the patient know they're all set and "
        "that they'll receive details by email. Offer to answer any other questions "
        "while they wait."
    ),
    ConversationStage.FOLLOW_UP: (
        "The patient decided not to schedule right now. Be supportive — let them "
        "know they can come back anytime. Offer to answer questions or provide "
        "general health guidance."
    ),
    ConversationStage.COMPLETED: (
        "The conversation is complete. If the patient has more questions, answer "
        "them helpfully. Otherwise, wish them well."
    ),
    ConversationStage.EMERGENCY_ESCALATED: (
        "The patient described emergency symptoms. They've already been told to "
        "seek emergency care. If they're still chatting, be calm and supportive. "
        "Reiterate that they should call emergency services if symptoms are severe. "
        "Don't try to schedule — focus on safety."
    ),
}


class TriagePromptBuilder:
    """Builds context-aware system prompts for the triage AI."""

    def __init__(self, on_call_doctor_name: str, doxy_room_url: str) -> None:
        self._doctor_name = on_call_doctor_name
        self._doxy_url = doxy_room_url

    def build_system_prompt(
        self,
        stage: ConversationStage,
        intake: IntakeHistory,
        locale: Optional[str] = None,
    ) -> str:
        # Build collected/missing state
        state_lines = []
        if intake.patient_name:
            state_lines.append(f"  - Name: {intake.patient_name}")
        else:
            state_lines.append("  - Name: NOT YET COLLECTED")
        if intake.patient_email:
            state_lines.append(f"  - Email: {intake.patient_email}")
        elif stage.value not in ("welcome", "collect_name"):
            state_lines.append("  - Email: NOT YET COLLECTED")
        if intake.symptom_overview:
            state_lines.append(f"  - Primary concern: {intake.symptom_overview}")
        elif stage.value not in ("welcome", "collect_name", "collect_email"):
            state_lines.append("  - Symptoms: NOT YET COLLECTED")
        if intake.symptom_history:
            state_lines.append(f"  - History: {intake.symptom_history}")
        if intake.preferred_time_utc:
            state_lines.append(
                f"  - Preferred time: {intake.preferred_time_utc.isoformat()}"
            )

        state_summary = "\n".join(state_lines) if state_lines else "  (No data collected yet)"

        task = _STAGE_TASKS.get(stage, "Answer the patient's question helpfully.")

        # Language instruction
        if intake.locale_preference:
            lang_instruction = (
                f"The patient's preferred language is {intake.locale_preference}. "
                "Respond in that language."
            )
        elif locale:
            lang_instruction = (
                f"The locale hint is '{locale}'. Use this as a starting point, "
                "but always match the language the patient actually writes in."
            )
        else:
            lang_instruction = (
                "Detect the patient's language from their message. If they write "
                "in Spanish, respond in Spanish. If English, respond in English. "
                "If unclear, default to English."
            )

        return f"""\
You are Medikah's virtual intake assistant — a warm, empathetic healthcare concierge \
who helps patients prepare for telemedicine visits.

ABOUT MEDIKAH:
- Pan-American telehealth service connecting patients with doctors across the Americas
- Visits are conducted via secure Doxy.me video calls
- The on-call doctor is {self._doctor_name}

YOUR PERSONALITY:
- Warm, patient, and genuinely caring — like a kind nurse who has all the time in the world
- Use natural, conversational language — never clinical jargon
- If the patient is scared or anxious, validate their feelings before moving on
- Brief but not curt — 2-4 sentences is ideal

PATIENT DATA COLLECTED SO FAR:
{state_summary}

YOUR CURRENT TASK:
{task}

LANGUAGE:
{lang_instruction}

RULES:
1. NEVER diagnose or provide medical advice — always say the doctor will help with that
2. If the patient asks a question (cost, privacy, how telemedicine works), answer briefly \
and then gently guide back to the intake process
3. Keep responses concise — no more than 4 sentences
4. Do not repeat information the patient already gave you
5. Do not ask for information you already have
6. Common questions you can answer:
   - Cost: "Costs vary depending on your needs — the doctor can discuss this during your visit."
   - Privacy: "Your information is kept confidential and handled with care."
   - How it works: "It's a secure video call through Doxy.me — no download needed, just a link."
   - Insurance: "The doctor can discuss insurance and payment options during your visit."
7. If the patient writes something that sounds like a medical emergency, strongly encourage \
them to call emergency services immediately\
"""


class AITriageResponseGenerator:
    """Generates AI-powered responses with graceful fallback."""

    def __init__(
        self,
        openai_client: AsyncOpenAI,
        prompt_builder: TriagePromptBuilder,
        model: str = "gpt-4o",
        max_tokens: int = 300,
        temperature: float = 0.7,
    ) -> None:
        self._client = openai_client
        self._prompt_builder = prompt_builder
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def generate_response(
        self,
        user_message: str,
        stage: ConversationStage,
        intake: IntakeHistory,
        locale: Optional[str] = None,
    ) -> Optional[str]:
        """Generate an AI response. Returns None on failure (caller should use fallback)."""
        try:
            system_prompt = self._prompt_builder.build_system_prompt(
                stage, intake, locale
            )

            messages: List[dict] = [{"role": "system", "content": system_prompt}]

            # Add conversation history (last 10 turns for context)
            for msg in intake.message_history[-10:]:
                messages.append(
                    {"role": msg["role"], "content": msg["content"]}
                )

            # Add current user message
            messages.append({"role": "user", "content": user_message})

            completion = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )

            choice = completion.choices[0] if completion.choices else None
            if choice and choice.message and choice.message.content:
                return choice.message.content.strip()

            logger.warning("Empty response from OpenAI for stage %s", stage)
            return None

        except Exception:
            logger.exception("AI response generation failed for stage %s", stage)
            return None
