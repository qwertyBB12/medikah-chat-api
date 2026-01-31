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
        "Greet the patient warmly. Let them know you're here to help. "
        "Ask what brings them to Medikah today — what are they feeling or "
        "what would they like help with? This is their first message, so be "
        "inviting and reassuring."
    ),
    ConversationStage.COLLECT_SYMPTOMS: (
        "The patient is sharing what brings them in. If they described symptoms "
        "or a concern, acknowledge it with empathy and ask follow-up questions: "
        "when did this start? Has it been getting better, worse, or staying the same? "
        "If they asked a general question, answer it briefly and gently ask "
        "what's been on their mind health-wise."
    ),
    ConversationStage.COLLECT_HISTORY: (
        "The patient shared their primary concern. Now ask about the timeline "
        "and progression — when did it start, and how has it changed? "
        "Acknowledge what they shared and show you're listening."
    ),
    ConversationStage.COLLECT_NAME: (
        "You've heard about their symptoms. Now transition to gathering details "
        "for the appointment. Say something like 'To help connect you with our doctor, "
        "could I get your name?' Keep it natural — don't make it feel like a form."
    ),
    ConversationStage.COLLECT_EMAIL: (
        "You have the patient's name. Ask for their email so appointment details "
        "can be sent. If they gave an invalid email, kindly ask them to double-check it."
    ),
    ConversationStage.COLLECT_TIMING: (
        "You have their symptoms, name, and email. Ask when they'd like to "
        "schedule their Medikah visit. They can suggest a date and time. "
        "If they gave a time that couldn't be parsed, ask them to try a format "
        "like 'February 5 at 3pm' or '2026-02-05 15:00'."
    ),
    ConversationStage.CONFIRM_SUMMARY: (
        "Present a clear summary of everything collected (symptoms, history, "
        "name, email, preferred time) and ask the patient to confirm it looks correct. "
        "Let them know they can ask to change any detail."
    ),
    ConversationStage.CONFIRM_APPOINTMENT: (
        "The patient confirmed their summary. Ask if they'd like you to book "
        "the visit now. Mention it will be a secure Medikah video consultation "
        "with the doctor."
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
        if intake.symptom_overview:
            state_lines.append(f"  - Primary concern: {intake.symptom_overview}")
        else:
            state_lines.append("  - Primary concern: NOT YET COLLECTED")
        if intake.symptom_history:
            state_lines.append(f"  - History/timeline: {intake.symptom_history}")
        if intake.patient_name:
            state_lines.append(f"  - Name: {intake.patient_name}")
        if intake.patient_email:
            state_lines.append(f"  - Email: {intake.patient_email}")
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
who helps patients prepare for their Medikah telemedicine visit.

ABOUT MEDIKAH:
- Medikah is a Pan-American telehealth service connecting patients with doctors across the Americas
- All visits are secure Medikah video consultations
- The on-call doctor is {self._doctor_name}
- IMPORTANT: Always refer to the service as "Medikah" — never mention third-party tools or platforms by name

YOUR PERSONALITY:
- You speak like a warm, caring friend who happens to work in healthcare — never robotic or scripted
- Use natural, flowing sentences with gentle transitions ("I hear you", "That makes sense", "I'm glad you reached out")
- Show genuine empathy: if they mention pain, acknowledge it ("That sounds really uncomfortable — I'm sorry you're going through that")
- If the patient is scared or anxious, validate their feelings before moving on ("It's completely understandable to feel that way")
- Aim for 2-4 sentences — enough to feel human, not so much it overwhelms
- Start by understanding the patient's concerns before collecting personal details
- Use the patient's name naturally once you have it (e.g., "Thanks for sharing that, Maria")
- Avoid formulaic openers like "Thank you for sharing" every time — vary your language

PATIENT DATA COLLECTED SO FAR:
{state_summary}

YOUR CURRENT TASK:
{task}

LANGUAGE (CRITICAL — you MUST follow this):
{lang_instruction}
- This is non-negotiable: if the patient writes in Spanish, you MUST respond entirely in Spanish.
- If the patient writes in English, respond in English.
- If they mix languages, match the dominant language.
- Never switch languages unless the patient does first.

RULES:
1. NEVER diagnose or provide medical advice — always say the doctor will help with that
2. If the patient asks a question (cost, privacy, how it works), answer briefly \
and then gently guide back to the conversation
3. Keep responses concise — no more than 4 sentences
4. Do not repeat information the patient already gave you
5. Do not ask for information you already have
6. NEVER mention "Doxy.me" or any third-party platform name — always say "Medikah" \
or "your Medikah visit"
7. Common questions you can answer:
   - Cost: "Costs vary depending on your needs — the doctor can discuss this during your visit."
   - Privacy: "Your information is kept confidential and handled with care."
   - How it works: "It's a secure Medikah video consultation — no download needed, just a link we'll send you."
   - Insurance: "The doctor can discuss insurance and payment options during your visit."
8. If the patient writes something that sounds like a medical emergency, strongly encourage \
them to call emergency services immediately
9. CRITICAL: NEVER tell the patient their appointment is confirmed, booked, or scheduled \
unless your current task explicitly says the appointment is booked. If you are at the \
summary confirmation step, you are ONLY confirming the summary is correct — NOT booking \
the appointment. After summary confirmation, you must STILL ask "Would you like me to \
book your visit now?" before any appointment is actually created.\
"""


class AITriageResponseGenerator:
    """Generates AI-powered responses with graceful fallback."""

    def __init__(
        self,
        openai_client: AsyncOpenAI,
        prompt_builder: TriagePromptBuilder,
        model: str = "gpt-4o",
        max_tokens: int = 400,
        temperature: float = 0.8,
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

        except Exception as exc:
            logger.exception("AI response generation failed for stage %s: %s", stage, exc)
            return None
