"""VLM adapter factories for VisionSensor (Task 15) + Visual VERIFY advisory
(Task 19).

Produces callables in the exact shapes those layers expect:

* ``make_sensor_vlm_fn()`` → ``(frame_path: str) -> Dict[str, Any]``
  (Task 15 Tier 2 classifier.)
* ``make_advisory_fn()`` → ``(pre_bytes: bytes, post_bytes: bytes,
  intent: str) -> Dict[str, Any]`` (Task 19 advisory.)

The adapter has two modes controlled by ``JARVIS_VISION_VLM_MODE``:

* ``stub`` (default) — returns a conservative ``verdict=unclear``
  payload without making any network call. Enables the sensor to
  *wire* Tier 2 without spending any budget; graduation arcs can
  exercise the dispatch path before the real provider is plugged in.
* ``doubleword`` — calls the DoubleWord Qwen3-VL-235B endpoint via
  the existing ``DoubleWordProvider`` infrastructure. Only flip to
  this mode once you've accepted the per-call cost + are ready for
  real Slice 2/4 sessions.

The ``doubleword`` branch is left as a named handoff — the exact
provider-call shape depends on the DoubleWord multi-modal API
contract, which lives in ``doubleword_provider.py`` and should be
wired at graduation time rather than speculatively. Until then, the
adapter stays ``stub`` and the sensor still boots cleanly.

Boundary Principle: this module knows NOTHING about the sensor's
policy layer / retention / I8 invariants. It's a pure provider
shim. Everything operator-visible flows through the sensor's own
counters and the advisory ledger.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("Ouroboros.VisionVLMAdapter")


_MODE_ENV = "JARVIS_VISION_VLM_MODE"
_MODE_STUB = "stub"
_MODE_DOUBLEWORD = "doubleword"
_VALID_MODES: frozenset = frozenset({_MODE_STUB, _MODE_DOUBLEWORD})


def _resolve_mode() -> str:
    raw = os.environ.get(_MODE_ENV, _MODE_STUB).strip().lower()
    if raw not in _VALID_MODES:
        logger.debug(
            "[VisionVLMAdapter] unknown %s=%r; falling back to %s",
            _MODE_ENV, raw, _MODE_STUB,
        )
        return _MODE_STUB
    return raw


# ---------------------------------------------------------------------------
# Tier 2 sensor classifier adapter
# ---------------------------------------------------------------------------


def make_sensor_vlm_fn() -> Callable[[str], Dict[str, Any]]:
    """Build the Tier 2 VLM callable for ``VisionSensor``.

    Returned shape: ``(frame_path: str) -> {verdict, confidence,
    model, reasoning}`` matching what
    :meth:`VisionSensor._maybe_run_tier2` consumes.

    Mode = ``stub`` by default: returns ``verdict=unclear,
    confidence=0.0`` so Tier 2 dispatch fires (sensor counts ``tier2_calls``
    + bills the cost ledger) but nothing is forwarded as a signal —
    the sensor's "drop on verdict=ok / low confidence" path applies.
    This keeps the CI path free of network calls + ensures the
    operator sees the Tier 2 wiring *work* before paying real money
    for it.
    """
    mode = _resolve_mode()

    if mode == _MODE_DOUBLEWORD:
        logger.info(
            "[VisionVLMAdapter] Tier 2 mode=doubleword — real Qwen3-VL-235B "
            "calls via DoubleWordProvider. Budget-aware caller required."
        )
        return _build_doubleword_sensor_fn()

    # Stub mode — returns unclear/0.0 (drops without emitting).
    logger.debug(
        "[VisionVLMAdapter] Tier 2 mode=stub — set %s=doubleword for real VLM",
        _MODE_ENV,
    )
    return _stub_sensor_vlm_fn


def _stub_sensor_vlm_fn(frame_path: str) -> Dict[str, Any]:
    """Stub classifier — returns ``verdict=ok`` so no signal emits.

    Per spec §Severity → route, ``ok`` is the only verdict that's
    dropped without routing. The stub uses ``ok`` (not ``unclear``,
    which would still emit at info/BACKGROUND) so the sensor's boot
    wiring is exercised end-to-end WITHOUT polluting the intake
    queue with stub-originated signals.

    Side effects of the dispatch chain still fire:
      * Tier 2 call counter increments,
      * cost ledger debited ($0.005 default),
      * ``tier2_ok_dropped`` stat bumps.

    Operator sees "Tier 2 is wired but nothing real is happening"
    until they flip ``JARVIS_VISION_VLM_MODE=doubleword`` and the
    real impl is plugged in.
    """
    _ = frame_path  # reserved for real impl
    return {
        "verdict": "ok",
        "confidence": 0.0,
        "model": "stub-qwen3-vl-235b",
        "reasoning": "stub adapter — no real VLM call made",
    }


def _build_doubleword_sensor_fn() -> Callable[[str], Dict[str, Any]]:
    """Real DoubleWord Qwen3-VL-235B wrapper for Tier 2 classification.

    Handoff: the exact provider-call shape depends on
    ``DoubleWordProvider.generate()``'s multi-modal input contract,
    which is operator-tuned at Slice 2 graduation time. Until that
    wiring is proven against real billable calls, this factory
    degrades to the stub so boot never fails.
    """
    try:
        # Reserved import — real implementation plugs DoubleWordProvider
        # here with a ``(frame_path) -> dict`` wrapper.
        from backend.core.ouroboros.governance.doubleword_provider import (  # noqa: F401
            DoubleWordProvider,
        )
    except Exception as exc:
        logger.warning(
            "[VisionVLMAdapter] doubleword mode requested but provider "
            "unavailable: %s — degrading to stub",
            exc,
        )
        return _stub_sensor_vlm_fn

    def _real_doubleword_sensor_fn(frame_path: str) -> Dict[str, Any]:
        # TODO(slice-2-graduation): construct the multi-modal request,
        # POST to the DoubleWord endpoint, parse response → verdict dict.
        # The stub path below unblocks boot + CI while the real impl is
        # tuned against billable calls.
        logger.warning(
            "[VisionVLMAdapter] doubleword sensor_fn not yet implemented; "
            "returning stub verdict for frame=%s", frame_path,
        )
        return _stub_sensor_vlm_fn(frame_path)

    return _real_doubleword_sensor_fn


# ---------------------------------------------------------------------------
# Advisory adapter (Slice 4 — Task 19)
# ---------------------------------------------------------------------------


def make_advisory_fn() -> Callable[[bytes, bytes, str], Dict[str, Any]]:
    """Build the advisory callable for ``visual_verify.run_advisory``.

    Returned shape: ``(pre_bytes: bytes, post_bytes: bytes, intent:
    str) -> {verdict, confidence, model, reasoning}`` where
    ``verdict ∈ {aligned, regressed, unclear}``.

    Default is stub ``aligned`` with zero confidence — doesn't trigger
    L2 regardless of threshold. Operator flips to ``doubleword`` at
    Slice 4 graduation.
    """
    mode = _resolve_mode()
    if mode == _MODE_DOUBLEWORD:
        logger.info(
            "[VisionVLMAdapter] Advisory mode=doubleword — real VLM calls",
        )
        return _build_doubleword_advisory_fn()
    logger.debug(
        "[VisionVLMAdapter] Advisory mode=stub — set %s=doubleword for real VLM",
        _MODE_ENV,
    )
    return _stub_advisory_fn


def _stub_advisory_fn(
    pre_bytes: bytes, post_bytes: bytes, intent: str,
) -> Dict[str, Any]:
    """Stub advisory — always ``aligned`` with zero confidence.

    This means:
      * no L2 dispatch (aligned never triggers L2 regardless of threshold),
      * no FP accumulation on disk (aligned isn't counted against the
        regressed-only FP rate that drives auto-demotion).
    Boot wiring is exercised; no real-world cost incurred.
    """
    _ = (pre_bytes, post_bytes, intent)  # reserved for real impl
    return {
        "verdict": "aligned",
        "confidence": 0.0,
        "model": "stub-qwen3-vl-235b",
        "reasoning": "stub adapter — no real VLM call made",
    }


def _build_doubleword_advisory_fn() -> Callable[[bytes, bytes, str], Dict[str, Any]]:
    """Real DoubleWord advisory wrapper.

    Same handoff pattern as the sensor fn — real impl plugs in at
    Slice 4 graduation after the pre/post/intent prompt template has
    been tuned against real billable calls.
    """
    try:
        from backend.core.ouroboros.governance.doubleword_provider import (  # noqa: F401
            DoubleWordProvider,
        )
    except Exception as exc:
        logger.warning(
            "[VisionVLMAdapter] doubleword mode requested but provider "
            "unavailable: %s — degrading to stub",
            exc,
        )
        return _stub_advisory_fn

    def _real_doubleword_advisory_fn(
        pre_bytes: bytes, post_bytes: bytes, intent: str,
    ) -> Dict[str, Any]:
        # TODO(slice-4-graduation): real DoubleWord multi-modal call
        # with pre+post images + intent prompt → verdict dict.
        logger.warning(
            "[VisionVLMAdapter] doubleword advisory_fn not yet implemented; "
            "returning stub for intent=%r", intent[:80],
        )
        return _stub_advisory_fn(pre_bytes, post_bytes, intent)

    return _real_doubleword_advisory_fn
