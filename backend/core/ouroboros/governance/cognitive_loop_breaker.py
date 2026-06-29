"""cognitive_loop_breaker.py -- Semantic Loop Breaker (Venom cognitive armor).

A small (7B) model will confidently emit the SAME broken tool call several rounds
in a row -- a repetition hallucination that the coarse budget/iteration guards
don't catch until tokens and deadline are already burned. This heuristic breaker
tracks the SEMANTIC SIMILARITY of the last ``window`` rounds' tool calls; when the
model is provably stuck it snaps the circuit so the loop can eject early with the
best context already gathered.

Pure + dependency-free: an order-independent normalized signature per round +
``difflib.SequenceMatcher`` ratio (stdlib, no embeddings). Fail-soft + gated:
any error / OFF / sub-window history -> ``False`` (never a false eject).

Env
---
JARVIS_VENOM_LOOP_BREAKER_ENABLED     default "true"
JARVIS_VENOM_LOOP_BREAKER_WINDOW      default 3  (consecutive rounds to compare)
JARVIS_VENOM_LOOP_BREAKER_SIMILARITY  default 0.9 (>= -> 'same' round)
"""
from __future__ import annotations

import logging
import os
import re
from collections import deque
from difflib import SequenceMatcher
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

_WS = re.compile(r"\s+")


def _enabled() -> bool:
    val = (os.environ.get("JARVIS_VENOM_LOOP_BREAKER_ENABLED", "true") or "true").strip().lower()
    return val not in ("false", "0", "no", "off")


def _window() -> int:
    try:
        return max(2, int(os.environ.get("JARVIS_VENOM_LOOP_BREAKER_WINDOW", "3")))
    except (ValueError, TypeError):
        return 3


def _similarity_threshold() -> float:
    try:
        return min(1.0, max(0.0, float(
            os.environ.get("JARVIS_VENOM_LOOP_BREAKER_SIMILARITY", "0.9")
        )))
    except (ValueError, TypeError):
        return 0.9


def call_signature(tool_calls: Optional[Sequence[Any]]) -> str:
    """Deterministic, ORDER-INDEPENDENT normalized signature for one round of
    tool calls (each has ``.name`` + ``.arguments``). NEVER raises -> "" on error."""
    try:
        parts = []
        for c in (tool_calls or []):
            name = str(getattr(c, "name", "") or "")
            args = getattr(c, "arguments", {}) or {}
            try:
                arg_str = ",".join("{}={}".format(k, args[k]) for k in sorted(args, key=str))
            except Exception:  # noqa: BLE001
                arg_str = str(args)
            parts.append("{}({})".format(name, arg_str))
        parts.sort()  # order-independent across the round's calls
        return _WS.sub(" ", "|".join(parts).strip().lower())
    except Exception:  # noqa: BLE001
        return ""


def _name_set(tool_calls: Optional[Sequence[Any]]) -> frozenset:
    """The multiset-as-set of tool NAMES in a round. A round that calls a
    DIFFERENT tool is genuine progress, never repetition -- the name is the
    dominant discriminator (args alone can look similar across distinct actions)."""
    try:
        return frozenset(str(getattr(c, "name", "") or "") for c in (tool_calls or []))
    except Exception:  # noqa: BLE001
        return frozenset()


class SemanticLoopBreaker:
    """Stateful per-tool-loop repetition detector. One instance per Venom run."""

    def __init__(self) -> None:
        # Each entry: (name_set, signature_string).
        self._rounds: "deque" = deque(maxlen=_window())

    def observe(self, tool_calls: Optional[Sequence[Any]]) -> bool:
        """Record this round's tool calls; return True iff the model is stuck in
        a semantic repetition loop -- the last ``window`` rounds ALL call the SAME
        tools with args pairwise-similar at/above the threshold. Gated + fail-soft
        -> False (never a false eject; an empty round is not repetition)."""
        try:
            if not _enabled() or not tool_calls:
                return False
            sig = call_signature(tool_calls)
            if not sig:
                return False
            self._rounds.append((_name_set(tool_calls), sig))
            win = _window()
            if len(self._rounds) < win:
                return False
            recent = list(self._rounds)[-win:]
            thr = _similarity_threshold()
            for (na, sa), (nb, sb) in zip(recent, recent[1:]):
                # Different tools => progress, not a loop. Same tools => compare args.
                if na != nb or SequenceMatcher(None, sa, sb).ratio() < thr:
                    return False
            logger.warning(
                "[CognitiveLoopBreaker] semantic repetition over %d rounds "
                "(same tools, args pairwise >=%.2f) -- snapping the circuit "
                "(model stuck)", win, thr,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[CognitiveLoopBreaker] observe fail-soft err=%r", exc)
            return False

    def reset(self) -> None:
        """Genuine progress -> clear the window (caller may reset on a successful
        mutating tool result). Fail-soft."""
        try:
            self._rounds.clear()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["SemanticLoopBreaker", "call_signature"]
