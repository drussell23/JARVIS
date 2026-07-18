"""Task 4 (ov cockpit silence, Slice 2, F1) — DW entitlement fallback.

Live run bt-2026-07-08-013911 hit two REAL 403 entitlement failures:

  * the Slice 39 surface probe (``dw_surface_probes.py::probe_batch_storage``)
  * a heavy probe (``dw_heavy_probe.py``)

both against models the account's DW plan does not currently entitle
(``Qwen/Qwen3.5-35B-A3B-FP8``, ``lightonai/LightOnOCR-2-1B``). Both paths
funnel through ``DoublewordProvider._upload_file`` /
``DoublewordProvider`` dispatch — this module is the ONE shared
resolver both call, so there is a single place that decides "what do
we try instead" rather than two copies of the same logic.

Design
------
Entitlement is a **live, per-account fact** — never a hardcoded model
id. The fallback is computed as::

    policy_preference_order  ∩  live_entitled_catalog

* ``policy_preference_order`` — a caller-supplied, dynamically-resolved
  ranked sequence of candidate model ids. **Never a literal.** The
  default (:func:`default_preference_order`) reads the
  ``PromotionLedger`` — the same policy-gated, catalog-derived ranked
  list ``preflight_probe._resolve_surface_probe_model`` already
  consults to pick a probe model in the first place. If a model at the
  front of that list is blocked, the natural fallback is the next
  entry that's still live-entitled.
* ``live_entitled_catalog`` — resolved via ``DwCatalogClient`` (the
  existing ``/v1/models`` catalog fetcher, Aegis-credentialed via its
  own ``_auth_headers()``). Cached in-process with a TTL
  (``JARVIS_DW_ENTITLEMENT_CACHE_TTL_S``, default 1800s — same
  cadence as the catalog's own background refresh) so a burst of 403s
  doesn't refetch ``/models`` per-failure.

Empty intersection → :func:`resolve_entitlement_fallback` returns
``None`` and the caller degrades exactly as before (no retry). NEVER
raises — entitlement-fallback resolution is a best-effort enhancement,
never a new failure mode.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Collection, FrozenSet, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.dw_entitlement_classifier import (
    KIND_ENTITLEMENT_BLOCKED,
    classify_4xx,
)

logger = logging.getLogger("Ouroboros.DWEntitlement")


# ---------------------------------------------------------------------------
# Detection — thin, reusable wrapper over the existing classifier
# ---------------------------------------------------------------------------


def is_entitlement_blocked(status: int, body: str) -> bool:
    """True iff *status*/*body* classify as a per-model entitlement
    block (account authenticated but not entitled to THIS model) —
    as opposed to a global auth failure or an unrelated 4xx.

    Single detection seam every entitlement-fallback consumer shares
    (surface probe, file-upload/dispatch). NEVER raises."""
    try:
        return classify_4xx(status, body or "").kind == KIND_ENTITLEMENT_BLOCKED
    except Exception:  # noqa: BLE001 — defensive, never break the caller
        return False


# ---------------------------------------------------------------------------
# Env knobs (read at call time — hot-tunable, test-monkeypatchable)
# ---------------------------------------------------------------------------


def entitlement_fallback_enabled() -> bool:
    """``JARVIS_DW_ENTITLEMENT_FALLBACK_ENABLED`` (default ``true``).

    Master switch — operator hot-revert path. When false, callers must
    skip fallback resolution entirely and degrade exactly as before
    Task 4."""
    raw = os.environ.get(
        "JARVIS_DW_ENTITLEMENT_FALLBACK_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


def _cache_ttl_s() -> float:
    """``JARVIS_DW_ENTITLEMENT_CACHE_TTL_S`` (default 1800s = 30 min,
    mirroring the catalog client's own default refresh cadence)."""
    try:
        return float(
            os.environ.get("JARVIS_DW_ENTITLEMENT_CACHE_TTL_S", "1800").strip(),
        )
    except (TypeError, ValueError):
        return 1800.0


# ---------------------------------------------------------------------------
# Per-process entitled-catalog cache
# ---------------------------------------------------------------------------


class EntitlementCatalogCache:
    """Per-process cache of the live entitled model-id set.

    Wraps anything shaped like ``DwCatalogClient`` — i.e. exposing an
    async ``fetch() -> CatalogSnapshot`` (the real client, or a test
    double). Avoids re-fetching ``/models`` on every 403 within the
    TTL window ("cache the entitled set per session" per the task
    brief). A fetch failure just means the cached (possibly empty) set
    is returned unchanged — the caller's intersection then comes up
    empty and degrades exactly as before. NEVER raises.
    """

    def __init__(self) -> None:
        self._entitled_ids: FrozenSet[str] = frozenset()
        self._fetched_at_monotonic: float = 0.0
        # Session-local ground-truth of models DW authoritatively 403'd for THIS
        # account (Dynamic Capability Propagation, 2026-07-18). Distinct from the
        # entitled allow-list: DW's /v1/models CATALOG lists a model as available
        # while actual USE returns 403 (entitlement is finer-grained than the
        # catalog listing). A plain cache reset re-fetches that catalog and
        # re-adds the model → election re-picks it → 403 storm. Recording the
        # observed 403 here lets election avoid the model regardless of what the
        # catalog re-lists. Survives a reset() by design — it is not a cache of
        # /v1/models, it is a record of DW's own denials.
        self._blocked_ids: FrozenSet[str] = frozenset()

    def _is_stale(self) -> bool:
        if self._fetched_at_monotonic <= 0.0:
            return True
        return (time.monotonic() - self._fetched_at_monotonic) >= _cache_ttl_s()

    async def get_entitled_ids(
        self, catalog_client: Any, *, force_refresh: bool = False,
    ) -> FrozenSet[str]:
        """Return the cached entitled id set (minus any 403-blocked models),
        refreshing from *catalog_client* when stale, forced, or never hydrated."""
        if not force_refresh and self._entitled_ids and not self._is_stale():
            return self._entitled_ids - self._blocked_ids
        try:
            snapshot = await catalog_client.fetch()
        except Exception as exc:  # noqa: BLE001 — never raise
            logger.debug("[DWEntitlement] catalog fetch failed: %r", exc)
            snapshot = None
        if snapshot is not None:
            ids_fn = getattr(snapshot, "model_ids", None)
            ids = frozenset(ids_fn()) if callable(ids_fn) else frozenset()
            if ids:
                self._entitled_ids = ids
                self._fetched_at_monotonic = time.monotonic()
        # Subtract observed 403s so a re-fetched catalog can never re-surface a
        # model DW just denied (the storm-avoidance invariant).
        return self._entitled_ids - self._blocked_ids

    def cached_entitled_ids(self) -> FrozenSet[str]:
        """SYNC peek at the last-fetched entitled set (minus 403-blocked models),
        with NO catalog I/O. For hot-path election filters that cannot await —
        empty when the catalog has never been hydrated (caller must fail open).
        NEVER raises."""
        try:
            return self._entitled_ids - self._blocked_ids
        except Exception:  # noqa: BLE001
            return frozenset()

    def mark_blocked(self, model_id: str) -> None:
        """Record that DW authoritatively 403'd *model_id* for this account, so
        every subsequent election avoids it — even after a cache reset re-fetches
        a catalog that still lists it. Surgical (one model), synchronous, and
        immediately reflected by :meth:`cached_entitled_ids`. NEVER raises."""
        try:
            mid = (model_id or "").strip()
            if not mid:
                return
            if mid not in self._blocked_ids:
                self._blocked_ids = self._blocked_ids | {mid}
                self._entitled_ids = self._entitled_ids - {mid}
        except Exception:  # noqa: BLE001
            pass

    def is_blocked(self, model_id: str) -> bool:
        """True iff *model_id* was recorded as 403-blocked this session. NEVER
        raises."""
        try:
            return (model_id or "").strip() in self._blocked_ids
        except Exception:  # noqa: BLE001
            return False

    def blocked_ids(self) -> FrozenSet[str]:
        """Read-only snapshot of the session's 403-blocked set. NEVER raises."""
        return self._blocked_ids

    def reset(self) -> None:
        """Test/operator hook — drop the cached entitled set + staleness clock.
        Does NOT clear ``_blocked_ids`` (an observed 403 is ground truth that
        must survive a catalog re-fetch); use :meth:`reset_blocked` for that."""
        self._entitled_ids = frozenset()
        self._fetched_at_monotonic = 0.0

    def reset_blocked(self) -> None:
        """Test hook — clear the 403-blocked set. Production code must not call
        this (a 403 is durable ground truth for the session). NEVER raises."""
        self._blocked_ids = frozenset()


_process_cache = EntitlementCatalogCache()


def get_process_entitlement_cache() -> EntitlementCatalogCache:
    """The shared per-process cache instance. Production callers use
    this singleton (one entitled-set fetch per TTL window across every
    call site in the process). Tests should construct their own
    ``EntitlementCatalogCache()`` for isolation."""
    return _process_cache


# ---------------------------------------------------------------------------
# Preference order — dynamic, never hardcoded
# ---------------------------------------------------------------------------


def default_preference_order() -> Tuple[str, ...]:
    """The default fallback candidate ranking: the ``PromotionLedger``'s
    already-vetted promoted models — the same policy-gated, catalog-
    derived dynamic list ``preflight_probe._resolve_surface_probe_model``
    consults to pick a probe model. Never a hardcoded model id: an
    empty/unavailable ledger yields ``()``, which correctly produces
    "no fallback" (legacy degrade) rather than a fabricated guess.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.dw_promotion_ledger import (
            PromotionLedger,
        )
        ledger = PromotionLedger()
        ledger.load()
        return ledger.promoted_models()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[DWEntitlement] default preference order unavailable: %r", exc,
        )
        return ()


# ---------------------------------------------------------------------------
# Pure selection
# ---------------------------------------------------------------------------


def select_fallback_model(
    *,
    blocked_model_id: str,
    preference_order: Sequence[str],
    entitled_ids: Collection[str],
) -> Optional[str]:
    """Pure: the first entry of *preference_order* (policy's ranking —
    caller-supplied, never a literal) that is BOTH present in
    *entitled_ids* (live catalog — the "∩ catalog" half) and is not
    the model that just got blocked. Returns ``None`` on empty
    intersection — the caller's contract is to degrade exactly as
    before (no retry, no retry storm)."""
    entitled = set(entitled_ids)
    for candidate in preference_order:
        if not candidate or candidate == blocked_model_id:
            continue
        if candidate in entitled:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Composed resolver — cache + selection + telemetry
# ---------------------------------------------------------------------------


async def resolve_entitlement_fallback(
    *,
    blocked_model_id: str,
    catalog_client: Any,
    preference_order: Optional[Sequence[str]] = None,
    cache: Optional[EntitlementCatalogCache] = None,
    force_refresh: bool = False,
) -> Optional[str]:
    """Compose cache-fetch + pure selection + structured telemetry.

    Returns the fallback model id, or ``None`` when the policy∩catalog
    intersection is empty (legacy degrade — caller must NOT retry).
    NEVER raises — a failure anywhere in resolution just yields
    ``None``, same as an empty intersection.
    """
    if not entitlement_fallback_enabled():
        return None
    try:
        order = (
            tuple(preference_order)
            if preference_order is not None
            else default_preference_order()
        )
        active_cache = cache if cache is not None else _process_cache
        entitled_ids = await active_cache.get_entitled_ids(
            catalog_client, force_refresh=force_refresh,
        )
        fallback = select_fallback_model(
            blocked_model_id=blocked_model_id,
            preference_order=order,
            entitled_ids=entitled_ids,
        )
    except Exception as exc:  # noqa: BLE001 — never raise
        logger.debug("[DWEntitlement] resolution failed: %r", exc)
        return None

    if fallback is not None:
        logger.warning(
            "[DWEntitlement] model=%s blocked -> fallback=%s (policy∩catalog)",
            blocked_model_id, fallback,
        )
    else:
        logger.warning(
            "[DWEntitlement] model=%s blocked -> no_fallback "
            "(policy∩catalog empty, %d entitled candidate(s))",
            blocked_model_id, len(entitled_ids),
        )
    return fallback


__all__ = [
    "EntitlementCatalogCache",
    "default_preference_order",
    "entitlement_fallback_enabled",
    "get_process_entitlement_cache",
    "is_entitlement_blocked",
    "resolve_entitlement_fallback",
    "select_fallback_model",
]
