"""Slice 249 — live human-in-the-loop steering registry.

A non-blocking, op-id-keyed guidance channel into a RUNNING tool loop — distinct
from the Slice 246 preemption path (which suspends the op). When the Sovereign
Host pushes guidance for an op that is actively exploring, the BackgroundAgent /
REPL calls :func:`inject_guidance`; the tool loop drains it at the next round
boundary via :func:`consume_guidance` and folds it into the live prompt WITHOUT
yielding the lane or suspending the operation.

Mirrors ``preemption.py``: a small thread-safe singleton registry, pure, env-
gated, NEVER raises (it sits on the round-boundary hot path). The actual prompt
fold + telemetry happen in the tool loop; this module only stores/drains/format.
"""
from __future__ import annotations

import os
import re
import threading
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional

_ENV_ENABLED = "JARVIS_LIVE_STEERING_ENABLED"
_ENV_GLOBAL_PROP = "JARVIS_STEERING_GLOBAL_PROPAGATION_ENABLED"

# Slice 251 — deterministic LOCAL-vs-GLOBAL steering-intent classification.
# The codebase idiom for fast classification is zero-LLM (urgency_router §5
# Tier 0, <1ms). LOCAL-vs-GLOBAL is a lexical/structural distinction, so a
# deterministic classifier is faster AND genuinely non-blocking (no "Tiny Prime"
# LLM latency on the hot path). A GLOBAL_DIRECTIVE carries a universal-scope
# signal ("always", "never", "from now on", "for all", ...); everything else is
# a LOCAL_CORRECTION. Conservative: ambiguous → LOCAL (never pollute global
# memory on a weak signal).
INTENT_LOCAL = "local_correction"
INTENT_GLOBAL = "global_directive"

_GLOBAL_PHRASES = (
    "from now on", "going forward", "by default", "in general", "as a rule",
    "across the", "for all", "for every", "moving forward",
)
_GLOBAL_WORDS = re.compile(
    r"\b(always|never|everywhere|globally|standardi[sz]e|henceforth|"
    r"whenever|all|every)\b"
)

_PENDING: Dict[str, Deque[str]] = defaultdict(deque)
_LOCK = threading.Lock()


def live_steering_enabled() -> bool:
    """Master gate (default-TRUE). When OFF, guidance is never absorbed (the tool
    loop runs byte-identical to pre-249). NEVER raises."""
    try:
        return os.getenv(_ENV_ENABLED, "true").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


def inject_guidance(op_id: str, text: str) -> None:
    """Queue a human guidance payload for a running op. Absorbed at the op's next
    tool-round boundary. Non-blocking, NEVER raises. Empty op_id / text ignored."""
    if not op_id or not text:
        return
    try:
        with _LOCK:
            _PENDING[op_id].append(str(text))
    except Exception:  # noqa: BLE001
        pass


def has_guidance(op_id: str) -> bool:
    """True iff guidance is pending for this op. NEVER raises."""
    if not op_id:
        return False
    try:
        with _LOCK:
            return bool(_PENDING.get(op_id))
    except Exception:  # noqa: BLE001
        return False


def consume_guidance(op_id: str) -> Optional[str]:
    """Drain ALL pending guidance for this op (consume-once), joined newest-last.
    Returns None when nothing is pending. NEVER raises."""
    if not op_id:
        return None
    try:
        with _LOCK:
            q = _PENDING.get(op_id)
            if not q:
                return None
            items = list(q)
            q.clear()
            _PENDING.pop(op_id, None)
        return "\n".join(items)
    except Exception:  # noqa: BLE001
        return None


def format_guidance_block(text: str) -> str:
    """Render absorbed guidance as a prompt block the model treats as a live
    human instruction. ASCII-only (Iron Gate strictness)."""
    return (
        "## LIVE HUMAN GUIDANCE (injected mid-flight by the Sovereign Host)\n"
        "The operator has steered this operation in real time. Treat the "
        "following as an authoritative course-correction and apply it to the "
        "remainder of your work:\n"
        f"{text}"
    )


def reset_guidance() -> None:
    """Test isolation — clear all pending guidance."""
    try:
        with _LOCK:
            _PENDING.clear()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Slice 251 — durable propagation of GLOBAL directives into agentic memory.
# ---------------------------------------------------------------------------


def classify_steering_intent(text: str) -> str:
    """Deterministic, zero-LLM LOCAL-vs-GLOBAL classifier. Returns
    ``INTENT_GLOBAL`` iff the guidance carries a universal-scope signal;
    otherwise ``INTENT_LOCAL``. NEVER raises (degrades to LOCAL)."""
    try:
        t = (text or "").strip().lower()
        if not t:
            return INTENT_LOCAL
        if any(p in t for p in _GLOBAL_PHRASES):
            return INTENT_GLOBAL
        if _GLOBAL_WORDS.search(t):
            return INTENT_GLOBAL
        return INTENT_LOCAL
    except Exception:  # noqa: BLE001
        return INTENT_LOCAL


def steering_global_propagation_enabled() -> bool:
    """Gate for persisting GLOBAL directives to durable agentic memory
    (default-TRUE). When OFF, steering stays ephemeral (Slice 249 only).
    NEVER raises."""
    try:
        return os.getenv(_ENV_GLOBAL_PROP, "true").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


async def propagate_directive(
    op_id: str,
    text: str,
    *,
    store: Optional[Any] = None,
) -> str:
    """Out-of-band durability for an absorbed steering payload. Classify it; if
    it is a GLOBAL_DIRECTIVE and propagation is enabled, persist it into the
    UserPreferenceMemory — the same store StrategicDirection injects into EVERY
    future agent's generation prompt, so the directive survives session-amnesia
    and all future agent inits boot with it. A LOCAL_CORRECTION is left ephemeral
    (Slice 249 absorbed it into the current op's prompt already).

    Reuses the existing global-memory channel (no ChromaDB, no new graph). Runs
    out-of-band (callers fire-and-forget via create_task) so the active tool loop
    is never blocked. Returns the classification. NEVER raises into the caller."""
    try:
        intent = classify_steering_intent(text)
        if intent != INTENT_GLOBAL or not steering_global_propagation_enabled():
            return intent
        _store = store
        if _store is None:
            from backend.core.ouroboros.governance.user_preference_memory import (
                get_default_store,
            )
            _store = get_default_store()
        _store.record_live_steering_directive(op_id=op_id, directive=text)
        return intent
    except Exception:  # noqa: BLE001 — durability is best-effort, never blocks/raises
        return INTENT_LOCAL
