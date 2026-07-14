"""Sovereign Transport Profiler Matrix (2026-06-20).

Learn-then-detach adaptive profile of which DW models are **batch-only** — i.e.
their RT streaming arm structurally yields ``done_before_content`` (empty), so only
the async batch API can serve them. The diff-capable ``-dottxt`` codegen variants
are the canonical case.

Why this exists
---------------
Two layers disagree on transport (see
``docs/superpowers/specs/2026-06-20-sovereign-transport-profiler-matrix.md``):
the budget layer (``_compute_primary_budget``, pre-call) sizes the op for the RT
budget (180s autarky) because the transport-hedge makes ``_slice36_should_force_batch``
return False; but the dispatch layer then races RT∥batch and — on a batch-only model
— only the batch arm can win, strangled by that 180s budget → TimeoutError.

The fix is to LEARN which models are batch-only (first contact may still time out
once), persist that knowledge **immortally**, and tag every subsequent op for that
model ``ASYNC_BATCH_PAYLOAD`` BEFORE the budget layer runs — so it receives the
batch budget, immunity from the Zero-Shot quarantine, and active detachment
(park → free worker → resume on batch completion).

Design discipline
-----------------
* **Immortal**: serialized to ``.jarvis/dw_transport_profile.json`` (GCS-backed by
  the existing ``state_persistence_daemon``), rehydrated per subprocess fork.
  Default TTL = 0 (never decays) per spec; an env TTL re-opens a model for RT
  re-probe should it gain streaming capability.
* **Fail-soft**: every method swallows its own errors — NEVER raises into dispatch.
* **Gated**: master ``JARVIS_DW_TRANSPORT_PROFILE_ENABLED`` (default true,
  failure-path-only). OFF → ``is_batch_only`` always False → byte-identical legacy.
* **No hardcoding**: the batch-only set is LEARNED from live ``done_before_content``
  signals, never a static model list.
* **Reuse-first**: mirrors ``dw_ttft_observer``'s proven persistence shape and
  reuses its ``_atomic_write`` helper (zero duplication).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, Mapping, Optional

# Reuse the proven atomic writer — no duplication.
from backend.core.ouroboros.governance.dw_ttft_observer import _atomic_write

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "transport_profile.2"
_LEGACY_SCHEMA_VERSIONS = ("transport_profile.1",)

_ENV_MASTER = "JARVIS_DW_TRANSPORT_PROFILE_ENABLED"
_ENV_TTL_S = "JARVIS_DW_TRANSPORT_PROFILE_TTL_S"
_ENV_ENTITLEMENT_TTL_S = "JARVIS_DW_ENTITLEMENT_TTL_S"
_ENV_STATE_PATH = "JARVIS_DW_TRANSPORT_PROFILE_STATE_PATH"

# Transport layers a model can be denied on. A denial is per-(model, transport):
# an account may hold batch entitlement while RT is refused by a routing rule
# (the Run-25c class — ``Qwen3.5-397B-...-dottxt`` 403s on RT, 201s on batch).
TRANSPORT_REALTIME = "realtime"
TRANSPORT_BATCH = "batch"

# Slice 17 defaults. Mandate 2: NO infinite persistence for a routing demotion.
# A demotion is a HYPOTHESIS about a live endpoint, and endpoints change
# (entitlements get granted, streaming gets enabled). Both decay so the system
# re-probes reality instead of trusting a fossil.
_DEFAULT_BATCH_ONLY_TTL_S = 6 * 3600.0        # 6h
_DEFAULT_ENTITLEMENT_TTL_S = 24 * 3600.0      # 24h — an admin grant is slower


def transport_profile_enabled() -> bool:
    """Master gate. Default TRUE — failure-path-only: only acts once a model has
    PROVEN it yields ``done_before_content`` on RT. =0 reverts to the legacy path
    (``is_batch_only`` always False → byte-identical). NEVER raises."""
    return (os.environ.get(_ENV_MASTER, "true") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _ttl_from_env(name: str, default_s: float) -> float:
    """Positive TTL seconds from *name*, else *default_s*. Clamped to
    (0, 30 days]. Mandate 2: a demotion TTL can never be 0/infinite — an
    explicit ``0`` (or garbage) falls back to the finite default rather than
    resurrecting the immortal-fossil behavior that condemned RT-capable
    models forever (Run-25c). NEVER raises."""
    raw = (os.environ.get(name, "") or "").strip()
    try:
        v = float(raw) if raw else default_s
    except (TypeError, ValueError):
        v = default_s
    if v <= 0.0:
        v = default_s
    return min(v, 30 * 24 * 3600.0)


def _profile_ttl_s() -> float:
    """Re-probe TTL for a learned batch-only tag. Finite by construction."""
    return _ttl_from_env(_ENV_TTL_S, _DEFAULT_BATCH_ONLY_TTL_S)


def _entitlement_ttl_s() -> float:
    """Re-probe TTL for a learned entitlement denial. Finite by construction."""
    return _ttl_from_env(_ENV_ENTITLEMENT_TTL_S, _DEFAULT_ENTITLEMENT_TTL_S)


def _state_path() -> Path:
    raw = (os.environ.get(
        _ENV_STATE_PATH, ".jarvis/dw_transport_profile.json",
    ) or "").strip()
    return Path(raw)


class TransportProfile:
    """Per-model transport profile over TWO structurally distinct axes.

    Pure profile — does NOT route, budget, or park. Emits READ-ONLY signals
    that the budget / quarantine / park / ladder layers consult.

    * ``is_batch_only(model)`` — a CAPABILITY claim: the model's RT arm
      structurally yields ``done_before_content``, so only batch can serve it.
    * ``is_unavailable(model, transport)`` — an AUTHORIZATION fact: the
      endpoint REFUSED the call (HTTP 403). The model is not weaker on that
      transport; we are not permitted to use it there at all.

    Conflating the two is what blinded Run-25c (Slice 17): a 403-on-RT was
    laundered into "batch-only", which shoved the op onto the 300s batch
    breaker, which timed out, which the bandit scored as a QUALITY failure —
    a death spiral that ended in ``exhausted all 1 DW models`` and zero
    generation. An entitlement denial must EXCLUDE the transport, never
    silently re-route to a slower one.
    """

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        autosave: bool = True,
    ) -> None:
        self._path = path
        self._autosave = autosave
        # model_id → unix timestamp when it was last observed batch-only.
        self._batch_only: Dict[str, float] = {}
        # model_id → {transport → unix ts of the 403 denial}.
        self._unavailable: Dict[str, Dict[str, float]] = {}
        self._lock = threading.RLock()
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _resolved_path(self) -> Path:
        return self._path if self._path is not None else _state_path()

    def load(self) -> None:
        """Load the profile from disk. Missing file = empty; corrupt = warn +
        start empty. NEVER raises."""
        with self._lock:
            self._loaded = True
            p = self._resolved_path()
            if not p.exists():
                return
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "[TransportProfile] corrupt state at %s — starting empty (%s)",
                    p, exc,
                )
                return
            if not isinstance(payload, Mapping):
                return
            _found = payload.get("schema_version")
            if _found not in (SCHEMA_VERSION,) + _LEGACY_SCHEMA_VERSIONS:
                logger.warning(
                    "[TransportProfile] schema mismatch at %s (found=%r expected=%r)"
                    " — starting empty", p, _found, SCHEMA_VERSION,
                )
                return
            raw = payload.get("batch_only", {})
            if isinstance(raw, Mapping):
                for mid, ts in raw.items():
                    try:
                        if isinstance(mid, str):
                            self._batch_only[mid] = float(ts)
                    except (ValueError, TypeError):
                        continue
            # v1 carried no entitlement axis — a v1 file simply has none, and
            # its batch_only tags now decay under the finite TTL rather than
            # persisting as fossils (mandate 2).
            raw_u = payload.get("unavailable", {})
            if isinstance(raw_u, Mapping):
                for mid, per_transport in raw_u.items():
                    if not isinstance(mid, str) or not isinstance(
                        per_transport, Mapping
                    ):
                        continue
                    for transport, ts in per_transport.items():
                        try:
                            if isinstance(transport, str):
                                self._unavailable.setdefault(mid, {})[
                                    transport
                                ] = float(ts)
                        except (ValueError, TypeError):
                            continue

    def save(self) -> None:
        """Persist the profile atomically. NEVER raises."""
        with self._lock:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "batch_only": dict(self._batch_only),
                "unavailable": {
                    m: dict(t) for m, t in self._unavailable.items()
                },
            }
            try:
                _atomic_write(
                    self._resolved_path(),
                    json.dumps(payload, sort_keys=True, indent=2),
                )
            except OSError as exc:
                logger.warning(
                    "[TransportProfile] save failed: %s — state remains in memory",
                    exc,
                )

    def _maybe_autosave(self) -> None:
        if self._autosave:
            self.save()

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------------
    # Write side
    # ------------------------------------------------------------------

    def record_unavailable(
        self, model_id: str, transport: str, *, status: int = 403,
    ) -> None:
        """Record an AUTHORIZATION denial: the endpoint refused ``model_id`` on
        ``transport`` (HTTP 403 — a routing rule / missing entitlement).

        Mandate 1: this is NOT a capability signal and MUST NOT be laundered
        into ``record_batch_only``. A refused transport is EXCLUDED, not
        downgraded — routing an unauthorized RT call onto the batch API buys
        a guaranteed 300s-breaker timeout and teaches the bandit a lie.

        Decays under ``_entitlement_ttl_s`` so an entitlement granted later is
        re-discovered. Gated + fail-soft — NEVER raises into dispatch.
        """
        try:
            if not model_id or not transport or not transport_profile_enabled():
                return
            self._ensure_loaded()
            with self._lock:
                self._unavailable.setdefault(model_id, {})[transport] = time.time()
                self._maybe_autosave()
            logger.warning(
                "[TransportProfile] model=%s transport=%s ENTITLEMENT DENIED "
                "(HTTP %s) — excluded from that transport (NOT batch-demoted); "
                "re-probe after %.0fs",
                model_id, transport, status, _entitlement_ttl_s(),
            )
        except Exception:  # noqa: BLE001 — never perturb the dispatch path
            logger.debug(
                "[TransportProfile] record_unavailable swallowed", exc_info=True,
            )

    def record_rt_success(self, model_id: str) -> None:
        """A REAL-TIME call succeeded for ``model_id`` — ground truth that beats
        every historical demotion.

        Mandate 2 (dynamic cache invalidation): evict BOTH the batch-only tag
        and any RT entitlement denial. Live evidence of an RT-capable,
        RT-authorized endpoint is the strongest signal the profile can hold, so
        it clears the fossils that would otherwise keep shoving this model onto
        batch. Gated + fail-soft — NEVER raises into dispatch.
        """
        try:
            if not model_id or not transport_profile_enabled():
                return
            self._ensure_loaded()
            with self._lock:
                dropped_batch = self._batch_only.pop(model_id, None) is not None
                per_transport = self._unavailable.get(model_id) or {}
                dropped_ent = per_transport.pop(TRANSPORT_REALTIME, None) is not None
                if not per_transport:
                    self._unavailable.pop(model_id, None)
                if dropped_batch or dropped_ent:
                    self._maybe_autosave()
            if dropped_batch or dropped_ent:
                logger.info(
                    "[TransportProfile] model=%s RT SUCCESS — invalidated stale "
                    "demotions (batch_only=%s rt_entitlement_denial=%s); the "
                    "endpoint serves real-time, so the fossil is dropped",
                    model_id, dropped_batch, dropped_ent,
                )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[TransportProfile] record_rt_success swallowed", exc_info=True,
            )

    def record_batch_only(self, model_id: str) -> None:
        """Tag ``model_id`` batch-only — a CAPABILITY claim, valid ONLY when RT
        structurally yields ``done_before_content``.

        Slice 17: this tag now DECAYS (``_profile_ttl_s``, finite by
        construction). The pre-Slice-17 "immortal" contract permanently
        condemned models that in fact serve RT fine (the plain 397B answers in
        1.6s; gpt-oss-120b in 0.7s — both were fossil-tagged), because ANY
        batch dispatch was read as proof of batch-boundness. Callers must only
        invoke this on genuine RT-rupture evidence. Gated + fail-soft — NEVER
        raises into dispatch.
        """
        try:
            if not model_id or not transport_profile_enabled():
                return
            self._ensure_loaded()
            with self._lock:
                self._batch_only[model_id] = time.time()
                self._maybe_autosave()
            logger.info(
                "[TransportProfile] model=%s tagged ASYNC_BATCH_PAYLOAD "
                "(RT yields done_before_content → batch-only; decays after "
                "%.0fs, and any RT success invalidates it immediately)",
                model_id, _profile_ttl_s(),
            )
        except Exception:  # noqa: BLE001 — never perturb the dispatch path
            logger.debug("[TransportProfile] record_batch_only swallowed", exc_info=True)

    def clear(self, model_id: str) -> None:
        """Drop a model's batch-only tag (re-opens it for RT). NEVER raises."""
        try:
            self._ensure_loaded()
            with self._lock:
                if self._batch_only.pop(model_id, None) is not None:
                    self._maybe_autosave()
        except Exception:  # noqa: BLE001
            logger.debug("[TransportProfile] clear swallowed", exc_info=True)

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def is_unavailable(self, model_id: str, transport: str) -> bool:
        """True iff ``model_id`` is known to be REFUSED on ``transport`` (a
        non-expired 403 entitlement denial).

        Mandate 4: the ladder consults this to bypass unauthorized variants
        BEFORE dispatch, instead of discovering the 403 by burning an op on it.
        Decays under ``_entitlement_ttl_s`` (an admin can grant access), and any
        RT success clears it outright. Master-off → False. NEVER raises.
        """
        try:
            if not model_id or not transport or not transport_profile_enabled():
                return False
            self._ensure_loaded()
            with self._lock:
                ts = (self._unavailable.get(model_id) or {}).get(transport)
                if ts is None:
                    return False
                if (time.time() - ts) > _entitlement_ttl_s():
                    # TTL elapsed → re-open for an entitlement re-probe.
                    per_transport = self._unavailable.get(model_id) or {}
                    per_transport.pop(transport, None)
                    if not per_transport:
                        self._unavailable.pop(model_id, None)
                    self._maybe_autosave()
                    return False
                return True
        except Exception:  # noqa: BLE001
            logger.debug("[TransportProfile] is_unavailable swallowed", exc_info=True)
            return False

    def is_batch_only(self, model_id: str) -> bool:
        """True iff ``model_id`` is a known batch-only model (non-expired tag). Decays
        the tag on read when a positive TTL is configured (re-opens RT re-probe);
        default TTL 0 = immortal. Master-off or unknown model → False. NEVER raises."""
        try:
            if not model_id or not transport_profile_enabled():
                return False
            self._ensure_loaded()
            with self._lock:
                ts = self._batch_only.get(model_id)
                if ts is None:
                    return False
                ttl = _profile_ttl_s()
                if ttl > 0.0 and (time.time() - ts) > ttl:
                    # TTL elapsed → re-open for RT re-probe (decay on read).
                    self._batch_only.pop(model_id, None)
                    self._maybe_autosave()
                    return False
                return True
        except Exception:  # noqa: BLE001
            logger.debug("[TransportProfile] is_batch_only swallowed", exc_info=True)
            return False


# ---------------------------------------------------------------------------
# Process-wide singleton (mirrors dw_ttft_observer.get_ttft_observer shape)
# ---------------------------------------------------------------------------

_DEFAULT_PROFILE: Optional[TransportProfile] = None
_DEFAULT_LOCK = threading.Lock()


def get_transport_profile() -> TransportProfile:
    """Return the process-wide TransportProfile singleton (lazy, thread-safe).
    Rehydrates from disk on first access in each subprocess fork. NEVER raises —
    on any error returns a fresh in-memory-only profile."""
    global _DEFAULT_PROFILE
    try:
        if _DEFAULT_PROFILE is None:
            with _DEFAULT_LOCK:
                if _DEFAULT_PROFILE is None:
                    _DEFAULT_PROFILE = TransportProfile()
                    _DEFAULT_PROFILE.load()
        return _DEFAULT_PROFILE
    except Exception:  # noqa: BLE001
        return TransportProfile(autosave=False)


__all__ = [
    "TransportProfile",
    "get_transport_profile",
    "transport_profile_enabled",
    "SCHEMA_VERSION",
    "TRANSPORT_REALTIME",
    "TRANSPORT_BATCH",
]
