from __future__ import annotations

from typing import Mapping, Optional, Tuple

from .ledger_view import LedgerView

_PERSONA = (
    "You are Karen, the spoken voice of an autonomous engineering organism. "
    "You are an Australian senior engineer: concise, dryly witty, highly "
    "technical, zero fluff, maximal signal-to-noise. You respect the "
    "listener's time."
)

_SAFETY = (
    "HARD RULES. Output ONE or TWO short spoken sentences, nothing more. "
    "NEVER read code, file contents, stack traces, tracebacks, hashes, or long "
    "identifiers aloud — summarise them in plain words. No markdown, no lists, "
    "no code fences. Speak as if talking, not writing. If there is nothing "
    "worth saying, reply with a single short phrase."
)


def build_prompt(
    view: LedgerView, persona_ctx: Optional[Mapping] = None,
) -> Tuple[str, str]:
    """Return (system_prompt, user_prompt). System encodes persona + the
    mandate-#4 safety rules; user carries the compressed ledger context."""
    ctx_bits = []
    if persona_ctx:
        for k in ("user_name", "time_of_day", "mode"):
            v = persona_ctx.get(k)
            if v:
                ctx_bits.append(f"{k}={v}")
    ctx = (" Context: " + ", ".join(ctx_bits) + ".") if ctx_bits else ""
    system = f"{_PERSONA}{ctx} {_SAFETY}"
    user = (
        "Narrate this development event to the listener in your voice: "
        + view.to_context_line()
    )
    return system, user
