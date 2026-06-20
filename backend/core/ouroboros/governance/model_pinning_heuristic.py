"""Sovereign Context-Routing Override Matrix — operator model pin with soft-lock.

When ``JARVIS_DW_PRIMARY_OVERRIDE=<model_id>`` is declared, the named model is
promoted to **Rank 1 across ALL routes** (IMMEDIATE / STANDARD / COMPLEX /
BACKGROUND / SPECULATIVE) by intercepting the resolved DW model tuple in
``provider_topology.dw_models_for_route``. The pin is composed *after*
``_fleet_guarded`` (the Sovereign Fleet Evaluator EWMA re-rank), so a healthy
pin overrides the EWMA ordering — and a *failing* pin yields straight back to it.

WHY THIS IS A SOFT LOCK, NOT A HARDCODE
---------------------------------------
The operator declares intent ("route to gpt-oss-120b"); the matrix honors it
only while the model is actually delivering. The pin is suspended automatically
when the model misbehaves:

  * **Driven by PASSIVELY OBSERVED runtime outcomes** — every real
    sentinel-dispatch success/failure (429 / 500 / live-transport rupture) is
    fed to :func:`note_pin_outcome` at the existing dispatch sites. There is NO
    active synthetic probe: an active boot probe is exactly the heavy-probe
    false-degrade pathology that wedged the 2026-06-20 container soak
    (one slow 550B probe degraded the whole stream lane). Observed truth only.
  * **Consecutive-failure trip:** once the pinned model logs
    ``JARVIS_DW_PIN_FAIL_THRESHOLD`` (default 3) consecutive failures, the pin
    enters a cooldown of ``JARVIS_DW_PIN_COOLDOWN_S`` (default 120s) during
    which ``dw_models_for_route`` returns the standard FleetEvaluator EWMA
    ranking unchanged. A single success clears the streak immediately.

GATING & SAFETY
---------------
  * Unset / empty ``JARVIS_DW_PRIMARY_OVERRIDE`` → OFF, byte-identical legacy.
  * Pure + fail-soft: any internal error returns the input ranking unchanged.
  * The pin is prepended (deduped) even if the model is absent from the resolved
    fleet — an explicit operator declaration is honored, and the soft-lock is
    the armor: a non-existent/cold model fails fast and the cooldown demotes it.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, Tuple

_MODEL_PIN_ENV = "JARVIS_DW_PRIMARY_OVERRIDE"
_PIN_FAIL_THRESHOLD_ENV = "JARVIS_DW_PIN_FAIL_THRESHOLD"
_PIN_COOLDOWN_ENV = "JARVIS_DW_PIN_COOLDOWN_S"

_DEFAULT_FAIL_THRESHOLD = 3
_DEFAULT_COOLDOWN_S = 120.0


def model_pin_override() -> str:
    """The operator-declared pinned model id, or ``""`` when unset/disabled.

    Reads ``JARVIS_DW_PRIMARY_OVERRIDE`` per call (env-precedence; an operator
    can flip the pin live). NEVER raises."""
    try:
        return os.environ.get(_MODEL_PIN_ENV, "").strip()
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _fail_threshold() -> int:
    try:
        v = int(os.environ.get(_PIN_FAIL_THRESHOLD_ENV, "").strip() or _DEFAULT_FAIL_THRESHOLD)
        return v if v >= 1 else _DEFAULT_FAIL_THRESHOLD
    except (ValueError, TypeError):
        return _DEFAULT_FAIL_THRESHOLD


def _cooldown_s() -> float:
    try:
        v = float(os.environ.get(_PIN_COOLDOWN_ENV, "").strip() or _DEFAULT_COOLDOWN_S)
        return v if v > 0.0 else _DEFAULT_COOLDOWN_S
    except (ValueError, TypeError):
        return _DEFAULT_COOLDOWN_S


class ModelPinLedger:
    """Per-model consecutive-failure tracker driving the pin soft-lock.

    Thread-safe (asyncio tasks + the AST/bg thread pools share it). All state
    is in-memory and process-local — the pin is a live operator override, not
    durable policy, so it MUST start clean every boot (a stale cooldown must
    never silently suppress a fresh pin)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive_failures: Dict[str, int] = {}
        self._locked_until: Dict[str, float] = {}

    def note_outcome(self, model_id: str, *, success: bool) -> None:
        """Feed one observed dispatch outcome for ``model_id``. A success clears
        the streak + any cooldown; a failure increments and, at threshold, arms
        the cooldown. NEVER raises."""
        if not model_id:
            return
        try:
            with self._lock:
                if success:
                    self._consecutive_failures.pop(model_id, None)
                    self._locked_until.pop(model_id, None)
                    return
                n = self._consecutive_failures.get(model_id, 0) + 1
                self._consecutive_failures[model_id] = n
                if n >= _fail_threshold():
                    self._locked_until[model_id] = time.monotonic() + _cooldown_s()
        except Exception:  # noqa: BLE001 — telemetry must never break dispatch
            pass

    def is_soft_locked(self, model_id: str) -> bool:
        """True while ``model_id`` is inside its failure cooldown. Expired
        cooldowns self-heal (the entry is cleared on read). NEVER raises."""
        if not model_id:
            return False
        try:
            with self._lock:
                until = self._locked_until.get(model_id)
                if until is None:
                    return False
                if time.monotonic() >= until:
                    # cooldown elapsed → give the pin another chance
                    self._locked_until.pop(model_id, None)
                    self._consecutive_failures.pop(model_id, None)
                    return False
                return True
        except Exception:  # noqa: BLE001
            return False

    def reset(self) -> None:
        """Clear all state (test hook / live ``/posture``-style reset)."""
        with self._lock:
            self._consecutive_failures.clear()
            self._locked_until.clear()


_LEDGER = ModelPinLedger()


def get_pin_ledger() -> ModelPinLedger:
    """Process-wide singleton ledger."""
    return _LEDGER


def note_pin_outcome(model_id: str, *, success: bool) -> None:
    """Convenience wired into the sentinel-dispatch success/failure sites.

    No-op (cheap early return) unless a pin is active AND this is the pinned
    model — the ledger only ever needs to track the pinned model. NEVER raises."""
    try:
        pin = model_pin_override()
        if pin and model_id == pin:
            _LEDGER.note_outcome(model_id, success=success)
    except Exception:  # noqa: BLE001
        pass


def apply_model_pin(route: str, ranked: Tuple[str, ...]) -> Tuple[str, ...]:
    """Promote the operator-pinned model to Rank 1 of ``ranked`` — the Override
    Matrix interception. Composed AFTER ``_fleet_guarded`` in
    ``dw_models_for_route``.

    Returns ``ranked`` unchanged when: no pin declared, the pin is soft-locked
    (cooldown active → defer to EWMA), or on any internal error. Otherwise
    returns the pinned model first, followed by the rest of ``ranked`` with the
    pin deduped. The pin is honored across every route because every route
    resolves through this one choke point. NEVER raises."""
    try:
        pin = model_pin_override()
        if not pin:
            return ranked
        if _LEDGER.is_soft_locked(pin):
            # Soft lock engaged — yield to the FleetEvaluator EWMA ranking.
            return ranked
        rest = tuple(m for m in ranked if m != pin)
        if len(rest) == len(ranked) and pin in ranked:
            # (defensive) pin equals an entry but filter kept all — shouldn't
            # happen; fall through to the prepend below which dedups anyway.
            pass
        return (pin,) + rest
    except Exception:  # noqa: BLE001 — pin is best-effort; never break routing
        return ranked


__all__ = [
    "model_pin_override",
    "ModelPinLedger",
    "get_pin_ledger",
    "note_pin_outcome",
    "apply_model_pin",
]
