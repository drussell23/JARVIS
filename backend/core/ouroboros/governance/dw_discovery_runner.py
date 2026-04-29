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
    modality_ledger: Optional[Any] = None,  # Slice G — ModalityLedger
    ttft_observer: Optional[Any] = None,    # Slice 12.2.C — TtftObserver
) -> DiscoveryResult:
    """Run one full discovery cycle. NEVER raises.

    The caller (sentinel preflight) supplies the existing aiohttp
    session from DoublewordProvider so connection pooling stays
    consistent. ``ledger`` mutations (newly_quarantined registration)
    happen here — the runner is the side-effect coordinator that
    keeps the classifier itself pure.

    Phase 12 Slice G — when ``modality_ledger`` is provided AND
    ``JARVIS_DW_MODALITY_VERIFICATION_ENABLED=true``, the runner
    invokes ``verify_catalog_modalities`` after fetch (parses metadata
    + fires micro-probes for ambiguous models), then passes the
    ledger to the classifier as a HARD GATE excluding NON_CHAT
    models from generative routes. modality_ledger=None preserves
    legacy classifier behavior — no modality filtering.

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

    # Step 1.4: Slice 3a — active topology recovery.
    # The catalog GET /models *is* the lightweight reachability probe
    # the architectural directive calls for: same auth, same session,
    # same endpoint, zero new infrastructure. When fetch succeeds AND
    # returns a populated model list, lift transient blocks via the
    # sentinel's apply_health_probe_result() helper. The 30-min refresh
    # cadence (JARVIS_DW_CATALOG_REFRESH_S) doubles as the recovery
    # cadence — operator-tunable; not hardcoded.
    if (
        snapshot.fetch_failure_reason is None
        and len(snapshot.models) > 0
    ):
        try:
            from backend.core.ouroboros.governance.topology_sentinel import (
                get_default_sentinel as _get_sent,
                topology_active_recovery_enabled as _ar_enabled,
            )
            if _ar_enabled():
                _recovered = _get_sent().apply_health_probe_result(
                    success=True,
                )
                if _recovered:
                    diagnostics.append(
                        f"topology_active_recovery:transitions={_recovered}"
                    )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[DiscoveryRunner] active topology recovery failed",
                exc_info=True,
            )

    # Step 1.5: Slice G — modality verification (metadata + probes)
    # Runs BEFORE classify so the ledger is populated with verdicts
    # the classifier can consult. Gated by master flag — when off,
    # modality_ledger stays empty and the classifier behaves legacy.
    if modality_ledger is not None:
        try:
            from backend.core.ouroboros.governance.dw_modality_ledger import (
                modality_verification_enabled,
            )
            from backend.core.ouroboros.governance.dw_modality_probe import (
                verify_catalog_modalities,
            )
        except ImportError as exc:
            logger.debug(
                "[DiscoveryRunner] modality module import failed: %s", exc,
            )
            modality_verification_enabled = lambda: False  # noqa: E731
            verify_catalog_modalities = None
        if (
            verify_catalog_modalities is not None
            and modality_verification_enabled()
        ):
            try:
                # Catalog snapshot id = stable hash of model_id set so
                # ledger verdicts invalidate when DW catalog changes
                _snapshot_id = _compute_snapshot_id(snapshot)

                # Phase 12 Slice H — when the snapshot id changes,
                # reset TERMINAL_OPEN breakers in the sentinel. DW
                # may have replaced/renamed models under the same id;
                # terminal verdicts deserve a fresh chance under the
                # new snapshot. The modality ledger handles whether
                # to re-classify on the next probe.
                if _snapshot_id and _snapshot_id != _last_snapshot_id():
                    _set_last_snapshot_id(_snapshot_id)
                    try:
                        from backend.core.ouroboros.governance.topology_sentinel import (  # noqa: E501
                            get_default_sentinel as _get_sent,
                        )
                        _reset = _get_sent().reset_all_terminal_breakers()
                        if _reset:
                            diagnostics.append(
                                f"terminal_breakers_reset:count={_reset}:"
                                f"new_snapshot={_snapshot_id[:12]}"
                            )
                    except Exception:  # noqa: BLE001 — defensive
                        logger.debug(
                            "[DiscoveryRunner] terminal breaker reset failed",
                            exc_info=True,
                        )

                _verify = await verify_catalog_modalities(
                    snapshot=snapshot,
                    ledger=modality_ledger,
                    session=session,
                    base_url=base_url,
                    api_key=api_key,
                    catalog_snapshot_id=_snapshot_id,
                )
                diagnostics.append(
                    f"modality_verify:metadata={_verify.metadata_verdicts}:"
                    f"probes={_verify.probes_fired}:"
                    f"chat_capable={_verify.probes_succeeded}:"
                    f"non_chat={_verify.probes_rejected}:"
                    f"unknown={_verify.probes_inconclusive}:"
                    f"skipped={_verify.skipped_already_known}:"
                    f"latency_ms={_verify.duration_ms}"
                )
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[DiscoveryRunner] modality verification failed",
                    exc_info=True,
                )
                diagnostics.append("modality_verify_failed")

    # Step 2: classify
    classifier = classifier or DwCatalogClassifier()
    try:
        outcome = classifier.classify(
            snapshot, ledger,
            modality_ledger=modality_ledger,
            ttft_observer=ttft_observer,
        )
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


# Phase 12 Slice H — track last seen catalog snapshot id so the runner
# can detect catalog refresh and reset TERMINAL_OPEN breakers. Module-
# level (process-lifetime) state; cleared by reset_boot_state_for_tests.
_LAST_SNAPSHOT_ID: str = ""


def _last_snapshot_id() -> str:
    return _LAST_SNAPSHOT_ID


def _set_last_snapshot_id(value: str) -> None:
    global _LAST_SNAPSHOT_ID
    _LAST_SNAPSHOT_ID = value or ""


def _compute_snapshot_id(snapshot: Any) -> str:
    """Stable id for a catalog snapshot — used to invalidate stale
    modality ledger verdicts on catalog refresh. Hashes the sorted
    model_id list so a re-fetch with identical contents produces the
    same id (no spurious invalidation), but a model add/remove flips
    the id and triggers ledger reset for that model.

    Returns first 16 chars of sha256 hex (collision-tolerant — false
    invalidation just means re-running the modality probe). NEVER
    raises."""
    import hashlib
    try:
        ids = sorted(m.model_id for m in getattr(snapshot, "models", ()))
        joined = ",".join(ids).encode("utf-8")
        return hashlib.sha256(joined).hexdigest()[:16]
    except Exception:  # noqa: BLE001 — defensive
        return ""


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
import os
import threading

from backend.core.ouroboros.governance.dw_catalog_client import (
    _refresh_interval_s as _refresh_interval_s_internal,
)


_BOOT_DISCOVERY_LOCK = asyncio.Lock()
_BOOT_DISCOVERY_DONE: bool = False
_REFRESH_TASK: Optional[asyncio.Task] = None
_HEAVY_PROBE_TASK: Optional[asyncio.Task] = None  # Slice 12.2.D
_LEDGER_SINGLETON: Optional[PromotionLedger] = None
_MODALITY_LEDGER_SINGLETON: Optional[Any] = None  # Slice G
_TTFT_OBSERVER_SINGLETON: Optional[Any] = None  # Slice 12.2.C
_HEAVY_PROBE_BUDGET_SINGLETON: Optional[Any] = None  # Slice 12.2.D
# Sync lock around the singleton hydration + boot flag — protects the
# very first-call window before the asyncio.Lock has been touched.
_BOOT_SYNC_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Sentinel-Pacemaker Handshake — force-refresh trigger
# ---------------------------------------------------------------------------
#
# Closes the catalog-deadlock loop diagnosed in soaks #2-#5: when the
# topology layer blocks BG ops because the catalog is purged, no DW
# calls happen → no breaker transitions → catalog stays purged →
# blocked ops keep accumulating until idle_timeout.
#
# Mechanism: the block site (candidate_generator) calls
# `request_force_refresh()`, which sets a module-level asyncio.Event.
# The discovery refresh loop awaits EITHER the cadence sleep OR this
# event — whichever fires first. On wake, it does an immediate
# /models probe; if DW is reachable, it repopulates the catalog and
# the next op flows normally. Rate-limited to once per
# JARVIS_FORCE_REFRESH_MIN_INTERVAL_S (default 30s) so block-storms
# don't thrash the endpoint.
_FORCE_REFRESH_EVENT: Optional[asyncio.Event] = None
_FORCE_REFRESH_LOCK = threading.Lock()
_LAST_FORCE_REFRESH_TS: float = 0.0


def _force_refresh_min_interval_s() -> float:
    """``JARVIS_FORCE_REFRESH_MIN_INTERVAL_S`` (default 30s) — minimum
    seconds between accepted force-refresh requests. Prevents block-
    storms from thrashing /models when many BG ops queue up
    simultaneously."""
    raw = os.environ.get("JARVIS_FORCE_REFRESH_MIN_INTERVAL_S", "30")
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 30.0


def sentinel_pacemaker_handshake_enabled() -> bool:
    """``JARVIS_SENTINEL_PACEMAKER_HANDSHAKE_ENABLED`` (default
    ``true``). When off, ``request_force_refresh`` is a no-op and the
    refresh loop ignores the event channel — legacy 30-min cadence
    is the only refresh trigger."""
    raw = os.environ.get(
        "JARVIS_SENTINEL_PACEMAKER_HANDSHAKE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


def _get_or_create_force_refresh_event() -> "asyncio.Event":
    """Lazy-init the module-level event. Defensive in case the loop
    binding rules change across Python versions."""
    global _FORCE_REFRESH_EVENT
    with _FORCE_REFRESH_LOCK:
        if _FORCE_REFRESH_EVENT is None:
            _FORCE_REFRESH_EVENT = asyncio.Event()
        return _FORCE_REFRESH_EVENT


def request_force_refresh(*, reason: str = "") -> bool:
    """Trigger the discovery loop to skip its sleep and probe DW
    immediately. Best-effort, NEVER raises.

    Returns True iff the event was actually set (i.e., handshake
    enabled, rate-limit not hit, asyncio context available); False
    otherwise. Callers should NOT branch on the return value for
    correctness — the handshake is a hint, not a contract.

    Rate-limited to one call per ``_force_refresh_min_interval_s()``
    so a storm of blocked ops doesn't thrash /models. The first
    request in a window wins; subsequent requests in the same window
    return False without setting the event.
    """
    global _LAST_FORCE_REFRESH_TS
    if not sentinel_pacemaker_handshake_enabled():
        return False
    try:
        import time as _t
        now = _t.monotonic()
        with _FORCE_REFRESH_LOCK:
            if (
                _LAST_FORCE_REFRESH_TS
                and (now - _LAST_FORCE_REFRESH_TS)
                < _force_refresh_min_interval_s()
            ):
                return False
            _LAST_FORCE_REFRESH_TS = now
        evt = _get_or_create_force_refresh_event()
        evt.set()
        logger.info(
            "[DiscoveryRunner] force_refresh requested reason=%s",
            (reason or "unspecified")[:120],
        )
        return True
    except Exception:  # noqa: BLE001 — never raise into caller
        logger.debug(
            "[DiscoveryRunner] request_force_refresh failed",
            exc_info=True,
        )
        return False


def reset_force_refresh_for_tests() -> None:
    """Test isolation — clear the event + last-ts."""
    global _FORCE_REFRESH_EVENT, _LAST_FORCE_REFRESH_TS
    with _FORCE_REFRESH_LOCK:
        _FORCE_REFRESH_EVENT = None
        _LAST_FORCE_REFRESH_TS = 0.0


def _get_or_create_ledger() -> PromotionLedger:
    """Lazy singleton — hydrates from disk on first access."""
    global _LEDGER_SINGLETON
    with _BOOT_SYNC_LOCK:
        if _LEDGER_SINGLETON is None:
            led = PromotionLedger()
            led.load()
            _LEDGER_SINGLETON = led
        return _LEDGER_SINGLETON


def _get_or_create_modality_ledger() -> Optional[Any]:
    """Slice G — lazy ModalityLedger singleton. Returns None when the
    master flag is off OR import fails (defensive for older deploys
    that haven't shipped the module yet)."""
    global _MODALITY_LEDGER_SINGLETON
    try:
        from backend.core.ouroboros.governance.dw_modality_ledger import (
            ModalityLedger,
            modality_verification_enabled,
        )
    except ImportError:
        return None
    if not modality_verification_enabled():
        return None
    with _BOOT_SYNC_LOCK:
        if _MODALITY_LEDGER_SINGLETON is None:
            mled = ModalityLedger()
            mled.load()
            _MODALITY_LEDGER_SINGLETON = mled
        return _MODALITY_LEDGER_SINGLETON


def _get_or_create_ttft_observer() -> Optional[Any]:
    """Slice 12.2.C — lazy TtftObserver singleton.

    Returns None when ``tracking_enabled()`` is ``false`` OR import
    fails. Hydrates from disk on first access. Defensive for older
    deploys that haven't shipped the module yet."""
    global _TTFT_OBSERVER_SINGLETON
    try:
        from backend.core.ouroboros.governance.dw_ttft_observer import (
            TtftObserver,
            tracking_enabled,
        )
    except ImportError:
        return None
    if not tracking_enabled():
        return None
    with _BOOT_SYNC_LOCK:
        if _TTFT_OBSERVER_SINGLETON is None:
            obs = TtftObserver()
            obs.load()
            _TTFT_OBSERVER_SINGLETON = obs
        return _TTFT_OBSERVER_SINGLETON


def get_ttft_observer() -> Optional[Any]:
    """Public accessor for callers outside the runner (e.g. the DW
    provider's first-chunk callsite). Same lazy-singleton semantics —
    returns None when tracking flag is off."""
    return _get_or_create_ttft_observer()


def _get_or_create_heavy_probe_budget() -> Optional[Any]:
    """Slice 12.2.D — lazy HeavyProbeBudget singleton.

    Returns None when ``heavy_probe_enabled()`` is ``false`` OR import
    fails. Hydrates from disk on first access. Defensive for older
    deploys that haven't shipped the module yet."""
    global _HEAVY_PROBE_BUDGET_SINGLETON
    try:
        from backend.core.ouroboros.governance.dw_heavy_probe import (
            HeavyProbeBudget,
            heavy_probe_enabled,
        )
    except ImportError:
        return None
    if not heavy_probe_enabled():
        return None
    with _BOOT_SYNC_LOCK:
        if _HEAVY_PROBE_BUDGET_SINGLETON is None:
            bud = HeavyProbeBudget()
            bud.load()
            _HEAVY_PROBE_BUDGET_SINGLETON = bud
        return _HEAVY_PROBE_BUDGET_SINGLETON


def get_heavy_probe_budget() -> Optional[Any]:
    """Public accessor for the HeavyProbeBudget singleton. Returns
    None when the heavy-probe master flag is off."""
    return _get_or_create_heavy_probe_budget()


def reset_boot_state_for_tests() -> None:
    """Test hook — clears the boot flag, cancels any refresh task,
    drops the ledger singletons. Production code MUST NOT call this."""
    global _BOOT_DISCOVERY_DONE, _REFRESH_TASK, _LEDGER_SINGLETON
    global _MODALITY_LEDGER_SINGLETON, _TTFT_OBSERVER_SINGLETON
    global _HEAVY_PROBE_TASK, _HEAVY_PROBE_BUDGET_SINGLETON
    global _LAST_SNAPSHOT_ID
    with _BOOT_SYNC_LOCK:
        _BOOT_DISCOVERY_DONE = False
        if _REFRESH_TASK is not None and not _REFRESH_TASK.done():
            _REFRESH_TASK.cancel()
        _REFRESH_TASK = None
        if _HEAVY_PROBE_TASK is not None and not _HEAVY_PROBE_TASK.done():
            _HEAVY_PROBE_TASK.cancel()
        _HEAVY_PROBE_TASK = None
        _LEDGER_SINGLETON = None
        _MODALITY_LEDGER_SINGLETON = None
        _TTFT_OBSERVER_SINGLETON = None
        _HEAVY_PROBE_BUDGET_SINGLETON = None
        _LAST_SNAPSHOT_ID = ""


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
        modality_ledger = _get_or_create_modality_ledger()  # Slice G
        ttft_observer = _get_or_create_ttft_observer()  # Slice 12.2.C
        first_result = await run_discovery(
            session=session,
            base_url=base_url,
            api_key=api_key,
            ledger=ledger,
            modality_ledger=modality_ledger,
            ttft_observer=ttft_observer,
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
                    modality_ledger=modality_ledger,
                    ttft_observer=ttft_observer,
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

        # Slice 12.2.D — spawn heavy-probe scheduler when master flag
        # is on. Same pattern as refresh loop: best-effort, swallows
        # missing-loop errors. Skipped silently when flag off so
        # master-off boot is bit-for-bit identical to pre-Slice-D.
        global _HEAVY_PROBE_TASK
        try:
            from backend.core.ouroboros.governance.dw_heavy_probe import (
                heavy_probe_enabled,
            )
            if heavy_probe_enabled():
                _HEAVY_PROBE_TASK = asyncio.create_task(
                    _heavy_probe_loop(
                        session=session,
                        base_url=base_url,
                        api_key=api_key,
                    ),
                    name="dw_heavy_probe_loop",
                )
        except RuntimeError:
            logger.debug(
                "[DiscoveryRunner] no running loop — heavy probe skipped",
            )
        except Exception:  # noqa: BLE001 — defensive (import / other)
            logger.debug(
                "[DiscoveryRunner] heavy probe spawn failed",
                exc_info=True,
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
    modality_ledger: Optional[Any] = None,  # Slice G
    ttft_observer: Optional[Any] = None,    # Slice 12.2.C
) -> None:
    """Periodic refresh. Each cycle:
      1. Sleeps for JARVIS_DW_CATALOG_REFRESH_S (default 1800s) OR
         until the force-refresh event fires (Sentinel-Pacemaker
         handshake — whichever comes first)
      2. Re-checks discovery flag (operator may have flipped off)
      3. Runs a full discovery cycle
      4. NEVER raises — all exceptions caught + logged

    Loop survives forever until task cancellation (which happens on
    process shutdown via reset_boot_state_for_tests or natural
    asyncio cleanup).

    Sentinel-Pacemaker handshake (2026-04-29): when the topology layer
    blocks an op because the catalog is purged/empty, it calls
    ``request_force_refresh()`` which sets the module-level event.
    This loop's wait pattern races the event against the cadence
    sleep so a blocked op triggers an immediate /models probe instead
    of waiting up to 30 minutes for the next cycle. The event is
    cleared after each wake so subsequent requests can re-trigger."""
    while True:
        # Race: either the cadence sleep completes OR the force-refresh
        # event fires (Sentinel-Pacemaker handshake). The event is
        # rate-limited at the trigger site (request_force_refresh) so
        # we don't thrash /models on a block-storm.
        cadence_s = _refresh_interval_s_internal()
        evt = _get_or_create_force_refresh_event()
        try:
            sleep_task = asyncio.ensure_future(asyncio.sleep(cadence_s))
            event_task = asyncio.ensure_future(evt.wait())
            done, pending = await asyncio.wait(
                (sleep_task, event_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
            # If the event fired, log + clear so the next cycle waits
            # cleanly. If the sleep completed, the event stays in
            # whatever state it was (typically already cleared from
            # a prior wake).
            if event_task in done:
                logger.info(
                    "[DiscoveryRunner] force_refresh wake — "
                    "bypassing %.0fs cadence sleep", cadence_s,
                )
                evt.clear()
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
                modality_ledger=modality_ledger,
                ttft_observer=ttft_observer,
            )
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 — defensive
            logger.exception(
                "[DiscoveryRunner] refresh cycle failed; "
                "next cycle will retry on schedule",
            )


async def _heavy_probe_loop(
    *,
    session: Any,
    base_url: str,
    api_key: str,
) -> None:
    """Slice 12.2.D — heavy-probe scheduler loop.

    Each cycle:
      1. Sleeps for ``_scheduler_cycle_s()`` (default 120s)
      2. Re-checks the master flag (operator may have flipped off)
      3. Reads the dynamic catalog for SPECULATIVE-route candidates
         (the exact set we want VRAM-warmth signal on — promoted +
         cold-storage models are skipped via HeavyProbeScheduler's
         eligibility rules)
      4. Calls scheduler.run_cycle to fire at most one probe

    NEVER raises out — every failure caught + logged + continues.
    Loop survives until task cancellation."""
    try:
        from backend.core.ouroboros.governance.dw_heavy_probe import (
            HeavyProber,
            HeavyProbeScheduler,
            _scheduler_cycle_s,
            heavy_probe_enabled,
        )
    except ImportError:
        logger.debug(
            "[HeavyProbeLoop] module unavailable — exiting cleanly",
        )
        return

    budget = _get_or_create_heavy_probe_budget()
    if budget is None:
        logger.debug(
            "[HeavyProbeLoop] budget unavailable — exiting cleanly",
        )
        return
    prober = HeavyProber(budget=budget)
    scheduler = HeavyProbeScheduler(prober=prober, budget=budget)

    while True:
        try:
            await asyncio.sleep(_scheduler_cycle_s())
        except asyncio.CancelledError:
            return
        if not heavy_probe_enabled():
            # Hot-revert in flight — keep loop alive so re-flip picks
            # up immediately. Same pattern as discovery refresh.
            continue
        try:
            from backend.core.ouroboros.governance.provider_topology import (
                get_dynamic_catalog,
            )
            cat = get_dynamic_catalog()
            # Heavy probes target the SPECULATIVE route — those are
            # the models in quarantine / cold-storage / unknown state
            # for which we want VRAM-warm signal.
            if cat is not None:
                candidates = cat.assignments_by_route.get(
                    "speculative", (),
                )
            else:
                candidates = ()
            await scheduler.run_cycle(
                session=session,
                base_url=base_url,
                api_key=api_key,
                candidate_ids=tuple(candidates),
            )
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 — defensive
            logger.exception(
                "[HeavyProbeLoop] cycle failed; "
                "next cycle will retry on schedule",
            )


__all__ = [
    "DiscoveryResult",
    "boot_discovery_once",
    "catalog_discovery_enabled",
    "get_heavy_probe_budget",
    "get_ttft_observer",
    "reset_boot_state_for_tests",
    "run_discovery",
]
