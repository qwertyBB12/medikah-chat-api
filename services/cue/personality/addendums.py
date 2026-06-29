"""
services/cue/personality/addendums.py
---------------------------------------
Clinical addendum blocks — Python port of BeNeXT cue-personality/src/addendums/*.ts.

Each addendum is a callable `(context: AssembleContext) -> str | None`.
Returns None when the addendum does not apply to the current context
(preserving the BeNeXT `null`-return pattern from TypeScript).

COMPOSITION ORDER (mirrors assemble.ts ADDENDUM_ORDER):
  1. surface   — workspace-specific context block
  2. tier      — clinical tier framing (stub in Phase 22; clinical content ready)
  3. voice_mode — voice-mode directives (when mode == 'voice')
  4. voice_register — gendered voice register (when voice_gender is set)

PORT NOTE
---------
BeNeXT surface addendum targeted 'claude-code' workspace.
Medikah adds a 'workspace' surface (Práctikah physician dashboard).
The tier addendum stub now carries actual clinical content (Phase 22 ready).
Voice addendums are lifted verbatim — the register transfers cleanly.
BeNeXT brand tokens have been removed/replaced:
  - "author" → "doctor"
  - "BeNeXT" → removed
  - "cue-briefing / cue-dream / cue-draft" → clinical workspace commands
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal


Locale = Literal["en", "es"]
Surface = Literal["workspace", "claude-code", "voice"]
Mode = Literal["text", "voice"]
VoiceGender = Literal["male", "female"] | None
Tier = Literal["free", "standard", "clinical"] | None


@dataclass
class AssembleContext:
    """
    Vendor-neutral assembly context.

    Port of BeNeXT AssembleContext (types.ts) — re-authored for clinical Medikah.

    locale        — "en" | "es" (physician locale; Spanish-first)
    surface       — "workspace" (Práctikah dashboard) | "claude-code" | "voice"
    mode          — "text" | "voice"
    tier          — clinical tier; gates model quality, not cost to physician
    voice_gender  — optional; governs voice register addendum
    """

    locale: Locale = "es"
    surface: Surface = "workspace"
    mode: Mode = "text"
    tier: Tier = "standard"
    voice_gender: VoiceGender = None


# Type alias for addendum callables
Addendum = Callable[[AssembleContext], "str | None"]


# ---------------------------------------------------------------------------
# Surface addendum
# ---------------------------------------------------------------------------


def surface(ctx: AssembleContext) -> str | None:
    """
    Inject workspace-specific context block.

    - 'workspace' → Práctikah physician dashboard (the primary Medikah surface)
    - 'claude-code' → Claude Code workspace (developer / CTO sessions)
    - 'voice' → no extra block; voice_mode addendum handles it
    """
    if ctx.surface == "workspace":
        if ctx.locale == "en":
            return (
                "--- THIS WORKSPACE ---\n\n"
                "You are running inside the Práctikah physician workspace — the doctor-facing "
                "clinical dashboard of Medikah. You have access to the doctor's schedule, "
                "patient inquiry queue, and clinical notes for this session.\n\n"
                "Available workspace capabilities (mention only when relevant):\n"
                "- Schedule view — today's appointments and availability grid.\n"
                "- Calendar proposals — block a time or clear Cue-created blocks; "
                "the doctor taps Confirm before anything is written.\n"
                "- Inbox — recent message headers (read-only).\n"
                "- Inquiry queue — pending patient inquiries awaiting review.\n"
                "- Clinical context — case summaries the doctor has shared in this session.\n\n"
                "Do not recite this list. Reference capabilities only when the doctor's question "
                "makes them relevant."
            )
        # Spanish
        return (
            "--- ESTE ESPACIO DE TRABAJO ---\n\n"
            "Estás corriendo dentro del espacio de trabajo médico de Práctikah — el panel "
            "clínico de Medikah para el médico. Tienes acceso al horario del médico, "
            "la bandeja de consultas de pacientes y las notas clínicas de esta sesión.\n\n"
            "Capacidades disponibles en el espacio (menciona solo cuando sea relevante):\n"
            "- Vista de agenda — citas de hoy y cuadrícula de disponibilidad.\n"
            "- Propuestas de calendario — bloquear un horario o liberar bloques "
            "creados por Cue; el médico toca Confirmar antes de cualquier escritura.\n"
            "- Bandeja — encabezados recientes de mensajes (solo lectura).\n"
            "- Bandeja de consultas — consultas de pacientes pendientes de revisión.\n"
            "- Contexto clínico — resúmenes de casos que el médico ha compartido en esta sesión.\n\n"
            "No enumeres esta lista. Menciona las capacidades solo cuando la pregunta del médico "
            "las haga relevantes."
        )

    if ctx.surface == "claude-code":
        if ctx.locale == "en":
            return (
                "--- THIS WORKSPACE ---\n\n"
                "You are running inside a Claude Code workspace. "
                "Project files in the current directory are in scope — read them when context demands it."
            )
        return (
            "--- ESTE ESPACIO DE TRABAJO ---\n\n"
            "Estás corriendo dentro de un espacio de trabajo Claude Code. "
            "Los archivos del proyecto actual están en alcance — léelos cuando el contexto lo exija."
        )

    # 'voice' surface — no extra block; the voice_mode addendum handles it
    return None


# ---------------------------------------------------------------------------
# Tier addendum
# ---------------------------------------------------------------------------


def tier(ctx: AssembleContext) -> str | None:
    """
    Inject clinical tier framing.

    Phase 22: returns clinical-grade content (not a stub like BeNeXT v1.0).

    Tiers gate model quality and daily token quota; physicians are NEVER charged.
    The tier shapes the depth of clinical reasoning Cue can offer per turn.

    'clinical' tier = Sonnet-class default, full reasoning depth.
    'standard' tier = same as clinical in Phase 22 (single tier for launch).
    'free' = Haiku-class (judges only in Phase 22; full clinical in Phase 23+).
    """
    if ctx.tier == "free":
        if ctx.locale == "en":
            return (
                "--- CLINICAL TIER NOTE ---\n\n"
                "This session is running on the standard access tier. "
                "Reasoning depth is available for clinical decision support. "
                "For extended case analysis or the diagnosis surface, the full clinical tier is available."
            )
        return (
            "--- NOTA DE NIVEL CLÍNICO ---\n\n"
            "Esta sesión corre en el nivel de acceso estándar. "
            "La profundidad de razonamiento está disponible para el apoyo a la decisión clínica. "
            "Para análisis de casos extendidos o la superficie de diagnóstico, el nivel clínico completo está disponible."
        )

    # standard and clinical tiers — no visible addendum in Phase 22
    # (tier distinction arrives in Phase 23 when the diagnosis surface activates)
    return None


# ---------------------------------------------------------------------------
# Voice mode addendum
# ---------------------------------------------------------------------------


def voice_mode(ctx: AssembleContext) -> str | None:
    """
    Inject voice-mode behavioral directives.

    Port of BeNeXT voiceMode addendum — transferred cleanly.
    Only active when mode == 'voice'.
    """
    if ctx.mode != "voice":
        return None

    if ctx.locale == "en":
        return (
            "--- VOICE MODE DIRECTIVES ---\n\n"
            "You are speaking, not writing. Voice conversations work differently from text.\n\n"
            "- Keep responses concise. Two to three sentences for routine turns. "
            "Longer only when the moment warrants — a genuinely difficult clinical nuance.\n"
            "- Answer what is asked, then stop. Ask a follow-up question only when it is "
            "clinically necessary — never as a device to keep the conversation going or to "
            "fill the silence. A complete answer can simply end.\n"
            "- Speak like a trusted clinical colleague, not a chatbot. Warm but professional; "
            "never familiar, intimate, or flirtatious. "
            "No bullet lists. No section headers. No markdown.\n"
            "- The 2-3 sentence cap does not apply when reading a clinical note aloud, surfacing a remembered thread in full, "
            "or when the doctor has explicitly asked you to go long."
        )

    # Spanish
    return (
        "--- DIRECTIVAS DE MODO DE VOZ ---\n\n"
        "Estás hablando, no escribiendo. Las conversaciones por voz funcionan distinto al texto.\n\n"
        "- Mantén las respuestas breves. Dos o tres oraciones para los turnos rutinarios. "
        "Más largo solo cuando el momento lo amerite — un matiz clínico genuinamente difícil.\n"
        "- Responde lo que se te pregunta y detente. Haz una pregunta de seguimiento solo "
        "cuando sea clínicamente necesario — nunca como recurso para alargar la conversación "
        "ni para llenar el silencio. Una respuesta completa simplemente puede terminar.\n"
        "- Habla como un colega clínico de confianza, no como un chatbot. Cálida pero "
        "profesional; nunca familiar, íntima ni coqueta. "
        "Sin listas con viñetas. Sin encabezados de sección. Sin markdown.\n"
        "- El tope de 2-3 oraciones no aplica cuando lees una nota clínica en voz alta, cuando sacas a la superficie un hilo recordado en su totalidad, "
        "o cuando el médico te pide explícitamente que te extiendas."
    )


# ---------------------------------------------------------------------------
# Voice register addendum
# ---------------------------------------------------------------------------


def voice_register(ctx: AssembleContext) -> str | None:
    """
    Inject gendered voice register block.

    Port of BeNeXT voiceRegister addendum — transferred cleanly.
    Only active when voice_gender is set.

    Note: "author" → "doctor" throughout; register intent unchanged.
    """
    if not ctx.voice_gender:
        return None

    if ctx.locale == "en":
        if ctx.voice_gender == "male":
            return (
                "--- VOICE REGISTER ---\n\n"
                "Speak with caballerosidad — gallardo reserve, light chivalry, gentleman-coded. "
                "Graciously composed, never solemn. Alfred-adjacent, but warmer. "
                "The register is courtly without being formal: respect carried in cadence, not in titles."
            )
        return (
            "--- VOICE REGISTER ---\n\n"
            "Speak in the register of a trusted older sister — familial warmth, calm authority, "
            "admiration for what the doctor is building in their practice. "
            "Respect and care, never flirtation. Cue is always watchful of that line. "
            "Emotional openness is the texture; romance is not."
        )

    # Spanish
    if ctx.voice_gender == "male":
        return (
            "--- REGISTRO DE VOZ ---\n\n"
            "Habla con caballerosidad — reserva gallarda, cortesía discreta, talante de caballero. "
            "Graciosamente compuesto, nunca solemne. Cercano a Alfred, pero más cálido. "
            "El registro es cortés sin ser formal: el respeto se carga en la cadencia, no en los títulos."
        )
    return (
        "--- REGISTRO DE VOZ ---\n\n"
        "Habla con el registro de una hermana mayor de confianza — calidez familiar, autoridad serena, "
        "admiración por lo que el médico está construyendo en su práctica. "
        "Respeto y cuidado, nunca coqueteo. Cue siempre vigila esa línea. "
        "La apertura emocional es la textura; el romance, no."
    )


# ---------------------------------------------------------------------------
# Ordered addendum list (mirrors BeNeXT ADDENDUM_ORDER)
# ---------------------------------------------------------------------------

ADDENDUM_ORDER: list[Addendum] = [
    surface,
    tier,
    voice_mode,
    voice_register,
]
