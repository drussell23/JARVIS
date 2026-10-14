"""
screen_question - answer a question that needs a look at the user's screen.

"Can you see my screen?" used to reach the text-only Mind, which can see
nothing, so JARVIS answered without looking. When the IntentClassifier says
the ANSWER needs the screen (``needs_screen``), the command processor routes
the question here instead: one capture through the vision loop's own
platform cascade, one question to the local vision model, asked as
FOREGROUND -- a person is waiting, so J-Prime may swap an idle primary model
out to admit the vision model when the card cannot hold both.

Every failure is reported as what it is (no vision model declared, the
screen could not be captured, the model could not be loaded), never as a
guess about the screen.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _reply(success: bool, text: str, **extra: Any) -> Dict[str, Any]:
    return {"success": success, "response": text, "command_type": "VISION",
            "source": "local_vision", **extra}


def _explain(error: str) -> str:
    """A user-facing reason for a failed vision request."""
    err = error or ""
    if "need ~" in err and "MiB" in err:
        return ("my vision model can't load right now because the graphics card's memory "
                "is in use")
    if err.startswith(("ClientConnectorError", "ServerDisconnectedError", "TimeoutError")):
        return "my vision model isn't reachable right now"
    if err == "reasoning_only":
        return "my vision model returned no answer"
    return f"my vision model failed ({err[:160]})"


async def answer_about_screen(question: str) -> Dict[str, Any]:
    """Look at the screen and answer *question* about it. Never raises."""
    from backend.core.host_platform import os_display_name
    from backend.vision.lean_loop import LeanVisionLoop
    from backend.vision.local_vision_client import get_local_vision_client

    client = get_local_vision_client()
    if not client.enabled:
        return _reply(False, "I don't have a vision model configured, so I can't see your screen.")
    try:
        png_b64 = await LeanVisionLoop.get_instance().capture_screen()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ScreenQuestion] capture failed: %s", exc)
        png_b64 = None
    if not png_b64:
        return _reply(False, "I tried to look, but I couldn't capture your screen.")

    prompt = (
        f"This is a screenshot of the user's {os_display_name()} screen, taken just now. "
        f"Answer their question about it: \"{question}\"\n"
        "Describe only what is actually visible: name the applications, windows and "
        "readable text you can see. Be concise and speak to the user directly."
    )
    answer = await client.describe(
        base64.b64decode(png_b64), prompt,
        max_tokens=int(os.getenv("JARVIS_SCREEN_ANSWER_MAX_TOKENS", "300")),
        priority="foreground",
    )
    if not answer.ok:
        logger.warning("[ScreenQuestion] vision model failed: %s", answer.error)
        return _reply(False, f"I tried to look at your screen, but {_explain(answer.error or '')}.")
    logger.info("[ScreenQuestion] answered in %.0fms via %s", answer.latency_ms, answer.model)
    return _reply(True, answer.text, model=answer.model, latency_ms=answer.latency_ms)


__all__ = ["answer_about_screen"]
