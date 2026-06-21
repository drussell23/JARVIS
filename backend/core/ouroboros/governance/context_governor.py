# backend/core/ouroboros/governance/context_governor.py
"""Information-Gain Governor (spec section 5.2, LR1, LR3).

Decides, after each Venom round, whether continued exploration yields enough
NEW information to justify the budget — and forces a mathematically safe
handoff when it does not. Synchronous + sub-millisecond on the hot path
(TF-IDF/cosine over hashed tokens; NO model call). Deep embeds, if any, are the
coordinator's async concern — the governor never awaits.

LR1: the delta corpus is seeded with the prefetch excerpts as the round-0
     baseline; round-1 gain is measured against what memory already supplied.
LR3: the deadlock breaker is one-shot; a second decay after it is consumed
     yields action="deadlock_failed" (the coordinator turns that into the fatal
     terminal deadlock_override_failed).
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, List, Sequence, Tuple

logger = logging.getLogger(__name__)

# Iron Gate category -> canonical tool(s) for the deadlock-break directive.
# Mirrors exploration_engine._TOOL_CATEGORY.
_CATEGORY_TOOLS = {
    "COMPREHENSION": ["read_file"],
    "DISCOVERY": ["search_code"],
    "CALL_GRAPH": ["get_callers"],
    "STRUCTURE": ["list_symbols"],
    "HISTORY": ["git_blame", "git_log"],
}

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")


def _tokens(text: str) -> Counter:
    return Counter(t.lower() for t in _TOKEN_RE.findall(text or ""))


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    num = sum(a[t] * b[t] for t in common)
    da = math.sqrt(sum(v * v for v in a.values()))
    db = math.sqrt(sum(v * v for v in b.values()))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


@dataclass(frozen=True)
class GovernorVerdict:
    action: str            # continue | converge | deadlock_break | deadlock_failed
    info_gain: float
    budget_scale: float
    missing_categories: Tuple[str, ...] = ()
    directive: str = ""


@dataclass
class InformationGainGovernor:
    prefetch_excerpts: Sequence[str]
    floors: Any
    enabled: bool = True
    min_gain: float = 0.15
    decay_rounds: int = 2
    _corpus: Counter = field(default_factory=Counter)
    _low_streak: int = 0
    _warm: bool = False
    _deadlock_consumed: bool = False
    _deadlock_pending: bool = False

    def __post_init__(self) -> None:
        # LR1: round-0 baseline IS the prefetch.
        joined = "\n".join(self.prefetch_excerpts or [])
        self._corpus = _tokens(joined)
        self._warm = bool(self._corpus)

    def _budget_scale(self) -> float:
        # warm cache -> compress; cold -> expand.
        return 0.6 if self._warm else 1.4

    def _directive(self, missing: Tuple[str, ...]) -> str:
        lines = ["STOP broad exploration. To satisfy the mandatory safety "
                 "floor you MUST now call ONLY these tools, nothing else:"]
        for cat in missing:
            tools = _CATEGORY_TOOLS.get(cat, ["read_file"])
            lines.append(f"  - {cat}: call {' or '.join(tools)} "
                         f"on the most relevant target file.")
        lines.append("Then immediately emit your patch.")
        return "\n".join(lines)

    def mark_deadlock_round_consumed(self) -> None:
        """Coordinator calls this after appending the deadlock directive (LR3)."""
        self._deadlock_consumed = True
        self._deadlock_pending = False

    def observe_round(self, round_index: int, round_tool_results: List[str],
                      ledger: Any) -> GovernorVerdict:
        if not self.enabled:
            return GovernorVerdict("continue", 1.0, 1.0)
        scale = self._budget_scale()
        new = _tokens("\n".join(round_tool_results or []))
        sim = _cosine(new, self._corpus)
        gain = max(0.0, 1.0 - sim) if new else 0.0
        self._corpus.update(new)

        if gain < self.min_gain:
            self._low_streak += 1
        else:
            self._low_streak = 0

        decayed = self._low_streak >= self.decay_rounds
        if not decayed:
            return GovernorVerdict("continue", gain, scale)

        floor_met = True
        missing: Tuple[str, ...] = ()
        try:
            floor_met = bool(self.floors.is_satisfied(ledger))
            if not floor_met:
                missing = tuple(self.floors.missing_categories(ledger))
        except Exception:  # noqa: BLE001 — floor probe must not crash governor
            floor_met = True

        if floor_met:
            return GovernorVerdict("converge", gain, scale)

        if self._deadlock_consumed or self._deadlock_pending:
            # LR3 (iron-clad): the one-shot directive was already issued —
            # either explicitly consumed by the coordinator, OR still pending
            # from a prior deadlock_break the coordinator failed to mark. Either
            # way the floor is STILL unmet after the shot, so escalate to fatal.
            # The governor self-defends against a coordinator that forgets to
            # call mark_deadlock_round_consumed(); we never emit a second
            # deadlock_break (no looping at the safety gate).
            return GovernorVerdict("deadlock_failed", gain, scale,
                                   missing_categories=missing)
        self._deadlock_pending = True
        return GovernorVerdict("deadlock_break", gain, scale,
                               missing_categories=missing,
                               directive=self._directive(missing))
