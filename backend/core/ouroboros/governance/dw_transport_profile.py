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

SCHEMA_VERSION = "transport_profile.1"

_ENV_MASTER = "JARVIS_DW_TRANSPORT_PROFILE_ENABLED"
_ENV_TTL_S = "JARVIS_DW_TRANSPORT_PROFILE_TTL_S"
_ENV_STATE_PATH = "JARVIS_DW_TRANSPORT_PROFILE_STATE_PATH"


def transport_profile_enabled() -> bool:
    """Master gate. Default TRUE — failure-path-only: only acts once a model has
    PROVEN it yields ``done_before_content`` on RT. =0 reverts to the legacy path
    (``is_batch_only`` always False → byte-identical). NEVER raises."""
    return (os.environ.get(_ENV_MASTER, "true") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _profile_ttl_s() -> float:
    """Re-probe TTL for a learned batch-only tag, in seconds. Default 0 = IMMORTAL
    (never decays — a model that needs batch keeps needing it). A positive value
    re-opens the model for RT re-probe after the window (so a model that GAINS
    streaming capability can be re-learned). Clamped to [0, 30 days]. NEVER raises."""
    raw = (os.environ.get(_ENV_TTL_S, "") or "").strip()
    try:
        v = float(raw) if raw else 0.0
    except (TypeError, ValueError):
        v = 0.0
    return max(0.0, min(v, 30 * 24 * 3600.0))


def _state_path() -> Path:
    raw = (os.environ.get(
        _ENV_STATE_PATH, ".jarvis/dw_transport_profile.json",
    ) or "").strip()
    return Path(raw)


class TransportProfile:
    """Per-model immortal batch-only profile.

    Pure profile — does NOT route, budget, or park. Emits one READ-ONLY signal
    (``is_batch_only``) that the budget / quarantine / park layers consult.
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
            if payload.get("schema_version") != SCHEMA_VERSION:
                logger.warning(
                    "[TransportProfile] schema mismatch at %s (found=%r expected=%r)"
                    " — starting empty", p, payload.get("schema_version"),
                    SCHEMA_VERSION,
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

    def save(self) -> None:
        """Persist the profile atomically. NEVER raises."""
        with self._lock:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "batch_only": dict(self._batch_only),
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

    def record_batch_only(self, model_id: str) -> None:
        """Tag ``model_id`` batch-only (RT yields ``done_before_content``). 1-strike,
        immortal (persisted), idempotent (refreshes the timestamp). Gated + fail-soft
        — NEVER raises into dispatch."""
        try:
            if not model_id or not transport_profile_enabled():
                return
            self._ensure_loaded()
            with self._lock:
                self._batch_only[model_id] = time.time()
                self._maybe_autosave()
            logger.info(
                "[TransportProfile] model=%s tagged ASYNC_BATCH_PAYLOAD "
                "(RT yields done_before_content → batch-only, immortal)", model_id,
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
]
