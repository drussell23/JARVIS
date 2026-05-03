"""FiringTelemetryRegistry — pure-stdlib bounded per-key fire counter.

Closes the audit's load-bearing behavioral observability gap: AST
shipped_code_invariants catch *structural* drift (a pin removed, a
substrate refactored away), but they cannot catch *behavioral*
drift — substrate that ships graduated default-True but never
.fires() in production. The Tier 0.5 batches 1+2 wired six
previously-dormant observers into ``governed_loop_service``; this
substrate is the BEHAVIORAL complement that proves they actually
tick under realistic load.

Usage::

    from backend.core.ouroboros.governance.firing_telemetry import (
        incr_fire_counter,
    )

    # Hot-path instrumentation — NEVER raises, fail-open.
    incr_fire_counter("observer.gradient.tick")
    incr_fire_counter("admission_gate.SHED_BUDGET_INSUFFICIENT")
    incr_fire_counter("cluster_coverage_envelope_emit")

    # Operator inspection at GET /observability/firing-telemetry::
    #
    #   {
    #     "schema_version": "firing_telemetry.v1",
    #     "session_started_ts": 1715000000.0,
    #     "snapshot_taken_ts":  1715003600.0,
    #     "session_uptime_s":   3600.0,
    #     "config": {
    #       "capacity": 4096,
    #       "key_max_chars": 128
    #     },
    #     "totals": {
    #       "distinct_keys":  142,
    #       "total_increments": 8917
    #     },
    #     "counters": [
    #       {"key": "observer.gradient.tick",
    #        "count": 12,
    #        "first_seen_ts": 1715000004.2},
    #       ...
    #     ]
    #   }

Discipline (load-bearing, mirrors AdmissionGate/CodebaseCharacter
substrate posture):
  * Pure-stdlib — zero non-stdlib imports. Slice 3 graduation may
    add the two registration-contract imports (FlagRegistry +
    shipped_code_invariants), nothing else.
  * Total — every public function NEVER raises; failure path returns
    ``FireCounterOutcome.FAILED`` and logs at DEBUG. Instrumentation
    MUST NOT crash business logic.
  * Thread-safe — RLock on the counter dict so sync + async producers
    can call safely from any context.
  * Bounded memory — capacity-clamped distinct-key set + key-length
    cap; over-capacity new keys are DROPPED rather than evicting
    existing counters (eviction would lie about historical fires).
  * No caller imports — substrate stays caller-agnostic.
    AST-pinned at Slice graduation.
  * Per-session reset — operator/harness calls ``reset_for_session()``
    at boot; every session starts with clean counters so soak
    post-analysis can compare apples-to-apples.

Vocabulary (closed taxonomy, AST-pinned):
  * ``FireCounterOutcome`` 5-value enum:
    - ``RECORDED`` — counter incremented
    - ``DROPPED`` — over capacity (new key) OR malformed input
    - ``DISABLED`` — master flag off
    - ``FAILED`` — exception caught (fail-open)
    - ``RESERVED`` — placeholder for forward-compat (Slice TBD)
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema version (pinned in Slice graduation tests)
# ---------------------------------------------------------------------------

FIRING_TELEMETRY_SCHEMA_VERSION = "firing_telemetry.v1"


# ---------------------------------------------------------------------------
# Closed vocabulary
# ---------------------------------------------------------------------------


class FireCounterOutcome(str, enum.Enum):
    """Closed taxonomy of fire-counter outcomes. AST-pinned."""

    RECORDED = "recorded"
    DROPPED = "dropped"
    DISABLED = "disabled"
    FAILED = "failed"
    RESERVED = "reserved"


# ---------------------------------------------------------------------------
# Env knobs (all clamped, no hardcoding)
# ---------------------------------------------------------------------------


def firing_telemetry_enabled() -> bool:
    """Master flag. Default-True at graduation.

    Empty / whitespace env value is treated as unset (asymmetric env
    semantics matching AdmissionGate / DirectionInferrer / Codebase
    Character).
    """
    raw = os.environ.get(
        "JARVIS_FIRING_TELEMETRY_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


def default_capacity() -> int:
    """Maximum distinct keys the registry will track per session.

    Default 4096, clamped [8, 65536]. Once at capacity, new keys are
    DROPPED — existing counters continue to increment normally.
    """
    raw = os.environ.get(
        "JARVIS_FIRING_TELEMETRY_CAPACITY", "",
    ).strip()
    try:
        val = int(raw) if raw else 4096
    except (TypeError, ValueError):
        val = 4096
    return max(8, min(65536, val))


def key_max_chars() -> int:
    """Per-key character cap. Default 128, clamped [16, 512].

    Keys longer than this are truncated before storage so a noisy
    instrumentation site cannot blow distinct-key memory.
    """
    raw = os.environ.get(
        "JARVIS_FIRING_TELEMETRY_KEY_MAX_CHARS", "",
    ).strip()
    try:
        val = int(raw) if raw else 128
    except (TypeError, ValueError):
        val = 128
    return max(16, min(512, val))


def per_key_value_cap() -> int:
    """Hard ceiling on a single key's count. Default 2^30, clamped
    [1024, 2^31 - 1]. Prevents 32-bit overflow on hot paths.
    """
    raw = os.environ.get(
        "JARVIS_FIRING_TELEMETRY_PER_KEY_VALUE_CAP", "",
    ).strip()
    try:
        val = int(raw) if raw else (1 << 30)
    except (TypeError, ValueError):
        val = 1 << 30
    return max(1024, min((1 << 31) - 1, val))


def snapshot_max_keys() -> int:
    """Hard cap on the number of keys returned in one snapshot.
    Default 1024, clamped [16, 8192]. Prevents unbounded JSON
    serialization from blowing the GET response.
    """
    raw = os.environ.get(
        "JARVIS_FIRING_TELEMETRY_SNAPSHOT_MAX_KEYS", "",
    ).strip()
    try:
        val = int(raw) if raw else 1024
    except (TypeError, ValueError):
        val = 1024
    return max(16, min(8192, val))


# ---------------------------------------------------------------------------
# Frozen records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FireCounterEntry:
    """One key's counter state. Frozen / hashable."""

    key: str
    count: int
    first_seen_ts: float
    last_seen_ts: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": str(self.key),
            "count": int(self.count),
            "first_seen_ts": float(self.first_seen_ts),
            "last_seen_ts": float(self.last_seen_ts),
        }


@dataclass(frozen=True)
class FireCounterSnapshot:
    """Total snapshot of the registry. Frozen / hashable."""

    schema_version: str
    counters: Tuple[FireCounterEntry, ...]
    distinct_keys: int
    total_increments: int
    capacity: int
    key_max_chars: int
    session_started_ts: float
    snapshot_taken_ts: float
    truncated_count: int  # entries omitted by snapshot_max_keys cap

    @property
    def session_uptime_s(self) -> float:
        return max(
            0.0, self.snapshot_taken_ts - self.session_started_ts,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": str(self.schema_version),
            "session_started_ts": float(self.session_started_ts),
            "snapshot_taken_ts": float(self.snapshot_taken_ts),
            "session_uptime_s": float(self.session_uptime_s),
            "config": {
                "capacity": int(self.capacity),
                "key_max_chars": int(self.key_max_chars),
            },
            "totals": {
                "distinct_keys": int(self.distinct_keys),
                "total_increments": int(self.total_increments),
            },
            "truncated_count": int(self.truncated_count),
            "counters": [c.to_dict() for c in self.counters],
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class FiringTelemetryRegistry:
    """Process-wide bounded counter store. Thread-safe via RLock.

    Total guarantee: every public method NEVER raises. Failure paths
    log at DEBUG and return ``FireCounterOutcome.FAILED`` /
    fall-through values so instrumentation cannot crash business
    logic.

    Bounded by construction:
      * Distinct keys capped at ``capacity`` (default 4096). Once at
        capacity, new keys are DROPPED rather than evicting existing
        counters (eviction would lie about historical fires).
      * Per-key count capped at ``per_key_value_cap`` (default 2^30)
        to prevent int32 overflow on hot paths.
      * Per-key string truncated to ``key_max_chars`` (default 128).
    """

    def __init__(
        self,
        *,
        capacity: Optional[int] = None,
        key_max_chars_override: Optional[int] = None,
        per_key_value_cap_override: Optional[int] = None,
    ) -> None:
        self._capacity = (
            int(capacity) if capacity is not None
            else default_capacity()
        )
        self._capacity = max(8, min(65536, self._capacity))
        self._key_max_chars = (
            int(key_max_chars_override)
            if key_max_chars_override is not None
            else key_max_chars()
        )
        self._key_max_chars = max(
            16, min(512, self._key_max_chars),
        )
        self._per_key_value_cap = (
            int(per_key_value_cap_override)
            if per_key_value_cap_override is not None
            else per_key_value_cap()
        )
        self._per_key_value_cap = max(
            1024, min((1 << 31) - 1, self._per_key_value_cap),
        )
        # Counter dict: key → (count, first_seen_ts, last_seen_ts)
        self._counters: Dict[str, List[float]] = {}
        self._total_increments: int = 0
        self._session_started_ts: float = time.time()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Hot path — NEVER raises
    # ------------------------------------------------------------------

    def incr(
        self, key: str, *, by: int = 1,
    ) -> FireCounterOutcome:
        """Increment ``key`` by ``by`` (default 1). NEVER raises.

        Returns ``RECORDED`` on successful increment,
        ``DROPPED`` when input is malformed OR new key arrives at
        capacity, ``DISABLED`` when master flag off, ``FAILED`` on
        any exception.
        """
        try:
            if not firing_telemetry_enabled():
                return FireCounterOutcome.DISABLED
            if not isinstance(key, str):
                return FireCounterOutcome.DROPPED
            stripped = key.strip()
            if not stripped:
                return FireCounterOutcome.DROPPED
            if len(stripped) > self._key_max_chars:
                stripped = stripped[: self._key_max_chars]
            try:
                inc = max(1, int(by))
            except (TypeError, ValueError):
                inc = 1
            now = time.time()
            with self._lock:
                if stripped not in self._counters:
                    if (
                        len(self._counters) >= self._capacity
                    ):
                        return FireCounterOutcome.DROPPED
                    self._counters[stripped] = [
                        float(min(inc, self._per_key_value_cap)),
                        now, now,
                    ]
                else:
                    entry = self._counters[stripped]
                    entry[0] = float(min(
                        int(entry[0]) + inc,
                        self._per_key_value_cap,
                    ))
                    entry[2] = now  # last_seen_ts
                self._total_increments += inc
            return FireCounterOutcome.RECORDED
        except Exception as exc:  # noqa: BLE001 — total guarantee
            logger.debug(
                "[firing_telemetry] incr degraded key=%r: %s",
                key, exc,
            )
            return FireCounterOutcome.FAILED

    # ------------------------------------------------------------------
    # Read path — NEVER raises
    # ------------------------------------------------------------------

    def snapshot(
        self, *, max_keys: Optional[int] = None,
    ) -> FireCounterSnapshot:
        """Bounded read-only snapshot. NEVER raises.

        Sorted by count descending then key ascending for determinism.
        Capped at ``max_keys`` (default ``snapshot_max_keys()``);
        omitted entries counted in ``truncated_count``.
        """
        try:
            cap = (
                int(max_keys) if max_keys is not None
                else snapshot_max_keys()
            )
            cap = max(1, min(8192, cap))
        except (TypeError, ValueError):
            cap = snapshot_max_keys()
        try:
            with self._lock:
                snap_ts = time.time()
                items: List[FireCounterEntry] = []
                for k, v in self._counters.items():
                    items.append(FireCounterEntry(
                        key=str(k),
                        count=int(v[0]),
                        first_seen_ts=float(v[1]),
                        last_seen_ts=float(v[2]),
                    ))
                distinct = len(items)
                total_incr = int(self._total_increments)
                session_started = float(self._session_started_ts)
                capacity = int(self._capacity)
                key_max = int(self._key_max_chars)
            items.sort(
                key=lambda e: (-int(e.count), str(e.key)),
            )
            kept = items[:cap]
            truncated = max(0, distinct - cap)
            return FireCounterSnapshot(
                schema_version=FIRING_TELEMETRY_SCHEMA_VERSION,
                counters=tuple(kept),
                distinct_keys=distinct,
                total_increments=total_incr,
                capacity=capacity,
                key_max_chars=key_max,
                session_started_ts=session_started,
                snapshot_taken_ts=snap_ts,
                truncated_count=truncated,
            )
        except Exception as exc:  # noqa: BLE001 — total guarantee
            logger.debug(
                "[firing_telemetry] snapshot degraded: %s", exc,
            )
            return FireCounterSnapshot(
                schema_version=FIRING_TELEMETRY_SCHEMA_VERSION,
                counters=(),
                distinct_keys=0,
                total_increments=0,
                capacity=self._capacity,
                key_max_chars=self._key_max_chars,
                session_started_ts=self._session_started_ts,
                snapshot_taken_ts=time.time(),
                truncated_count=0,
            )

    def get_count(self, key: str) -> int:
        """Look up a single key's count. NEVER raises. Returns 0 for
        missing key OR any error path."""
        try:
            if not isinstance(key, str):
                return 0
            stripped = key.strip()[: self._key_max_chars]
            with self._lock:
                entry = self._counters.get(stripped)
            return int(entry[0]) if entry else 0
        except Exception:  # noqa: BLE001
            return 0

    def reset_for_session(self) -> None:
        """Clear all counters + restart the session timer.

        Operator/harness calls this at session boot so each soak
        starts with clean counts and post-analysis can compare
        apples-to-apples. NEVER raises.
        """
        try:
            with self._lock:
                self._counters.clear()
                self._total_increments = 0
                self._session_started_ts = time.time()
        except Exception:  # noqa: BLE001
            pass

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def session_started_ts(self) -> float:
        return self._session_started_ts


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_DEFAULT_REGISTRY: Optional[FiringTelemetryRegistry] = None
_DEFAULT_REGISTRY_LOCK = threading.Lock()


def get_default_registry() -> FiringTelemetryRegistry:
    """Process-wide singleton. First call initializes; subsequent
    calls return the cached instance. Mirror of
    ``semantic_index.get_default_index`` discipline.
    """
    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = FiringTelemetryRegistry()
        return _DEFAULT_REGISTRY


def reset_singleton_for_tests() -> None:
    """Clear the process-wide singleton. Tests only.

    Production session reset uses ``get_default_registry().
    reset_for_session()`` (preserves the singleton, clears state)
    rather than this (drops the singleton entirely).
    """
    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        _DEFAULT_REGISTRY = None


def incr_fire_counter(
    key: str, *, by: int = 1,
) -> FireCounterOutcome:
    """Module-level convenience wrapper. NEVER raises.

    Equivalent to ``get_default_registry().incr(key, by=by)``.
    Designed to be the import-once-call-everywhere primitive for
    instrumentation across the codebase.
    """
    try:
        return get_default_registry().incr(key, by=by)
    except Exception:  # noqa: BLE001
        return FireCounterOutcome.FAILED


# ---------------------------------------------------------------------------
# Slice graduation — module-owned shipped_code_invariants + FlagRegistry
# seeds. Discovered automatically via the existing
# _INVARIANT_PROVIDER_PACKAGES / _FLAG_PROVIDER_PACKAGES contracts.
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Module-owned shipped-code invariants. Returns the list so the
    centralized seed loader can register at boot. NEVER raises.

    Four invariants pin the firing-telemetry substrate:

      1. ``fire_counter_outcome_vocabulary`` — closed 5-value enum
         frozen.
      2. ``firing_telemetry_incr_total`` — incr() body MUST NOT
         contain a `raise` statement (the "NEVER raises" contract is
         load-bearing for instrumentation discipline — every caller
         expects fail-open).
      3. ``firing_telemetry_no_caller_imports`` — substrate stays
         caller-agnostic.
      4. ``firing_telemetry_schema_version_pinned`` — schema string
         is byte-stable so cross-soak post-analysis is comparable.
    """
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    import ast as _ast

    def _validate_outcome_vocabulary(tree, source) -> tuple:
        violations = []
        required = {
            "RECORDED", "DROPPED", "DISABLED", "FAILED", "RESERVED",
        }
        seen = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and (
                node.name == "FireCounterOutcome"
            ):
                for stmt in node.body:
                    if isinstance(stmt, _ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, _ast.Name):
                                seen.add(target.id)
        missing = required - seen
        if missing:
            violations.append(
                f"FireCounterOutcome lost values: {sorted(missing)}"
                " — closed taxonomy frozen at graduation"
            )
        unexpected = seen - required - {"_generate_next_value_"}
        if unexpected:
            violations.append(
                f"FireCounterOutcome gained unpinned values: "
                f"{sorted(unexpected)} — update the AST pin when "
                "widening the vocabulary"
            )
        return tuple(violations)

    def _validate_incr_total(tree, source) -> tuple:
        # incr() body MUST NOT contain Raise (top-level — outer
        # try/except is the function body). The "NEVER raises"
        # contract is what makes incr_fire_counter safe to call from
        # any hot path without a wrapper try/except at every site.
        violations = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef) and (
                node.name == "incr"
            ):
                for sub in _ast.walk(node):
                    if isinstance(sub, _ast.Raise):
                        violations.append(
                            f"FiringTelemetryRegistry.incr body "
                            f"contains a `raise` statement at line "
                            f"{sub.lineno} — the function MUST be "
                            "total (NEVER raises) so callers can "
                            "instrument hot paths without wrappers"
                        )
                break
        return tuple(violations)

    def _validate_no_caller_imports(tree, source) -> tuple:
        violations = []
        forbidden = {
            "orchestrator", "candidate_generator",
            "governed_loop_service", "iron_gate", "risk_tier",
            "change_engine", "gate", "policy",
            "intake", "providers", "tool_executor",
        }
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                for f in forbidden:
                    if f in parts:
                        violations.append(
                            f"forbidden caller-side import: "
                            f"{node.module}"
                        )
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    for f in forbidden:
                        if f in parts:
                            violations.append(
                                f"forbidden caller-side import: "
                                f"{alias.name}"
                            )
        return tuple(violations)

    def _validate_schema_version_pinned(tree, source) -> tuple:
        # FIRING_TELEMETRY_SCHEMA_VERSION literal must be exactly
        # "firing_telemetry.v1". A bump to v2 requires explicit
        # operator approval (the schema contract is what makes
        # cross-soak post-analysis comparable).
        violations = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, _ast.Name)
                        and target.id
                        == "FIRING_TELEMETRY_SCHEMA_VERSION"
                    ):
                        if isinstance(node.value, _ast.Constant):
                            if node.value.value != (
                                "firing_telemetry.v1"
                            ):
                                violations.append(
                                    f"FIRING_TELEMETRY_SCHEMA_VERSION"
                                    f" changed to "
                                    f"{node.value.value!r} — schema "
                                    "bumps require explicit Slice "
                                    "graduation update"
                                )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="fire_counter_outcome_vocabulary",
            target_file=(
                "backend/core/ouroboros/governance/"
                "firing_telemetry.py"
            ),
            description=(
                "FireCounterOutcome's 5-value closed taxonomy is "
                "frozen. Adding a 6th value silently breaks the "
                "instrumentation contract at every call site."
            ),
            validate=_validate_outcome_vocabulary,
        ),
        ShippedCodeInvariant(
            invariant_name="firing_telemetry_incr_total",
            target_file=(
                "backend/core/ouroboros/governance/"
                "firing_telemetry.py"
            ),
            description=(
                "FiringTelemetryRegistry.incr MUST NOT contain a "
                "`raise` statement in its body. The 'NEVER raises' "
                "contract is load-bearing for instrumentation "
                "discipline — every caller expects fail-open."
            ),
            validate=_validate_incr_total,
        ),
        ShippedCodeInvariant(
            invariant_name="firing_telemetry_no_caller_imports",
            target_file=(
                "backend/core/ouroboros/governance/"
                "firing_telemetry.py"
            ),
            description=(
                "Substrate stays caller-agnostic: no orchestrator / "
                "governed_loop_service / candidate_generator / "
                "intake / providers imports. Dependency direction "
                "is one-way — callers import us; we import nothing "
                "back."
            ),
            validate=_validate_no_caller_imports,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "firing_telemetry_schema_version_pinned"
            ),
            target_file=(
                "backend/core/ouroboros/governance/"
                "firing_telemetry.py"
            ),
            description=(
                "FIRING_TELEMETRY_SCHEMA_VERSION literal is pinned "
                "at 'firing_telemetry.v1'. Schema bumps require "
                "explicit Slice graduation update because cross-"
                "soak post-analysis comparability depends on it."
            ),
            validate=_validate_schema_version_pinned,
        ),
    ]


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except ImportError:
        return 0
    specs = [
        FlagSpec(
            name="JARVIS_FIRING_TELEMETRY_ENABLED",
            type=FlagType.BOOL,
            default=True,
            description=(
                "Master kill switch for FiringTelemetryRegistry. "
                "Default TRUE — instrumentation MUST be on by "
                "default so dead-code regressions are detectable. "
                "Hot-revert: ``=false`` instantly disables every "
                "incr_fire_counter call (env re-read on every call "
                "— no restart needed). The DISABLED outcome is a "
                "first-class enum value so callers can distinguish "
                "'silent' from 'failed'."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "firing_telemetry.py"
            ),
            example="true",
            since="FiringTelemetry Slice 1 (2026-05-03)",
        ),
        FlagSpec(
            name="JARVIS_FIRING_TELEMETRY_CAPACITY",
            type=FlagType.INT,
            default=4096,
            description=(
                "Maximum distinct keys the registry tracks per "
                "session. Default 4096, clamped [8, 65536]. Once "
                "at capacity, NEW keys are DROPPED (existing "
                "counters continue to increment normally — eviction "
                "would lie about historical fires)."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "firing_telemetry.py"
            ),
            example="4096",
            since="FiringTelemetry Slice 1 (2026-05-03)",
        ),
        FlagSpec(
            name="JARVIS_FIRING_TELEMETRY_KEY_MAX_CHARS",
            type=FlagType.INT,
            default=128,
            description=(
                "Per-key character cap before storage truncation. "
                "Default 128, clamped [16, 512]. Prevents a noisy "
                "instrumentation site from blowing distinct-key "
                "memory."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "firing_telemetry.py"
            ),
            example="128",
            since="FiringTelemetry Slice 1 (2026-05-03)",
        ),
        FlagSpec(
            name=(
                "JARVIS_FIRING_TELEMETRY_PER_KEY_VALUE_CAP"
            ),
            type=FlagType.INT,
            default=(1 << 30),
            description=(
                "Hard ceiling on a single key's count. Default "
                "2^30, clamped [1024, 2^31 - 1]. Prevents int32 "
                "overflow on hot-path counters."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "firing_telemetry.py"
            ),
            example=str(1 << 30),
            since="FiringTelemetry Slice 1 (2026-05-03)",
        ),
        FlagSpec(
            name=(
                "JARVIS_FIRING_TELEMETRY_SNAPSHOT_MAX_KEYS"
            ),
            type=FlagType.INT,
            default=1024,
            description=(
                "Hard cap on entries returned per snapshot. "
                "Default 1024, clamped [16, 8192]. Prevents "
                "unbounded JSON serialization from blowing the "
                "GET /observability/firing-telemetry response. "
                "Sorted by count desc then key asc; entries beyond "
                "the cap surface as truncated_count."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "firing_telemetry.py"
            ),
            example="1024",
            since="FiringTelemetry Slice 1 (2026-05-03)",
        ),
    ]
    try:
        return int(registry.bulk_register(specs, override=False))
    except Exception:
        return 0
