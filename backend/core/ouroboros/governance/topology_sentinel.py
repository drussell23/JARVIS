"""AsyncTopologySentinel — Slice 1 (foundation, no consumers).

The Sentinel replaces the static ``dw_allowed: false`` blocks in
``brain_selection_policy.yaml`` with a live, asynchronous, per-model
health observer driven by:

  1. **Active probing** — context-weighted (light + heavy) probes on a
     jittered exponential backoff schedule (existing
     ``preemption_fsm._compute_backoff_ms`` with ``full_jitter=True``).
  2. **Passive failure ingest** — live-traffic exceptions reported by
     ``candidate_generator`` carry heavier weight than probe failures,
     because they're load-realistic.
  3. **Slow-start ramp** — on OPEN→HALF_OPEN→CLOSED recovery, BG/SPEC
     ops drain through a leaky-bucket capacity schedule rather than
     stampeding the freshly-recovered endpoint.
  4. **Persistent state** — current snapshot + transition history land
     on disk every transition; on boot the Sentinel hydrates from
     ``.jarvis/topology_sentinel_current.json``, refusing to attempt
     DW for the remainder of an in-flight SEVERED window.

This module is the **foundation** layer: state machine, prober, ramp,
persistence. **Slice 1 ships it isolated** — no orchestrator wiring,
no candidate_generator consultation, no provider topology refactor.
The behavior of the running system is byte-identical pre- and post-
merge of Slice 1.

## Authority posture (AST-pinned in tests)

  * **Top-level imports**: stdlib + asyncio + typing only. Every
    governance/provider symbol is imported lazily inside method
    bodies so this module can be loaded without booting the
    orchestrator. Test ``test_top_level_imports_stdlib_only``
    enforces this.
  * **No primitive duplication**: 3-state FSM is
    ``rate_limiter.CircuitBreaker``; backoff is
    ``preemption_fsm._compute_backoff_ms``; rate-limit primitive is
    ``rate_limiter.TokenBucket``. Test ``test_no_local_fsm_or_bucket``
    asserts no class definitions named ``*Breaker`` / ``*Bucket`` /
    ``*Backoff`` exist in this file.
  * **No orchestrator/policy/iron_gate imports**: AST-pinned. The
    Sentinel is a pure observer; cascade-decision authority lives in
    ``candidate_generator`` (Slice 3).
  * **NEVER raises**: every public method swallows exceptions and
    returns a fail-safe value. ``get_state`` defaults to
    ``BreakerState.CLOSED`` when the sentinel is uninitialized so
    Slice 3's cascade matrix can interpret "unknown" as "let it try"
    rather than "force cascade" (which would be a cascade storm on
    boot before the first probe completes).

## Boot-loop protection (the directive's primary correctness goal)

A process killed mid-SEVERED would, in a memory-only design, init
its replacement to CLOSED on next boot — first BG op would attempt
DW, stream-stall, cascade to Claude. Repeat per-op until the in-
memory streak rebuilds. That is the boot-loop Claude-burn the
directive forbids.

Mitigation: the persisted snapshot carries ``state``, ``opened_at``,
and ``backoff_idx``. On hydrate, if ``state == OPEN`` and
``opened_at + recovery_timeout_s > now``, the new sentinel rebuilds
its ``CircuitBreaker`` already in OPEN with the original
``opened_at``. The next ``check()`` therefore reproduces exactly the
same rejection that would have fired pre-restart. No probe burn,
no Claude burn, no thrash.

## Master flag

``JARVIS_TOPOLOGY_SENTINEL_ENABLED`` (default ``false``). When off,
the module is fully importable and tests pass; the singleton
``get_default_sentinel()`` returns a sentinel with ``start()`` not
called and every ``get_state(...)`` returning ``CLOSED`` (fail-open
to legacy yaml authority — no behavior change).

When on, callers (Slice 3+) consult ``get_state`` for routing
decisions. The Sentinel itself doesn't change cascade behavior;
that's the cascade-matrix in candidate_generator's job.
"""
from __future__ import annotations

import asyncio
import contextvars
import enum
import json
import logging
import os
import random
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slice 3.6 — ContextVar for per-attempt DW model override
# ---------------------------------------------------------------------------
#
# Slice 3 stamped ``ctx._dw_model_override`` directly on the
# OperationContext, but that dataclass is frozen — setattr raised
# FrozenInstanceError (subclass of AttributeError), the dispatcher
# caught it silently, returned None, and fell through to the legacy
# yaml gate. Session bt-2026-04-27-203746 demonstrated this with
# Slice 3.5's preflight loud signal: 5 healthy preflight log lines +
# 0 sentinel dispatch attempts. Slice 3.5's value: it surfaced the
# defect; Slice 3.6's value: it fixes it without breaking the frozen
# contract.
#
# ``contextvars.ContextVar`` is Python's async-safe per-task storage.
# Each asyncio task gets its own value (no thread leakage); each
# .set() returns a token that .reset() restores; recursion-safe via
# the contextvar context inheritance. The dispatcher stamps the var
# before each per-attempt DW call; the DoublewordProvider's
# ``_resolve_effective_model`` reads the var; the dispatcher resets
# the var on attempt completion (success / failure / cascade).
#
# Bonus: ContextVar is observable via /sentinel REPL (Slice 4 hook).
# A future operator can ``DW_MODEL_OVERRIDE_VAR.get()`` to see what
# model the current task is attempting WITHOUT having to thread it
# through every API call.

DW_MODEL_OVERRIDE_VAR: "contextvars.ContextVar[Optional[str]]" = (
    contextvars.ContextVar(
        "dw_model_override", default=None,
    )
)


def get_dw_model_override() -> Optional[str]:
    """Read the per-task DW model override.

    Returns the value the dispatcher most recently set for the
    current asyncio task (or ``None`` if the dispatcher hasn't
    stamped it). The DW provider's ``_resolve_effective_model``
    consults this BEFORE the legacy ``model_for_route`` mapping so
    sentinel-driven dispatch wins.

    NEVER raises. ContextVar API is exception-free for ``get`` /
    ``set`` / ``reset``."""
    try:
        return DW_MODEL_OVERRIDE_VAR.get()
    except LookupError:
        # Cannot happen with a default-set ContextVar, but defensive.
        return None


def set_dw_model_override(model_id: Optional[str]) -> Any:
    """Stamp the per-task DW model override. Returns the
    ``contextvars.Token`` the caller MUST pass to
    :func:`reset_dw_model_override` in a finally block to restore
    the previous value (typically ``None`` outside any attempt).

    Async-safe: each asyncio task has its own value. Calling this
    inside a task does NOT leak to sibling tasks.
    """
    return DW_MODEL_OVERRIDE_VAR.set(model_id)


def reset_dw_model_override(token: Any) -> None:
    """Restore the per-task DW model override to its prior value.
    NEVER raises. Token-mismatch / wrong-type is silently ignored
    (defensive — caller might pass a stale token from a prior task,
    or a non-Token sentinel during test teardown)."""
    try:
        DW_MODEL_OVERRIDE_VAR.reset(token)
    except (ValueError, LookupError, TypeError):
        logger.debug(
            "[TopologySentinel] dw_model_override reset_token mismatch — "
            "ignored (likely test fixture cleanup race)",
        )


SCHEMA_VERSION = "topology_sentinel.1"


# ---------------------------------------------------------------------------
# Boundary isolation — explicit env propagation contract (Slice 3.5)
# ---------------------------------------------------------------------------
#
# Per the 2026-04-27 directive ("Process Boundary Isolation & Pre-Flight
# Handshake"): the harness MUST NOT rely on implicit env inheritance to
# reach the orchestrator subprocess. Every env var the sentinel layer
# consumes is enumerated here so ``live_fire_soak._build_env_for_flag``
# can forward them explicitly + so a future operator can grep this
# constant to discover the full sentinel-related env surface.
#
# Adding a new sentinel env var? Add it here AND update
# ``test_sentinel_env_propagation_contract`` (which asserts every
# JARVIS_TOPOLOGY_* var the module reads is in this list).

_SENTINEL_PROPAGATED_VARS: Tuple[str, ...] = (
    # Master flag
    "JARVIS_TOPOLOGY_SENTINEL_ENABLED",
    "JARVIS_TOPOLOGY_FORCE_SEVERED",
    # Slice 3a — active recovery from successful catalog probes
    "JARVIS_TOPOLOGY_ACTIVE_RECOVERY_ENABLED",
    # Threshold + decay
    "JARVIS_TOPOLOGY_SEVERED_THRESHOLD_WEIGHTED",
    "JARVIS_TOPOLOGY_SUCCESS_DECAY",
    # Probe schedule
    "JARVIS_TOPOLOGY_HEALTHY_PROBE_INTERVAL_S",
    "JARVIS_TOPOLOGY_PROBE_BACKOFF_BASE_S",
    "JARVIS_TOPOLOGY_PROBE_BACKOFF_CAP_S",
    "JARVIS_TOPOLOGY_HEAVY_PROBE_RATIO",
    "JARVIS_TOPOLOGY_LIGHT_PROBE_FIRST_TOKEN_TIMEOUT_S",
    "JARVIS_TOPOLOGY_HEAVY_PROBE_TOTAL_TIMEOUT_S",
    "JARVIS_TOPOLOGY_HEAVY_PROBE_MAX_TOKENS",
    "JARVIS_TOPOLOGY_PROBE_DAILY_USD_CAP",
    # Slow-start ramp
    "JARVIS_TOPOLOGY_RAMP_SCHEDULE",
    "JARVIS_TOPOLOGY_RAMP_MAX_WAIT_S",
    # State persistence
    "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR",
    "JARVIS_TOPOLOGY_SENTINEL_HISTORY_SIZE",
    "JARVIS_TOPOLOGY_STATE_MAX_AGE_S",
    # Per-source weight overrides
    "JARVIS_TOPOLOGY_WEIGHT_LIVE_STREAM_STALL",
    "JARVIS_TOPOLOGY_WEIGHT_LIVE_TRANSPORT",
    "JARVIS_TOPOLOGY_WEIGHT_LIVE_HTTP_5XX",
    "JARVIS_TOPOLOGY_WEIGHT_LIVE_HTTP_429",
    "JARVIS_TOPOLOGY_WEIGHT_LIVE_PARSE_ERROR",
    "JARVIS_TOPOLOGY_WEIGHT_HEAVY_PROBE_FAIL",
    "JARVIS_TOPOLOGY_WEIGHT_LIGHT_PROBE_FAIL",
    "JARVIS_TOPOLOGY_WEIGHT_LIGHT_PROBE_TIMEOUT",
)


def sentinel_propagated_vars() -> Tuple[str, ...]:
    """Tuple of env var names the harness MUST forward into the
    orchestrator subprocess. Read by
    ``live_fire_soak._build_env_for_flag`` for explicit propagation.

    Never raises. Stable ordering — operators can diff this surface
    over time without churning the harness."""
    return _SENTINEL_PROPAGATED_VARS


# ---------------------------------------------------------------------------
# Env helpers — same idiom as posture_observer / posture_store
# ---------------------------------------------------------------------------


_TRUTHY = ("1", "true", "yes", "on")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def _env_path(name: str, default: str) -> Path:
    raw = os.environ.get(name)
    return Path(raw) if raw else Path(default)


# ---------------------------------------------------------------------------
# Master flag + tunables
# ---------------------------------------------------------------------------


def is_sentinel_enabled() -> bool:
    """Master flag — ``JARVIS_TOPOLOGY_SENTINEL_ENABLED`` (default
    ``false``). When off, ``get_state`` returns ``CLOSED`` for every
    ``model_id`` and consumers MUST treat the verdict as advisory
    (legacy static yaml stays authoritative)."""
    return _env_bool("JARVIS_TOPOLOGY_SENTINEL_ENABLED", default=False)


def topology_active_recovery_enabled() -> bool:
    """Slice 3a — self-healing recovery from a successful catalog probe.

    ``JARVIS_TOPOLOGY_ACTIVE_RECOVERY_ENABLED`` (default ``true``).
    When on, ``apply_health_probe_result(success=True)`` resets every
    ``TERMINAL_OPEN`` breaker (the catalog probe itself just proved
    reachability — auth/modality verdicts may have been transient or
    are stale under a new snapshot) AND calls ``record_success`` on
    every ``HALF_OPEN`` breaker (decays the weighted streak; pushes
    HALF_OPEN→CLOSED). Hot-revert path: ``export
    JARVIS_TOPOLOGY_ACTIVE_RECOVERY_ENABLED=false`` returns the runner
    to legacy "reset only on snapshot id change" behavior."""
    return _env_bool(
        "JARVIS_TOPOLOGY_ACTIVE_RECOVERY_ENABLED", default=True,
    )


def severed_threshold_weighted() -> float:
    """Weighted-sum threshold to trip CLOSED→OPEN. Default 3.0 means a
    single live-traffic stream-stall (weight 3.0) trips alone, OR three
    light probe failures (weight 1.0 each) trip together."""
    return _env_float(
        "JARVIS_TOPOLOGY_SEVERED_THRESHOLD_WEIGHTED", default=3.0,
        minimum=0.5,
    )


def heavy_probe_ratio() -> float:
    """Fraction of probes that are heavy (mid-weight payload). Default
    0.2 = 1-in-5 probes test full-stream throughput."""
    raw = _env_float(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_RATIO", default=0.2, minimum=0.0,
    )
    return min(1.0, raw)


def light_probe_first_token_timeout_s() -> float:
    return _env_float(
        "JARVIS_TOPOLOGY_LIGHT_PROBE_FIRST_TOKEN_TIMEOUT_S",
        default=2.0, minimum=0.5,
    )


def heavy_probe_total_timeout_s() -> float:
    return _env_float(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_TOTAL_TIMEOUT_S",
        default=15.0, minimum=2.0,
    )


def heavy_probe_max_tokens() -> int:
    return _env_int(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_MAX_TOKENS",
        default=500, minimum=10,
    )


def probe_backoff_base_s() -> float:
    """Base interval for the jittered probe schedule. Equivalent to
    ``backoff_base_seconds`` in ``RetryBudget``."""
    return _env_float(
        "JARVIS_TOPOLOGY_PROBE_BACKOFF_BASE_S",
        default=10.0, minimum=1.0,
    )


def probe_backoff_cap_s() -> float:
    """Cap on probe interval. Equivalent to ``backoff_cap_seconds``."""
    return _env_float(
        "JARVIS_TOPOLOGY_PROBE_BACKOFF_CAP_S",
        default=300.0, minimum=10.0,
    )


def healthy_probe_interval_s() -> float:
    """Interval between probes when the endpoint is CLOSED (HEALTHY).
    Single fixed value; backoff applies only to OPEN-state recovery."""
    return _env_float(
        "JARVIS_TOPOLOGY_HEALTHY_PROBE_INTERVAL_S",
        default=30.0, minimum=5.0,
    )


def state_max_age_s() -> float:
    """Maximum age of a hydrated current.json snapshot. Older
    snapshots are discarded (cold-start). Default 1h — long enough to
    survive routine restarts, short enough that an old SEVERED state
    doesn't pin a now-recovered endpoint."""
    return _env_float(
        "JARVIS_TOPOLOGY_STATE_MAX_AGE_S", default=3600.0, minimum=60.0,
    )


def probe_daily_usd_cap() -> float:
    """Soft cap on daily probe spend. When breached, the active
    prober short-circuits to no-op (returns CLOSED for state queries —
    fail-open to availability rather than fail-closed to cascade
    storm). Default $1.00."""
    return _env_float(
        "JARVIS_TOPOLOGY_PROBE_DAILY_USD_CAP",
        default=1.0, minimum=0.0,
    )


def history_size() -> int:
    return max(
        16,
        _env_int(
            "JARVIS_TOPOLOGY_SENTINEL_HISTORY_SIZE",
            default=512, minimum=16,
        ),
    )


def force_severed() -> bool:
    """Operator panic switch — pin every endpoint OPEN. Used during
    incidents when the operator wants every cascade-eligible op to
    skip DW even if the sentinel thinks it's healthy."""
    return _env_bool("JARVIS_TOPOLOGY_FORCE_SEVERED", default=False)


def state_dir() -> Path:
    """Directory holding ``topology_sentinel_*.{json,jsonl}``. Default
    ``.jarvis/`` matches every other governance disk artifact."""
    return _env_path(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR",
        default=str(Path(".jarvis").resolve()),
    )


# ---------------------------------------------------------------------------
# Failure classification — live > heavy > light
# ---------------------------------------------------------------------------


class FailureSource(str, enum.Enum):
    """Source of a failure signal. Drives the weight toward the
    weighted-streak threshold."""

    LIVE_STREAM_STALL = "live_stream_stall"        # 3.0 — single-occurrence trip
    LIVE_TRANSPORT = "live_transport"              # 1.0
    LIVE_HTTP_5XX = "live_http_5xx"                # 1.0
    LIVE_HTTP_429 = "live_http_429"                # 0.5 (transient, upstream-handled)
    LIVE_PARSE_ERROR = "live_parse_error"          # 1.0
    HEAVY_PROBE_FAIL = "heavy_probe_fail"          # 1.5
    LIGHT_PROBE_FAIL = "light_probe_fail"          # 1.0
    LIGHT_PROBE_TIMEOUT = "light_probe_timeout"    # 1.0


_DEFAULT_FAILURE_WEIGHTS: Dict[FailureSource, float] = {
    FailureSource.LIVE_STREAM_STALL: 3.0,
    FailureSource.LIVE_TRANSPORT: 1.0,
    FailureSource.LIVE_HTTP_5XX: 1.0,
    FailureSource.LIVE_HTTP_429: 0.5,
    FailureSource.LIVE_PARSE_ERROR: 1.0,
    FailureSource.HEAVY_PROBE_FAIL: 1.5,
    FailureSource.LIGHT_PROBE_FAIL: 1.0,
    FailureSource.LIGHT_PROBE_TIMEOUT: 1.0,
}


def failure_weight(source: FailureSource) -> float:
    """Per-source weight, with optional env overrides
    ``JARVIS_TOPOLOGY_WEIGHT_<SOURCE_UPPER>``. Bounded to
    ``[0.0, 10.0]`` to prevent absurd configs from disabling the
    streak entirely or instantly tripping on noise."""
    env_name = f"JARVIS_TOPOLOGY_WEIGHT_{source.name}"
    default = _DEFAULT_FAILURE_WEIGHTS[source]
    raw = _env_float(env_name, default=default, minimum=0.0)
    return min(10.0, raw)


def success_decay() -> float:
    """Weighted-streak decay applied on each ``report_success``.
    Slow forgetting (default 0.5) prevents flapping — a single
    success doesn't immediately erase a real failure history."""
    return _env_float(
        "JARVIS_TOPOLOGY_SUCCESS_DECAY", default=0.5, minimum=0.0,
    )


# ---------------------------------------------------------------------------
# Probe domain types
# ---------------------------------------------------------------------------


class ProbeWeight(str, enum.Enum):
    LIGHT = "light"
    HEAVY = "heavy"


class ProbeOutcome(str, enum.Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ProbeResult:
    model_id: str
    weight: ProbeWeight
    outcome: ProbeOutcome
    latency_s: float
    failure_source: Optional[FailureSource] = None
    failure_detail: str = ""
    cost_usd: float = 0.0
    ts_epoch: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Snapshot + transition record
# ---------------------------------------------------------------------------


@dataclass
class EndpointSnapshot:
    """Per-``model_id`` state, captured durably to
    ``topology_sentinel_current.json``. Fields are denormalized from
    the ``CircuitBreaker`` instance so a fresh process can rebuild
    the breaker faithfully on hydrate."""

    model_id: str
    state: str                          # "CLOSED" / "OPEN" / "HALF_OPEN" (BreakerState.value)
    weighted_failure_streak: float = 0.0
    consecutive_passes: int = 0
    backoff_idx: int = 0                # retry_index for _compute_backoff_ms
    opened_at_epoch: float = 0.0        # wall-clock; survives restart
    last_transition_at_epoch: float = field(default_factory=time.time)
    last_failure_source: Optional[str] = None
    last_failure_detail: str = ""
    last_probe_at_epoch: float = 0.0
    last_probe_outcome: Optional[str] = None
    ramp_phase: int = 0                 # 0=full, 1+=ramp tier (slow-start)
    schema_version: str = SCHEMA_VERSION

    def to_json(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "state": self.state,
            "weighted_failure_streak": self.weighted_failure_streak,
            "consecutive_passes": self.consecutive_passes,
            "backoff_idx": self.backoff_idx,
            "opened_at_epoch": self.opened_at_epoch,
            "last_transition_at_epoch": self.last_transition_at_epoch,
            "last_failure_source": self.last_failure_source,
            "last_failure_detail": self.last_failure_detail[:200],
            "last_probe_at_epoch": self.last_probe_at_epoch,
            "last_probe_outcome": self.last_probe_outcome,
            "ramp_phase": self.ramp_phase,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_json(cls, payload: Dict[str, Any]) -> Optional["EndpointSnapshot"]:
        if payload.get("schema_version") != SCHEMA_VERSION:
            return None
        try:
            return cls(
                model_id=str(payload["model_id"]),
                state=str(payload.get("state", "CLOSED")),
                weighted_failure_streak=float(
                    payload.get("weighted_failure_streak", 0.0),
                ),
                consecutive_passes=int(
                    payload.get("consecutive_passes", 0),
                ),
                backoff_idx=int(payload.get("backoff_idx", 0)),
                opened_at_epoch=float(
                    payload.get("opened_at_epoch", 0.0),
                ),
                last_transition_at_epoch=float(
                    payload.get(
                        "last_transition_at_epoch", time.time(),
                    ),
                ),
                last_failure_source=payload.get("last_failure_source"),
                last_failure_detail=str(
                    payload.get("last_failure_detail", ""),
                ),
                last_probe_at_epoch=float(
                    payload.get("last_probe_at_epoch", 0.0),
                ),
                last_probe_outcome=payload.get("last_probe_outcome"),
                ramp_phase=int(payload.get("ramp_phase", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "[TopologySentinel] EndpointSnapshot.from_json failed: %s",
                exc,
            )
            return None


@dataclass(frozen=True)
class TransitionRecord:
    """One row of ``topology_sentinel_history.jsonl``. Captured on
    every state change AND on every probe (transition_kind="probe")
    so the audit log is complete."""

    ts_epoch: float
    model_id: str
    transition_kind: str                # "state_change" | "probe" | "failure_report"
    from_state: str = ""
    to_state: str = ""
    weighted_failure_streak: float = 0.0
    failure_source: Optional[str] = None
    failure_detail: str = ""
    probe_weight: Optional[str] = None
    probe_outcome: Optional[str] = None
    probe_latency_s: float = 0.0
    probe_cost_usd: float = 0.0
    schema_version: str = SCHEMA_VERSION

    def to_json(self) -> Dict[str, Any]:
        return {
            "ts_epoch": self.ts_epoch,
            "model_id": self.model_id,
            "transition_kind": self.transition_kind,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "weighted_failure_streak": self.weighted_failure_streak,
            "failure_source": self.failure_source,
            "failure_detail": self.failure_detail[:200],
            "probe_weight": self.probe_weight,
            "probe_outcome": self.probe_outcome,
            "probe_latency_s": self.probe_latency_s,
            "probe_cost_usd": self.probe_cost_usd,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# SentinelStateStore — disk persistence (mirrors PostureStore idiom)
# ---------------------------------------------------------------------------


class SentinelStateStore:
    """Durable triplet under ``state_dir()``:

      * ``topology_sentinel_current.json``  — Dict[model_id, snapshot],
        atomic temp+rename.
      * ``topology_sentinel_history.jsonl`` — append-only ring trimmed
        to ``history_size()``.
      * ``topology_sentinel.lock``           — single-writer flock-style
        guard via ``threading.Lock`` (the orchestrator process is
        single — multi-process locking is out of scope; the lock
        prevents in-process races between probe loop and
        report_failure callers).
    """

    def __init__(
        self,
        directory: Optional[Path] = None,
        history_capacity: Optional[int] = None,
    ) -> None:
        self._dir = Path(directory) if directory else state_dir()
        self._capacity = (
            history_capacity if history_capacity is not None
            else history_size()
        )
        self._lock = threading.Lock()

    @property
    def current_path(self) -> Path:
        return self._dir / "topology_sentinel_current.json"

    @property
    def history_path(self) -> Path:
        return self._dir / "topology_sentinel_history.jsonl"

    def _ensure_dir(self) -> bool:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            return True
        except OSError as exc:
            logger.warning(
                "[TopologySentinel] cannot create state dir %s: %s",
                self._dir, exc,
            )
            return False

    def hydrate(self) -> Dict[str, EndpointSnapshot]:
        """Read the current snapshot map. Returns an empty dict on any
        failure — caller treats as cold-start."""
        if not self.current_path.exists():
            return {}
        try:
            payload = json.loads(self.current_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "[TopologySentinel] hydrate read failed: %s", exc,
            )
            return {}
        if not isinstance(payload, dict):
            return {}
        if payload.get("schema_version") != SCHEMA_VERSION:
            logger.info(
                "[TopologySentinel] schema mismatch (got %r), cold-starting",
                payload.get("schema_version"),
            )
            return {}
        # Age check — discard if too old (safety net for systems left
        # offline for days: an OPEN state from 3 days ago is meaningless).
        snapshot_ts = float(payload.get("written_at_epoch", 0.0))
        if snapshot_ts <= 0.0:
            return {}
        if (time.time() - snapshot_ts) > state_max_age_s():
            logger.info(
                "[TopologySentinel] snapshot age %.1fs exceeds max %s; "
                "cold-starting",
                time.time() - snapshot_ts, state_max_age_s(),
            )
            return {}
        snapshots: Dict[str, EndpointSnapshot] = {}
        for model_id, raw in (payload.get("endpoints") or {}).items():
            snap = EndpointSnapshot.from_json(raw)
            if snap is not None:
                snapshots[model_id] = snap
        return snapshots

    def write_current(
        self, snapshots: Dict[str, EndpointSnapshot],
    ) -> bool:
        """Atomic temp+rename so readers never see a torn write."""
        if not self._ensure_dir():
            return False
        payload: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "written_at_epoch": time.time(),
            "endpoints": {
                mid: snap.to_json() for mid, snap in snapshots.items()
            },
        }
        with self._lock:
            try:
                fd, tmp = tempfile.mkstemp(
                    prefix="topology_sentinel_current_",
                    suffix=".json.tmp",
                    dir=str(self._dir),
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh, indent=2, sort_keys=True)
                    os.replace(tmp, self.current_path)
                    return True
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            except OSError as exc:
                logger.warning(
                    "[TopologySentinel] write_current failed: %s", exc,
                )
                return False

    def append_history(self, record: TransitionRecord) -> bool:
        if not self._ensure_dir():
            return False
        line = json.dumps(record.to_json(), sort_keys=True) + "\n"
        with self._lock:
            try:
                with open(self.history_path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError as exc:
                logger.warning(
                    "[TopologySentinel] append_history failed: %s", exc,
                )
                return False
            self._maybe_trim_history()
        return True

    def _maybe_trim_history(self) -> None:
        """Bounded trim: keep only the last ``capacity`` lines."""
        try:
            with open(self.history_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return
        if len(lines) <= self._capacity:
            return
        kept = lines[-self._capacity:]
        try:
            fd, tmp = tempfile.mkstemp(
                prefix="topology_sentinel_history_",
                suffix=".jsonl.tmp",
                dir=str(self._dir),
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.writelines(kept)
            os.replace(tmp, self.history_path)
        except OSError as exc:
            logger.debug(
                "[TopologySentinel] history trim failed: %s", exc,
            )


# ---------------------------------------------------------------------------
# SlowStartRamp — wraps rate_limiter.TokenBucket; BG/SPEC concurrency cap
# ---------------------------------------------------------------------------


# Default ramp schedule: (seconds_since_close, capacity). Operators can
# override the entire schedule via JARVIS_TOPOLOGY_RAMP_SCHEDULE. Each
# tuple says "by elapsed t, allow up to N concurrent ops/sec for BG/SPEC."
_DEFAULT_RAMP_SCHEDULE: Tuple[Tuple[float, float], ...] = (
    (0.0, 1.0),
    (10.0, 2.0),
    (30.0, 4.0),
    (60.0, 8.0),
    (120.0, 16.0),    # baseline cap; downstream BG_POOL_SIZE clamps further
)


def parse_ramp_schedule_env() -> Tuple[Tuple[float, float], ...]:
    """Parse ``JARVIS_TOPOLOGY_RAMP_SCHEDULE`` of the form
    ``"0:1.0,10:2.0,30:4.0,..."``. Returns the default if unset or
    malformed."""
    raw = os.environ.get("JARVIS_TOPOLOGY_RAMP_SCHEDULE", "").strip()
    if not raw:
        return _DEFAULT_RAMP_SCHEDULE
    try:
        steps: List[Tuple[float, float]] = []
        for part in raw.split(","):
            t_str, c_str = part.split(":", 1)
            t = max(0.0, float(t_str.strip()))
            c = max(0.0, float(c_str.strip()))
            steps.append((t, c))
        steps.sort(key=lambda x: x[0])
        return tuple(steps) if steps else _DEFAULT_RAMP_SCHEDULE
    except (ValueError, IndexError):
        logger.warning(
            "[TopologySentinel] malformed ramp schedule %r; "
            "using default", raw,
        )
        return _DEFAULT_RAMP_SCHEDULE


def ramp_max_wait_s() -> float:
    """Per-acquire wait cap when the ramp is throttling. If the
    bucket would block longer than this, ``try_acquire`` returns
    ``(False, ...)`` so the BG worker re-queues rather than holds a
    pool slot indefinitely. Default 10s."""
    return _env_float(
        "JARVIS_TOPOLOGY_RAMP_MAX_WAIT_S",
        default=10.0, minimum=0.5,
    )


class SlowStartRamp:
    """BG/SPEC concurrency ramp on OPEN→CLOSED recovery.

    Composes ``rate_limiter.TokenBucket`` whose ``set_throttle(m)`` is
    the public primitive for "scale my effective refill rate by m"
    (m ∈ (0, 1]). Ramp = wall-clock schedule that calls
    ``set_throttle`` with progressively higher m until full baseline
    rate is restored. No bucket-internals reach-in.

    Failure during ramp (``register_failure()``) calls ``deactivate``
    + ``activate`` to restart the schedule from t=0; the breaker
    re-trip is the caller's responsibility (sentinel does it inside
    ``report_failure``).

    BG/SPEC-only — IMMEDIATE/STANDARD/COMPLEX cascade decisions are
    urgency-gated by the Slice 3 cascade matrix and bypass the ramp
    entirely because user-driven traffic is itself the recovery test.
    """

    def __init__(
        self,
        schedule: Optional[Tuple[Tuple[float, float], ...]] = None,
        max_wait_s: Optional[float] = None,
    ) -> None:
        self._schedule = schedule or parse_ramp_schedule_env()
        self._max_wait_s = (
            max_wait_s if max_wait_s is not None else ramp_max_wait_s()
        )
        self._closed_at: float = 0.0   # 0 = ramp inactive
        self._bucket: Any = None        # rate_limiter.TokenBucket; lazy
        self._failure_resets: int = 0   # observability
        self._lock = threading.Lock()

    @property
    def baseline_capacity_per_s(self) -> float:
        """Capacity at the final schedule tier — corresponds to
        ``set_throttle(1.0)`` on the underlying TokenBucket."""
        return self._schedule[-1][1]

    def _capacity_for(self, elapsed: float) -> float:
        capacity = self._schedule[0][1]
        for t, c in self._schedule:
            if elapsed >= t:
                capacity = c
            else:
                break
        return capacity

    def _throttle_for(self, elapsed: float) -> float:
        baseline = self.baseline_capacity_per_s
        if baseline <= 0:
            return 1.0
        return min(1.0, max(0.01, self._capacity_for(elapsed) / baseline))

    def _ensure_bucket(self) -> Any:
        if self._bucket is None:
            from backend.core.ouroboros.governance.rate_limiter import (
                MemoryRateLimitStore, TokenBucket,
            )
            # rpm = ops_per_sec × 60. Burst = at most one second of
            # baseline so a queue surge can't stampede past the ramp.
            baseline = self.baseline_capacity_per_s
            rpm = max(1, int(round(baseline * 60.0)))
            burst = max(1, int(round(baseline)))
            self._bucket = TokenBucket(
                key="topology_sentinel_ramp",
                store=MemoryRateLimitStore(),
                rpm=rpm,
                burst=burst,
            )
        return self._bucket

    def activate(self) -> None:
        """Start the ramp clock. Called when the breaker transitions
        HALF_OPEN→CLOSED."""
        with self._lock:
            self._closed_at = time.monotonic()
            self._ensure_bucket()
            # Throttle to entry tier — first token issued at t=0 only,
            # subsequent tokens accrue at the (low) ramp rate.
            self._bucket.set_throttle(self._throttle_for(0.0))

    def deactivate(self) -> None:
        """Cancel the ramp (e.g. breaker re-tripped to OPEN, or ramp
        finished and we want full throughput restored)."""
        with self._lock:
            self._closed_at = 0.0
            if self._bucket is not None:
                self._bucket.set_throttle(1.0)

    def is_active(self) -> bool:
        return self._closed_at > 0.0

    def current_capacity(self) -> float:
        if not self.is_active():
            return self.baseline_capacity_per_s
        elapsed = time.monotonic() - self._closed_at
        if elapsed >= self._schedule[-1][0]:
            # Schedule complete — auto-deactivate to baseline.
            self.deactivate()
            return self.baseline_capacity_per_s
        return self._capacity_for(elapsed)

    async def try_acquire(self) -> Tuple[bool, float]:
        """Returns ``(allowed, wait_s)``. NEVER raises.

          * ``allowed=True``  — a token was acquired (bucket may have
            slept up to ``max_wait_s`` for refill); BG worker proceeds.
          * ``allowed=False`` — bucket would have slept longer than
            ``max_wait_s``; BG worker should re-queue the op rather
            than tie up a pool slot.

        When the ramp is inactive returns ``(True, 0.0)`` — full
        throughput, no throttle applied."""
        if not self.is_active():
            return (True, 0.0)
        # Sync the bucket's throttle to the current ramp tier.
        elapsed = time.monotonic() - self._closed_at
        bucket = self._ensure_bucket()
        bucket.set_throttle(self._throttle_for(elapsed))
        try:
            wait_s = await asyncio.wait_for(
                bucket.acquire(1), timeout=self._max_wait_s,
            )
            return (True, wait_s)
        except asyncio.TimeoutError:
            return (False, self._max_wait_s)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[TopologySentinel] ramp.try_acquire failed: %s", exc,
            )
            return (True, 0.0)   # fail-open — don't starve BG on ramp bug

    def register_failure(self) -> None:
        """Snap ramp back to entry tier on a failure during ramp. The
        sentinel calls this BEFORE re-tripping the breaker; when the
        breaker re-trips, ``deactivate()`` follows from the
        report_failure path."""
        with self._lock:
            self._failure_resets += 1
            if self._bucket is None or self._closed_at == 0.0:
                return
            # Restart the schedule clock at t=0 (entry tier).
            self._closed_at = time.monotonic()
            self._bucket.set_throttle(self._throttle_for(0.0))

    def snapshot(self) -> Dict[str, Any]:
        return {
            "active": self.is_active(),
            "elapsed_s": (
                time.monotonic() - self._closed_at
                if self.is_active() else 0.0
            ),
            "current_capacity": self.current_capacity(),
            "baseline_capacity": self.baseline_capacity_per_s,
            "failure_resets": self._failure_resets,
            "schedule": list(self._schedule),
            "max_wait_s": self._max_wait_s,
        }


# ---------------------------------------------------------------------------
# ContextWeightedProber — light + heavy probe orchestration
# ---------------------------------------------------------------------------


# Probe payload = callable returning (outcome, latency_s, cost_usd, detail).
# The default factory wires it to DoublewordProvider.complete_sync; tests
# inject a deterministic mock.
ProbeFn = Callable[[str, ProbeWeight], Awaitable[ProbeResult]]


class ContextWeightedProber:
    """Generates probes. State-free; the sentinel owns scheduling and
    state. Defers to the provided ``probe_fn`` for the actual call —
    any DW provider must satisfy the ``ProbeFn`` shape. The probe_fn
    NEVER raises; failures are reported via ``ProbeResult.outcome``."""

    def __init__(
        self,
        probe_fn: ProbeFn,
        heavy_ratio: Optional[float] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._probe_fn = probe_fn
        self._heavy_ratio = (
            heavy_ratio if heavy_ratio is not None else heavy_probe_ratio()
        )
        self._rng = rng or random.Random()
        self._counter = 0

    def pick_weight(self) -> ProbeWeight:
        """Deterministic-ish 1-in-N heavy schedule. Uses an internal
        counter so every Nth probe is heavy; jitters ±1 via RNG so
        probe schedules across processes desync."""
        self._counter += 1
        if self._heavy_ratio <= 0.0:
            return ProbeWeight.LIGHT
        if self._heavy_ratio >= 1.0:
            return ProbeWeight.HEAVY
        # Expected-period heavy probe: 1/heavy_ratio. Small RNG nudge.
        period = max(2, int(round(1.0 / self._heavy_ratio)))
        nudge = self._rng.randint(0, max(0, period - 1))
        if (self._counter + nudge) % period == 0:
            return ProbeWeight.HEAVY
        return ProbeWeight.LIGHT

    async def probe(self, model_id: str) -> ProbeResult:
        weight = self.pick_weight()
        try:
            return await self._probe_fn(model_id, weight)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[TopologySentinel] probe_fn raised: %s", exc,
            )
            return ProbeResult(
                model_id=model_id, weight=weight,
                outcome=ProbeOutcome.FAIL, latency_s=0.0,
                failure_source=(
                    FailureSource.HEAVY_PROBE_FAIL
                    if weight == ProbeWeight.HEAVY
                    else FailureSource.LIGHT_PROBE_FAIL
                ),
                failure_detail=(
                    f"probe_fn_raised:{type(exc).__name__}"
                ),
            )


# ---------------------------------------------------------------------------
# TopologySentinel — coordinator
# ---------------------------------------------------------------------------


class TopologySentinel:
    """Per-``model_id`` health observer. Composes existing primitives.

    Threading: state mutations are guarded by ``self._lock`` so the
    probe loop (asyncio task) and synchronous failure-report calls
    from candidate_generator (Slice 4) interleave safely. ``get_state``
    reads through the breaker which has its own atomic semantics.
    """

    def __init__(
        self,
        prober: Optional[ContextWeightedProber] = None,
        store: Optional[SentinelStateStore] = None,
        weighted_threshold: Optional[float] = None,
    ) -> None:
        self._prober = prober
        self._store = store or SentinelStateStore()
        self._threshold = (
            weighted_threshold if weighted_threshold is not None
            else severed_threshold_weighted()
        )
        self._breakers: Dict[str, Any] = {}     # model_id -> CircuitBreaker
        self._snapshots: Dict[str, EndpointSnapshot] = {}
        self._ramps: Dict[str, SlowStartRamp] = {}
        # RLock — force_severed/force_healthy hold the lock then call
        # register_endpoint which also acquires it. Same re-entrancy
        # pattern that bit posture_observer (slice5_arc_a fix).
        self._lock = threading.RLock()
        self._probe_task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._daily_probe_cost_usd = 0.0
        self._daily_probe_window_start = time.time()
        self._listeners: List[Callable[[TransitionRecord], None]] = []

    # -- imports (lazy) -----------------------------------------------------

    @staticmethod
    def _BreakerCls() -> Any:
        from backend.core.ouroboros.governance.rate_limiter import (
            CircuitBreaker,
        )
        return CircuitBreaker

    @staticmethod
    def _BreakerStateCls() -> Any:
        from backend.core.ouroboros.governance.rate_limiter import (
            BreakerState,
        )
        return BreakerState

    @staticmethod
    def _compute_backoff(retry_index: int) -> float:
        """Wraps ``preemption_fsm._compute_backoff_ms`` with a
        ``RetryBudget`` constructed from the sentinel's env knobs.
        Returns seconds (not ms) for ``asyncio.sleep`` ergonomics."""
        from backend.core.ouroboros.governance.contracts.fsm_contract import (
            RetryBudget,
        )
        from backend.core.ouroboros.governance.preemption_fsm import (
            _compute_backoff_ms,
        )
        budget = RetryBudget(
            backoff_base_seconds=probe_backoff_base_s(),
            backoff_cap_seconds=probe_backoff_cap_s(),
            full_jitter=True,
        )
        return _compute_backoff_ms(retry_index, budget) / 1000.0

    # -- public API ---------------------------------------------------------

    def register_endpoint(
        self,
        model_id: str,
        failure_threshold: int = 3,
        recovery_timeout_s: float = 30.0,
    ) -> None:
        """Idempotent — register a new endpoint, hydrating from disk
        if a snapshot exists. Safe to call repeatedly."""
        with self._lock:
            if model_id in self._breakers:
                return
            BreakerCls = self._BreakerCls()
            breaker = BreakerCls(
                failure_threshold=failure_threshold,
                recovery_timeout_s=recovery_timeout_s,
            )
            self._breakers[model_id] = breaker
            self._ramps[model_id] = SlowStartRamp()
            # Hydrate if we have a persisted snapshot.
            snap = self._snapshots.get(model_id)
            if snap is not None:
                self._restore_breaker_state(breaker, snap)
            else:
                self._snapshots[model_id] = EndpointSnapshot(
                    model_id=model_id, state="CLOSED",
                )

    def _restore_breaker_state(
        self, breaker: Any, snap: EndpointSnapshot,
    ) -> None:
        """Rebuild a CircuitBreaker into the persisted state. The
        ``CircuitBreaker`` API is small; we reach into private slots
        only here, in one isolated location, to avoid a parallel
        FSM."""
        BreakerState = self._BreakerStateCls()
        if snap.state == "TERMINAL_OPEN":
            # Phase 12 Slice H — terminal verdicts MUST survive
            # restart. A process crash mid-modality-rejection cannot
            # accidentally re-attempt a known-bad model. Operator
            # explicit reset_terminal() / catalog refresh is the only
            # way out, by design.
            breaker._state = BreakerState.TERMINAL_OPEN  # noqa: SLF001
            breaker._failure_count = breaker._failure_threshold  # noqa: SLF001
            breaker._opened_at = time.monotonic()  # noqa: SLF001
        elif snap.state == "OPEN":
            breaker._state = BreakerState.OPEN  # noqa: SLF001
            breaker._failure_count = breaker._failure_threshold  # noqa: SLF001
            # Reconstruct opened_at in monotonic frame: offset by
            # how long ago the wall-clock event happened.
            wall_ago = max(0.0, time.time() - snap.opened_at_epoch)
            breaker._opened_at = (  # noqa: SLF001
                time.monotonic() - wall_ago
            )
        elif snap.state == "HALF_OPEN":
            # Half-open is transient; re-open conservatively so a
            # crash mid-half-open doesn't unblock cascade storm.
            breaker._state = BreakerState.OPEN  # noqa: SLF001
            breaker._failure_count = breaker._failure_threshold  # noqa: SLF001
            breaker._opened_at = time.monotonic()  # noqa: SLF001
        # CLOSED: default state, no reach-in needed.

    def hydrate(self) -> int:
        """Read persisted snapshots into memory. Endpoints become
        active when the caller subsequently invokes
        ``register_endpoint``. Returns the number of snapshots
        loaded."""
        loaded = self._store.hydrate()
        with self._lock:
            self._snapshots = dict(loaded)
        logger.info(
            "[TopologySentinel] hydrated %d endpoint snapshot(s)",
            len(loaded),
        )
        return len(loaded)

    def get_state(self, model_id: str) -> str:
        """Synchronous, lock-free read. Returns the BreakerState value
        as a string ("CLOSED" / "OPEN" / "HALF_OPEN" / "TERMINAL_OPEN").

        Uninitialized endpoint → "CLOSED" (fail-open to availability).
        Sentinel master flag off → "CLOSED" (legacy yaml authoritative).
        Force-severed env → "OPEN" (operator panic switch).

        Phase 12 Slice H — TERMINAL_OPEN is preserved through the
        check()-raises path: read breaker.state.value AFTER catching
        the exception so the dispatcher can distinguish OPEN (probe-
        recoverable) from TERMINAL_OPEN (deterministic ban)."""
        if force_severed():
            return "OPEN"
        if not is_sentinel_enabled():
            return "CLOSED"
        breaker = self._breakers.get(model_id)
        if breaker is None:
            return "CLOSED"
        try:
            breaker.check()
            return breaker.state.value
        except Exception:  # noqa: BLE001 — CircuitBreakerOpen + others
            # Phase 12 Slice H — preserve the actual state instead of
            # collapsing all raise paths to "OPEN". TERMINAL_OPEN must
            # be reported distinctly so the dispatcher can apply
            # different recovery semantics.
            try:
                return breaker.state.value
            except Exception:  # noqa: BLE001 — defensive
                return "OPEN"

    def is_dw_allowed(self, model_id: str) -> bool:
        # Phase 12 Slice H — TERMINAL_OPEN is also disallowed
        state = self.get_state(model_id)
        return state not in ("OPEN", "TERMINAL_OPEN")

    def report_failure(
        self,
        model_id: str,
        source: FailureSource,
        detail: str = "",
        *,
        status_code: Optional[int] = None,
        response_body: str = "",
        is_terminal: bool = False,
    ) -> None:
        """Ingest a live-traffic OR probe failure. Adds the source's
        weight to the model's weighted streak; trips CLOSED→OPEN
        when the streak reaches ``severed_threshold_weighted``.
        NEVER raises.

        Phase 12 Slice H — accepts structured fields from Slice F's
        unmasked DoublewordInfraError:

          * ``status_code`` — actual HTTP status (when available)
          * ``response_body`` — server's response body excerpt
          * ``is_terminal`` — True when caller has classified this
            as a deterministic terminal failure (4xx modality or
            401/403 auth). When True, breaker flips DIRECTLY to
            TERMINAL_OPEN regardless of weighted streak — single
            ground-truth signal is enough.

        Backward compatible: callers that pass only the legacy 3
        args (model_id, source, detail) get exactly the prior behavior."""
        try:
            with self._lock:
                if model_id not in self._breakers:
                    return
                breaker = self._breakers[model_id]
                snap = self._snapshots.get(model_id)
                if snap is None:
                    snap = EndpointSnapshot(
                        model_id=model_id, state="CLOSED",
                    )
                    self._snapshots[model_id] = snap
                weight = failure_weight(source)
                snap.weighted_failure_streak += weight
                snap.consecutive_passes = 0
                snap.last_failure_source = source.value
                snap.last_failure_detail = detail
                # Snap ramp back to entry tier — even if we don't trip
                # the breaker, we don't want a partial-failure window
                # to keep ramping concurrency upward.
                ramp = self._ramps.get(model_id)
                if ramp is not None:
                    ramp.register_failure()
                # Should we trip?
                pre_state = breaker.state.value
                if is_terminal:
                    # Slice H — bypass the weighted-streak threshold;
                    # single ground-truth terminal signal flips to
                    # TERMINAL_OPEN regardless of streak / state.
                    breaker.record_failure(is_terminal=True)
                elif (
                    pre_state == "CLOSED"
                    and snap.weighted_failure_streak >= self._threshold
                ):
                    # Translate weighted streak into CircuitBreaker's
                    # integer failure_count by bumping enough times.
                    while breaker.state.value == "CLOSED":
                        breaker.record_failure()
                elif pre_state == "HALF_OPEN":
                    # Single failure during half-open re-opens.
                    breaker.record_failure()
                post_state = breaker.state.value
                if post_state != pre_state:
                    snap.state = post_state
                    if post_state in ("OPEN", "TERMINAL_OPEN"):
                        snap.opened_at_epoch = time.time()
                        if post_state == "OPEN":
                            snap.backoff_idx += 1
                        if ramp is not None:
                            ramp.deactivate()
                    snap.last_transition_at_epoch = time.time()
                    self._store.write_current(self._snapshots)
                    extra: Dict[str, Any] = {
                        "weighted_failure_streak":
                            snap.weighted_failure_streak,
                        "failure_source": source.value,
                        "failure_detail": detail,
                    }
                    if status_code is not None:
                        extra["status_code"] = status_code
                    if response_body:
                        extra["response_body"] = response_body[:512]
                    if is_terminal:
                        extra["is_terminal"] = True
                    self._emit_transition(
                        model_id, "state_change",
                        from_state=pre_state, to_state=post_state,
                        **extra,
                    )
                else:
                    # Persist updated streak even without transition;
                    # log a failure_report record for observability.
                    self._store.write_current(self._snapshots)
                    extra2: Dict[str, Any] = {
                        "weighted_failure_streak":
                            snap.weighted_failure_streak,
                        "failure_source": source.value,
                        "failure_detail": detail,
                    }
                    if status_code is not None:
                        extra2["status_code"] = status_code
                    if response_body:
                        extra2["response_body"] = response_body[:512]
                    self._emit_transition(
                        model_id, "failure_report",
                        from_state=pre_state, to_state=post_state,
                        **extra2,
                    )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[TopologySentinel] report_failure failed", exc_info=True,
            )

    def report_success(self, model_id: str) -> None:
        """Record a successful live-traffic call. Decays the weighted
        streak (slow forgetting) and steps the breaker through
        HALF_OPEN→CLOSED if applicable. NEVER raises."""
        try:
            with self._lock:
                if model_id not in self._breakers:
                    return
                breaker = self._breakers[model_id]
                snap = self._snapshots.get(model_id)
                if snap is None:
                    return
                pre_state = breaker.state.value
                breaker.record_success()
                post_state = breaker.state.value
                snap.consecutive_passes += 1
                snap.weighted_failure_streak = max(
                    0.0,
                    snap.weighted_failure_streak - success_decay(),
                )
                if post_state != pre_state:
                    snap.state = post_state
                    snap.last_transition_at_epoch = time.time()
                    if post_state == "CLOSED":
                        # Activate slow-start ramp for BG/SPEC drainage.
                        ramp = self._ramps.get(model_id)
                        if ramp is not None:
                            ramp.activate()
                        snap.backoff_idx = 0
                    self._store.write_current(self._snapshots)
                    self._emit_transition(
                        model_id, "state_change",
                        from_state=pre_state, to_state=post_state,
                        weighted_failure_streak=snap.weighted_failure_streak,
                    )
                else:
                    self._store.write_current(self._snapshots)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[TopologySentinel] report_success failed", exc_info=True,
            )

    def get_ramp(self, model_id: str) -> Optional[SlowStartRamp]:
        return self._ramps.get(model_id)

    def reset_terminal_breaker(self, model_id: str) -> bool:
        """Phase 12 Slice H — explicit reset of a TERMINAL_OPEN breaker
        back to CLOSED. Used by:

          * Operator override (manual unban after fixing credentials)
          * Discovery runner's catalog-refresh hook (when DW catalog
            changes and the modality ledger drops a stale verdict)

        Returns True if state changed (was TERMINAL_OPEN); False
        otherwise. NEVER raises.

        Does NOT clear OPEN/HALF_OPEN/CLOSED — those recover via
        normal probe paths and don't need explicit reset."""
        try:
            with self._lock:
                if model_id not in self._breakers:
                    return False
                breaker = self._breakers[model_id]
                if breaker.state.value != "TERMINAL_OPEN":
                    return False
                changed = breaker.reset_terminal()
                if changed:
                    snap = self._snapshots.get(model_id)
                    if snap is not None:
                        pre_state = snap.state
                        snap.state = "CLOSED"
                        snap.weighted_failure_streak = 0.0
                        snap.consecutive_passes = 0
                        snap.opened_at_epoch = 0.0
                        snap.last_transition_at_epoch = time.time()
                        self._store.write_current(self._snapshots)
                        self._emit_transition(
                            model_id, "state_change",
                            from_state=pre_state, to_state="CLOSED",
                            failure_source="terminal_reset",
                            failure_detail="explicit reset",
                        )
                return changed
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[TopologySentinel] reset_terminal_breaker failed",
                exc_info=True,
            )
            return False

    def reset_all_terminal_breakers(self) -> int:
        """Phase 12 Slice H — reset every breaker currently in
        TERMINAL_OPEN. Returns the count reset.

        Called by the discovery runner when a catalog refresh detects
        a new snapshot id — DW may have renamed/replaced models, so
        all terminal verdicts deserve a fresh chance under the new
        snapshot. The modality ledger handles re-classification on
        the next discovery cycle.

        NEVER raises."""
        try:
            with self._lock:
                terminal_ids = [
                    mid for mid, b in self._breakers.items()
                    if b.state.value == "TERMINAL_OPEN"
                ]
            count = 0
            for mid in terminal_ids:
                if self.reset_terminal_breaker(mid):
                    count += 1
            if count:
                logger.info(
                    "[TopologySentinel] catalog refresh reset %d "
                    "TERMINAL_OPEN breaker(s)", count,
                )
            return count
        except Exception:  # noqa: BLE001 — defensive
            return 0

    def list_blocked_endpoints(self) -> Tuple[str, ...]:
        """Slice 3c — return the model_ids currently in OPEN or
        TERMINAL_OPEN. Stable-sorted. Used by SensorGovernor for
        topology-aware backpressure (low-urgency sensors throttle when
        any DW endpoint is blocked).

        When the master flag is off, returns ``()`` because
        ``get_state`` collapses every endpoint to CLOSED in that case
        (legacy yaml authoritative). NEVER raises."""
        if not is_sentinel_enabled():
            return ()
        try:
            with self._lock:
                blocked = [
                    mid for mid, b in self._breakers.items()
                    if b.state.value in ("OPEN", "TERMINAL_OPEN")
                ]
            return tuple(sorted(blocked))
        except Exception:  # noqa: BLE001 — defensive
            return ()

    def apply_health_probe_result(self, *, success: bool) -> int:
        """Slice 3a — active recovery from a lightweight catalog probe.

        Called by ``dw_discovery_runner.run_discovery`` after the
        ``GET /models`` fetch. The fetch IS the probe — when DW returns
        a populated catalog, the endpoint is reachable, and any
        transient block deserves a fresh chance.

        On ``success=True``:
          * Every ``TERMINAL_OPEN`` breaker is reset (auth/modality
            verdicts that survived persistence get a fresh probe path)
          * Every ``HALF_OPEN`` breaker has ``record_success`` called
            on it (decays weighted streak; transitions HALF_OPEN→CLOSED
            via the existing rate_limiter state machine)
          * ``OPEN`` breakers are left alone — the rate_limiter's
            time-based ``recovery_timeout_s`` (default 30s) handles
            OPEN→HALF_OPEN auto-transition; the next live call probes
            it and triggers HALF_OPEN→CLOSED via the existing path.
            Forcing OPEN→CLOSED here would race the post-failure cool-
            down and could mask a real fault.

        On ``success=False``:
          * No-op. Probe failure is a catalog-layer signal, not a
            model-layer signal — penalising every model for a single
            catalog timeout would double-count failures and risks a
            cascade-storm on transient network blips.

        Both master flags are honoured: when
        ``JARVIS_TOPOLOGY_SENTINEL_ENABLED=false`` OR
        ``JARVIS_TOPOLOGY_ACTIVE_RECOVERY_ENABLED=false``, this method
        is a no-op returning 0.

        Returns the count of breakers transitioned (TERMINAL_OPEN
        resets + HALF_OPEN→CLOSED transitions). NEVER raises."""
        if not success:
            return 0
        if not is_sentinel_enabled():
            return 0
        if not topology_active_recovery_enabled():
            return 0
        transitions = 0
        # Step 1: TERMINAL_OPEN resets — explicit method already exists
        try:
            transitions += self.reset_all_terminal_breakers()
        except Exception:  # noqa: BLE001 — defensive
            pass
        # Step 2: HALF_OPEN → CLOSED via record_success on each
        try:
            with self._lock:
                half_open_ids = [
                    mid for mid, b in self._breakers.items()
                    if b.state.value == "HALF_OPEN"
                ]
            for mid in half_open_ids:
                pre_state = self.get_state(mid)
                self.report_success(mid)
                post_state = self.get_state(mid)
                if pre_state != post_state:
                    transitions += 1
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[TopologySentinel] HALF_OPEN recovery failed",
                exc_info=True,
            )
        if transitions:
            logger.info(
                "[TopologySentinel] active recovery: %d breaker(s) "
                "transitioned after successful catalog probe",
                transitions,
            )
        return transitions

    def force_severed(self, model_id: str, reason: str) -> None:
        """Operator override — pin the endpoint OPEN immediately."""
        with self._lock:
            self.register_endpoint(model_id)
            breaker = self._breakers[model_id]
            BreakerState = self._BreakerStateCls()
            breaker._state = BreakerState.OPEN  # noqa: SLF001
            breaker._opened_at = time.monotonic()  # noqa: SLF001
            breaker._failure_count = breaker._failure_threshold  # noqa: SLF001
            snap = self._snapshots[model_id]
            pre_state = snap.state
            snap.state = "OPEN"
            snap.opened_at_epoch = time.time()
            snap.last_failure_source = "operator_force_severed"
            snap.last_failure_detail = reason[:200]
            snap.last_transition_at_epoch = time.time()
            ramp = self._ramps.get(model_id)
            if ramp is not None:
                ramp.deactivate()
            self._store.write_current(self._snapshots)
            self._emit_transition(
                model_id, "state_change",
                from_state=pre_state, to_state="OPEN",
                failure_source="operator_force_severed",
                failure_detail=reason,
            )

    def force_healthy(self, model_id: str) -> None:
        """Operator override — pin the endpoint CLOSED immediately
        (use after confirming DW is back up)."""
        with self._lock:
            self.register_endpoint(model_id)
            breaker = self._breakers[model_id]
            BreakerState = self._BreakerStateCls()
            breaker._state = BreakerState.CLOSED  # noqa: SLF001
            breaker._failure_count = 0  # noqa: SLF001
            breaker._opened_at = 0.0  # noqa: SLF001
            snap = self._snapshots[model_id]
            pre_state = snap.state
            snap.state = "CLOSED"
            snap.weighted_failure_streak = 0.0
            snap.last_transition_at_epoch = time.time()
            ramp = self._ramps.get(model_id)
            if ramp is not None:
                ramp.activate()
            self._store.write_current(self._snapshots)
            self._emit_transition(
                model_id, "state_change",
                from_state=pre_state, to_state="CLOSED",
                failure_source="operator_force_healthy",
            )

    def add_listener(
        self, listener: Callable[[TransitionRecord], None],
    ) -> None:
        """Subscribe to TransitionRecord emissions (Slice 4 SSE bridge
        will add the broker listener here)."""
        self._listeners.append(listener)

    def snapshot(self) -> Dict[str, Any]:
        """Read-only observability surface."""
        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "enabled": is_sentinel_enabled(),
                "force_severed_env": force_severed(),
                "weighted_threshold": self._threshold,
                "endpoints": {
                    mid: snap.to_json()
                    for mid, snap in self._snapshots.items()
                },
                "ramps": {
                    mid: ramp.snapshot()
                    for mid, ramp in self._ramps.items()
                },
                "daily_probe_cost_usd": round(
                    self._daily_probe_cost_usd, 6,
                ),
            }

    # -- probe loop ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn the probe loop. Idempotent. Master-flag-aware: when
        off, returns immediately (no probe burn)."""
        if not is_sentinel_enabled():
            return
        if self._prober is None:
            logger.info(
                "[TopologySentinel] no prober wired; start is a no-op",
            )
            return
        if self._probe_task is not None and not self._probe_task.done():
            return
        self._stopping.clear()
        self._probe_task = asyncio.create_task(
            self._probe_loop(), name="topology_sentinel_probe_loop",
        )

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._stopping.set()
        task = self._probe_task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._probe_task = None

    async def _probe_loop(self) -> None:
        """Per-model probe schedule. Single loop drains all registered
        endpoints; each endpoint gets a wall-clock-aware "next probe
        at" computed from its breaker state."""
        try:
            while not self._stopping.is_set():
                await self._refresh_daily_window()
                model_ids = list(self._breakers.keys())
                if not model_ids:
                    await self._sleep_or_stop(
                        healthy_probe_interval_s(),
                    )
                    continue
                # Pick the model whose next-probe-time is earliest;
                # probe it; loop. This avoids starving slow-recovering
                # endpoints when many endpoints are registered.
                next_at: Dict[str, float] = {}
                now = time.monotonic()
                for mid in model_ids:
                    next_at[mid] = self._next_probe_at(mid, now)
                target = min(next_at.items(), key=lambda kv: kv[1])
                mid, when = target
                wait_s = max(0.0, when - now)
                if wait_s > 0:
                    if await self._sleep_or_stop(wait_s):
                        return
                if self._daily_probe_cost_usd >= probe_daily_usd_cap():
                    # Cost-cap breached — halt active probing for the
                    # day. Fail-open: existing breakers continue to
                    # serve get_state from their last-known states.
                    if await self._sleep_or_stop(60.0):
                        return
                    continue
                await self._do_probe_and_apply(mid)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception(
                "[TopologySentinel] probe loop crashed; exiting",
            )

    async def _sleep_or_stop(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(
                self._stopping.wait(), timeout=seconds,
            )
            return True
        except asyncio.TimeoutError:
            return False

    def _next_probe_at(self, model_id: str, now_mono: float) -> float:
        snap = self._snapshots.get(model_id)
        breaker = self._breakers.get(model_id)
        if snap is None or breaker is None:
            return now_mono + healthy_probe_interval_s()
        if breaker.state.value == "OPEN":
            backoff_s = self._compute_backoff(snap.backoff_idx)
            # Reference point is monotonic-translated from wall clock.
            wall_now = time.time()
            since_open = max(0.0, wall_now - snap.opened_at_epoch)
            wait_s = max(0.0, backoff_s - since_open)
            return now_mono + wait_s
        # CLOSED / HALF_OPEN — fixed cadence
        return now_mono + healthy_probe_interval_s()

    async def _do_probe_and_apply(self, model_id: str) -> None:
        if self._prober is None:
            return
        result = await self._prober.probe(model_id)
        with self._lock:
            snap = self._snapshots.get(model_id)
            if snap is not None:
                snap.last_probe_at_epoch = result.ts_epoch
                snap.last_probe_outcome = result.outcome.value
            self._daily_probe_cost_usd += result.cost_usd
        # Translate probe outcome → breaker call.
        if result.outcome == ProbeOutcome.PASS:
            self.report_success(model_id)
        elif result.outcome == ProbeOutcome.FAIL:
            src = result.failure_source or (
                FailureSource.HEAVY_PROBE_FAIL
                if result.weight == ProbeWeight.HEAVY
                else FailureSource.LIGHT_PROBE_FAIL
            )
            self.report_failure(model_id, src, result.failure_detail)
        # Record probe in history regardless of outcome.
        self._emit_transition(
            model_id, "probe",
            probe_weight=result.weight.value,
            probe_outcome=result.outcome.value,
            probe_latency_s=result.latency_s,
            probe_cost_usd=result.cost_usd,
            failure_source=(
                result.failure_source.value
                if result.failure_source else None
            ),
            failure_detail=result.failure_detail,
        )

    async def _refresh_daily_window(self) -> None:
        # Reset the daily probe cost window every 24h.
        now = time.time()
        if (now - self._daily_probe_window_start) >= 86400.0:
            self._daily_probe_window_start = now
            self._daily_probe_cost_usd = 0.0

    # -- internals ----------------------------------------------------------

    def _emit_transition(
        self,
        model_id: str,
        kind: str,
        from_state: str = "",
        to_state: str = "",
        weighted_failure_streak: float = 0.0,
        failure_source: Optional[str] = None,
        failure_detail: str = "",
        probe_weight: Optional[str] = None,
        probe_outcome: Optional[str] = None,
        probe_latency_s: float = 0.0,
        probe_cost_usd: float = 0.0,
    ) -> None:
        record = TransitionRecord(
            ts_epoch=time.time(),
            model_id=model_id,
            transition_kind=kind,
            from_state=from_state,
            to_state=to_state,
            weighted_failure_streak=weighted_failure_streak,
            failure_source=failure_source,
            failure_detail=failure_detail,
            probe_weight=probe_weight,
            probe_outcome=probe_outcome,
            probe_latency_s=probe_latency_s,
            probe_cost_usd=probe_cost_usd,
        )
        self._store.append_history(record)
        for listener in list(self._listeners):
            try:
                listener(record)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[TopologySentinel] listener raised; ignored",
                    exc_info=True,
                )


# ---------------------------------------------------------------------------
# Slice 3.5 — Pre-flight handshake (boundary isolation diagnostic)
# ---------------------------------------------------------------------------
#
# Per the 2026-04-27 directive: when the master flag is on, the
# orchestrator MUST natively verify its sentinel state before accepting
# traffic. Silent failure inside the dispatcher's lazy import was the
# bug that caused the once-proof on session bt-2026-04-27-194550 to
# never enter the sentinel branch despite env being set.
#
# ``preflight_check()`` runs at the moment the dispatcher gate fires.
# If any assertion fails, the dispatcher raises
# ``SentinelInitializationError`` so the operator sees the boundary
# isolation defect at the point of decision, not minutes later in
# the postmortem.


class SentinelInitializationError(RuntimeError):
    """Raised when ``preflight_check()`` detects a boundary-isolation
    failure that prevents the AsyncTopologySentinel from making
    correct routing decisions for the current operation.

    Carries a structured failure list so the orchestrator's existing
    accept-failure branch (and future SSE telemetry) can record
    exactly what went wrong without parsing free-form strings.

    Raised path: ``candidate_generator._generate_dispatch`` →
    ``preflight_check()`` → if not healthy → raise. Caller may catch
    + cascade-to-Claude OR re-raise depending on operator policy.
    """

    def __init__(
        self, failed_assertions: Tuple[str, ...], diagnostics: Tuple[str, ...] = (),
    ) -> None:
        self.failed_assertions = tuple(failed_assertions)
        self.diagnostics = tuple(diagnostics)
        msg = (
            "AsyncTopologySentinel boundary-isolation failure: "
            + "; ".join(failed_assertions)
        )
        super().__init__(msg)


@dataclass(frozen=True)
class SentinelPreflightResult:
    """Structured snapshot of the sentinel's readiness at one
    decision point. Returned by ``preflight_check()``; consumed by
    the dispatcher gate AND by tests + observability surfaces.

    A "healthy" result means the dispatcher can proceed with full
    confidence that:
      * the master flag is on
      * the topology yaml is loaded with v2 schema + ranked dw_models
      * the singleton is hydrated (or hydration was attempted cleanly)
      * the running asyncio event loop is the loop the sentinel will
        attach its probe task to (no orphan-loop bug)

    Any of the above missing → ``failed_assertions`` populated → the
    dispatcher raises ``SentinelInitializationError``.
    """

    flag_enabled: bool
    module_imported: bool
    singleton_initialized: bool
    topology_loaded: bool
    schema_version: str
    routes_with_dw_models: Tuple[str, ...]
    monitor_config_present: bool
    event_loop_bound: bool
    state_dir_writable: bool
    diagnostics: Tuple[str, ...] = ()
    failed_assertions: Tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return not self.failed_assertions

    def to_dict(self) -> Dict[str, Any]:
        """Stable JSON shape for telemetry + audit."""
        return {
            "schema_version": "preflight.1",
            "flag_enabled": self.flag_enabled,
            "module_imported": self.module_imported,
            "singleton_initialized": self.singleton_initialized,
            "topology_loaded": self.topology_loaded,
            "topology_schema_version": self.schema_version,
            "routes_with_dw_models": list(self.routes_with_dw_models),
            "monitor_config_present": self.monitor_config_present,
            "event_loop_bound": self.event_loop_bound,
            "state_dir_writable": self.state_dir_writable,
            "diagnostics": list(self.diagnostics),
            "failed_assertions": list(self.failed_assertions),
            "healthy": self.healthy,
        }


def preflight_check(
    *,
    require_routes: bool = True,
) -> SentinelPreflightResult:
    """Native, structured initialization check.

    Runs at the dispatcher gate; designed for the orchestrator
    subprocess context (NOT pure-test ergonomics — tests mock the
    pieces). NEVER raises directly — every check is bounded by
    try/except + assertion accumulator. The CALLER decides whether
    ``not healthy`` is fatal (dispatcher raises
    ``SentinelInitializationError``; observability surfaces just
    report).

    Parameters:
      ``require_routes`` — when True (default), at least one route
      MUST have a non-empty ``dw_models`` list. The IMMEDIATE-only
      degenerate case is caught by the dispatcher's per-route
      empty-list fall-through and isn't a healthy sentinel system.
      Tests pass False for the no-routes case.

    Returns ``SentinelPreflightResult`` with explicit booleans +
    diagnostics + failed_assertions list. ``healthy`` is True iff
    failed_assertions is empty.
    """
    diagnostics: List[str] = []
    failed: List[str] = []

    flag_enabled = is_sentinel_enabled()
    if not flag_enabled:
        # Not an error — just informational. Dispatcher won't enter
        # this code path in this case, but the helper still returns
        # a complete shape for telemetry.
        diagnostics.append("master_flag_off")

    module_imported = True   # if this function is running, the module imported
    singleton_initialized = False
    topology_loaded = False
    schema_version = ""
    monitor_config_present = False
    routes_with_dw_models: List[str] = []

    try:
        sentinel = get_default_sentinel()
        singleton_initialized = sentinel is not None
    except Exception as exc:  # noqa: BLE001
        failed.append(
            f"singleton_init_failed:{type(exc).__name__}:{str(exc)[:80]}"
        )
        sentinel = None

    try:
        from backend.core.ouroboros.governance.provider_topology import (
            get_topology,
        )
        topo = get_topology()
        topology_loaded = bool(getattr(topo, "enabled", False))
        schema_version = str(getattr(topo, "schema_version", ""))
        monitor_config_present = topo.monitor_config() is not None
        for route in (
            "immediate", "standard", "complex", "background", "speculative",
        ):
            try:
                models = topo.dw_models_for_route(route)
                if models:
                    routes_with_dw_models.append(route)
            except Exception as exc:  # noqa: BLE001
                diagnostics.append(
                    f"dw_models_query_failed:{route}:{type(exc).__name__}"
                )
    except Exception as exc:  # noqa: BLE001
        failed.append(
            f"topology_load_failed:{type(exc).__name__}:{str(exc)[:80]}"
        )

    if not topology_loaded:
        failed.append("topology_not_loaded")
    if require_routes and not routes_with_dw_models:
        # Phase 12 Slice E — post-purge YAML has empty dw_models on
        # all generative routes; discovery populates the catalog
        # asynchronously after the boot hook fires. Preflight runs
        # BEFORE the boot hook, so empty-routes is the legitimate
        # cold-start state when discovery is enabled. Tolerate it as
        # a diagnostic; treat as failed only when discovery is OFF
        # AND YAML has nothing — that's a genuine misconfiguration.
        try:
            from backend.core.ouroboros.governance.dw_catalog_client import (
                discovery_enabled as _disc_enabled,
            )
            if _disc_enabled():
                diagnostics.append("dw_models_pending_discovery")
            else:
                failed.append("no_routes_have_dw_models")
        except Exception:  # noqa: BLE001 — fail closed on import error
            failed.append("no_routes_have_dw_models")

    # Event-loop binding probe — the directive's "async event loop
    # binding" check. ``asyncio.get_running_loop()`` raises
    # RuntimeError when called outside an active loop; if the
    # dispatcher is calling this, an active loop exists. Any failure
    # here = the sentinel's probe task would be orphaned.
    event_loop_bound = False
    try:
        loop = asyncio.get_running_loop()
        event_loop_bound = loop is not None and not loop.is_closed()
    except RuntimeError:
        # Called from a non-async context — preflight_check still
        # works for synchronous callers (tests, /sentinel REPL); the
        # dispatcher's call site is async so it'll always have a loop.
        diagnostics.append("preflight_called_outside_async_loop")
    except Exception as exc:  # noqa: BLE001
        failed.append(
            f"event_loop_probe_failed:{type(exc).__name__}"
        )

    # State dir writability — the persistence path. If the harness
    # picked a state dir we can't write to, sentinel state wouldn't
    # survive process restart (boot-loop protection broken).
    state_dir_writable = False
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        # Probe a lock file (cheap, doesn't pollute the real ledger).
        probe = d / ".preflight_probe.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        state_dir_writable = True
    except Exception as exc:  # noqa: BLE001
        diagnostics.append(
            f"state_dir_unwritable:{type(exc).__name__}"
        )

    return SentinelPreflightResult(
        flag_enabled=flag_enabled,
        module_imported=module_imported,
        singleton_initialized=singleton_initialized,
        topology_loaded=topology_loaded,
        schema_version=schema_version,
        routes_with_dw_models=tuple(routes_with_dw_models),
        monitor_config_present=monitor_config_present,
        event_loop_bound=event_loop_bound,
        state_dir_writable=state_dir_writable,
        diagnostics=tuple(diagnostics),
        failed_assertions=tuple(failed),
    )


# ---------------------------------------------------------------------------
# Module-level singleton (Slice 5 wires GovernedLoopService to call .start)
# ---------------------------------------------------------------------------


_default_sentinel: Optional[TopologySentinel] = None
_default_sentinel_lock = threading.Lock()


def get_default_sentinel(
    prober: Optional[ContextWeightedProber] = None,
) -> TopologySentinel:
    """Module singleton. Slice 5 boot wiring will call this from
    ``GovernedLoopService.start`` to spawn the probe loop. Tests may
    inject a custom prober the first time this is called.

    NEVER raises. When the master flag is off, the returned sentinel
    is fully functional but ``start()`` is a no-op (no probe burn)."""
    global _default_sentinel
    with _default_sentinel_lock:
        if _default_sentinel is None:
            _default_sentinel = TopologySentinel(prober=prober)
            _default_sentinel.hydrate()
    return _default_sentinel


def reset_default_sentinel_for_tests() -> None:
    """Tests-only escape hatch — clears the module singleton so each
    test can construct its own."""
    global _default_sentinel
    with _default_sentinel_lock:
        _default_sentinel = None


__all__ = [
    "ContextWeightedProber",
    "DW_MODEL_OVERRIDE_VAR",
    "EndpointSnapshot",
    "FailureSource",
    "ProbeFn",
    "SentinelInitializationError",
    "SentinelPreflightResult",
    "get_dw_model_override",
    "preflight_check",
    "reset_dw_model_override",
    "sentinel_propagated_vars",
    "set_dw_model_override",
    "ProbeOutcome",
    "ProbeResult",
    "ProbeWeight",
    "SCHEMA_VERSION",
    "SentinelStateStore",
    "SlowStartRamp",
    "TopologySentinel",
    "TransitionRecord",
    "failure_weight",
    "force_severed",
    "get_default_sentinel",
    "healthy_probe_interval_s",
    "heavy_probe_max_tokens",
    "heavy_probe_ratio",
    "heavy_probe_total_timeout_s",
    "history_size",
    "is_sentinel_enabled",
    "light_probe_first_token_timeout_s",
    "parse_ramp_schedule_env",
    "probe_backoff_base_s",
    "probe_backoff_cap_s",
    "probe_daily_usd_cap",
    "ramp_max_wait_s",
    "reset_default_sentinel_for_tests",
    "severed_threshold_weighted",
    "state_dir",
    "state_max_age_s",
    "success_decay",
]
