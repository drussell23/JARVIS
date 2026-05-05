"""M9 Slice 2 — CuriosityCollector + per-cluster persistence
(PRD §30.5.1).

The async observer that converts three independent input streams
(GENERATE-phase logprob entropy + Prophecy prediction error + the
Coherence Auditor's RECURRENCE_DRIFT signal) into per-cluster
:class:`CuriosityScore` instances. Slice 3's `SensorGovernor`
consumer pulls scores from this collector at ``request_budget()``
time; Slice 4's observability surfaces project the snapshot.

Architectural locks (operator mandate):

  * **Atomic frozen-swap mutation** (mirrors :class:`Epistemic-
    BudgetTracker` from Upgrade 1 + :class:`FailureModeStore`
    from Upgrade 3). Per-cluster ring buffers live behind a
    ``threading.RLock``; mutations create a new
    :class:`CuriosityScore` and atomically swap it into the
    state dict. **No reader ever sees a torn write.**
  * **Decision A1** — per-cluster JSONL persistence under
    ``.jarvis/curiosity/{cluster_id}.jsonl`` via
    :func:`cross_process_jsonl.flock_critical_section` /
    :func:`flock_append_line`. Symmetric to M11's per-cluster
    JSONL pattern.
  * **Decision A3 SemanticIndex-optional** — :func:`resolve_-
    cluster_id` accepts a path / free-form string and an
    optional :class:`SemanticIndex` reference. When the index
    is unavailable or the input doesn't match any cluster,
    falls back to ``_global`` rather than inventing buckets.
    Keeps cluster_id space stable + composable with downstream
    consumers.
  * **Decision B1** — async observer pattern: producers call
    :meth:`record_logprob_entropy` / :meth:`record_prophecy_-
    error` / :meth:`record_recurrence_drift` from Slice 5
    wire-up sites; collector aggregates + persists. Pure
    pull-side query via :meth:`score_for_cluster` for Slice 3.
  * **Decision D1 pull-based consumer** — collector NEVER pushes
    into :class:`SensorGovernor`. Governor lazy-imports +
    queries at ``request_budget()`` time. Decoupled by design.
  * **Authority asymmetry** (AST-pinned at Slice 5) — collector
    MUST NOT import ``orchestrator`` / ``iron_gate`` /
    ``providers`` / ``urgency_router`` / ``tool_executor`` /
    ``candidate_generator`` / ``sensor_governor`` /
    ``strategic_direction`` / ``policy``. Inputs are caller-
    pushed; outputs are caller-pulled.
  * **Auto-decay** — closed enum :class:`CuriosityDecayReason`
    drives the decay paths:
      * ``STALE_FOCUS`` — last update older than
        :func:`curiosity_stale_focus_hours`. Multiplier rebases
        to 1.0 even if raw signal is high.
      * ``RECURRENCE_LOOP`` — same source has dominated for
        N+ consecutive observations without intervening other
        sources. Slice 2 detects via per-cluster source-history
        ring; Slice 4 surfaces via /curiosity REPL.
      * ``OPERATOR_RESET`` — Slice 4's ``/curiosity reset
        <id>`` write surface.

Authoritative public API:
  * :func:`get_default_collector` — process-singleton entry
  * :class:`CuriosityCollector` — mutation + query surface
  * :func:`resolve_cluster_id` — path → cluster_id helper
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import (
    Any, Deque, Dict, Iterable, List, Optional, Tuple,
)

from backend.core.ouroboros.governance._scoring_primitives import (
    weight_score,
)
from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
    flock_critical_section,
)
from backend.core.ouroboros.governance.curiosity_gradient import (
    CURIOSITY_GRADIENT_SCHEMA_VERSION,
    CuriosityDecayReason,
    CuriosityObservation,
    CuriosityScore,
    CuriositySource,
    compute_curiosity,
    curiosity_gradient_enabled,
    curiosity_min_samples,
    curiosity_stale_focus_hours,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env knobs — bounded clamping, defaults documented
# ---------------------------------------------------------------------------


def _read_int_knob(
    name: str, default: int, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
        if n < floor:
            return floor
        if n > ceiling:
            return ceiling
        return n
    except (TypeError, ValueError):
        return default


def curiosity_window_size() -> int:
    """``JARVIS_CURIOSITY_WINDOW_SIZE`` — per-cluster ring-buffer
    cap. Default 64; clamped [8, 1024]. Bounded growth; older
    samples drop off as the ring rotates. Captured at collector
    construction for stability."""
    return _read_int_knob(
        "JARVIS_CURIOSITY_WINDOW_SIZE", 64, 8, 1024,
    )


def curiosity_recurrence_loop_threshold() -> int:
    """``JARVIS_CURIOSITY_RECURRENCE_LOOP_THRESHOLD`` — when a
    cluster's source-history ring shows the same dominant source
    this many times consecutively, ``RECURRENCE_LOOP`` decay
    fires. Default 10; clamped [3, 100]."""
    return _read_int_knob(
        "JARVIS_CURIOSITY_RECURRENCE_LOOP_THRESHOLD", 10, 3, 100,
    )


def curiosity_persist_enabled() -> bool:
    """``JARVIS_CURIOSITY_PERSIST_ENABLED`` — when true, every
    observation is also appended to the per-cluster JSONL.
    Default true (master flag still controls whether the
    collector is invoked at all). Set false to run in-memory-
    only for high-throughput benchmarks."""
    raw = os.environ.get(
        "JARVIS_CURIOSITY_PERSIST_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw not in ("0", "false", "no", "off")


def curiosity_history_dir() -> Path:
    """``JARVIS_CURIOSITY_HISTORY_DIR`` — directory under which
    per-cluster JSONLs live. Default ``.jarvis/curiosity``.
    Mirrors the M11 + Upgrade 3 per-cluster pattern."""
    raw = os.environ.get(
        "JARVIS_CURIOSITY_HISTORY_DIR", "",
    ).strip()
    if not raw:
        return Path(".jarvis") / "curiosity"
    return Path(raw)


# ---------------------------------------------------------------------------
# Cluster-id resolution — Decision A3 SemanticIndex-optional
# ---------------------------------------------------------------------------


_GLOBAL_FALLBACK_CLUSTER_ID: str = "_global"


def _normalize_cluster_id(raw: Any) -> str:
    """Lowercase + strip; empty falls through to ``_global``."""
    try:
        s = str(raw or "").strip().lower()
        if not s:
            return _GLOBAL_FALLBACK_CLUSTER_ID
        return s
    except Exception:  # noqa: BLE001 — defensive
        return _GLOBAL_FALLBACK_CLUSTER_ID


def resolve_cluster_id(
    region_or_path: Any,
    *,
    semantic_index: Optional[Any] = None,
) -> str:
    """Resolve a free-form region identifier (file path,
    function name, manual cluster label, etc.) to a stable
    cluster_id string for collector ingest.

    Resolution priority:
      1. If ``region_or_path`` is already a non-empty string
         that looks like an explicit cluster_id (no path
         separator, no dot — e.g., ``"backend"``,
         ``"verification"``), use it verbatim (lowercased +
         stripped).
      2. If ``semantic_index`` is provided AND has a
         ``representative_paths``-aware ``clusters`` snapshot,
         match the supplied path against any cluster's
         ``representative_paths`` tuple. First match wins
         (stable + cluster-id is the integer prefixed with
         ``"sem-"`` for namespacing). NEVER raises — any
         exception falls through to the next branch.
      3. Otherwise: ``_global``.

    Slice 5 producer wire-ups call this; the collector itself
    accepts opaque cluster_id strings so its API stays simple."""
    s = _normalize_cluster_id(region_or_path)
    if s == _GLOBAL_FALLBACK_CLUSTER_ID:
        return _GLOBAL_FALLBACK_CLUSTER_ID
    # Step 1 — looks like an explicit label?
    if "/" not in s and "\\" not in s and "." not in s:
        return s
    # Step 2 — try SemanticIndex match
    if semantic_index is not None:
        try:
            clusters = getattr(semantic_index, "clusters", None)
            if clusters is not None:
                snap = (
                    clusters
                    if isinstance(clusters, tuple)
                    else tuple(clusters)
                )
                # Compare the supplied path tail to every
                # cluster's representative_paths tail.
                target_tail = s.split("/")[-1].split("\\")[-1]
                for c in snap:
                    rep_paths = getattr(
                        c, "representative_paths", (),
                    ) or ()
                    for rp in rep_paths:
                        rp_norm = (
                            str(rp).strip().lower()
                        )
                        if not rp_norm:
                            continue
                        if (
                            rp_norm == s
                            or rp_norm.endswith("/" + target_tail)
                            or rp_norm.endswith("\\" + target_tail)
                            or rp_norm == target_tail
                        ):
                            cid = getattr(c, "cluster_id", None)
                            if cid is not None:
                                return f"sem-{int(cid)}"
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[curiosity_collector] resolve_cluster_id "
                "semantic_index lookup raised: %s", exc,
            )
    # Step 3 — _global fallback (Decision A3)
    return _GLOBAL_FALLBACK_CLUSTER_ID


# ---------------------------------------------------------------------------
# Per-cluster state — encapsulates ring buffer + source history
# ---------------------------------------------------------------------------


class _ClusterState:
    """Mutable per-cluster state. Lives behind the collector's
    RLock; readers never touch directly — they pull a snapshotted
    list of observations and call :func:`compute_curiosity`."""

    __slots__ = (
        "cluster_id",
        "observations",
        "source_history",
        "last_score",
        "last_observed_at_unix",
    )

    def __init__(
        self, cluster_id: str, *, window_size: int,
    ) -> None:
        self.cluster_id: str = cluster_id
        # Bounded ring; deque drops oldest on append past maxlen
        self.observations: Deque[CuriosityObservation] = deque(
            maxlen=max(8, window_size),
        )
        # Source-history ring for RECURRENCE_LOOP detection —
        # capped same size as the threshold ceiling so a
        # consecutive run of N source-tags is detectable
        self.source_history: Deque[CuriositySource] = deque(
            maxlen=max(3, curiosity_recurrence_loop_threshold()),
        )
        self.last_score: Optional[CuriosityScore] = None
        self.last_observed_at_unix: float = 0.0


# ---------------------------------------------------------------------------
# CuriosityCollector — atomic frozen-swap state container
# ---------------------------------------------------------------------------


class CuriosityCollector:
    """Per-process collector. Thread-safe via ``threading.RLock``.

    Responsibilities:
      * Receive observations (3 record_* methods, one per source)
      * Maintain per-cluster bounded ring buffer
      * Compute :class:`CuriosityScore` on demand via the pure
        :func:`compute_curiosity` aggregator (Slice 1)
      * Detect + apply auto-decay (STALE_FOCUS / RECURRENCE_LOOP)
      * Best-effort persist to per-cluster JSONL
      * Provide pull-side ``score_for_cluster`` + ``snapshot_all``

    NEVER raises out of any public method — defensive everywhere.
    """

    __slots__ = (
        "_states",
        "_lock",
        "_window_size",
        "_history_dir",
        "_persist_enabled",
        "_decay_overrides",
    )

    def __init__(
        self,
        *,
        window_size: Optional[int] = None,
        history_dir: Optional[Path] = None,
        persist_enabled: Optional[bool] = None,
    ) -> None:
        self._states: Dict[str, _ClusterState] = {}
        self._lock = threading.RLock()
        self._window_size: int = (
            window_size
            if isinstance(window_size, int) and window_size >= 8
            else curiosity_window_size()
        )
        self._history_dir: Path = (
            Path(history_dir)
            if history_dir is not None
            else curiosity_history_dir()
        )
        self._persist_enabled: bool = (
            persist_enabled
            if persist_enabled is not None
            else curiosity_persist_enabled()
        )
        # Per-cluster operator-explicit decay overrides — Slice
        # 4's `/curiosity reset <id>` writes here. Cleared on
        # next observation in that cluster.
        self._decay_overrides: Dict[
            str, CuriosityDecayReason,
        ] = {}

    # ---- internal helpers --------------------------------------

    def _get_or_create(
        self, cluster_id: str,
    ) -> _ClusterState:
        """Caller must hold :attr:`_lock`."""
        s = self._states.get(cluster_id)
        if s is None:
            s = _ClusterState(
                cluster_id, window_size=self._window_size,
            )
            self._states[cluster_id] = s
        return s

    def _persist_observation(
        self, obs: CuriosityObservation,
    ) -> None:
        """Best-effort append to ``.jarvis/curiosity/{cid}.jsonl``.
        NEVER raises."""
        if not self._persist_enabled:
            return
        try:
            path = self._history_dir / f"{obs.cluster_id}.jsonl"
            row = json.dumps(
                {
                    "schema_version": (
                        CURIOSITY_GRADIENT_SCHEMA_VERSION
                    ),
                    "source": obs.source.value,
                    "cluster_id": obs.cluster_id,
                    "value": float(obs.value),
                    "at_unix": float(obs.at_unix),
                    "op_id": obs.op_id,
                },
                separators=(",", ":"),
                ensure_ascii=False,
            )
            flock_append_line(path, row)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[curiosity_collector] persist raised: %s",
                exc,
            )

    def _detect_recurrence_loop(
        self, state: _ClusterState,
    ) -> bool:
        """Returns True if the source-history ring shows the same
        source ``threshold`` consecutive times. Caller holds
        lock."""
        threshold = curiosity_recurrence_loop_threshold()
        if len(state.source_history) < threshold:
            return False
        # Only trigger when *every* slot in the ring is the same
        # source. Stricter than "majority"; protects against
        # alternating-source noise looking like a loop.
        recent = list(state.source_history)[-threshold:]
        if not recent:
            return False
        first = recent[0]
        return all(s is first for s in recent)

    def _resolve_decay_reason(
        self,
        cluster_id: str,
        state: _ClusterState,
        *,
        now_ts: float,
    ) -> CuriosityDecayReason:
        """Closed-enum dispatch. Operator-reset overrides
        everything; otherwise stale-focus precedes recurrence-
        loop precedes NONE. Caller holds lock."""
        # Operator-explicit reset trumps automatic decay
        override = self._decay_overrides.pop(cluster_id, None)
        if override is not None:
            return override
        # Stale-focus check
        if state.last_score is not None:
            if state.last_score.is_stale(now_ts=now_ts):
                return CuriosityDecayReason.STALE_FOCUS
        # Recurrence-loop check
        if self._detect_recurrence_loop(state):
            return CuriosityDecayReason.RECURRENCE_LOOP
        return CuriosityDecayReason.NONE

    def _record(
        self,
        *,
        source: CuriositySource,
        cluster_id: str,
        value: float,
        op_id: str,
        at_unix: Optional[float] = None,
    ) -> Optional[CuriosityScore]:
        """Internal record helper — shared by all 3 public
        record_* methods. NEVER raises."""
        try:
            if not curiosity_gradient_enabled():
                return None
            cid = _normalize_cluster_id(cluster_id)
            ts = (
                float(at_unix)
                if at_unix is not None
                else time.time()
            )
            # Defensive value clamp — primitive layer also
            # clamps but we clamp at ingest too so the
            # persisted JSONL is always in [0, 1].
            v = float(value)
            if not (v == v):  # NaN check via self-comparison
                return None
            if v < 0.0:
                v = 0.0
            elif v > 1.0:
                v = 1.0
            obs = CuriosityObservation(
                source=source,
                cluster_id=cid,
                value=v,
                at_unix=ts,
                op_id=str(op_id or ""),
            )
            with self._lock:
                state = self._get_or_create(cid)
                state.observations.append(obs)
                state.source_history.append(source)
                state.last_observed_at_unix = ts
                # Compute new score (atomic frozen-swap pattern
                # — old state.last_score becomes garbage, new
                # one becomes authoritative under the lock).
                decay = self._resolve_decay_reason(
                    cid, state, now_ts=ts,
                )
                new_score = compute_curiosity(
                    cid,
                    list(state.observations),
                    now_ts=ts,
                    decay_reason_override=(
                        decay
                        if decay is not CuriosityDecayReason.NONE
                        else None
                    ),
                )
                state.last_score = new_score
            # Persist OUTSIDE the lock — JSONL append doesn't
            # need state-lock and can be slow under contention.
            self._persist_observation(obs)
            return new_score
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[curiosity_collector] _record raised: %s", exc,
            )
            return None

    # ---- public record API -------------------------------------

    def record_logprob_entropy(
        self,
        cluster_id: str,
        entropy_normalized: float,
        *,
        op_id: str = "",
        at_unix: Optional[float] = None,
    ) -> Optional[CuriosityScore]:
        """Slice 5 wire-up: GENERATE phase via phase_capture
        adapter. ``entropy_normalized`` MUST be in [0, 1] (caller
        normalizes via ``H / max_H_per_window``); collector
        clamps defensively."""
        return self._record(
            source=CuriositySource.LOGPROB_ENTROPY,
            cluster_id=cluster_id,
            value=entropy_normalized,
            op_id=op_id,
            at_unix=at_unix,
        )

    def record_prophecy_error(
        self,
        cluster_id: str,
        error_magnitude: float,
        *,
        op_id: str = "",
        at_unix: Optional[float] = None,
    ) -> Optional[CuriosityScore]:
        """Slice 5 wire-up: post-VERIFY. ``error_magnitude`` =
        ``|predicted_risk - actual_outcome_indicator|`` in
        [0, 1]."""
        return self._record(
            source=CuriositySource.PROPHECY_ERROR,
            cluster_id=cluster_id,
            value=error_magnitude,
            op_id=op_id,
            at_unix=at_unix,
        )

    def record_recurrence_drift(
        self,
        cluster_id: str,
        recurrence_count: int,
        *,
        op_id: str = "",
        at_unix: Optional[float] = None,
    ) -> Optional[CuriosityScore]:
        """Slice 5 wire-up: Coherence Auditor's RECURRENCE_DRIFT
        signal. ``recurrence_count`` is normalized via
        :func:`_scoring_primitives.weight_score` (log-scale,
        saturating near reference) so a single 50-recurrence
        outlier doesn't dominate the aggregate."""
        try:
            normalized = weight_score(int(recurrence_count))
        except Exception:  # noqa: BLE001 — defensive
            normalized = 0.0
        return self._record(
            source=CuriositySource.POSTMORTEM_RECURRENCE,
            cluster_id=cluster_id,
            value=normalized,
            op_id=op_id,
            at_unix=at_unix,
        )

    # ---- public query API --------------------------------------

    def score_for_cluster(
        self, cluster_id: str,
        *,
        now_ts: Optional[float] = None,
    ) -> CuriosityScore:
        """Pull-side query. Slice 3's `SensorGovernor` lazy-
        imports + calls this from ``_weighted_cap``. Recomputes
        the score on demand (incorporates fresh stale-focus
        check) — does NOT use the cached ``last_score`` blindly,
        so an idle cluster's STALE_FOCUS is detected even if
        no new observations have arrived.

        Returns an inert score (DISABLED / INSUFFICIENT_DATA)
        when master flag is off / cluster is unknown / has too
        few samples."""
        try:
            if not curiosity_gradient_enabled():
                return compute_curiosity(
                    cluster_id, [], enabled_override=False,
                )
            cid = _normalize_cluster_id(cluster_id)
            ts = (
                float(now_ts)
                if now_ts is not None
                else time.time()
            )
            with self._lock:
                state = self._states.get(cid)
                if state is None:
                    # Unknown cluster — cold-start
                    return compute_curiosity(cid, [], now_ts=ts)
                obs_snapshot = list(state.observations)
                decay = self._resolve_decay_reason(
                    cid, state, now_ts=ts,
                )
            # Compute outside the lock — pure function on a
            # stable snapshot, doesn't touch state
            new_score = compute_curiosity(
                cid, obs_snapshot, now_ts=ts,
                decay_reason_override=(
                    decay
                    if decay is not CuriosityDecayReason.NONE
                    else None
                ),
            )
            # Update the cached last_score atomically under lock
            # so a subsequent score_for_cluster call sees the
            # updated decay_reason / last_updated_at_unix
            with self._lock:
                state = self._states.get(cid)
                if state is not None:
                    state.last_score = new_score
            return new_score
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[curiosity_collector] score_for_cluster "
                "raised: %s", exc,
            )
            return compute_curiosity(
                cluster_id, [], enabled_override=False,
            )

    def snapshot_all(self) -> Tuple[CuriosityScore, ...]:
        """All currently-tracked cluster scores. Slice 4
        observability + REPL projection. NEVER raises."""
        try:
            now = time.time()
            with self._lock:
                cluster_ids = list(self._states.keys())
            scores: List[CuriosityScore] = []
            for cid in cluster_ids:
                try:
                    scores.append(
                        self.score_for_cluster(
                            cid, now_ts=now,
                        ),
                    )
                except Exception:  # noqa: BLE001 — defensive
                    continue
            return tuple(scores)
        except Exception:  # noqa: BLE001 — defensive
            return tuple()

    def all_cluster_ids(self) -> Tuple[str, ...]:
        """Currently-tracked cluster_id snapshot. NEVER raises."""
        try:
            with self._lock:
                return tuple(self._states.keys())
        except Exception:  # noqa: BLE001 — defensive
            return tuple()

    # ---- public mutation API (operator-explicit only) ----------

    def reset_cluster(
        self, cluster_id: str,
        *,
        reason: CuriosityDecayReason = (
            CuriosityDecayReason.OPERATOR_RESET
        ),
    ) -> bool:
        """Slice 4 ``/curiosity reset <id>`` write surface.
        Marks the cluster for forced decay on the next
        :meth:`score_for_cluster` call. Idempotent — repeated
        resets are no-ops. Returns True on success, False on
        master-off or invalid input. NEVER raises."""
        try:
            if not curiosity_gradient_enabled():
                return False
            cid = _normalize_cluster_id(cluster_id)
            if cid == _GLOBAL_FALLBACK_CLUSTER_ID:
                # Allow reset on _global too — operator may want
                # to clear the fallback bucket
                pass
            with self._lock:
                self._decay_overrides[cid] = reason
            return True
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[curiosity_collector] reset_cluster raised: "
                "%s", exc,
            )
            return False

    def __len__(self) -> int:
        try:
            with self._lock:
                return len(self._states)
        except Exception:  # noqa: BLE001 — defensive
            return 0


# ---------------------------------------------------------------------------
# Default collector singleton (process-global)
# ---------------------------------------------------------------------------


_DEFAULT_COLLECTOR: Optional[CuriosityCollector] = None
_DEFAULT_COLLECTOR_LOCK = threading.Lock()


def get_default_collector() -> CuriosityCollector:
    """Return the process-global default collector. Lazy-
    constructed; threadsafe."""
    global _DEFAULT_COLLECTOR  # noqa: PLW0603
    with _DEFAULT_COLLECTOR_LOCK:
        if _DEFAULT_COLLECTOR is None:
            _DEFAULT_COLLECTOR = CuriosityCollector()
        return _DEFAULT_COLLECTOR


def reset_default_collector_for_tests() -> None:
    """Test-only — drop the default collector. Production code
    NEVER calls this."""
    global _DEFAULT_COLLECTOR  # noqa: PLW0603
    with _DEFAULT_COLLECTOR_LOCK:
        _DEFAULT_COLLECTOR = None


# ---------------------------------------------------------------------------
# JSONL replay — useful for debugging + Slice 4 detail endpoints
# ---------------------------------------------------------------------------


def read_observations_for_cluster(
    cluster_id: str,
    *,
    limit: int = 1000,
    history_dir: Optional[Path] = None,
) -> Tuple[CuriosityObservation, ...]:
    """Read up to ``limit`` most-recent observations from the
    per-cluster JSONL. Tail-keep semantics — when the file has
    more rows than ``limit``, the most recent ``limit`` are
    returned. Returns empty tuple on any error. NEVER raises."""
    try:
        cid = _normalize_cluster_id(cluster_id)
        base = (
            history_dir
            if history_dir is not None
            else curiosity_history_dir()
        )
        path = Path(base) / f"{cid}.jsonl"
        if not path.exists():
            return tuple()
        with flock_critical_section(path) as acquired:
            if not acquired:
                return tuple()
            try:
                lines = path.read_text(
                    encoding="utf-8",
                ).splitlines()
            except Exception:  # noqa: BLE001 — defensive
                return tuple()
        # Tail-keep most recent
        if len(lines) > limit:
            lines = lines[-limit:]
        out: List[CuriosityObservation] = []
        for line in lines:
            try:
                row = json.loads(line)
                source_value = row.get("source")
                if not source_value:
                    continue
                try:
                    source = CuriositySource(source_value)
                except ValueError:
                    continue
                out.append(
                    CuriosityObservation(
                        source=source,
                        cluster_id=str(
                            row.get("cluster_id", cid),
                        ),
                        value=float(row.get("value", 0.0)),
                        at_unix=float(row.get("at_unix", 0.0)),
                        op_id=str(row.get("op_id", "")),
                    ),
                )
            except Exception:  # noqa: BLE001 — defensive
                continue
        return tuple(out)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[curiosity_collector] read_observations_for_"
            "cluster raised: %s", exc,
        )
        return tuple()


__all__ = [
    "CuriosityCollector",
    "curiosity_history_dir",
    "curiosity_persist_enabled",
    "curiosity_recurrence_loop_threshold",
    "curiosity_window_size",
    "get_default_collector",
    "read_observations_for_cluster",
    "reset_default_collector_for_tests",
    "resolve_cluster_id",
]
