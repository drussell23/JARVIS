"""Phase 12 Slice C — Discovery runner: orchestrates fetch + classify
+ ledger registration + dynamic catalog population.

This module is the SHADOW-MODE entry point. It runs catalog discovery
end-to-end:

    1. Fetch DW's /models via DwCatalogClient (Slice A)
    2. Classify the snapshot via DwCatalogClassifier (Slice B)
    3. Register newly-quarantined models with PromotionLedger (Slice B)
    4. Populate ProviderTopology's _DYNAMIC_CATALOG holder (Slice C)
    5. Compute YAML diff (Slice C) and surface diagnostic strings

In shadow mode (Slice C default), step 4's holder is OBSERVATION-ONLY —
the dispatcher continues consuming YAML via dw_models_for_route. Slice
D flips dw_models_for_route to read the holder first.

Authority surface:
  * ``DiscoveryResult`` — structured outcome (success/failure markers,
    diagnostic strings, yaml_diff payload)
  * ``run_discovery(...)`` — async entry point; never raises
  * ``catalog_discovery_enabled()`` — re-read at call time

NEVER raises out of run_discovery. Every failure path produces a
DiscoveryResult with explicit failure_reason populated; the sentinel
preflight surfaces it as a diagnostic, not a failed assertion.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.dw_catalog_classifier import (
    ClassificationOutcome,
    DwCatalogClassifier,
)
from backend.core.ouroboros.governance.dw_catalog_client import (
    CatalogSnapshot,
    DwCatalogClient,
    discovery_enabled as catalog_discovery_enabled,
)
from backend.core.ouroboros.governance.dw_promotion_ledger import (
    PromotionLedger,
)
from backend.core.ouroboros.governance.provider_topology import (
    RouteDiff,
    compute_yaml_diff,
    set_dynamic_catalog,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DiscoveryResult — structured outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryResult:
    """End-to-end outcome of one discovery cycle. Frozen + hashable so
    consumers (sentinel preflight, observability surfaces) can keep
    snapshots without copying.

    ``ok`` is True when fetch succeeded AND classification produced at
    least one route assignment. ``ok=False`` covers fetch failure,
    empty catalog, classifier crash. The dispatcher treats either case
    as 'fall through to YAML' — Slice C never hard-fails the dispatcher
    on a discovery failure.

    ``yaml_diff`` is populated even on fetch failure when the runner
    used a stale-cache snapshot — operators can still audit
    catalog vs YAML. Empty when no snapshot at all.
    """
    ok: bool
    fetched_at_unix: float
    model_count: int
    fetch_failure_reason: Optional[str]
    fetch_latency_ms: int
    newly_quarantined: Tuple[str, ...]
    routes_assigned: Tuple[str, ...]
    yaml_diff: Mapping[str, RouteDiff]
    diagnostic_strings: Tuple[str, ...]
    schema_version: str = "discovery_result.1"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_discovery(
    *,
    session: Any,                       # aiohttp.ClientSession
    base_url: str,
    api_key: str,
    ledger: PromotionLedger,
    cache_path: Optional[Any] = None,   # Path | None
    classifier: Optional[DwCatalogClassifier] = None,
) -> DiscoveryResult:
    """Run one full discovery cycle. NEVER raises.

    The caller (sentinel preflight) supplies the existing aiohttp
    session from DoublewordProvider so connection pooling stays
    consistent. ``ledger`` mutations (newly_quarantined registration)
    happen here — the runner is the side-effect coordinator that
    keeps the classifier itself pure.

    Returns a DiscoveryResult; check ``result.ok`` to know whether
    the holder was populated with a fresh snapshot or whether the
    dispatcher should fall through to YAML.
    """
    diagnostics: list = []

    # Step 1: fetch
    client = DwCatalogClient(
        session=session,
        base_url=base_url,
        api_key=api_key,
        cache_path=cache_path,
    )
    try:
        snapshot = await client.fetch()
    except Exception as exc:  # noqa: BLE001 — fetch() shouldn't raise but defense-in-depth
        logger.warning(
            "[DiscoveryRunner] fetch raised unexpectedly: %s", exc,
        )
        return _failed_result(
            fetch_failure_reason=f"runner_fetch_unhandled:"
                                 f"{type(exc).__name__}:{str(exc)[:80]}",
            diagnostics=("fetch_unhandled",),
        )

    diagnostics.append(
        f"catalog_fetched:models={len(snapshot.models)}:"
        f"latency_ms={snapshot.fetch_latency_ms}"
    )
    if snapshot.fetch_failure_reason:
        diagnostics.append(
            f"catalog_fetch_failed:{snapshot.fetch_failure_reason}"
        )

    # Step 2: classify
    classifier = classifier or DwCatalogClassifier()
    try:
        outcome = classifier.classify(snapshot, ledger)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[DiscoveryRunner] classify raised unexpectedly: %s", exc,
        )
        return DiscoveryResult(
            ok=False,
            fetched_at_unix=snapshot.fetched_at_unix,
            model_count=len(snapshot.models),
            fetch_failure_reason=snapshot.fetch_failure_reason,
            fetch_latency_ms=snapshot.fetch_latency_ms,
            newly_quarantined=(),
            routes_assigned=(),
            yaml_diff={},
            diagnostic_strings=tuple(diagnostics + [
                f"classify_failed:{type(exc).__name__}",
            ]),
        )

    # Step 3: register newly-quarantined models with the ledger
    quarantine_count = 0
    for mid in outcome.newly_quarantined:
        try:
            ledger.register_quarantine(mid)
            quarantine_count += 1
        except Exception:  # noqa: BLE001 — defensive (ledger NEVER raises but DiD)
            logger.debug(
                "[DiscoveryRunner] ledger register_quarantine failed for %s",
                mid, exc_info=True,
            )
    if quarantine_count:
        diagnostics.append(f"newly_quarantined:count={quarantine_count}")

    # Step 4: populate dynamic catalog holder (shadow-mode observation;
    # dispatcher still reads YAML in Slice C)
    assignments_for_holder: Dict[str, Tuple[str, ...]] = {
        route: assn.ranked_model_ids
        for route, assn in outcome.assignments.items()
    }
    routes_assigned = tuple(sorted(
        r for r, ids in assignments_for_holder.items() if ids
    ))
    try:
        set_dynamic_catalog(
            assignments_for_holder,
            fetched_at_unix=snapshot.fetched_at_unix,
            fetch_failure_reason=snapshot.fetch_failure_reason,
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[DiscoveryRunner] set_dynamic_catalog failed", exc_info=True,
        )
        diagnostics.append("dynamic_catalog_set_failed")

    diagnostics.append(
        f"routes_assigned:count={len(routes_assigned)}:{','.join(routes_assigned)}"
    )

    # Step 5: YAML diff for operator review
    try:
        yaml_diff = compute_yaml_diff(
            catalog_assignments=assignments_for_holder,
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[DiscoveryRunner] compute_yaml_diff failed", exc_info=True,
        )
        yaml_diff = {}
    if yaml_diff:
        diagnostics.append(_diff_summary(yaml_diff))

    return DiscoveryResult(
        ok=(snapshot.fetch_failure_reason is None
            and len(snapshot.models) > 0),
        fetched_at_unix=snapshot.fetched_at_unix,
        model_count=len(snapshot.models),
        fetch_failure_reason=snapshot.fetch_failure_reason,
        fetch_latency_ms=snapshot.fetch_latency_ms,
        newly_quarantined=outcome.newly_quarantined,
        routes_assigned=routes_assigned,
        yaml_diff=yaml_diff,
        diagnostic_strings=tuple(diagnostics),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _diff_summary(yaml_diff: Mapping[str, RouteDiff]) -> str:
    """Compact one-line summary of the YAML-vs-catalog diff. Mostly for
    grep-friendly debug.log readability."""
    parts = []
    for route, diff in yaml_diff.items():
        parts.append(
            f"{route}:yaml_only={len(diff.yaml_only)}:"
            f"catalog_only={len(diff.catalog_only)}:"
            f"both={len(diff.both)}"
        )
    return f"yaml_diff[{';'.join(parts)}]"


def _failed_result(
    *,
    fetch_failure_reason: str,
    diagnostics: Tuple[str, ...],
) -> DiscoveryResult:
    """Build a DiscoveryResult for the case where fetch itself raised
    (defense-in-depth — DwCatalogClient.fetch is supposed to never
    raise, but this preserves the invariant if its contract slips)."""
    return DiscoveryResult(
        ok=False,
        fetched_at_unix=0.0,
        model_count=0,
        fetch_failure_reason=fetch_failure_reason,
        fetch_latency_ms=0,
        newly_quarantined=(),
        routes_assigned=(),
        yaml_diff={},
        diagnostic_strings=diagnostics,
    )


# ---------------------------------------------------------------------------
# Phase 12 Slice E — Boot-time hook + periodic refresh loop
# ---------------------------------------------------------------------------
#
# The dispatcher (candidate_generator._dispatch_via_sentinel) calls
# ``boot_discovery_once`` on every dispatch when discovery is enabled.
# The first call:
#   1. Acquires the boot lock
#   2. Ensures the singleton ledger is hydrated from disk
#   3. Awaits one full discovery cycle inline (~200ms typical) so the
#      catalog holder is populated BEFORE the dispatcher's first
#      cascade walks dw_models_for_route
#   4. Spawns a background refresh task on JARVIS_DW_CATALOG_REFRESH_S
#      cadence (default 1800s = 30min)
# Subsequent calls in the same process see the boot flag set and
# return immediately — idempotent, hot-path-safe.
#
# When discovery is OFF, ``boot_discovery_once`` is a no-op — Phase 12
# graduation hot-revert path stays clean.
#
# The refresh loop NEVER raises out of an iteration; every cycle's
# exceptions are caught + logged + the next refresh fires on schedule.
# Operator can hot-revert via JARVIS_DW_CATALOG_DISCOVERY_ENABLED=false
# at runtime; the loop checks the flag each cycle and skips the fetch
# when off (it does NOT cancel itself — staying alive lets a re-flip
# pick up immediately without process restart).

import asyncio
import threading

from backend.core.ouroboros.governance.dw_catalog_client import (
    _refresh_interval_s as _refresh_interval_s_internal,
)


_BOOT_DISCOVERY_LOCK = asyncio.Lock()
_BOOT_DISCOVERY_DONE: bool = False
_REFRESH_TASK: Optional[asyncio.Task] = None
_LEDGER_SINGLETON: Optional[PromotionLedger] = None
# Sync lock around the singleton hydration + boot flag — protects the
# very first-call window before the asyncio.Lock has been touched.
_BOOT_SYNC_LOCK = threading.Lock()


def _get_or_create_ledger() -> PromotionLedger:
    """Lazy singleton — hydrates from disk on first access."""
    global _LEDGER_SINGLETON
    with _BOOT_SYNC_LOCK:
        if _LEDGER_SINGLETON is None:
            led = PromotionLedger()
            led.load()
            _LEDGER_SINGLETON = led
        return _LEDGER_SINGLETON


def reset_boot_state_for_tests() -> None:
    """Test hook — clears the boot flag, cancels any refresh task,
    drops the ledger singleton. Production code MUST NOT call this."""
    global _BOOT_DISCOVERY_DONE, _REFRESH_TASK, _LEDGER_SINGLETON
    with _BOOT_SYNC_LOCK:
        _BOOT_DISCOVERY_DONE = False
        if _REFRESH_TASK is not None and not _REFRESH_TASK.done():
            _REFRESH_TASK.cancel()
        _REFRESH_TASK = None
        _LEDGER_SINGLETON = None


async def boot_discovery_once(
    *,
    session: Any,
    base_url: str,
    api_key: str,
) -> Optional[DiscoveryResult]:
    """Fire-once boot hook. Idempotent: subsequent calls in the same
    process see the boot flag set and return None immediately
    (hot-path-safe).

    The first call:
      * runs one full discovery cycle inline (caller awaits)
      * populates the dynamic catalog holder
      * spawns the periodic refresh task

    NEVER raises. Discovery off → returns None. Discovery enabled but
    fetch failed → returns the failure-marked DiscoveryResult; refresh
    task is still spawned so a recovering DW endpoint gets re-tried."""
    global _BOOT_DISCOVERY_DONE, _REFRESH_TASK
    if not catalog_discovery_enabled():
        return None
    # Fast-path no-op when already booted (avoids acquiring the lock
    # on every dispatch).
    if _BOOT_DISCOVERY_DONE:
        return None
    async with _BOOT_DISCOVERY_LOCK:
        if _BOOT_DISCOVERY_DONE:
            return None
        ledger = _get_or_create_ledger()
        first_result = await run_discovery(
            session=session,
            base_url=base_url,
            api_key=api_key,
            ledger=ledger,
        )
        _BOOT_DISCOVERY_DONE = True
        # Spawn the refresh loop. We DON'T await; it runs forever
        # until cancellation. Capture in module-level for shutdown.
        try:
            _REFRESH_TASK = asyncio.create_task(
                _discovery_refresh_loop(
                    session=session,
                    base_url=base_url,
                    api_key=api_key,
                    ledger=ledger,
                ),
                name="dw_discovery_refresh_loop",
            )
        except RuntimeError:
            # No running loop → caller is sync-only; refresh disabled.
            # Boot still counts as done; operator can re-enable via
            # reset_boot_state_for_tests + re-call from async ctx.
            logger.debug(
                "[DiscoveryRunner] no running loop — refresh skipped",
            )
        logger.info(
            "[DiscoveryRunner] boot complete: ok=%s models=%d "
            "newly_quarantined=%d routes_assigned=%s",
            first_result.ok, first_result.model_count,
            len(first_result.newly_quarantined),
            list(first_result.routes_assigned),
        )
        return first_result


async def _discovery_refresh_loop(
    *,
    session: Any,
    base_url: str,
    api_key: str,
    ledger: PromotionLedger,
) -> None:
    """Periodic refresh. Each cycle:
      1. Sleeps for JARVIS_DW_CATALOG_REFRESH_S (default 1800s)
      2. Re-checks discovery flag (operator may have flipped off)
      3. Runs a full discovery cycle
      4. NEVER raises — all exceptions caught + logged

    Loop survives forever until task cancellation (which happens on
    process shutdown via reset_boot_state_for_tests or natural
    asyncio cleanup)."""
    while True:
        try:
            await asyncio.sleep(_refresh_interval_s_internal())
        except asyncio.CancelledError:
            return
        if not catalog_discovery_enabled():
            # Hot-revert in flight — skip the fetch but stay alive
            # so a re-flip picks up immediately.
            continue
        try:
            await run_discovery(
                session=session,
                base_url=base_url,
                api_key=api_key,
                ledger=ledger,
            )
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 — defensive
            logger.exception(
                "[DiscoveryRunner] refresh cycle failed; "
                "next cycle will retry on schedule",
            )


__all__ = [
    "DiscoveryResult",
    "boot_discovery_once",
    "catalog_discovery_enabled",
    "reset_boot_state_for_tests",
    "run_discovery",
]
