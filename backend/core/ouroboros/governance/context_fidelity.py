"""Fidelity Watchdog — notice when the prompt stopped being able to answer.

The dependency pruner degrades the whole set one rung at a time: FULL →
SIGNATURES → NAMES. Only the first two rungs let a caller *call* anything:

  * FULL        — bodies included; the model can read the implementation.
  * SIGNATURES  — bodies stripped, argument lists intact. Still sufficient:
                  the model knows the name, arity and keywords.
  * NAMES       — a bare list of identifiers. The model knows a symbol
                  exists and nothing about how to invoke it, so it must
                  invent the call. Every AttributeError and TypeError that
                  follows is *caused by the prompt*, not by the model.

The NAMES rung is therefore not a smaller prompt, it is a MISLEADING one, and
it is silent: the assembler logs a line at INFO and generation proceeds. A soak
can burn hundreds of iterations against bare names and report the failures as
model quality. This module makes that condition an explicit, countable signal.

Its first job is regression detection. The dependency budget was sized for
30-line snippets (~375 tokens) while one module's signatures measured 4,289
chars, so EVERY dependency sat at NAMES; the budget now derives from the
negotiated window. This watchdog is what notices if it ever silently returns.

Design notes:

  * The observation lives in ``fit_dependencies`` — the single place that
    decides the rung — so every caller is covered, not only the assembler.
  * Severity derives from the rung's POSITION in the ladder, not from a
    hardcoded rung name, so adding a rung does not silently skip the gate.
  * Warnings latch and escalate on powers of two, so a persistent starvation
    costs a logarithmic number of log lines rather than one per generation.
  * Never raises and never blocks: an observability failure must not be able
    to take down generation.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# How many recent verdicts to keep for the sentinel. Bounded: a multi-day soak
# assembles context continuously and an unbounded history is a slow leak.
_WINDOW_ENV = "JARVIS_CONTEXT_FIDELITY_WINDOW"
_DEFAULT_WINDOW = 256


def window_size() -> int:
    """Ring capacity, operator-overridable. Falls back rather than raising."""
    try:
        value = int(os.environ.get(_WINDOW_ENV, "") or _DEFAULT_WINDOW)
        return value if value > 0 else _DEFAULT_WINDOW
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW


# Severity names, ordered worst-last so comparisons are positional.
SEVERITIES: Tuple[str, ...] = ("ok", "degraded", "starved", "overflow")


@dataclass(frozen=True)
class FidelityVerdict:
    """One context assembly, judged."""

    label: str
    rung: str
    modules: int
    used_tokens: int
    budget_tokens: int
    severity: str
    reason: str

    @property
    def starved(self) -> bool:
        """True when the prompt can no longer support a correct call."""
        return SEVERITIES.index(self.severity) >= SEVERITIES.index("starved")

    @property
    def headroom(self) -> float:
        """Fraction of the budget consumed; >1.0 means the rung overflowed."""
        if self.budget_tokens <= 0:
            return 0.0
        return self.used_tokens / float(self.budget_tokens)


@dataclass
class _LabelState:
    streak: int = 0
    last_severity: str = "ok"
    counts: Dict[str, int] = field(default_factory=dict)


class FidelityWatchdog:
    """Bounded, thread-safe record of context-assembly fidelity."""

    def __init__(self, window: Optional[int] = None) -> None:
        self._verdicts: Deque[FidelityVerdict] = deque(
            maxlen=window or window_size()
        )
        self._labels: Dict[str, _LabelState] = {}
        self._lock = threading.Lock()

    # -- judging ---------------------------------------------------------

    @staticmethod
    def judge(
        label: str,
        rung: str,
        ladder: Tuple[str, ...],
        modules: int,
        used_tokens: int,
        budget_tokens: int,
    ) -> FidelityVerdict:
        """Classify one assembly. Severity comes from the rung's POSITION in
        *ladder*, so a new rung inserted tomorrow is graded, not ignored."""
        if budget_tokens > 0 and used_tokens > budget_tokens:
            # The set does not fit even at the floor rung. Worse than
            # starvation: the prompt is over budget AND uninformative.
            return FidelityVerdict(
                label=label, rung=rung, modules=modules,
                used_tokens=used_tokens, budget_tokens=budget_tokens,
                severity="overflow",
                reason=(
                    f"{modules} module(s) exceed the {budget_tokens}-token "
                    f"budget even at '{rung}' ({used_tokens} tokens)"
                ),
            )
        try:
            position = ladder.index(rung)
        except ValueError:
            position = 0
        floor = max(0, len(ladder) - 1)
        if position >= floor and floor > 0:
            severity = "starved"
            reason = (
                f"dependencies degraded to '{rung}' — bare identifiers with "
                "no argument lists; any call the model writes is a guess"
            )
        elif position > 0:
            severity = "degraded"
            reason = (
                f"dependencies degraded to '{rung}' — bodies dropped, "
                "signatures intact"
            )
        else:
            severity = "ok"
            reason = f"dependencies at '{rung}'"
        return FidelityVerdict(
            label=label, rung=rung, modules=modules,
            used_tokens=used_tokens, budget_tokens=budget_tokens,
            severity=severity, reason=reason,
        )

    # -- recording -------------------------------------------------------

    def record(self, verdict: FidelityVerdict) -> FidelityVerdict:
        """Store *verdict* and emit at most a logarithmic number of warnings
        for a persistent condition. Never raises."""
        try:
            with self._lock:
                self._verdicts.append(verdict)
                state = self._labels.setdefault(verdict.label, _LabelState())
                state.counts[verdict.severity] = (
                    state.counts.get(verdict.severity, 0) + 1
                )
                if verdict.severity == state.last_severity:
                    state.streak += 1
                else:
                    state.streak = 1
                    state.last_severity = verdict.severity
                streak = state.streak
            if verdict.starved and _is_escalation_point(streak):
                logger.warning(
                    "[ContextStarvation] %s: %s (%d/%d tokens, %d module(s)) "
                    "— %d consecutive assembly(ies). Failures downstream are "
                    "caused by the prompt, not the model.",
                    verdict.label, verdict.reason, verdict.used_tokens,
                    verdict.budget_tokens, verdict.modules, streak,
                )
        except Exception:  # noqa: BLE001 — observability never blocks work
            logger.debug("[ContextStarvation] record failed", exc_info=True)
        return verdict

    # -- reading ---------------------------------------------------------

    def recent(self, limit: int = 0) -> List[FidelityVerdict]:
        with self._lock:
            items = list(self._verdicts)
        return items[-limit:] if limit > 0 else items

    def summary(self) -> Dict[str, object]:
        """What the sentinel shows: per-label severity counts and the current
        streak, plus the one number that matters — the starved share."""
        with self._lock:
            labels = {
                name: {
                    "counts": dict(state.counts),
                    "streak": state.streak,
                    "severity": state.last_severity,
                }
                for name, state in self._labels.items()
            }
            verdicts = list(self._verdicts)
        starved = sum(1 for v in verdicts if v.starved)
        return {
            "observed": len(verdicts),
            "starved": starved,
            "starved_share": (starved / len(verdicts)) if verdicts else 0.0,
            "labels": labels,
        }

    def reset(self) -> None:
        with self._lock:
            self._verdicts.clear()
            self._labels.clear()


def _is_escalation_point(streak: int) -> bool:
    """True at 1, 2, 4, 8, 16 ... — a persistent condition stays visible
    without one line per generation."""
    return streak > 0 and (streak & (streak - 1)) == 0


_WATCHDOG: Optional[FidelityWatchdog] = None
_WATCHDOG_LOCK = threading.Lock()


def get_watchdog() -> FidelityWatchdog:
    """Process-wide watchdog."""
    global _WATCHDOG
    if _WATCHDOG is None:
        with _WATCHDOG_LOCK:
            if _WATCHDOG is None:
                _WATCHDOG = FidelityWatchdog()
    return _WATCHDOG


def observe(
    label: str,
    rung: str,
    ladder: Tuple[str, ...],
    modules: int,
    used_tokens: int,
    budget_tokens: int,
) -> FidelityVerdict:
    """The seam ``fit_dependencies`` calls. Judges and records in one step."""
    watchdog = get_watchdog()
    return watchdog.record(
        watchdog.judge(
            label, rung, ladder, modules, used_tokens, budget_tokens,
        )
    )


def starvation_summary() -> Dict[str, object]:
    """Convenience for the CLI/sentinel."""
    return get_watchdog().summary()


__all__ = [
    "SEVERITIES",
    "FidelityVerdict",
    "FidelityWatchdog",
    "get_watchdog",
    "observe",
    "starvation_summary",
    "window_size",
]
