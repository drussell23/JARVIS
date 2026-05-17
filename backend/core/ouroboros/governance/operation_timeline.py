"""OperationTimeline — the causal join read-model (PRD §42, Slice 1).

The root problem (§42.1): O+V acts while the operator is away. Git stores
*content* but is structurally incapable of storing the *relation*
``signal → op → plan → diff → commit → outcome → checkpoint``. The
``OpsDigestObserver`` protocol already *emits* every piece of that
relation as it happens — it just scatters them into session-local
``summary.json`` instead of one durable causal index. This module is
that missing index, and **nothing more**.

Slice 1 contract (zero behavior change)
---------------------------------------

This slice ships ONLY the read-model substrate. It implements the
existing :class:`backend.core.ouroboros.governance.ops_digest_observer.OpsDigestObserver`
protocol (the three ``on_*`` methods) so it *can* be registered later,
but Slice 1 deliberately does **not** wire it into the single global
observer pointer — that fan-out wiring is Slice 2's "causal join
completion + read surface" scope (§42.8). With
``JARVIS_OPERATION_TIMELINE_ENABLED`` default-FALSE (§33.1 / §42.7),
every observer method is a hard no-op: zero rows, zero disk I/O, zero
behavior change anywhere in the loop. This mirrors the Stage-1.6
Park-spike "Slice 1 = zero runtime change" precedent exactly.

Architectural contract (zero duplication — §42.3)
-------------------------------------------------

  * **Composes canonical surfaces only**:
      - ``cross_process_jsonl.flock_append_line`` — the single
        cross-process JSONL append seam (Vector #10 / v2.82). AST-pinned:
        no homegrown ``fcntl`` / raw append / ``json.dump`` substitute.
      - The ``OpsDigestObserver`` protocol method *shapes* (consumes
        the events the orchestrator/AutoCommitter already emit; never
        re-derives them — AST-pinned: no ``git`` subprocess, no
        TestRunner in this module).

  * **The timeline owns only the edges.** Every datum is a foreign key
    to an authority that already owns it: ``op_id`` → OperationLedger,
    ``diff_ref`` → DiffArchive ``d-N`` ring, ``commit_hash`` →
    AutoCommitter/git, ``checkpoint_ref`` → WorkspaceCheckpointManager.
    The row stores pointers, never copies of those bodies (no
    ``diff_text``, no full plan body, no state-machine transitions —
    OperationLedger remains the state authority).

  * **In-memory cache is hot read; JSONL is authoritative audit.** Each
    observer callback merges its fields into the per-``op_id`` row and
    appends a fresh JSONL row (append-only audit — re-applied/reverted
    ops add rows, never mutate). The in-memory projection collapses to
    latest-write-wins per ``op_id`` for the scrub view. Identical
    persistence discipline to the SWE-Bench-Pro ``EvaluationResultStore``
    (Phase D) — reused, not reinvented.

Authority invariant (§42.6 pins 1–4)
-------------------------------------

This is a telemetry-only read-model with **zero authority**. It never
writes ``OperationState``, never assigns a risk tier, never imports the
orchestrator / policy_engine / iron_gate / change_engine /
candidate_generator / governed_loop_service / repair_engine. It can
never corrupt the loop because it is structurally incapable of acting
on it. AST pins prove this.

Fail-closed contract (§7)
-------------------------

Every public method NEVER raises. The observer protocol is explicitly
best-effort fire-and-forget; a misbehaving observer must not derail
APPLY / VERIFY / commit. Internal failure is swallowed at DEBUG and the
method returns (observer hooks) or returns an empty/zero result
(query/replay).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
)

logger = logging.getLogger("Ouroboros.OperationTimeline")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================

#: Schema version for one timeline row. The Slice-2 causal-join fields
#: (signal_source, urgency, risk_tier, plan_ref, diff_ref, file_paths,
#: checkpoint_ref, parent_op_id, terminal_state, reverted_by) are present
#: NOW as Optional/None so Slice 2 *populates* them without a schema bump
#: — forward-compatible by construction (§42.4).
TIMELINE_SCHEMA_VERSION: str = "timeline.1"

#: Monotonic operator-facing ref handle prefix. Free prefix per the §42
#: ref-table (t-/d-/o-/n-/p-/q-/b- are taken). Never reused; the counter
#: is never reset within a process lifetime.
REF_PREFIX: str = "r-"

OPERATION_TIMELINE_ENABLED_ENV_VAR: str = "JARVIS_OPERATION_TIMELINE_ENABLED"
OPERATION_TIMELINE_PATH_ENV_VAR: str = "JARVIS_OPERATION_TIMELINE_PATH"
OPERATION_TIMELINE_MAX_ROWS_ENV_VAR: str = "JARVIS_OPERATION_TIMELINE_MAX_ROWS"

_DEFAULT_TIMELINE_PATH: str = ".jarvis/operation_timeline.jsonl"
_DEFAULT_MAX_ROWS: int = 5000
_MAX_ROWS_FLOOR: int = 1
_MAX_ROWS_CEIL: int = 1_000_000


# ===========================================================================
# Frozen TimelineRow (§33.5 symmetric to_dict/from_dict)
# ===========================================================================


@dataclass(frozen=True)
class TimelineRow:
    """One causal row: the join the system currently lacks.

    Slice 1 populates the milestone fields from the three
    ``OpsDigestObserver`` callbacks. The causal-join fields default to
    ``None`` and are filled by Slice 2's read-only joins over the
    IntentEnvelope / DiffArchive / WorkspaceCheckpointManager — without
    a schema bump (forward-compatible).
    """

    # -- provenance (timeline-owned) -----------------------------------
    schema_version: str
    ref: str                       # monotonic r-N handle; stable per op_id
    op_id: str                     # FK → OperationLedger (authority)
    first_seen_iso: str            # ISO-8601 UTC of the first callback
    updated_iso: str               # ISO-8601 UTC of the latest merge
    monotonic_at: float            # intra-session ordering stability

    # -- milestone fields (Slice 1 — from OpsDigestObserver callbacks) -
    apply_mode: Optional[str] = None        # none|single|multi
    apply_files: Optional[int] = None       # count of files APPLY touched
    verify_passed: Optional[int] = None
    verify_total: Optional[int] = None
    verify_scoped_to_op: Optional[bool] = None
    commit_hash: Optional[str] = None       # THE missing link

    # -- causal-join fields (Slice 2 — present now, populated later) ---
    session_id: Optional[str] = None
    parent_op_id: Optional[str] = None
    signal_source: Optional[str] = None
    urgency: Optional[str] = None
    risk_tier: Optional[str] = None         # copied string ONLY (pin 4)
    plan_ref: Optional[Mapping[str, Any]] = None  # {hash, summary}
    diff_ref: Optional[str] = None          # FK → DiffArchive d-N
    file_paths: Tuple[str, ...] = ()        # blast-radius join key
    checkpoint_ref: Optional[str] = None    # FK → WorkspaceCheckpointMgr
    terminal_state: Optional[str] = None    # denormalized ledger pointer
    reverted_by: Optional[str] = None       # back-edge filled by /revert

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ref": self.ref,
            "op_id": self.op_id,
            "first_seen_iso": self.first_seen_iso,
            "updated_iso": self.updated_iso,
            "monotonic_at": self.monotonic_at,
            "apply_mode": self.apply_mode,
            "apply_files": self.apply_files,
            "verify_passed": self.verify_passed,
            "verify_total": self.verify_total,
            "verify_scoped_to_op": self.verify_scoped_to_op,
            "commit_hash": self.commit_hash,
            "session_id": self.session_id,
            "parent_op_id": self.parent_op_id,
            "signal_source": self.signal_source,
            "urgency": self.urgency,
            "risk_tier": self.risk_tier,
            "plan_ref": dict(self.plan_ref) if self.plan_ref else None,
            "diff_ref": self.diff_ref,
            "file_paths": list(self.file_paths),
            "checkpoint_ref": self.checkpoint_ref,
            "terminal_state": self.terminal_state,
            "reverted_by": self.reverted_by,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TimelineRow":
        plan_ref = payload.get("plan_ref")
        raw_paths = payload.get("file_paths") or ()
        return cls(
            schema_version=str(
                payload.get("schema_version", TIMELINE_SCHEMA_VERSION)
            ),
            ref=str(payload["ref"]),
            op_id=str(payload["op_id"]),
            first_seen_iso=str(payload.get("first_seen_iso", "")),
            updated_iso=str(payload.get("updated_iso", "")),
            monotonic_at=float(payload.get("monotonic_at", 0.0) or 0.0),
            apply_mode=payload.get("apply_mode"),
            apply_files=payload.get("apply_files"),
            verify_passed=payload.get("verify_passed"),
            verify_total=payload.get("verify_total"),
            verify_scoped_to_op=payload.get("verify_scoped_to_op"),
            commit_hash=payload.get("commit_hash"),
            session_id=payload.get("session_id"),
            parent_op_id=payload.get("parent_op_id"),
            signal_source=payload.get("signal_source"),
            urgency=payload.get("urgency"),
            risk_tier=payload.get("risk_tier"),
            plan_ref=dict(plan_ref) if isinstance(plan_ref, Mapping) else None,
            diff_ref=payload.get("diff_ref"),
            file_paths=tuple(str(p) for p in raw_paths),
            checkpoint_ref=payload.get("checkpoint_ref"),
            terminal_state=payload.get("terminal_state"),
            reverted_by=payload.get("reverted_by"),
        )


# ===========================================================================
# Env loaders (NEVER raise)
# ===========================================================================


def _timeline_enabled() -> bool:
    """Master flag query (§33.1 default-FALSE). OFF ⇒ every observer
    method is a hard no-op (the zero-behavior-change guarantee)."""
    raw = os.environ.get(
        OPERATION_TIMELINE_ENABLED_ENV_VAR, "",
    ).strip().lower()
    return raw in ("true", "1", "yes", "on")


def _resolve_timeline_path(explicit: Optional[Path]) -> Path:
    """Resolve the durable causal-index path. Precedence: explicit
    argument > env var > default. NEVER raises."""
    if explicit is not None:
        return Path(explicit)
    raw = os.environ.get(OPERATION_TIMELINE_PATH_ENV_VAR, "").strip()
    if raw:
        return Path(raw)
    return Path(_DEFAULT_TIMELINE_PATH)


def _resolve_max_rows() -> int:
    """Bounded tail-scan cap, read at call time (monkeypatchable in
    tests — identical discipline to the SWE-Bench-Pro
    ``_LOCAL_JSONL_MAX_ROWS`` precedent). Invalid ⇒ default. Clamped to
    a sane range. NEVER raises."""
    raw = os.environ.get(OPERATION_TIMELINE_MAX_ROWS_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_MAX_ROWS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ROWS
    if value < _MAX_ROWS_FLOOR:
        return _MAX_ROWS_FLOOR
    if value > _MAX_ROWS_CEIL:
        return _MAX_ROWS_CEIL
    return value


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _monotonic() -> float:
    # Local import-free monotonic; time is stdlib and authority-free.
    import time

    return time.monotonic()


# ===========================================================================
# OperationTimeline — the read-model
# ===========================================================================


class OperationTimeline:
    """Causal join read-model implementing the OpsDigestObserver
    protocol shape.

    The three observer callbacks arrive at different times for the same
    ``op_id`` (apply → verify → commit, any of which may be absent). Each
    callback *merges* its fields into the per-``op_id`` row, keeping the
    row's stable ``r-N`` ref, and appends a fresh JSONL audit row. The
    in-memory projection is latest-write-wins per ``op_id``.

    Parameters
    ----------
    persistence_path:
        Optional override for the JSONL path. ``None`` ⇒ env > default.
    enabled:
        Optional override for the master flag. ``None`` ⇒ env. Allows
        test-time injection without env juggling. When effectively
        False, every observer method is a hard no-op.
    """

    def __init__(
        self,
        *,
        persistence_path: Optional[Path] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self._path: Path = _resolve_timeline_path(persistence_path)
        self._enabled_override: Optional[bool] = enabled
        self._rows: Dict[str, TimelineRow] = {}
        self._seq: int = 0
        self._lock = threading.Lock()

    # -- introspection --------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    @property
    def persistence_path(self) -> Path:
        return self._path

    def is_enabled(self) -> bool:
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        return _timeline_enabled()

    def clear(self) -> None:
        """Drop the in-memory projection. Does NOT touch the JSONL on
        disk — the audit history is intentionally append-only and
        survives in-memory resets (test teardown / daemon restart)."""
        with self._lock:
            self._rows.clear()

    # -- OpsDigestObserver protocol shape ------------------------------

    def on_apply_succeeded(
        self, *, op_id: str, mode: str, files: int,
    ) -> None:
        """An APPLY phase concluded. Merge mode/files into the row.
        Hard no-op when the master flag is off. NEVER raises."""
        self._merge(
            op_id,
            {"apply_mode": str(mode), "apply_files": int(files)},
        )

    def on_verify_completed(
        self,
        *,
        op_id: str,
        passed: int,
        total: int,
        scoped_to_applied_op: bool = True,
    ) -> None:
        """A VERIFY phase finished. Merge test counts. Hard no-op when
        the master flag is off. NEVER raises."""
        self._merge(
            op_id,
            {
                "verify_passed": int(passed),
                "verify_total": int(total),
                "verify_scoped_to_op": bool(scoped_to_applied_op),
            },
        )

    def on_commit_succeeded(
        self, *, op_id: str, commit_hash: str,
    ) -> None:
        """AutoCommitter published a commit — THE missing link. Merge
        the hash. Hard no-op when the master flag is off. NEVER
        raises."""
        self._merge(op_id, {"commit_hash": str(commit_hash)})

    def on_op_classified(
        self,
        *,
        op_id: str,
        signal_source: str,
        urgency: str,
        risk_tier: str,
    ) -> None:
        """The op's causal origin (signal → op edge) became known at
        the INTENT seam. Merge signal_source / urgency / risk_tier —
        the one edge the apply/verify/commit callbacks structurally
        cannot carry. Flows through the canonical OpsDigestObserver
        seam (PRD §42.3 — no parallel op_id→envelope registry). Hard
        no-op when the master flag is off. NEVER raises."""
        self._merge(
            op_id,
            {
                "signal_source": str(signal_source) or None,
                "urgency": str(urgency) or None,
                "risk_tier": str(risk_tier) or None,
            },
        )

    # -- internal merge + append ---------------------------------------

    def _merge(self, op_id: str, fields: Mapping[str, Any]) -> None:
        """Upsert the per-op row with ``fields`` and append a fresh
        JSONL audit row. Fail-closed: the master-flag gate is the FIRST
        executable statement (the zero-behavior-change guarantee);
        nothing below it runs when disabled. NEVER raises."""
        try:
            if not self.is_enabled():
                return
            if not op_id:
                return
            now_iso = _now_iso()
            with self._lock:
                existing = self._rows.get(op_id)
                if existing is None:
                    self._seq += 1
                    row = TimelineRow(
                        schema_version=TIMELINE_SCHEMA_VERSION,
                        ref=f"{REF_PREFIX}{self._seq}",
                        op_id=op_id,
                        first_seen_iso=now_iso,
                        updated_iso=now_iso,
                        monotonic_at=_monotonic(),
                        **dict(fields),
                    )
                else:
                    # Stable ref + first_seen; merge new fields; bump
                    # updated_iso. replace() keeps the frozen dataclass
                    # immutable while producing the merged successor.
                    row = replace(
                        existing,
                        updated_iso=now_iso,
                        **dict(fields),
                    )
                self._rows[op_id] = row
            # Read-only causal join: pull the edges that ARE op_id-keyed
            # in a canonical singleton-reachable substrate (DiffArchive).
            # Lazy + best-effort + only fills still-empty fields — never
            # overwrites a value an explicit callback already set, never
            # owns or duplicates DiffArchive state.
            row = self._join_diff_archive(op_id, row)
            self._append_jsonl(row)
        except Exception:  # noqa: BLE001 — observer is fail-closed
            logger.debug(
                "[OperationTimeline] _merge swallowed", exc_info=True,
            )

    def _join_diff_archive(
        self, op_id: str, row: TimelineRow,
    ) -> TimelineRow:
        """Fill diff_ref / file_paths / risk_tier from the canonical
        DiffArchive (op_id-keyed, singleton-reachable, authority-free).
        Adaptive: only fills fields still unset — an explicit
        on_op_classified risk_tier wins over the diff's copy. NEVER
        raises; returns the row unchanged on any failure."""
        try:
            from backend.core.ouroboros.battle_test.diff_archive import (
                get_default_archive,
            )

            matches = get_default_archive().find_by_op_id(op_id)
            if not matches:
                return row
            newest = matches[-1]  # find_by_op_id is oldest → newest
            patch: Dict[str, Any] = {}
            new_ref = getattr(newest, "ref", None)
            if new_ref and row.diff_ref != new_ref:
                patch["diff_ref"] = str(new_ref)
            paths = tuple(getattr(newest, "file_paths", ()) or ())
            if paths and not row.file_paths:
                patch["file_paths"] = paths
            if not row.risk_tier:
                rt = getattr(newest, "risk_tier", None)
                if rt:
                    patch["risk_tier"] = str(rt)
            if not patch:
                return row
            merged = replace(row, **patch)
            with self._lock:
                self._rows[op_id] = merged
            return merged
        except Exception:  # noqa: BLE001
            logger.debug(
                "[OperationTimeline] _join_diff_archive swallowed",
                exc_info=True,
            )
            return row

    def _append_jsonl(self, row: TimelineRow) -> None:
        """Append one row to the durable causal index via the canonical
        cross-process flock primitive. Sync by design: the
        OpsDigestObserver protocol is sync fire-and-forget and the
        flock scope is open-write-flush-close (microseconds — see
        cross_process_jsonl module docstring). NEVER raises."""
        try:
            line = json.dumps(
                row.to_dict(), sort_keys=True, default=str,
            )
            ok = flock_append_line(self._path, line)
            if not ok:
                logger.debug(
                    "[OperationTimeline] flock append returned False "
                    "for %s", row.ref,
                )
            self._publish_sse(row)
        except Exception:  # noqa: BLE001 — belt-and-suspenders
            logger.debug(
                "[OperationTimeline] _append_jsonl swallowed",
                exc_info=True,
            )

    def _publish_sse(self, row: TimelineRow) -> None:
        """Best-effort SSE notification on the existing StreamEventBroker
        (PRD §42 Slice 2 read surface). Composes the canonical
        publish_task_event hook — which itself gates on stream_enabled()
        and never raises. Lazy import keeps the timeline decoupled from
        the stream module when observability is off. NEVER raises."""
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                EVENT_TYPE_OPERATION_TIMELINE_ROW,
                publish_task_event,
            )

            publish_task_event(
                EVENT_TYPE_OPERATION_TIMELINE_ROW,
                row.op_id,
                {
                    "ref": row.ref,
                    "op_id": row.op_id,
                    "signal_source": row.signal_source,
                    "urgency": row.urgency,
                    "risk_tier": row.risk_tier,
                    "apply_mode": row.apply_mode,
                    "verify_passed": row.verify_passed,
                    "verify_total": row.verify_total,
                    "commit_hash": row.commit_hash,
                    "diff_ref": row.diff_ref,
                    "updated_iso": row.updated_iso,
                },
            )
        except Exception:  # noqa: BLE001 — read surface is best-effort
            logger.debug(
                "[OperationTimeline] _publish_sse swallowed",
                exc_info=True,
            )

    def list_recent(self, *, limit: int = 5) -> Tuple[TimelineRow, ...]:
        """Most-recent rows, newest first — the /expand summary +
        /timeline REPL accessor. Thin bounded wrapper over
        :meth:`query`. NEVER raises."""
        try:
            return self.query(limit=max(1, int(limit)))
        except Exception:  # noqa: BLE001
            return ()

    # -- query ----------------------------------------------------------

    def query(
        self,
        *,
        op_id: Optional[str] = None,
        terminal_state: Optional[str] = None,
        has_commit: Optional[bool] = None,
        limit: Optional[int] = None,
    ) -> Tuple[TimelineRow, ...]:
        """Bounded snapshot read of the in-memory projection, newest
        first (descending ``monotonic_at``, ties broken by ref).
        NEVER raises — returns ``()`` on internal failure."""
        try:
            with self._lock:
                snapshot: List[TimelineRow] = list(self._rows.values())
            snapshot.sort(
                key=lambda r: (r.monotonic_at, r.ref), reverse=True,
            )
            out: List[TimelineRow] = []
            for r in snapshot:
                if op_id is not None and r.op_id != op_id:
                    continue
                if terminal_state is not None and (
                    r.terminal_state != terminal_state
                ):
                    continue
                if has_commit is True and not r.commit_hash:
                    continue
                if has_commit is False and r.commit_hash:
                    continue
                out.append(r)
                if limit is not None and len(out) >= limit:
                    break
            return tuple(out)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[OperationTimeline] query swallowed", exc_info=True,
            )
            return ()

    def lookup(self, ref: str) -> Optional[TimelineRow]:
        """Reverse-lookup a row by its ``r-N`` ref. NEVER raises."""
        try:
            with self._lock:
                for r in self._rows.values():
                    if r.ref == ref:
                        return r
            return None
        except Exception:  # noqa: BLE001
            return None

    # -- disk replay (cross-session morning-after substrate) -----------

    def replay_from_disk(self) -> int:
        """Reconstruct the in-memory projection from the JSONL audit
        file. Returns the count of rows replayed. Bounded by
        :func:`_resolve_max_rows`. Idempotent: the per-``op_id`` dedup
        collapses duplicates so the last-written row wins. Malformed
        rows are skipped at DEBUG. NEVER raises.

        This is the substrate the cross-session scrub (§42.9 criterion
        2) reads on a fresh boot — the capability Claude Code
        structurally cannot have.
        """
        try:
            path = self._path
            if not path.exists():
                return 0
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                return 0

            lines = [ln for ln in text.splitlines() if ln.strip()]
            max_rows = _resolve_max_rows()
            # Bounded: only the most recent ``max_rows`` lines matter
            # for the projection (latest-write-wins collapses earlier
            # duplicates anyway). Tail-scan, not full-file load.
            if len(lines) > max_rows:
                lines = lines[-max_rows:]

            count = 0
            max_seq = self._seq
            for raw in lines:
                try:
                    payload = json.loads(raw)
                    row = TimelineRow.from_dict(payload)
                except (
                    json.JSONDecodeError, KeyError, ValueError, TypeError,
                ):
                    logger.debug(
                        "[OperationTimeline] skipped malformed row",
                    )
                    continue
                with self._lock:
                    self._rows[row.op_id] = row
                count += 1
                # Keep the monotonic counter ahead of any replayed ref
                # so newly-created rows never collide with replayed
                # ones (refs are never reused).
                num = _ref_number(row.ref)
                if num is not None and num > max_seq:
                    max_seq = num
            with self._lock:
                if max_seq > self._seq:
                    self._seq = max_seq
            return count
        except Exception:  # noqa: BLE001
            logger.debug(
                "[OperationTimeline] replay_from_disk swallowed",
                exc_info=True,
            )
            return 0


def _ref_number(ref: str) -> Optional[int]:
    """Parse the integer N out of an ``r-N`` ref. None if malformed."""
    try:
        if not ref or not ref.startswith(REF_PREFIX):
            return None
        return int(ref[len(REF_PREFIX):])
    except (TypeError, ValueError):
        return None


# ===========================================================================
# Module-level singleton (mirrors get_default_store / get_default_broker)
# ===========================================================================


_DEFAULT_TIMELINE_LOCK = threading.Lock()
_DEFAULT_TIMELINE: Optional[OperationTimeline] = None


def get_default_timeline() -> OperationTimeline:
    """Return the process-global default timeline, constructing it on
    first call. Thread-safe; idempotent."""
    global _DEFAULT_TIMELINE
    with _DEFAULT_TIMELINE_LOCK:
        if _DEFAULT_TIMELINE is None:
            _DEFAULT_TIMELINE = OperationTimeline()
        return _DEFAULT_TIMELINE


def reset_default_timeline() -> None:
    """Drop the singleton instance. Primarily for tests. NEVER raises."""
    global _DEFAULT_TIMELINE
    with _DEFAULT_TIMELINE_LOCK:
        _DEFAULT_TIMELINE = None


# ===========================================================================
# FlagRegistry self-registration (auto-discovered by §33.3 walker —
# the top-level governance package is already in _FLAG_PROVIDER_PACKAGES,
# so zero edits to flag_registry_seed.py are required)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. Returns count
    successfully registered. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    source_file = (
        "backend/core/ouroboros/governance/operation_timeline.py"
    )
    since = "PRD §42 Slice 1 (2026-05-16)"

    specs = [
        FlagSpec(
            name=OPERATION_TIMELINE_ENABLED_ENV_VAR,
            type=FlagType.BOOL,
            default=False,
            description=(
                "PRD §42 master switch (§33.1 default-FALSE): when ON, "
                "the OperationTimeline read-model records every "
                "OpsDigestObserver milestone (apply/verify/commit) as "
                "one durable causal row at "
                "JARVIS_OPERATION_TIMELINE_PATH via the canonical "
                "cross_process_jsonl.flock_append_line primitive. OFF "
                "⇒ every observer callback is a hard no-op (zero rows, "
                "zero disk I/O, zero behavior change). The read-model "
                "has zero authority over the loop."
            ),
            category=Category.OBSERVABILITY,
            source_file=source_file,
            example="false",
            since=since,
        ),
        FlagSpec(
            name=OPERATION_TIMELINE_PATH_ENV_VAR,
            type=FlagType.STR,
            default=_DEFAULT_TIMELINE_PATH,
            description=(
                "Durable causal-index JSONL path for the PRD §42 "
                "Operation Timeline. Parent directory auto-created on "
                "first append. Appended via cross_process_jsonl."
                "flock_append_line — safe across concurrent battle-test "
                "processes. Append-only audit (re-applied/reverted ops "
                "add rows, never mutate); the in-memory projection "
                f"collapses to latest-write-wins per op_id. Default "
                f"{_DEFAULT_TIMELINE_PATH!r}."
            ),
            category=Category.OBSERVABILITY,
            source_file=source_file,
            example=_DEFAULT_TIMELINE_PATH,
            since=since,
        ),
        FlagSpec(
            name=OPERATION_TIMELINE_MAX_ROWS_ENV_VAR,
            type=FlagType.INT,
            default=_DEFAULT_MAX_ROWS,
            description=(
                "Bounded tail-scan cap for OperationTimeline."
                "replay_from_disk: only the most recent N JSONL lines "
                "are loaded into the in-memory projection on a fresh "
                "boot (latest-write-wins collapses earlier duplicates "
                "anyway). Read at call time so tests can monkeypatch "
                "it — identical discipline to the SWE-Bench-Pro "
                f"_LOCAL_JSONL_MAX_ROWS precedent. Default "
                f"{_DEFAULT_MAX_ROWS}; clamped to "
                f"[{_MAX_ROWS_FLOOR}, {_MAX_ROWS_CEIL}]."
            ),
            category=Category.CAPACITY,
            source_file=source_file,
            example=str(_DEFAULT_MAX_ROWS),
            since=since,
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[OperationTimeline] flag registration failed for %s",
                getattr(spec, "name", "?"), exc_info=True,
            )
    return count


__all__ = [
    "OPERATION_TIMELINE_ENABLED_ENV_VAR",
    "OPERATION_TIMELINE_MAX_ROWS_ENV_VAR",
    "OPERATION_TIMELINE_PATH_ENV_VAR",
    "OperationTimeline",
    "REF_PREFIX",
    "TIMELINE_SCHEMA_VERSION",
    "TimelineRow",
    "get_default_timeline",
    "register_flags",
    "reset_default_timeline",
]
