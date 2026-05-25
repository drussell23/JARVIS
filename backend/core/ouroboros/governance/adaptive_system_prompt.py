"""Adaptive Cognitive Feedback — system prompt escalation on retry.

Closes the behavioral gap surfaced by the ultimate Aegis soak
``bt-2026-05-25-004146``: Claude completely ignored the Iron Gate's
retry feedback ("You MUST call read_file/search_code/get_callers
≥2 times BEFORE proposing any patch") on two consecutive retries.
The model treated the exploration mandate as skimmable context
because the rejection message was buried in the middle of a
multi-section USER prompt body while the SYSTEM prompt stayed a
static constant across all retries.

# Cognitive engineering principles applied

Anthropic's documented model behavior:

  1. **System prompts** are cache-promoted + receive higher-priority
     attention treatment than user-prompt-body context.
  2. **<xml_tag> structured blocks** trigger the model's
     structured-parsing pathway — content inside named tags gets
     processed as semantically grouped instructions, not skimmed
     as ambient context.
  3. **Position matters**: instructions placed BEFORE the base
     system prompt get processed FIRST and frame the model's
     interpretation of everything that follows.

This module combines all three: when the orchestrator detects a
retry-after-rejection state, the rejection reason is escalated
from the user-prompt body INTO an XML-tagged envelope PREPENDED
to the base system prompt.

# Public API

  * :func:`compose_system_prompt` — pure function, no I/O, no
    side effects. Returns the base unchanged on first-attempt ops
    (zero behavior change for non-retry paths); prepends the
    envelope on retry ops.

# Single seam discipline

  * Reads ``ctx.strategic_memory_prompt`` (already populated by
    the orchestrator's retry-injection logic at
    ``generate_runner.py:2149-2167``). The function does NOT
    introduce new ctx state.
  * Reads ``ctx.op_id`` + ``ctx.attempt`` for envelope metadata
    (audit-trail correlation).
  * Returns a string. The caller (providers.py) passes this
    string directly to the SDK's ``system=`` kwarg.
  * No env flags — adaptive is always on; base behavior is
    structurally preserved by the "empty strategic_memory_prompt
    returns base unchanged" branch.

# Operator bindings honored

  * **No hardcoding** — XML tag names are module-level constants;
    rejection content read from ctx (no string-pattern matching
    on the rejection text).
  * **No silent fallback** — if ctx is malformed (missing fields),
    the function defensively returns base unchanged (same as
    first-attempt) — never raises into the provider call path.
  * **Single seam** — providers.py imports + calls this one
    function at 3 wiring sites; AST-pinned.
  * **Build on existing** — reuses ``ctx.strategic_memory_prompt``
    (already populated), ``ctx.op_id`` (already present), the
    Anthropic SDK's ``system=`` kwarg (already wired through
    Aegis); no parallel state.
"""

from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape as _xml_escape


# Module-level constants — single source of truth for the envelope
# shape. AST pins elsewhere reference these by name (not literal),
# so future contributors who rename tags update the pin too.

ENVELOPE_OPEN: str = "<previous_failure_context>"
ENVELOPE_CLOSE: str = "</previous_failure_context>"

# Compliance directive — added verbatim to every envelope. Phrased
# to force the model's attention onto the rules BEFORE generation.
# Constant string so the pin can verify it appears in retry envelopes.
_COMPLIANCE_DIRECTIVE: str = (
    "You MUST address every rule above BEFORE generating any new "
    "code. Your previous attempt violated these rules. Failure to "
    "comply again will trigger another retry rejection and exhaust "
    "this operation's budget. Use the provided tools to satisfy "
    "every rule before proposing any patch."
)

# Op-id prefix length for the envelope's audit-trail tag. Full
# UUIDs are long; first 12 chars are sufficient to correlate with
# debug.log without bloating the prompt cache key.
_OP_ID_PREFIX_LEN: int = 12


def _is_truly_empty(s: Any) -> bool:
    """Defensive: treat None, non-string, or whitespace-only as empty.
    Single seam — every "is this string meaningfully populated"
    check in this module routes through here so the empty policy
    is consistent.
    """
    if not isinstance(s, str):
        return True
    return not s.strip()


def _safe_int_attempt(ctx: Any) -> int:
    """Read ``ctx.attempt`` defensively. Defaults to 1 if missing
    or not an int — a retry op without an attempt counter still
    deserves the envelope (we know it's a retry because
    strategic_memory_prompt is populated)."""
    raw = getattr(ctx, "attempt", None)
    if isinstance(raw, int) and raw >= 0:
        return max(1, raw)  # min 1 — never report "attempt 0" in a retry envelope
    return 1


def _safe_op_id_prefix(ctx: Any) -> str:
    """Read first N chars of ``ctx.op_id`` defensively. Returns
    empty string if missing — the envelope omits the op_id tag in
    that case rather than emitting a malformed one."""
    raw = getattr(ctx, "op_id", None)
    if not isinstance(raw, str):
        return ""
    raw = raw.strip()
    if not raw:
        return ""
    return raw[:_OP_ID_PREFIX_LEN]


def _build_envelope(
    *, rejection_message: str, attempt: int, op_id_prefix: str,
) -> str:
    """Pure-data envelope builder. All inputs already validated +
    XML-escaped at the call site (or here, just before insertion).

    Envelope shape (no whitespace stripping — Anthropic's parser
    is whitespace-tolerant; readability for log/audit beats byte
    minimization):
    """
    escaped_msg = _xml_escape(rejection_message)
    op_id_tag = (
        f"  <op_id>{_xml_escape(op_id_prefix)}</op_id>\n"
        if op_id_prefix else ""
    )
    return (
        f"{ENVELOPE_OPEN}\n"
        f"  <attempt_number>{attempt}</attempt_number>\n"
        f"{op_id_tag}"
        f"  <rules_violated>\n"
        f"{escaped_msg}\n"
        f"  </rules_violated>\n"
        f"  <compliance_directive>\n"
        f"{_COMPLIANCE_DIRECTIVE}\n"
        f"  </compliance_directive>\n"
        f"{ENVELOPE_CLOSE}\n\n"
    )


def compose_system_prompt(
    *,
    base_system_prompt: str,
    ctx: Any,
) -> str:
    """Compose the effective system prompt for this op.

    First attempt / non-retry (empty ``ctx.strategic_memory_prompt``):
      Returns ``base_system_prompt`` UNCHANGED. Byte-identical to
      legacy behavior — zero impact on non-retry paths.

    Retry-after-rejection (``ctx.strategic_memory_prompt`` populated):
      Returns ``<previous_failure_context>...</previous_failure_context>\\n\\n`` +
      ``base_system_prompt`` — the XML envelope PREFIXED to the
      base. The model's structured-parsing pathway processes the
      envelope's rules FIRST, then reads the base instructions
      with those rules already framing its attention.

    Args:
        base_system_prompt: The legacy/static system prompt
            (``_CODEGEN_SYSTEM_PROMPT`` constant or equivalent).
            Returned unchanged on first-attempt ops.
        ctx: The :class:`OperationContext`. Only three attributes
            are read (defensively, via ``getattr``):
            ``strategic_memory_prompt`` (trigger), ``attempt``
            (envelope metadata), ``op_id`` (audit-trail prefix).
            Caller is NOT required to pass a fully-formed ctx —
            missing attributes default gracefully.

    Returns:
        A string. Never raises (any unexpected ctx shape folds to
        the "return base unchanged" branch — silent degradation,
        not silent error).
    """
    if not isinstance(base_system_prompt, str):
        # Defensive: caller passed something non-string. Coerce or
        # return empty rather than blow up the provider call.
        base_system_prompt = str(base_system_prompt or "")

    rejection = getattr(ctx, "strategic_memory_prompt", None)
    if _is_truly_empty(rejection):
        # First-attempt path. Return base byte-identical.
        return base_system_prompt

    envelope = _build_envelope(
        rejection_message=rejection,
        attempt=_safe_int_attempt(ctx),
        op_id_prefix=_safe_op_id_prefix(ctx),
    )
    return envelope + base_system_prompt


__all__ = [
    "compose_system_prompt",
    "ENVELOPE_OPEN",
    "ENVELOPE_CLOSE",
]
