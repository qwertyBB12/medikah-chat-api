"""
services/cue/tools/__init__.py
-------------------------------
Cue tool layer — neutral tool registry, session-scoped dispatcher,
and no-op executor stubs (Phase 22 contract; Phase 23 HANDS fills real bodies).

CUE-11 IDOR DISCIPLINE
-----------------------
No tool input_schema declares a physician_id or slug property.
Every executor receives physician_id exclusively from dispatch_tool(),
which sources it from the verified FastAPI session — never from tool_input.

See registry.py for NEUTRAL_TOOLS definitions and dispatch_tool().
See executors.py for the no-op executor stubs.
"""

from services.cue.tools.registry import NEUTRAL_TOOLS, dispatch_tool

__all__ = ["NEUTRAL_TOOLS", "dispatch_tool"]
