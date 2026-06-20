"""Autonomic Repair Sentinel — background self-verification of DW O+V health.

On idle cycles, runs a small (mutated, anti-overfit) slice of the repair battery
against the monitored DW coder(s) and records each pass/fail into the EXISTING
``FleetCalibrationStore`` (``record_probe(kind="code", code_pass=...)``). That
feeds the SAME EWMA → ``fleet_rerank`` → ``graduation_ready`` machinery the
FleetEvaluator already owns — so if DeepSeek-V4-Pro starts failing synthetic
repairs in the background, its ``ast_pass_rate`` EWMA drops and it is
auto-deprioritised in routing BEFORE it fails a live op. No new demotion logic:
the sentinel is a new SIGNAL SOURCE into the existing calibration brain.

Gated (``JARVIS_FLEET_SENTINEL_ENABLED`` default OFF — opt-in: it spends real DW
budget), idle-gated, and cost-capped via the shared store spend ledger. NEVER
raises into the caller. Reuses fleet_repair_battery (cycle) + fleet_repair_mutator
(variants) + fleet_evaluator (cost knobs) — zero duplication.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional, Sequence

from backend.core.ouroboros.governance.fleet_repair_battery import (
    BATTERY,
    DEFAULT_MODELS,
    Defect,
    repair_one,
)
from backend.core.ouroboros.governance.fleet_repair_mutator import mutate
from backend.core.ouroboros.governance.fleet_calibration_store import (
    FleetCalibrationStore,
)
from backend.core.ouroboros.governance.fleet_evaluator import (
    _daily_usd_cap,
    _estimate_probe_usd,
)

logger = logging.getLogger(__name__)
_TRUE = {"1", "true", "yes", "on"}


def repair_sentinel_enabled() -> bool:
    return os.environ.get("JARVIS_FLEET_SENTINEL_ENABLED", "").strip().lower() in _TRUE


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default


def _max_defects_per_cycle() -> int:
    return _int_env("JARVIS_FLEET_SENTINEL_DEFECTS_PER_CYCLE", 3)


def _probe_max_tokens() -> int:
    return _int_env("JARVIS_FLEET_SENTINEL_MAX_TOKENS", 1024)


class RepairSentinel:
    """Idle-driven DW O+V self-verification. All collaborators injectable."""

    def __init__(
        self,
        *,
        model_caller: Callable[..., Awaitable[Any]],
        store: Optional[FleetCalibrationStore] = None,
        idle_check: Optional[Callable[[], bool]] = None,
        clock: Optional[Callable[[], float]] = None,
        monitored_models: Sequence[str] = DEFAULT_MODELS,
        defects: Sequence[Defect] = BATTERY,
        seed_source: Optional[Callable[[], int]] = None,
        emit: Optional[Callable[..., None]] = None,
    ) -> None:
        self.model_caller = model_caller
        self.store = store or FleetCalibrationStore()
        self.idle_check = idle_check or (lambda: True)
        self.clock = clock or time.time
        self.monitored_models = tuple(monitored_models)
        self.defects = tuple(defects)
        # Default seed rotates with the clock so variants differ each cycle.
        self.seed_source = seed_source or (lambda: int(self.clock()) & 0x7FFFFFFF)
        self._emit_fn = emit
        self._cursor = 0  # rotates which defects + model are probed each cycle

    async def maybe_run_sentinel(self, *, now: float) -> int:
        """Idle entry. Returns #defects probed this cycle (0 if skipped).
        NEVER raises."""
        try:
            if not repair_sentinel_enabled():
                return 0
            if not self.idle_check():
                return 0
            cap = _daily_usd_cap()
            if cap > 0.0 and self.store.spend_today(now) >= cap:
                logger.info(
                    "[RepairSentinel] daily cap reached ($%.4f>=$%.2f) — skip",
                    self.store.spend_today(now), cap,
                )
                return 0
            if not self.monitored_models or not self.defects:
                return 0
            # Rotate: one model + a window of defects per cycle (bounds cost).
            model = self.monitored_models[self._cursor % len(self.monitored_models)]
            n = max(1, _max_defects_per_cycle())
            start = self._cursor % len(self.defects)
            window = [self.defects[(start + i) % len(self.defects)] for i in range(min(n, len(self.defects)))]
            self._cursor += 1
            seed = int(self.seed_source())
            probed = 0
            for defect in window:
                variant = mutate(defect, seed=seed)
                res = await repair_one(
                    self.model_caller, variant,
                    models=(model,), max_tokens=_probe_max_tokens(),
                )
                tok_per_s = res.completion_tokens / max(res.seconds, 1e-3)
                self.store.record_probe(
                    model,
                    kind="code",
                    code_pass=res.applied,
                    ttft_ms=res.seconds * 1000.0,
                    tok_per_s=tok_per_s,
                    now=now,
                )
                self.store.add_spend(
                    _estimate_probe_usd(res.completion_tokens), now=now,
                )
                probed += 1
                logger.info(
                    "[RepairSentinel] model=%s defect=%s applied=%s %.1fs (%s)",
                    model, variant.name, res.applied, res.seconds, res.note,
                )
                self._emit("fleet_calibrated", {
                    "source": "repair_sentinel",
                    "model_id": model,
                    "defect": variant.name,
                    "applied": res.applied,
                    "seconds": round(res.seconds, 2),
                })
            try:
                self.store.save()
            except Exception:  # noqa: BLE001
                pass
            return probed
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RepairSentinel] cycle skipped: %s", exc)
            return 0

    def _emit(self, event_type: str, payload: dict) -> None:
        """Best-effort SSE (reuses fleet_calibrated event type). NEVER raises."""
        try:
            if self._emit_fn is not None:
                self._emit_fn(event_type, payload)
                return
            from backend.core.ouroboros.governance.ide_observability_stream import (
                get_default_broker,
            )
            broker = get_default_broker()
            if broker is not None:
                broker.publish(event_type, f"sentinel-{event_type}", payload)
        except Exception:  # noqa: BLE001
            pass
