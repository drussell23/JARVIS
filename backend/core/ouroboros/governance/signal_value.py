# backend/core/ouroboros/governance/signal_value.py
"""
Signal value scoring — the intake half of the appetite layer (Slice 15 T3/T4).

Run-24 operator verdict: everything O+V initiates on its own is
annotation-grade. The pipeline is proven; the JUDGMENT about what deserves
compute is not. This module scores a signal's semantic value from
MATHEMATICALLY VERIFIABLE state only (mandate 1 — zero keyword matching,
zero priority lists):

  * ``BAND_ORACLE`` (3) — the signal carries resolved test→source
    attribution (Slice 6: a REAL failing test — nonzero pytest exit
    observed by the sensor — whose source loci were resolved by
    deterministic AST import tracing). The red test is an objective,
    machine-checkable oracle; nothing outranks it.
  * ``BAND_EXECUTABLE`` (2) — ≥1 Python target parses and contains ≥1
    executable statement (the T2 engine's docstring-stripped
    ``ast.stmt`` census — structural fact, not guess).
  * ``BAND_COSMETIC_CLASS`` (1) — every classifiable target is a declared
    line-grammar / zero-statement format (requirements/cfg/ini or a
    docstring-only module): the Run-22 noise class, by format identity.
  * ``BAND_INDETERMINATE`` (0) — nothing verifiable (no targets,
    unreadable, unparseable). Consumers MUST fail safe to the restricted
    BACKGROUND route (mandate 4) — never escalate on a guess.

Consumers (all existing surfaces — mandate 3, no second queue):
  * ``UrgencyRouter.classify`` — band clamp/escalation priority block.
  * The per-route generation-timeout seams (BOTH generate paths) —
    ``adaptive_generation_scale``: allocation as a FUNCTION of band over
    the existing route baselines (env-tunable coefficients, no new
    hardcoded absolutes; mandate 2).
  * ``_value_ceiling_risk_floor`` (both GATE paths) — above the adaptive
    ceiling the op HALTS for the human exception-handler
    (APPROVAL_REQUIRED; the Orange flow is preserved, never demoted).

Pure, stdlib + the T2 engine. Never raises. Master:
``JARVIS_SIGNAL_VALUE_ROUTING_ENABLED`` (default true, read at call sites).
"""
from __future__ import annotations

import ast
import fnmatch
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

BAND_INDETERMINATE = 0
BAND_COSMETIC_CLASS = 1
BAND_EXECUTABLE = 2
BAND_ORACLE = 3

_TRUTHY = ("1", "true", "yes", "on")


def signal_value_routing_enabled() -> bool:
    """Master — ``JARVIS_SIGNAL_VALUE_ROUTING_ENABLED`` (default true)."""
    return os.environ.get(
        "JARVIS_SIGNAL_VALUE_ROUTING_ENABLED", "true",
    ).strip().lower() in _TRUTHY


def score_signal(
    signal_source: str,
    target_files: Sequence[Any],
    intake_evidence_json: str,
    project_root: Any,
) -> int:
    """Band from verifiable state only. Never raises.

    Evidence order: resolved attribution (the failing-test oracle) beats
    everything; then the AST executable-statement census of the targets;
    then format identity; then the indeterminate floor.
    """
    # ---- Oracle: Slice-6 attribution evidence (schema-versioned) ----------
    try:
        _ev = json.loads(intake_evidence_json) if intake_evidence_json else {}
        _att = _ev.get("attribution") or {}
        if str(_att.get("status", "")).strip().lower() == "resolved":
            return BAND_ORACLE
    except Exception:  # noqa: BLE001 — malformed evidence is not an oracle
        pass

    # ---- AST census over the targets (the T2 engine — mandate 3) ---------
    try:
        from backend.core.ouroboros.governance.candidate_value_gate import (
            _LINE_GRAMMAR_GLOBS,
            _stmt_count,
            _stripped_tree,
        )

        try:
            _root = Path(project_root) if project_root else None
        except (TypeError, ValueError):
            _root = None
        saw_classifiable_low = False
        for _f in (target_files or ()):
            _fs = str(_f)
            _name = Path(_fs).name
            _p = Path(_fs)
            if not _p.is_absolute() and _root is not None:
                _p = _root / _fs
            if _name.endswith(".py"):
                try:
                    _src = _p.read_text(errors="replace")
                except OSError:
                    continue  # unreadable — proves nothing
                try:
                    if _stmt_count(_stripped_tree(_src)) >= 1:
                        return BAND_EXECUTABLE
                    saw_classifiable_low = True  # docstring-only module
                except SyntaxError:
                    continue  # unparseable — proves nothing
            elif any(fnmatch.fnmatch(_name, g) for g in _LINE_GRAMMAR_GLOBS):
                saw_classifiable_low = True
        if saw_classifiable_low:
            return BAND_COSMETIC_CLASS
        return BAND_INDETERMINATE
    except Exception:  # noqa: BLE001 — mandate 4: unresolvable → floor
        logger.debug("[SignalValue] scoring error — indeterminate",
                     exc_info=True)
        return BAND_INDETERMINATE


def adaptive_generation_scale(band: int) -> Tuple[float, float]:
    """(time_multiplier, cost_multiplier) as a FUNCTION of band over the
    existing route baselines (mandate 2 — no new absolutes; coefficients
    env-tunable: ``JARVIS_VALUE_TIME_SCALE_COEFF`` /
    ``JARVIS_VALUE_COST_SCALE_COEFF``, default 0.5 per band step above
    the cosmetic class). Band ≤ 1 and the indeterminate floor scale 1.0 —
    restricted work never earns extra compute. Never raises."""
    try:
        _ct = float(os.environ.get("JARVIS_VALUE_TIME_SCALE_COEFF", "0.5"))
    except ValueError:
        _ct = 0.5
    try:
        _cc = float(os.environ.get("JARVIS_VALUE_COST_SCALE_COEFF", "0.5"))
    except ValueError:
        _cc = 0.5
    try:
        _steps = max(0, int(band) - BAND_COSMETIC_CLASS)
    except (TypeError, ValueError):
        _steps = 0
    return (1.0 + _ct * _steps, 1.0 + _cc * _steps)


def value_ceiling_breached(band: int, n_target_files: int) -> bool:
    """Mandate 4: above the adaptive ceiling, HALT for the human. An
    oracle-band op spanning more target files than
    ``JARVIS_VALUE_CEILING_FILES`` (default 4) is real, high-value, and
    high-blast — exactly the exception the human handles. Never raises."""
    try:
        _ceiling = int(os.environ.get("JARVIS_VALUE_CEILING_FILES", "4"))
    except ValueError:
        _ceiling = 4
    try:
        return int(band) >= BAND_ORACLE and int(n_target_files) > _ceiling
    except (TypeError, ValueError):
        return False


def score_ctx(ctx: Any) -> int:
    """Convenience: band for an OperationContext-shaped object (the fields
    every consumer already carries). Never raises."""
    try:
        return score_signal(
            str(getattr(ctx, "signal_source", "") or ""),
            getattr(ctx, "target_files", ()) or (),
            str(getattr(ctx, "intake_evidence_json", "") or ""),
            os.environ.get("JARVIS_PROJECT_ROOT", "") or os.getcwd(),
        )
    except Exception:  # noqa: BLE001
        return BAND_INDETERMINATE
