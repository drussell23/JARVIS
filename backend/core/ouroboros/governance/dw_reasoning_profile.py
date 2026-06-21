"""Sovereign Reasoning-Capability Profiler (2026-06-21).

Adaptive, self-learning profile of which DW models REJECT ``reasoning_effort="none"``
(i.e. cannot disable reasoning). Meryem @ Doubleword (2026-06-21) reported that
``gpt-oss-120b`` errors when sent ``reasoning_effort="none"`` because the model can't
disable reasoning; Seb @ DW (2026-06-08) reported the same for ``deepseek-v4-pro``.

Rather than maintain a hardcoded model list, this module LEARNS the constraint from
live DW error feedback — the same learn-then-apply pattern as
``dw_transport_profile`` (which learns batch-only models). When a request to model M
errors with a reasoning-rejection signature, ``record_reasoning_floor(M)`` persists a
minimum-effort floor for M; every subsequent request for M is then floored above
``none`` so the error never recurs. Composes the EXISTING ``_dw_model_min_effort``
3-tier resolver (dynamic catalog → learned → static seed → none) — no duplication of
the effort-clamp logic.

Design discipline (mirrors dw_transport_profile):
  * **Immortal**: serialized to ``.jarvis/dw_reasoning_profile.json`` (GCS-backed by
    the state-persistence daemon), rehydrated per subprocess fork.
  * **Fail-soft**: every method swallows its own errors — NEVER raises into dispatch.
  * **Gated**: master ``JARVIS_DW_REASONING_PROFILE_ENABLED`` (default true,
    failure-path-only). OFF → ``learned_min_effort`` always None → byte-identical
    legacy (static map + catalog still apply).
  * **No hardcoding**: the reasoning-incapable set is LEARNED from live errors, never
    a static model list. The error signature is an env-tunable pattern, not a model
    name.
  * **Reuse-first**: mirrors ``dw_transport_profile``'s persistence shape and reuses
    its ``_atomic_write`` helper.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Dict, Mapping, Optional

# Reuse the proven atomic writer — no duplication.
from backend.core.ouroboros.governance.dw_ttft_observer import _atomic_write

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "reasoning_profile.1"

_ENV_MASTER = "JARVIS_DW_REASONING_PROFILE_ENABLED"
_ENV_STATE_PATH = "JARVIS_DW_REASONING_PROFILE_STATE_PATH"
_ENV_REJECTION_PATTERNS = "JARVIS_DW_REASONING_REJECTION_PATTERNS"
_ENV_DEFAULT_FLOOR = "JARVIS_DW_REASONING_LEARNED_FLOOR"

# Substrings (lowercased) in a DW error body that indicate the model rejected an
# attempt to disable / under-spend reasoning. Env-tunable (CSV). Deliberately
# conservative — we floor a model ONLY when its error body clearly implicates the
# reasoning knob, so a 5xx / entitlement / timeout error never mis-trains the floor.
_DEFAULT_REJECTION_PATTERNS = (
    "reasoning_effort",
    "reasoning effort",
    "disable reasoning",
    "cannot disable reasoning",
    "reasoning cannot be disabled",
    "does not support reasoning",
    "reasoning is required",
    "must enable reasoning",
)

# Valid effort tokens (mirror doubleword_provider._EFFORT_ORDER) — kept local to
# avoid an import cycle; validated on read.
_EFFORT_TOKENS = ("none", "low", "medium", "high")


def reasoning_profile_enabled() -> bool:
    """Master gate. Default TRUE — failure-path-only: only acts once a model has
    PROVEN (via a reasoning-rejection error) that it can't disable reasoning. =0
    reverts to legacy (learned_min_effort always None). NEVER raises."""
    return (os.environ.get(_ENV_MASTER, "true") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _learned_floor_default() -> str:
    """The effort to floor a learned reasoning-incapable model to. Default ``low``
    (the minimum non-``none`` effort). Env-tunable; validated. NEVER raises."""
    raw = (os.environ.get(_ENV_DEFAULT_FLOOR, "") or "").strip().lower()
    return raw if raw in _EFFORT_TOKENS and raw != "none" else "low"


def _rejection_patterns() -> tuple:
    """Resolve the reasoning-rejection error substrings (lowercased). Env CSV
    override; defaults to the canonical set. NEVER raises."""
    raw = (os.environ.get(_ENV_REJECTION_PATTERNS, "") or "").strip()
    if not raw:
        return _DEFAULT_REJECTION_PATTERNS
    parts = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    return parts or _DEFAULT_REJECTION_PATTERNS


def error_indicates_reasoning_rejection(error_text: str) -> bool:
    """True iff ``error_text`` (a DW error body / message) matches a
    reasoning-rejection signature — i.e. the model errored because of the
    reasoning knob, not transport/entitlement/timeout. Conservative by design so a
    generic 5xx never mis-trains the floor. NEVER raises."""
    try:
        if not error_text or not isinstance(error_text, str):
            return False
        blob = error_text.lower()
        return any(pat in blob for pat in _rejection_patterns())
    except Exception:  # noqa: BLE001
        return False


def _state_path() -> Path:
    raw = (os.environ.get(
        _ENV_STATE_PATH, ".jarvis/dw_reasoning_profile.json",
    ) or "").strip()
    return Path(raw)


class ReasoningProfile:
    """Per-model immortal minimum-reasoning-effort profile, learned from DW errors.

    Pure profile — does NOT clamp or dispatch. Emits one READ-ONLY signal
    (``learned_min_effort``) that ``doubleword_provider._dw_model_min_effort``
    consults as the adaptive tier of its resolver.
    """

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        autosave: bool = True,
    ) -> None:
        self._path = path
        self._autosave = autosave
        # model_id → learned minimum effort token (e.g. "low").
        self._min_effort: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistence (mirrors dw_transport_profile)
    # ------------------------------------------------------------------

    def _resolved_path(self) -> Path:
        return self._path if self._path is not None else _state_path()

    def load(self) -> None:
        """Load the profile from disk. Missing = empty; corrupt = warn + empty.
        NEVER raises."""
        with self._lock:
            self._loaded = True
            p = self._resolved_path()
            if not p.exists():
                return
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "[ReasoningProfile] corrupt state at %s — starting empty (%s)",
                    p, exc,
                )
                return
            if not isinstance(payload, Mapping):
                return
            if payload.get("schema_version") != SCHEMA_VERSION:
                logger.warning(
                    "[ReasoningProfile] schema mismatch at %s (found=%r expected=%r)"
                    " — starting empty", p, payload.get("schema_version"),
                    SCHEMA_VERSION,
                )
                return
            raw = payload.get("min_effort", {})
            if isinstance(raw, Mapping):
                for mid, eff in raw.items():
                    if isinstance(mid, str) and isinstance(eff, str) and eff in _EFFORT_TOKENS:
                        self._min_effort[mid] = eff

    def save(self) -> None:
        """Persist the profile atomically. NEVER raises."""
        with self._lock:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "min_effort": dict(self._min_effort),
            }
            try:
                _atomic_write(
                    self._resolved_path(),
                    json.dumps(payload, sort_keys=True, indent=2),
                )
            except OSError as exc:
                logger.warning(
                    "[ReasoningProfile] save failed: %s — state remains in memory",
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

    def record_reasoning_floor(self, model_id: str, floor: str = "") -> None:
        """Tag ``model_id`` as reasoning-incapable: it rejected an attempt to
        disable/under-spend reasoning, so floor its effort to ``floor`` (default
        ``low``) going forward. 1-strike, immortal (persisted), monotonic (never
        lowers an existing floor). Gated + fail-soft — NEVER raises into dispatch."""
        try:
            if not model_id or not reasoning_profile_enabled():
                return
            new_floor = (floor or "").strip().lower()
            if new_floor not in _EFFORT_TOKENS or new_floor == "none":
                new_floor = _learned_floor_default()
            self._ensure_loaded()
            with self._lock:
                cur = self._min_effort.get(model_id)
                # Monotonic: only raise the floor, never lower it.
                if cur is not None and _EFFORT_TOKENS.index(cur) >= _EFFORT_TOKENS.index(new_floor):
                    return
                self._min_effort[model_id] = new_floor
                self._maybe_autosave()
            logger.info(
                "[ReasoningProfile] model=%s learned reasoning floor=%s "
                "(rejected reasoning_effort=none — can't disable reasoning, immortal)",
                model_id, new_floor,
            )
        except Exception:  # noqa: BLE001 — never perturb the dispatch path
            logger.debug("[ReasoningProfile] record swallowed", exc_info=True)

    def clear(self, model_id: str) -> None:
        """Drop a model's learned floor. NEVER raises."""
        try:
            self._ensure_loaded()
            with self._lock:
                if self._min_effort.pop(model_id, None) is not None:
                    self._maybe_autosave()
        except Exception:  # noqa: BLE001
            logger.debug("[ReasoningProfile] clear swallowed", exc_info=True)

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def learned_min_effort(self, model_id: str) -> Optional[str]:
        """Return the learned minimum effort for ``model_id``, or None if unknown /
        master-off. Consulted by ``_dw_model_min_effort`` as its adaptive tier.
        NEVER raises."""
        try:
            if not model_id or not reasoning_profile_enabled():
                return None
            self._ensure_loaded()
            with self._lock:
                return self._min_effort.get(model_id)
        except Exception:  # noqa: BLE001
            logger.debug("[ReasoningProfile] read swallowed", exc_info=True)
            return None


# ---------------------------------------------------------------------------
# Process-wide singleton (mirrors get_transport_profile)
# ---------------------------------------------------------------------------

_DEFAULT_PROFILE: Optional[ReasoningProfile] = None
_DEFAULT_LOCK = threading.Lock()


def get_reasoning_profile() -> ReasoningProfile:
    """Return the process-wide ReasoningProfile singleton (lazy, thread-safe,
    rehydrates per fork). NEVER raises — on error returns an in-memory-only
    profile."""
    global _DEFAULT_PROFILE
    try:
        if _DEFAULT_PROFILE is None:
            with _DEFAULT_LOCK:
                if _DEFAULT_PROFILE is None:
                    _DEFAULT_PROFILE = ReasoningProfile()
                    _DEFAULT_PROFILE.load()
        return _DEFAULT_PROFILE
    except Exception:  # noqa: BLE001
        return ReasoningProfile(autosave=False)


def maybe_learn_from_error(model_id: str, reasoning_effort_sent: str, error_text: str) -> bool:
    """Adaptive learn hook for the DW error seam. If ``error_text`` indicates a
    reasoning-rejection AND we sent a disable/under-spend effort, record a floor for
    ``model_id``. Returns True iff a floor was learned. Gated + fail-soft — NEVER
    raises. This is the ONLY coupling the provider needs."""
    try:
        if not model_id or not reasoning_profile_enabled():
            return False
        if not error_indicates_reasoning_rejection(error_text):
            return False
        # Only learn when we actually sent a too-low effort (none/low). A rejection
        # at higher effort is a different problem and must not silently raise floors.
        sent = (reasoning_effort_sent or "").strip().lower()
        if sent not in ("none", ""):
            # If we already sent >= low and it still rejects, step the floor up one.
            try:
                idx = _EFFORT_TOKENS.index(sent)
                floor = _EFFORT_TOKENS[min(idx + 1, len(_EFFORT_TOKENS) - 1)]
            except ValueError:
                floor = _learned_floor_default()
        else:
            floor = _learned_floor_default()
        get_reasoning_profile().record_reasoning_floor(model_id, floor)
        return True
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "ReasoningProfile",
    "get_reasoning_profile",
    "reasoning_profile_enabled",
    "error_indicates_reasoning_rejection",
    "maybe_learn_from_error",
    "SCHEMA_VERSION",
]
