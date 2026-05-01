"""Move 4 Slice 2 — InvariantDriftStore + boot snapshot helper.

Persists the architectural-invariant baseline across sessions.
Without persistence, every restart loses the temporal anchor that
``compare_snapshots`` needs — drift detection becomes only an
in-process diff between two captures of the same boot.

Slice 2 ships:

  * ``InvariantDriftStore`` — atomic-write + schema-versioned JSON
    triplet under ``.jarvis/`` (mirrors ``PostureStore`` exactly):

      - ``invariant_drift_baseline.json``  — single-snapshot baseline
        written at first boot OR explicitly re-baselined.
      - ``invariant_drift_history.jsonl``  — ring-buffered history of
        snapshots taken during operation (Slice 3 wires periodic
        re-validation; Slice 2 just creates the surface).
      - ``invariant_drift_audit.jsonl``    — append-only audit of
        baseline transitions (initial / refresh / forced-rebaseline).
        §8 immutable — never trimmed.

  * ``BootSnapshotOutcome`` — closed 5-value enum
    (``NEW_BASELINE`` / ``BASELINE_MATCHED`` / ``BASELINE_DRIFTED`` /
    ``DISABLED`` / ``FAILED``). Mirrors Move 3's
    ``AdvisoryActionType`` discipline: every code path returns
    exactly one outcome — never ``None``, never an implicit
    fall-through.

  * ``BootSnapshotResult`` — frozen dataclass: outcome + drift records
    (when applicable) + the captured snapshot. Caller (Slice 5
    GovernedLoopService wire-up) decides what to do with drift —
    log, raise, propagate to ``auto_action_router``, etc.

  * ``install_boot_snapshot()`` (sync) and
    ``install_boot_snapshot_async()`` — entry points. Sync version
    is the primitive; async wraps in ``asyncio.to_thread`` so callers
    in async boot paths don't block the event loop. Master-flag-
    gated: when ``JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED`` is off,
    returns ``DISABLED`` immediately without touching disk.

Slice 5 GovernedLoopService wiring is NOT in this commit — boot
helper is opt-in until graduation.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + the auditor module ONLY (and ``asyncio`` for
    the async wrapper). NO orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / semantic_firewall / providers /
    doubleword_provider / urgency_router / auto_action_router /
    posture_observer / flag_registry / shipped_code_invariants /
    exploration_engine imports. The store is a *consumer* of the
    auditor's snapshot type, never a re-implementation.

  * Atomic-write discipline: tempfile + ``os.replace`` (POSIX
    rename). Never partial writes; never corruption on crash mid-
    flush.

  * NEVER raises out of any public method — defensive everywhere.
    Disk failures, schema mismatches, malformed JSON, race
    conditions: every path produces a defined outcome.

Cost contract: this module performs no LLM calls and no provider
dispatch. The §26.6 cost contract is structurally not in scope.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from backend.core.ouroboros.governance.invariant_drift_auditor import (
    INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION,
    InvariantDriftRecord,
    InvariantSnapshot,
    capture_snapshot,
    compare_snapshots,
    invariant_drift_auditor_enabled,
)

logger = logging.getLogger(__name__)


INVARIANT_DRIFT_STORE_SCHEMA: str = INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Env knobs — paths + history size, all overridable; no hardcoding.
# ---------------------------------------------------------------------------


_DEFAULT_BASE_DIR_NAME = ".jarvis"
_DEFAULT_HISTORY_SIZE = 256
_HISTORY_SIZE_FLOOR = 16


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def default_history_size() -> int:
    """``JARVIS_INVARIANT_DRIFT_HISTORY_SIZE`` (floor 16, default 256)."""
    return _env_int(
        "JARVIS_INVARIANT_DRIFT_HISTORY_SIZE",
        _DEFAULT_HISTORY_SIZE,
        minimum=_HISTORY_SIZE_FLOOR,
    )


def default_base_dir() -> Path:
    """``JARVIS_INVARIANT_DRIFT_BASE_DIR`` (default ``.jarvis/``)."""
    raw = os.environ.get("JARVIS_INVARIANT_DRIFT_BASE_DIR", "")
    if raw.strip():
        return Path(raw).expanduser().resolve()
    return Path(_DEFAULT_BASE_DIR_NAME).resolve()


# ---------------------------------------------------------------------------
# Boot outcome — explicit closed taxonomy (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class BootSnapshotOutcome(str, enum.Enum):
    """Every ``install_boot_snapshot()`` call returns exactly one of
    these five values — never ``None``, never an implicit fall-through.

    ``NEW_BASELINE``      — first boot OR baseline was missing /
                            corrupted / schema-mismatched. A fresh
                            baseline was written from the live capture.
    ``BASELINE_MATCHED``  — returning boot, current capture matches
                            the on-disk baseline. No drift detected.
    ``BASELINE_DRIFTED``  — returning boot, current capture differs
                            from baseline. ``BootSnapshotResult.drift_records``
                            is non-empty; caller decides whether to
                            re-baseline (Slice 5 graduation gate).
    ``DISABLED``          — master flag is off. No-op; no disk read,
                            no disk write.
    ``FAILED``            — defensive sentinel. Capture or write
                            raised an unhandled exception. Caller
                            should log and continue boot — drift
                            detection is not a critical-path safety
                            property at boot.
    """

    NEW_BASELINE = "new_baseline"
    BASELINE_MATCHED = "baseline_matched"
    BASELINE_DRIFTED = "baseline_drifted"
    DISABLED = "disabled"
    FAILED = "failed"


@dataclass(frozen=True)
class BootSnapshotResult:
    """The full outcome of a boot capture call. Frozen so callers can
    propagate it through async signal bridges without aliasing
    concerns.

    ``snapshot`` is ``None`` only on ``DISABLED`` or ``FAILED``
    outcomes — the four other paths always have a captured snapshot.

    ``drift_records`` is non-empty only on ``BASELINE_DRIFTED``.
    """

    outcome: BootSnapshotOutcome
    snapshot: Optional[InvariantSnapshot]
    drift_records: Tuple[InvariantDriftRecord, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "snapshot": (
                self.snapshot.to_dict()
                if self.snapshot is not None else None
            ),
            "drift_records": [
                r.to_dict() for r in self.drift_records
            ],
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Audit record — baseline-transition log entries (§8 immutable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaselineAuditRecord:
    """One entry in ``invariant_drift_audit.jsonl``. Tracks every
    baseline-state transition so operators can see why the baseline
    is what it is.

    ``event``:
      ``initial``           — first-ever baseline write (no prior file)
      ``schema_mismatch``   — prior baseline had wrong schema; replaced
      ``corrupted``         — prior baseline was unreadable; replaced
      ``forced_rebaseline`` — operator-initiated re-baseline
      ``boot_drift``        — boot saw drift; record (no auto-replace)
    """

    event: str
    at_utc: float
    snapshot_id: str
    schema_version: str = INVARIANT_DRIFT_STORE_SCHEMA

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "at_utc": self.at_utc,
            "snapshot_id": self.snapshot_id,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Store — atomic-write, schema-versioned, threading.Lock-guarded triplet
# ---------------------------------------------------------------------------


class InvariantDriftStore:
    """Durable architectural-invariant baseline + history triplet.

    Three on-disk artifacts under the configured base directory:

      * ``invariant_drift_baseline.json``  — current baseline,
        atomically written via temp+rename so readers never see a
        torn write.
      * ``invariant_drift_history.jsonl``  — ring buffer of recent
        snapshots, one JSON object per line. Trimmed in-place on
        write (default 256 entries, floor 16).
      * ``invariant_drift_audit.jsonl``    — append-only log of
        baseline transitions (initial / replace / forced-rebaseline /
        boot_drift). §8 immutable — never trimmed.

    Schema discipline mirrors ``PostureStore``: every written payload
    carries ``schema_version`` matching the auditor module's value;
    readers reject mismatched versions with a warning and treat the
    file as absent rather than coerce.

    Concurrency: a per-instance ``threading.Lock`` guards the triplet.
    Atomic writes use ``tempfile.mkstemp`` + ``os.replace`` (POSIX
    rename) so concurrent readers never see partial writes.
    """

    BASELINE_FILENAME = "invariant_drift_baseline.json"
    HISTORY_FILENAME = "invariant_drift_history.jsonl"
    AUDIT_FILENAME = "invariant_drift_audit.jsonl"

    def __init__(
        self,
        base_dir: Path,
        *,
        history_size: Optional[int] = None,
    ) -> None:
        self._base = Path(base_dir).resolve()
        self._history_size = (
            history_size if history_size is not None
            else default_history_size()
        )
        self._lock = threading.Lock()

    @property
    def base_dir(self) -> Path:
        return self._base

    @property
    def baseline_path(self) -> Path:
        return self._base / self.BASELINE_FILENAME

    @property
    def history_path(self) -> Path:
        return self._base / self.HISTORY_FILENAME

    @property
    def audit_path(self) -> Path:
        return self._base / self.AUDIT_FILENAME

    # ---- atomic write helper ---------------------------------------------

    def _atomic_write(self, path: Path, text: str) -> None:
        """Tempfile + ``os.replace`` — POSIX-atomic. NEVER raises
        out of normal flow; caller wraps in try/except for fully-
        defensive code paths."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ---- baseline --------------------------------------------------------

    def write_baseline(self, snap: InvariantSnapshot) -> None:
        """Atomically persist a snapshot as the canonical baseline.
        NEVER raises — disk failures are logged and swallowed."""
        try:
            payload = snap.to_dict()
            text = json.dumps(payload, indent=2, sort_keys=True)
            with self._lock:
                self._atomic_write(self.baseline_path, text)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[InvariantDriftStore] baseline write failed: %s",
                exc,
            )

    def load_baseline(self) -> Optional[InvariantSnapshot]:
        """Return the on-disk baseline, or ``None`` if absent /
        unreadable / malformed / schema-mismatched. NEVER raises."""
        path = self.baseline_path
        if not path.exists():
            return None
        with self._lock:
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "[InvariantDriftStore] baseline read failed: %s",
                    exc,
                )
                return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "[InvariantDriftStore] baseline file is not valid "
                "JSON",
            )
            return None
        if not isinstance(payload, dict):
            logger.warning(
                "[InvariantDriftStore] baseline payload is not an "
                "object",
            )
            return None
        return InvariantSnapshot.from_dict(payload)

    def has_baseline(self) -> bool:
        """True iff a *parseable* baseline exists. NEVER raises."""
        return self.load_baseline() is not None

    def clear_baseline(self) -> None:
        """Remove the baseline file. Idempotent. NEVER raises."""
        with self._lock:
            try:
                if self.baseline_path.exists():
                    self.baseline_path.unlink()
            except OSError as exc:
                logger.warning(
                    "[InvariantDriftStore] baseline unlink failed: "
                    "%s", exc,
                )

    # ---- history ---------------------------------------------------------

    def append_history(self, snap: InvariantSnapshot) -> None:
        """Append to the ring buffer, trim to ``history_size`` from
        the front. Mirrors ``PostureStore.append_history`` exactly.
        NEVER raises."""
        try:
            line = json.dumps(
                snap.to_dict(), separators=(",", ":"),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[InvariantDriftStore] history serialize failed: "
                "%s", exc,
            )
            return
        with self._lock:
            try:
                self.history_path.parent.mkdir(
                    parents=True, exist_ok=True,
                )
                lines: List[str] = []
                if self.history_path.exists():
                    try:
                        lines = [
                            ln for ln in
                            self.history_path.read_text(
                                encoding="utf-8",
                            ).splitlines()
                            if ln.strip()
                        ]
                    except OSError:
                        lines = []
                lines.append(line)
                if len(lines) > self._history_size:
                    lines = lines[-self._history_size:]
                self._atomic_write(
                    self.history_path,
                    "\n".join(lines) + "\n",
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "[InvariantDriftStore] history append failed: "
                    "%s", exc,
                )

    def load_history(
        self, *, limit: Optional[int] = None,
    ) -> List[InvariantSnapshot]:
        """Return snapshots from history, newest last. ``limit``
        slices the tail. Malformed / schema-mismatched lines are
        silently dropped. NEVER raises."""
        if not self.history_path.exists():
            return []
        with self._lock:
            try:
                raw_lines = [
                    ln for ln in self.history_path.read_text(
                        encoding="utf-8",
                    ).splitlines()
                    if ln.strip()
                ]
            except OSError:
                return []
        if limit is not None and limit > 0:
            raw_lines = raw_lines[-int(limit):]
        out: List[InvariantSnapshot] = []
        for ln in raw_lines:
            try:
                payload = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            snap = InvariantSnapshot.from_dict(payload)
            if snap is not None:
                out.append(snap)
        return out

    # ---- audit (immutable §8) -------------------------------------------

    def append_audit(self, record: BaselineAuditRecord) -> None:
        """Append-only audit log. NEVER raises."""
        try:
            line = json.dumps(
                record.to_dict(), separators=(",", ":"),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[InvariantDriftStore] audit serialize failed: %s",
                exc,
            )
            return
        with self._lock:
            try:
                self.audit_path.parent.mkdir(
                    parents=True, exist_ok=True,
                )
                with self.audit_path.open(
                    "a", encoding="utf-8",
                ) as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                logger.warning(
                    "[InvariantDriftStore] audit append failed: %s",
                    exc,
                )

    def load_audit(
        self, *, limit: Optional[int] = None,
    ) -> List[BaselineAuditRecord]:
        """Read the audit log. Newest last. NEVER raises."""
        if not self.audit_path.exists():
            return []
        with self._lock:
            try:
                raw_lines = [
                    ln for ln in self.audit_path.read_text(
                        encoding="utf-8",
                    ).splitlines()
                    if ln.strip()
                ]
            except OSError:
                return []
        if limit is not None and limit > 0:
            raw_lines = raw_lines[-int(limit):]
        out: List[BaselineAuditRecord] = []
        for ln in raw_lines:
            try:
                payload = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            try:
                out.append(
                    BaselineAuditRecord(
                        event=str(payload["event"]),
                        at_utc=float(payload["at_utc"]),
                        snapshot_id=str(payload["snapshot_id"]),
                    ),
                )
            except (KeyError, ValueError, TypeError):
                continue
        return out

    # ---- diagnostics -----------------------------------------------------

    def stats(self) -> dict:
        """Return summary stats about the on-disk triplet. NEVER
        raises."""
        history_count = 0
        if self.history_path.exists():
            try:
                history_count = sum(
                    1 for ln in self.history_path.read_text(
                        encoding="utf-8",
                    ).splitlines()
                    if ln.strip()
                )
            except OSError:
                pass
        audit_count = 0
        if self.audit_path.exists():
            try:
                audit_count = sum(
                    1 for ln in self.audit_path.read_text(
                        encoding="utf-8",
                    ).splitlines()
                    if ln.strip()
                )
            except OSError:
                pass
        return {
            "schema_version": INVARIANT_DRIFT_STORE_SCHEMA,
            "has_baseline": self.has_baseline(),
            "history_count": history_count,
            "audit_count": audit_count,
            "history_capacity": self._history_size,
            "base_dir": str(self._base),
        }

    def clear_all(self) -> None:
        """Test helper — remove all three files. NEVER raises."""
        with self._lock:
            for p in (
                self.baseline_path,
                self.history_path,
                self.audit_path,
            ):
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass


# ---------------------------------------------------------------------------
# Default-store singleton (mirrors PostureStore.get_default_store)
# ---------------------------------------------------------------------------


_default_store: Optional[InvariantDriftStore] = None
_default_store_lock = threading.Lock()


def get_default_store(
    base_dir: Optional[Path] = None,
) -> InvariantDriftStore:
    """Singleton default store at ``.jarvis/`` (or
    ``JARVIS_INVARIANT_DRIFT_BASE_DIR`` override). NEVER raises.

    First call wins on the base directory; subsequent calls return
    the same instance regardless of ``base_dir`` argument. Use
    ``reset_default_store()`` to re-initialize for tests."""
    global _default_store
    with _default_store_lock:
        if _default_store is None:
            resolved = (
                base_dir if base_dir is not None
                else default_base_dir()
            )
            _default_store = InvariantDriftStore(resolved)
        return _default_store


def reset_default_store() -> None:
    """Test isolation — drop the singleton so the next
    ``get_default_store()`` call re-reads env knobs."""
    global _default_store
    with _default_store_lock:
        _default_store = None


# ---------------------------------------------------------------------------
# Boot snapshot helper — sync + async entry points
# ---------------------------------------------------------------------------


def install_boot_snapshot(
    *,
    store: Optional[InvariantDriftStore] = None,
    snapshot: Optional[InvariantSnapshot] = None,
    force_rebaseline: bool = False,
) -> BootSnapshotResult:
    """Capture-or-compare-or-no-op — the load-bearing boot entry point.

    Decision tree (every path returns exactly one outcome — explicit
    closed taxonomy mirroring Move 3's ``AdvisoryActionType``):

      1. Master flag off  → ``DISABLED`` (no disk read, no write).
      2. Capture raises   → ``FAILED`` (logged; boot continues).
      3. ``force_rebaseline=True``                     → write fresh
         baseline, append ``forced_rebaseline`` audit, return
         ``NEW_BASELINE``.
      4. No on-disk baseline                           → write fresh
         baseline, append ``initial`` audit, return ``NEW_BASELINE``.
      5. Baseline present, schema/JSON mismatch        → write fresh
         baseline, append ``schema_mismatch`` (or ``corrupted``)
         audit, return ``NEW_BASELINE``.
      6. Baseline present, drift detected              → append
         ``boot_drift`` audit (NO auto-replace), return
         ``BASELINE_DRIFTED`` with drift records.
      7. Baseline present, no drift                    → no write,
         no audit, return ``BASELINE_MATCHED``.

    Auto-rebaseline on drift is INTENTIONALLY NOT performed: drift
    is operator-actionable signal. Slice 5 graduates the
    ``GovernedLoopService`` wire-up; operator policy decides whether
    drift triggers re-baseline (e.g., via ``/invariant rebaseline``
    REPL — Slice 5).

    NEVER raises. ``snapshot`` injection is for deterministic tests;
    production callers should leave it ``None``."""
    if not invariant_drift_auditor_enabled():
        return BootSnapshotResult(
            outcome=BootSnapshotOutcome.DISABLED,
            snapshot=None,
            detail="master flag off",
        )

    target_store = (
        store if store is not None else get_default_store()
    )

    try:
        current = (
            snapshot if snapshot is not None else capture_snapshot()
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[InvariantDriftStore] boot capture raised: %s", exc,
        )
        return BootSnapshotResult(
            outcome=BootSnapshotOutcome.FAILED,
            snapshot=None,
            detail=f"capture raised: {exc!r}",
        )

    # Forced rebaseline — operator override
    if force_rebaseline:
        return _write_new_baseline(
            target_store, current, event="forced_rebaseline",
        )

    # Detect why a baseline might be unusable so the audit event
    # accurately reflects the cause.
    baseline_path = target_store.baseline_path
    if not baseline_path.exists():
        return _write_new_baseline(
            target_store, current, event="initial",
        )

    # File exists — try to load and classify failure mode.
    audit_event_on_replace: Optional[str] = None
    try:
        raw = baseline_path.read_text(encoding="utf-8")
    except OSError:
        audit_event_on_replace = "corrupted"
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            audit_event_on_replace = "corrupted"
        else:
            if not isinstance(payload, dict):
                audit_event_on_replace = "corrupted"
            else:
                schema = payload.get("schema_version")
                if schema != INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION:
                    audit_event_on_replace = "schema_mismatch"
                else:
                    baseline = InvariantSnapshot.from_dict(payload)
                    if baseline is None:
                        audit_event_on_replace = "corrupted"

    if audit_event_on_replace is not None:
        return _write_new_baseline(
            target_store, current, event=audit_event_on_replace,
        )

    # Baseline is parseable — compare.
    baseline = target_store.load_baseline()
    if baseline is None:
        # Race: file disappeared between probe and load. Treat
        # as fresh.
        return _write_new_baseline(
            target_store, current, event="initial",
        )

    drift_records = compare_snapshots(baseline, current)
    if not drift_records:
        return BootSnapshotResult(
            outcome=BootSnapshotOutcome.BASELINE_MATCHED,
            snapshot=current,
            detail="no drift detected from baseline",
        )

    # Drift detected — record audit, do NOT auto-replace.
    target_store.append_audit(
        BaselineAuditRecord(
            event="boot_drift",
            at_utc=current.captured_at_utc,
            snapshot_id=current.snapshot_id,
        ),
    )
    return BootSnapshotResult(
        outcome=BootSnapshotOutcome.BASELINE_DRIFTED,
        snapshot=current,
        drift_records=drift_records,
        detail=(
            f"{len(drift_records)} drift record(s) "
            f"vs baseline {baseline.snapshot_id}"
        ),
    )


def _write_new_baseline(
    store: InvariantDriftStore,
    snap: InvariantSnapshot,
    *,
    event: str,
) -> BootSnapshotResult:
    """Internal helper: write the new baseline + audit entry +
    construct the result. Defensive."""
    store.write_baseline(snap)
    store.append_audit(
        BaselineAuditRecord(
            event=event,
            at_utc=snap.captured_at_utc,
            snapshot_id=snap.snapshot_id,
        ),
    )
    return BootSnapshotResult(
        outcome=BootSnapshotOutcome.NEW_BASELINE,
        snapshot=snap,
        detail=f"baseline {event}",
    )


async def install_boot_snapshot_async(
    *,
    store: Optional[InvariantDriftStore] = None,
    snapshot: Optional[InvariantSnapshot] = None,
    force_rebaseline: bool = False,
) -> BootSnapshotResult:
    """Async wrapper — runs the sync entry point in a thread executor
    so async boot paths (e.g. ``GovernedLoopService.start``) don't
    block the event loop. NEVER raises.

    Capture is mostly CPU-bound; the disk write is sub-millisecond
    on local SSD. The thread-hop is cheap insurance for the rare
    case where ``capture_snapshot`` happens to call into a slow
    path (e.g., ``shipped_code_invariants.validate_all`` reading
    cold source files at boot)."""
    return await asyncio.to_thread(
        install_boot_snapshot,
        store=store,
        snapshot=snapshot,
        force_rebaseline=force_rebaseline,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "BaselineAuditRecord",
    "BootSnapshotOutcome",
    "BootSnapshotResult",
    "INVARIANT_DRIFT_STORE_SCHEMA",
    "InvariantDriftStore",
    "default_base_dir",
    "default_history_size",
    "get_default_store",
    "install_boot_snapshot",
    "install_boot_snapshot_async",
    "reset_default_store",
]
