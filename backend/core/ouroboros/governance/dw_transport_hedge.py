"""Slice 188 Phase 1+2 — the proactive transport-hedge (concurrent supremacy).

The paradigm flip: we do NOT react to or predict DW's RT ruptures — we STRUCTURALLY neutralize
them by racing the fast path (RT stream) against the stable path (batch) concurrently and taking
the winner. A rupture on the fast path simply means batch wins; the op NEVER sees the failure.

Phase 1 — ``hedged_race``: fire both, ``asyncio.wait(FIRST_COMPLETED)``, take the first SUCCESS,
aggressively ``cancel()`` the loser (no orphaned tasks / wasted credits). A fast-path rupture is
swallowed so the stable path can still win.

Phase 2 — ``should_skip_race_for_storm``: the cortex is demoted from safety-net to ECONOMIC
governor. Before racing, consult the forecast; if a platform-wide STORM is imminent, racing the
RT path is pure waste (it will rupture) — so bypass it and go batch-only, saving the credits.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, Optional


def transport_hedge_enabled() -> bool:
    """Master for the proactive hedge. Default **FALSE** (§33.1 — new default-path behavior that
    can double-spend; opt-in per deployment). NEVER raises."""
    return os.environ.get("JARVIS_DW_TRANSPORT_HEDGE_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def hedge_gate_aware_enabled() -> bool:
    """Slice 227 master — the context-aware hedge governor. Default **TRUE**:
    when an op faces the Iron Gate exploration floor, the batch arm is held
    speculative so the tool-using RT arm gets the slot (rupture fallback intact).
    OFF restores the byte-identical legacy FIRST_COMPLETED race. NEVER raises."""
    return os.environ.get(
        "JARVIS_HEDGE_GATE_AWARE_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


def _storm_threshold() -> float:
    try:
        raw = os.environ.get("JARVIS_DW_STORM_SKIP_THRESHOLD", "").strip()
        v = float(raw) if raw else 0.8
        return min(1.0, max(0.0, v))
    except Exception:  # noqa: BLE001
        return 0.8


def should_skip_race_for_storm(storm_probability: float) -> bool:
    """Phase 2 — the cortex as cost-optimizer. If a platform-wide rupture STORM is forecast above
    threshold, don't waste a hedge racing the RT path (it will rupture) — go batch-only. NEVER
    raises."""
    try:
        return float(storm_probability) >= _storm_threshold()
    except Exception:  # noqa: BLE001
        return False


async def hedged_race(
    fast: Callable[[], Awaitable[Any]],
    stable: Callable[[], Awaitable[Any]],
    *,
    is_rupture: Callable[[BaseException], bool] = lambda e: True,
    fast_label: str = "rt",
    stable_label: str = "batch",
    on_outcome: Optional[Callable[[str, bool], None]] = None,
    on_abandoned: Optional[
        Callable[[Optional[BaseException], Optional[BaseException]], None]
    ] = None,
    prefer_fast: bool = False,
) -> Any:
    """Race ``fast`` (RT) against ``stable`` (batch). Return the FIRST successful result; cancel
    the loser aggressively. A ``fast`` failure that ``is_rupture`` returns True for is swallowed so
    ``stable`` can still win. If BOTH fail, the last exception is raised. Cancellation is awaited
    so no orphaned tasks survive.

    ``on_outcome(winner_label, rupture_swallowed)`` is invoked on the winning result so the caller
    can record telemetry — which transport won, and whether an RT rupture was made INVISIBLE by
    the stable path winning (a proactive capital-save). Best-effort: a sink error never breaks the
    race.

    ``on_abandoned(fast_exc, stable_exc)`` (Slice 194) fires when the race dies with NO winner —
    both arms resolved, neither succeeded — passing each arm's captured exception (None for an
    arm that was cancelled / never errored). The caller's triage engine classifies the pair to
    confirm a hard model/endpoint blockage and rotate candidates. Best-effort: a sink error never
    changes the raise behavior; the abandoned race still raises its last exception.

    Slice 227 — ``prefer_fast`` (the context-aware hedge governor). When True, a winning STABLE
    (batch) result does NOT pre-empt the race: it is held in a speculative buffer and the race
    keeps waiting for the FAST (RT) arm. This is the gate-aware mode — the RT arm runs the Venom
    tool loop (exploration), the batch arm does not, so an un-explored batch candidate that
    arrives first would fail the Iron Gate's exploration floor (the live GOAL-001::file-00 layer-3
    bug). The buffered batch is used ONLY if the RT arm then ruptures / fails / yields no success,
    so the hedge's rupture-protection guarantee is fully preserved. ``prefer_fast=False`` (default)
    is byte-identical to the legacy FIRST_COMPLETED race."""
    loop = asyncio.get_event_loop()
    t_fast = loop.create_task(fast())
    t_stable = loop.create_task(stable())
    pending = {t_fast, t_stable}
    last_exc: Optional[BaseException] = None
    fast_exc: Optional[BaseException] = None
    stable_exc: Optional[BaseException] = None
    fast_ruptured = False
    _UNSET = object()
    buffered_stable: Any = _UNSET  # Slice 227 speculative buffer (prefer_fast)

    def _report(winner_label: str) -> None:
        if on_outcome is not None:
            try:
                on_outcome(winner_label, fast_ruptured)
            except Exception:  # noqa: BLE001 — telemetry never breaks the race
                pass

    async def _claim(result: Any, winner_label: str) -> Any:
        # Cancel any still-pending loser + await its unwind, then report + return.
        for other in pending:
            other.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        _report(winner_label)
        return result

    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            # Slice 227 — in prefer_fast mode, process the FAST arm first within a
            # batch where both completed, so a co-completed RT success is taken
            # over a stable success. Legacy (prefer_fast=False) keeps the original
            # arbitrary set iteration → byte-identical.
            done_iter = (
                sorted(done, key=lambda t: 0 if t is t_fast else 1)
                if prefer_fast else done
            )
            for t in done_iter:
                try:
                    result = t.result()
                except asyncio.CancelledError:
                    continue
                except BaseException as exc:  # noqa: BLE001
                    last_exc = exc
                    # Slice 194 — capture per-arm so a dual failure can be triaged.
                    if t is t_fast:
                        fast_exc = exc
                        if is_rupture(exc):
                            fast_ruptured = True
                        # Slice 227 — the RT arm failed. If we're holding a
                        # speculative batch result (prefer_fast), claim it now —
                        # the rupture/failure fallback the hedge exists for.
                        if buffered_stable is not _UNSET:
                            return await _claim(buffered_stable, stable_label)
                        # otherwise wait for the stable arm (still pending)
                        continue
                    else:
                        stable_exc = exc
                    # a fast-path rupture is non-fatal — let the stable path keep racing
                    # (legacy comment preserved); a stable error waits for the other arm.
                    continue
                else:
                    # SUCCESS.
                    if (
                        prefer_fast
                        and t is t_stable
                        and buffered_stable is _UNSET
                        and t_fast in pending
                    ):
                        # Batch won the race, but this op needs the RT arm's
                        # exploration to clear the Iron Gate. Hold batch in the
                        # speculative buffer and keep waiting for RT — do NOT
                        # cancel it. RT success supersedes; RT rupture falls back.
                        buffered_stable = result
                        continue
                    # FIRST usable SUCCESS — cancel the loser + return.
                    return await _claim(result, fast_label if t is t_fast else stable_label)
        # Slice 227 — race drained with no live success. A buffered batch result
        # (RT never produced a success) is still a valid candidate — use it.
        if buffered_stable is not _UNSET:
            _report(stable_label)
            return buffered_stable
        # both finished without a success
        if last_exc is not None:
            # Slice 194 — the race was ABANDONED (no winner). Hand both arms'
            # exceptions to the caller's triage engine before raising. Only
            # fires when at least one arm actually errored — a pure-cancellation
            # unwind (outer shutdown) is not an abandoned race.
            if on_abandoned is not None:
                try:
                    on_abandoned(fast_exc, stable_exc)
                except Exception:  # noqa: BLE001 — triage never changes the raise
                    pass
            raise last_exc
        raise RuntimeError("hedged_race: both transports resolved without a result")
    finally:
        for t in (t_fast, t_stable):
            if not t.done():
                t.cancel()
