"""Sovereign Fleet-Wide Hibernation Matrix (2026-06-20).

Catastrophic-outage failsafe for the autonomic Crucible. If the topology sentinel
(entitlement) + TtftObserver (latency) quarantine 100% of the diff-capable DW
models for a route, generation has NOWHERE to go — every soak would burn the full
wall-cap producing nothing while Spot credits drain. This module detects that
**fleet-exhausted** state and drives a **DeepSleep**: the cadence idles for a
mathematically-bounded backoff, then **flushes the ephemeral quarantine and
re-probes** so a recovered DoubleWord API is re-discovered (a transient outage
self-heals; a genuine entitlement block simply re-quarantines — bounded, no spin).

Reuse-first: composes the EXISTING `provider_topology.dw_models_for_route`,
`topology_sentinel` (get_state / reset_all_terminal_breakers), and
`dw_ttft_observer` (is_cold_storage / clear). No new prober, no new state machine.

## Authority posture (locked)
  * **Read-mostly + fail-OPEN** — any error in detection returns "not exhausted"
    (never falsely sleeps the engine). NEVER raises.
  * **Env-tunable** — DeepSleep backoff + enable flag; no hardcoded constants in
    the decision.
"""
from __future__ import annotations

import logging
import os
from typing import Tuple

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_FLEET_EXHAUSTION_FAILSAFE_ENABLED"
_ENV_DEEPSLEEP_S = "JARVIS_FLEET_DEEPSLEEP_S"
_DEFAULT_DEEPSLEEP_S = 2700.0  # 45 min — DW cold-start / outage recovery window


def failsafe_enabled() -> bool:
    """Master gate. Default TRUE — failure-path-only: only acts when 100% of the
    fleet is quarantined, which is exactly when continuing is pure waste. =0
    reverts to the legacy spin-until-budget behavior. NEVER raises."""
    return (os.environ.get(_ENV_ENABLED, "true") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def deepsleep_seconds() -> float:
    """Bounded DeepSleep backoff. Env-tunable; defensive default 45 min. Clamped
    to [60s, 4h] so a typo can't idle forever or busy-spin. NEVER raises."""
    raw = (os.environ.get(_ENV_DEEPSLEEP_S, "") or "").strip()
    try:
        v = float(raw) if raw else _DEFAULT_DEEPSLEEP_S
    except (TypeError, ValueError):
        v = _DEFAULT_DEEPSLEEP_S
    return max(60.0, min(v, 4 * 3600.0))


def _candidate_models(route: str) -> Tuple[str, ...]:
    try:
        from backend.core.ouroboros.governance.provider_topology import (
            get_topology,
        )
        return tuple(get_topology().dw_models_for_route(route) or ())
    except Exception:  # noqa: BLE001
        return ()


def _is_quarantined(model_id: str) -> bool:
    """True iff the model is entitlement-banned (sentinel OPEN/TERMINAL_OPEN) OR
    latency-banned (TtftObserver cold-storage). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.topology_sentinel import (
            get_default_sentinel,
        )
        state = get_default_sentinel().get_state(model_id)
        if state in ("OPEN", "TERMINAL_OPEN"):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.core.ouroboros.governance.dw_discovery_runner import (
            get_ttft_observer,
        )
        obs = get_ttft_observer()
        if obs is not None and obs.is_cold_storage(model_id):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def fleet_exhausted(route: str = "standard") -> bool:
    """True iff EVERY candidate DW model for ``route`` is quarantined (0 available
    for generation). Fail-OPEN: an empty candidate list or any error → False (we
    never sleep the engine on uncertainty). NEVER raises."""
    if not failsafe_enabled():
        return False
    models = _candidate_models(route)
    if not models:
        # No candidate list resolved → cannot prove exhaustion; do not sleep.
        return False
    available = [m for m in models if not _is_quarantined(m)]
    if available:
        return False
    logger.warning(
        "[FleetExhaustion] ALL %d candidate models quarantined for route=%s "
        "(entitlement/latency) — fleet exhausted, DeepSleep advised", len(models), route,
    )
    return True


def flush_ephemeral_quarantine(route: str = "standard") -> int:
    """Wake from DeepSleep: clear the ephemeral quarantine so the fleet re-probes
    fresh. Resets sentinel TERMINAL_OPEN breakers + clears TtftObserver
    cold-storage for the route's candidates. Returns the count cleared. A genuine
    persistent block simply re-quarantines on the next probe (bounded). NEVER
    raises."""
    cleared = 0
    try:
        from backend.core.ouroboros.governance.topology_sentinel import (
            get_default_sentinel,
        )
        cleared += int(get_default_sentinel().reset_all_terminal_breakers() or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[FleetExhaustion] sentinel flush swallowed: %s", exc)
    try:
        from backend.core.ouroboros.governance.dw_discovery_runner import (
            get_ttft_observer,
        )
        obs = get_ttft_observer()
        if obs is not None:
            for m in _candidate_models(route):
                try:
                    obs.clear(m)
                    cleared += 1
                except Exception:  # noqa: BLE001
                    continue
    except Exception as exc:  # noqa: BLE001
        logger.debug("[FleetExhaustion] ttft flush swallowed: %s", exc)
    logger.info("[FleetExhaustion] ephemeral quarantine flushed (%d entries)", cleared)
    return cleared


def _main() -> int:  # pragma: no cover — CLI for crucible_cadence.sh
    """CLI: ``--check`` exits 0 iff fleet-exhausted (cadence then DeepSleeps);
    ``--flush`` clears the quarantine on wake; ``--deepsleep-s`` prints the backoff."""
    import sys
    logging.basicConfig(level=logging.INFO)
    args = set(sys.argv[1:])
    route = os.environ.get("JARVIS_FLEET_EXHAUSTION_ROUTE", "standard")
    if "--deepsleep-s" in args:
        print(int(deepsleep_seconds()))
        return 0
    if "--flush" in args:
        flush_ephemeral_quarantine(route)
        return 0
    if "--check" in args:
        return 0 if fleet_exhausted(route) else 1
    return 2


__all__ = [
    "failsafe_enabled",
    "deepsleep_seconds",
    "fleet_exhausted",
    "flush_ephemeral_quarantine",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
