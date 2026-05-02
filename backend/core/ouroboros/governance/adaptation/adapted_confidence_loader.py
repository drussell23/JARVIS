"""Gap #2 Slice 3 — Adapted-confidence-thresholds boot-time loader.

Bridges operator-approved adaptation proposals (from the
``CONFIDENCE_MONITOR_THRESHOLDS`` surface added in Slice 2) into
the live ``ConfidenceMonitor`` evaluation path. Reads
``.jarvis/adapted_confidence_thresholds.yaml`` at process boot
and exposes per-knob accessors that the existing
``confidence_monitor`` accessors consult **only when the env knob
is unset** (operator env-explicit always wins over adapted YAML).

## Precedence (load-bearing)

For every threshold knob:

  ``operator env explicit  >  adapted YAML  >  hardcoded default``

Operator override is always sovereign — they can hot-revert any
graduated tightening by exporting the env knob to the looser
value. The cage's universal monotonic-tightening rule does NOT
apply to env knobs (those are operator-direct; only proposals
routed through ``AdaptationLedger`` are gated). This is by design:
the cage prevents the *AI* from loosening the gate, not the
operator.

## Defense-in-depth: tighten-only filter

Even though Slice 2's surface validator only persists tightening
proposals, the YAML file is plain-text on disk and could be
hand-edited. The loader runs a per-knob tighten check vs. the
**hardcoded baseline** before returning any value:

  ============================  ==================================
   knob                          accept iff
  ============================  ==================================
   ``floor``                      proposed >= baseline (raise only)
   ``window_k``                   proposed <= baseline (shrink only)
   ``approaching_factor``         proposed >= baseline (widen only)
   ``enforce``                    proposed is True   (enable only)
  ============================  ==================================

A loosening value in the YAML is silently dropped (logged at
WARNING) — the loader returns ``None`` for that knob and the
consumer falls through to the hardcoded default. Prevents a
manually-edited YAML file from quietly weakening the gate.

## Schema

``.jarvis/adapted_confidence_thresholds.yaml``::

    schema_version: 1
    proposal_id: "conf-..."
    approved_at: "2026-..."
    approved_by: "alice"
    thresholds:
      floor: 0.10
      window_k: 8
      approaching_factor: 2.0
      enforce: true

All four keys under ``thresholds`` are OPTIONAL — a proposal that
only moved the floor materializes a YAML with just
``thresholds: {floor: 0.10}``; the other three remain at the
hardcoded baseline. This matches the ``compute_policy_diff``
contract from Slice 1 (per-dimension classification, not
all-or-nothing).

## Default-off

``JARVIS_CONFIDENCE_LOAD_ADAPTED`` (default ``false`` until Slice
5 graduation). When off, every accessor returns ``None`` and the
``confidence_monitor`` accessors behave byte-identically to the
pre-Slice-3 baseline.

## Authority surface

  * Imports stdlib + ``adaptation.ledger`` (schema-version pin
    only; no propose/approve calls). PyYAML imported lazily inside
    ``load_adapted_thresholds`` so the import surface stays clean
    when the YAML path doesn't exist.
  * Reads ``.jarvis/adapted_confidence_thresholds.yaml`` only.
    No other I/O, no shell-out, no env mutation, no network.
  * MUST NOT import: ``confidence_monitor`` (one-way: monitor
    consumes loader, never the reverse — prevents circular import
    + keeps the loader testable in isolation), orchestrator,
    iron_gate, policy, risk_engine, change_engine, tool_executor,
    providers, candidate_generator, semantic_guardian,
    semantic_firewall, scoped_tool_backend, subagent_scheduler,
    confidence_policy (substrate is upstream of the loader; the
    loader doesn't need its decision rule because the YAML has
    already been ratified by Slice 2's validator before
    materialization).
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# Hardcoded baselines (mirror confidence_monitor.py — pinned by
# `tests/governance/test_adapted_confidence_loader.py::test_baselines
# _match_confidence_monitor_defaults` so drift is structurally
# impossible). Duplicated rather than imported to avoid a circular
# import (confidence_monitor depends on this module).
# ---------------------------------------------------------------------------


_BASELINE_FLOOR: float = 0.05
_BASELINE_WINDOW_K: int = 16
_BASELINE_APPROACHING_FACTOR: float = 1.5
_BASELINE_ENFORCE: bool = False
_MIN_APPROACHING_FACTOR: float = 1.0  # mirrors confidence_monitor


# Hard cap on YAML file size — defense against a corrupted /
# attacker-supplied file inflating memory at boot. 64 KiB is
# generous: a fully-populated record with comments fits in <2 KiB.
MAX_YAML_BYTES: int = 64 * 1024

_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag + path resolution
# ---------------------------------------------------------------------------


def is_loader_enabled() -> bool:
    """``JARVIS_CONFIDENCE_LOAD_ADAPTED`` (default ``false`` until
    Slice 5 graduation). Empty / unset / whitespace = default.
    NEVER raises."""
    try:
        return os.environ.get(
            "JARVIS_CONFIDENCE_LOAD_ADAPTED", "",
        ).strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001 — defensive
        return False


def adapted_thresholds_path() -> Path:
    """Return the YAML path. Env-overridable via
    ``JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH``; defaults to
    ``.jarvis/adapted_confidence_thresholds.yaml`` under cwd.
    NEVER raises."""
    try:
        raw = os.environ.get(
            "JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH",
        )
        if raw:
            return Path(raw)
    except Exception:  # noqa: BLE001 — defensive
        pass
    return Path(".jarvis") / "adapted_confidence_thresholds.yaml"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdaptedConfidenceThresholds:
    """Loaded + tighten-filtered adapted thresholds. Each knob is
    ``None`` when the YAML didn't supply it OR the supplied value
    failed the tighten-only filter (logged at WARNING)."""

    floor: Optional[float] = None
    window_k: Optional[int] = None
    approaching_factor: Optional[float] = None
    enforce: Optional[bool] = None
    proposal_id: str = ""
    approved_at: str = ""
    approved_by: str = ""
    schema_version: int = ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "floor": self.floor,
            "window_k": self.window_k,
            "approaching_factor": self.approaching_factor,
            "enforce": self.enforce,
            "proposal_id": self.proposal_id,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
            "schema_version": self.schema_version,
        }

    def is_empty(self) -> bool:
        """True when every knob is None — equivalent to no
        adaptation in effect."""
        return (
            self.floor is None
            and self.window_k is None
            and self.approaching_factor is None
            and self.enforce is None
        )


_EMPTY = AdaptedConfidenceThresholds()


# ---------------------------------------------------------------------------
# Per-knob tighten-only filter (defense-in-depth)
# ---------------------------------------------------------------------------


def _filter_floor(raw: Any) -> Optional[float]:
    """Accept iff numeric, finite, in [0.0, 1.0], and >= baseline.
    NEVER raises."""
    try:
        v = float(raw)
        if not math.isfinite(v):
            return None
        if v < 0.0 or v > 1.0:
            logger.warning(
                "[AdaptedConfidenceLoader] floor=%g outside "
                "[0.0,1.0] — dropped", v,
            )
            return None
        if v < _BASELINE_FLOOR:
            logger.warning(
                "[AdaptedConfidenceLoader] floor=%g below baseline "
                "%g — would loosen, dropped",
                v, _BASELINE_FLOOR,
            )
            return None
        return v
    except (TypeError, ValueError):
        return None


def _filter_window_k(raw: Any) -> Optional[int]:
    """Accept iff int, >= 1, and <= baseline. NEVER raises."""
    try:
        v = int(raw)
        if v < 1:
            logger.warning(
                "[AdaptedConfidenceLoader] window_k=%d below 1 — "
                "dropped", v,
            )
            return None
        if v > _BASELINE_WINDOW_K:
            logger.warning(
                "[AdaptedConfidenceLoader] window_k=%d above "
                "baseline %d — would loosen, dropped",
                v, _BASELINE_WINDOW_K,
            )
            return None
        return v
    except (TypeError, ValueError):
        return None


def _filter_approaching_factor(raw: Any) -> Optional[float]:
    """Accept iff numeric, finite, >= 1.0, and >= baseline.
    NEVER raises."""
    try:
        v = float(raw)
        if not math.isfinite(v):
            return None
        if v < _MIN_APPROACHING_FACTOR:
            logger.warning(
                "[AdaptedConfidenceLoader] approaching_factor=%g "
                "below %g — dropped",
                v, _MIN_APPROACHING_FACTOR,
            )
            return None
        if v < _BASELINE_APPROACHING_FACTOR:
            logger.warning(
                "[AdaptedConfidenceLoader] approaching_factor=%g "
                "below baseline %g — would loosen, dropped",
                v, _BASELINE_APPROACHING_FACTOR,
            )
            return None
        return v
    except (TypeError, ValueError):
        return None


def _filter_enforce(raw: Any) -> Optional[bool]:
    """Accept iff value is the literal ``True`` (enable-only).
    A YAML ``false`` is dropped because baseline is ``False`` —
    the YAML can never *enable* a loosening from the baseline,
    but ``True`` is the meaningful tightening direction. NEVER
    raises."""
    if raw is True:
        return True
    if raw is False:
        # Silently None — matches baseline, so no-op materialization
        return None
    # Any other shape (string, int, etc.) — strict reject
    return None


# ---------------------------------------------------------------------------
# Public: load_adapted_thresholds
# ---------------------------------------------------------------------------


def load_adapted_thresholds(
    yaml_path: Optional[Path] = None,
) -> AdaptedConfidenceThresholds:
    """Read the adapted-thresholds YAML and return a frozen
    ``AdaptedConfidenceThresholds`` record with each knob already
    tighten-filtered. Returns ``_EMPTY`` (every knob ``None``) on:

      * Master flag off
      * YAML file missing
      * File exceeds ``MAX_YAML_BYTES``
      * Read fails
      * PyYAML unavailable
      * Parse fails
      * Top-level not a mapping
      * ``schema_version`` mismatch
      * ``thresholds`` key missing or not a mapping

    Per-knob values fall through to ``None`` when the tighten-
    only filter rejects them (logged at WARNING). The loader
    NEVER raises into the caller — every error path returns
    ``_EMPTY`` or a partially-populated record."""
    try:
        if not is_loader_enabled():
            return _EMPTY

        path = (
            yaml_path if yaml_path is not None
            else adapted_thresholds_path()
        )
        if not path.exists():
            logger.debug(
                "[AdaptedConfidenceLoader] no adapted-thresholds "
                "yaml at %s — returning empty",
                path,
            )
            return _EMPTY

        try:
            size = path.stat().st_size
        except OSError as exc:
            logger.warning(
                "[AdaptedConfidenceLoader] stat failed for %s: %s",
                path, exc,
            )
            return _EMPTY
        if size > MAX_YAML_BYTES:
            logger.warning(
                "[AdaptedConfidenceLoader] %s exceeds "
                "MAX_YAML_BYTES=%d (was %d) — refusing to load",
                path, MAX_YAML_BYTES, size,
            )
            return _EMPTY

        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "[AdaptedConfidenceLoader] read failed for %s: %s",
                path, exc,
            )
            return _EMPTY
        if not raw_text.strip():
            return _EMPTY

        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            logger.warning(
                "[AdaptedConfidenceLoader] PyYAML not available "
                "— cannot load adapted thresholds",
            )
            return _EMPTY

        try:
            doc = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            logger.warning(
                "[AdaptedConfidenceLoader] YAML parse failed at "
                "%s: %s", path, exc,
            )
            return _EMPTY

        if not isinstance(doc, dict):
            logger.warning(
                "[AdaptedConfidenceLoader] %s top-level is not a "
                "mapping — skip", path,
            )
            return _EMPTY

        try:
            schema_v = int(doc.get("schema_version") or 0)
        except (TypeError, ValueError):
            schema_v = 0
        if schema_v != ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION:
            logger.warning(
                "[AdaptedConfidenceLoader] schema_version=%s "
                "!= %d — skip",
                schema_v, ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION,
            )
            return _EMPTY

        thresholds_raw = doc.get("thresholds")
        if not isinstance(thresholds_raw, dict):
            logger.warning(
                "[AdaptedConfidenceLoader] thresholds key missing "
                "or not a mapping — skip",
            )
            return _EMPTY

        floor_v = (
            _filter_floor(thresholds_raw["floor"])
            if "floor" in thresholds_raw
            else None
        )
        window_v = (
            _filter_window_k(thresholds_raw["window_k"])
            if "window_k" in thresholds_raw
            else None
        )
        approaching_v = (
            _filter_approaching_factor(
                thresholds_raw["approaching_factor"],
            )
            if "approaching_factor" in thresholds_raw
            else None
        )
        enforce_v = (
            _filter_enforce(thresholds_raw["enforce"])
            if "enforce" in thresholds_raw
            else None
        )

        result = AdaptedConfidenceThresholds(
            floor=floor_v,
            window_k=window_v,
            approaching_factor=approaching_v,
            enforce=enforce_v,
            proposal_id=str(doc.get("proposal_id") or ""),
            approved_at=str(doc.get("approved_at") or ""),
            approved_by=str(doc.get("approved_by") or ""),
        )

        if not result.is_empty():
            logger.info(
                "[AdaptedConfidenceLoader] loaded adapted "
                "thresholds from %s: %s",
                path, result.to_dict(),
            )
        return result
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[AdaptedConfidenceLoader] load_adapted_thresholds "
            "raised: %s", exc,
        )
        return _EMPTY


# ---------------------------------------------------------------------------
# Per-knob accessors (consumed by confidence_monitor)
# ---------------------------------------------------------------------------


def adapted_floor() -> Optional[float]:
    """``None`` when no adapted floor in effect; otherwise the
    tighten-filtered value. NEVER raises."""
    return load_adapted_thresholds().floor


def adapted_window_k() -> Optional[int]:
    """``None`` when no adapted window_k in effect; otherwise the
    tighten-filtered value. NEVER raises."""
    return load_adapted_thresholds().window_k


def adapted_approaching_factor() -> Optional[float]:
    """``None`` when no adapted approaching_factor in effect;
    otherwise the tighten-filtered value. NEVER raises."""
    return load_adapted_thresholds().approaching_factor


def adapted_enforce() -> Optional[bool]:
    """``None`` when no adapted enforce in effect; otherwise the
    tighten-filtered value (only ``True`` materializes — see
    ``_filter_enforce``). NEVER raises."""
    return load_adapted_thresholds().enforce


# ---------------------------------------------------------------------------
# Baseline accessors (test-only — pinned against confidence_monitor)
# ---------------------------------------------------------------------------


def baseline_floor() -> float:
    """Baseline floor — pinned to ``confidence_monitor._DEFAULT_FLOOR``
    by ``test_baselines_match_confidence_monitor_defaults``."""
    return _BASELINE_FLOOR


def baseline_window_k() -> int:
    """Baseline window_k — pinned to ``confidence_monitor._DEFAULT_WINDOW_K``."""
    return _BASELINE_WINDOW_K


def baseline_approaching_factor() -> float:
    """Baseline approaching_factor — pinned to
    ``confidence_monitor._DEFAULT_APPROACHING_FACTOR``."""
    return _BASELINE_APPROACHING_FACTOR


def baseline_enforce() -> bool:
    """Baseline enforce — pinned to the default ``confidence_monitor_enforce``
    behavior when env unset."""
    return _BASELINE_ENFORCE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "ADAPTED_CONFIDENCE_LOADER_SCHEMA_VERSION",
    "AdaptedConfidenceThresholds",
    "MAX_YAML_BYTES",
    "adapted_approaching_factor",
    "adapted_enforce",
    "adapted_floor",
    "adapted_thresholds_path",
    "adapted_window_k",
    "baseline_approaching_factor",
    "baseline_enforce",
    "baseline_floor",
    "baseline_window_k",
    "is_loader_enabled",
    "load_adapted_thresholds",
]
