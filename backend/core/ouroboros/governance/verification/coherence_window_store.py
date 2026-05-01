"""Priority #1 Slice 2 — Cross-process flock'd window store.

Persistent storage for the Coherence Auditor's two timelines:

  1. ``.jarvis/coherence_window.jsonl`` — bounded ring buffer of
     ``BehavioralSignature`` records. Rotates at cap so the window
     never grows unbounded. Read-trim-atomic-write pattern wrapped
     in cross-process flock (Tier 1 #3) so concurrent processes
     cannot race the ring-buffer mutation.
  2. ``.jarvis/coherence_audit.jsonl`` — APPEND-ONLY drift verdict
     audit log per §8 invariant (immutable audit chain). Uses
     ``flock_append_line`` directly — no rotation, no read-modify-
     write, structurally append-only.

The signature ring buffer is the *measurement instrument* Slice 3's
observer reads from to compute drift; the audit log is the
*operator-visible record* of what the auditor decided.

Direct-solve principles:

  * **Asynchronous-ready** — All public functions are sync but
    short-running (one read + one write per call). Slice 3's
    async observer wraps these via ``asyncio.to_thread`` (mirrors
    Move 5 Slice 3 pattern). Frozen dataclasses propagate cleanly.

  * **Dynamic** — Window length, max signatures, base directory
    all env-tunable with floor+ceiling clamps. NO hardcoded paths
    or sizes.

  * **Adaptive** — Schema-mismatched lines silently dropped on
    read (forward-compat for ``coherence_auditor.2``). Empty
    window file → empty tuple, NOT a raise. Missing file → empty
    tuple. Corrupt single line in middle of file → that line
    skipped, others returned.

  * **Intelligent** — Time-bounded reads use ``window_end_ts``
    field (not file mtime) so window-hours bound is content-
    accurate even if files were written out-of-order. Drift audit
    `since_ts` filtering happens during read so Slice 3 can pass a
    cheap floor without buffering the entire audit log.

  * **Robust** — Every public function is total. NEVER raises.
    Disk failures (ENOSPC, EACCES, FS unavailable) all map to
    ``WindowOutcome.FAILED`` or empty-tuple read. Cross-process
    lock acquire failure does NOT corrupt state — falls through
    to in-process serialization (single-process correctness
    preserved).

  * **No hardcoding** — base dir resolved via env knob;
    `_atomic_write` uses ``tempfile.mkstemp`` + ``os.replace``
    (POSIX-atomic, no string-mangling); `flock_append_line` is the
    sole writer for the audit log (no parallel implementation).

Authority invariants (AST-pinned by Slice 5 graduation):

  * Imports stdlib + Tier 1 #3 (``cross_process_jsonl``) +
    Slice 1 (``coherence_auditor``) ONLY.
  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.
  * MUST reference ``flock_append_line`` symbol from
    ``cross_process_jsonl`` (catches refactor that drops cross-
    process safety — pinned in Slice 5).
  * No mutation tools.
  * No exec/eval/compile.
  * No async functions (Slice 3 introduces async via
    ``asyncio.to_thread`` wrappers).
"""
from __future__ import annotations

import enum
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
    flock_critical_section,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (
    COHERENCE_AUDITOR_SCHEMA_VERSION,
    BehavioralDriftFinding,
    BehavioralDriftKind,
    BehavioralDriftVerdict,
    BehavioralSignature,
    CoherenceOutcome,
    DriftSeverity,
)

logger = logging.getLogger(__name__)


COHERENCE_WINDOW_STORE_SCHEMA_VERSION: str = (
    "coherence_window_store.1"
)


# ---------------------------------------------------------------------------
# Path resolution — env-tunable, NO hardcoded paths
# ---------------------------------------------------------------------------


_DEFAULT_BASE_DIR_NAME: str = ".jarvis"
_WINDOW_FILENAME: str = "coherence_window.jsonl"
_AUDIT_FILENAME: str = "coherence_audit.jsonl"


def coherence_base_dir() -> Path:
    """``JARVIS_COHERENCE_BASE_DIR`` (default ``.jarvis/``).

    Mirrors ``invariant_drift_store.default_base_dir`` pattern.
    Empty/whitespace = unset = fall through to default."""
    raw = os.environ.get("JARVIS_COHERENCE_BASE_DIR", "")
    if raw.strip():
        return Path(raw).expanduser().resolve()
    return Path(_DEFAULT_BASE_DIR_NAME).resolve()


def coherence_window_path() -> Path:
    """Resolved path to the bounded ring buffer JSONL."""
    return coherence_base_dir() / _WINDOW_FILENAME


def coherence_audit_path() -> Path:
    """Resolved path to the append-only audit log JSONL."""
    return coherence_base_dir() / _AUDIT_FILENAME


# ---------------------------------------------------------------------------
# Cap structure — every numeric env-tunable with floor+ceiling
# ---------------------------------------------------------------------------


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    """Read env var as int; clamp to [floor, ceiling]; fall back
    to default on missing / garbage. NEVER raises."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


def window_hours_default() -> int:
    """``JARVIS_COHERENCE_WINDOW_HOURS`` (default 168 = 7 days,
    floor 24, ceiling 720)."""
    return _env_int_clamped(
        "JARVIS_COHERENCE_WINDOW_HOURS",
        168, floor=24, ceiling=720,
    )


def max_signatures_default() -> int:
    """``JARVIS_COHERENCE_MAX_SIGNATURES`` (default 200, floor 10,
    ceiling 5000). Cap for the bounded ring buffer — when the
    window file would exceed this count, the oldest entries are
    evicted to maintain bounded growth."""
    return _env_int_clamped(
        "JARVIS_COHERENCE_MAX_SIGNATURES",
        200, floor=10, ceiling=5000,
    )


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of window-store outcomes (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class WindowOutcome(str, enum.Enum):
    """5-value closed taxonomy. Every public store call returns
    exactly one — never None, never implicit fall-through.

    ``RECORDED``       — append succeeded, no rotation needed.
    ``WINDOW_ROTATED`` — append succeeded AND oldest entries were
                         evicted to maintain MAX_SIGNATURES cap.
    ``READ_OK``        — read returned a populated window.
    ``READ_EMPTY``     — read returned empty (file missing /
                         empty / all entries outside time window).
    ``FAILED``         — defensive sentinel: serialize error,
                         lock acquire failed AND in-process write
                         also failed, etc."""

    RECORDED = "recorded"
    WINDOW_ROTATED = "window_rotated"
    READ_OK = "read_ok"
    READ_EMPTY = "read_empty"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Result containers — frozen for safe propagation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowReadResult:
    """Result of a ``read_window`` call. Frozen for safe
    propagation. Empty tuple is the canonical empty-window
    representation."""

    outcome: WindowOutcome
    signatures: Tuple[BehavioralSignature, ...] = tuple()
    detail: str = ""
    schema_version: str = COHERENCE_WINDOW_STORE_SCHEMA_VERSION


@dataclass(frozen=True)
class AuditReadResult:
    """Result of a ``read_drift_audit`` call. Frozen."""

    outcome: WindowOutcome
    verdicts: Tuple[BehavioralDriftVerdict, ...] = tuple()
    detail: str = ""
    schema_version: str = COHERENCE_WINDOW_STORE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Internal: in-process lock registry (mirrors invariant_drift_store)
# ---------------------------------------------------------------------------


_INPROCESS_LOCK = threading.RLock()


def _atomic_write(path: Path, text: str) -> None:
    """Tempfile + ``os.replace`` — POSIX-atomic. Raises on disk
    failure; callers wrap in try/except. Mirrors Move 4
    InvariantDriftStore's ``_atomic_write``."""
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


def _read_jsonl_lines(path: Path) -> List[str]:
    """Read non-empty lines from a JSONL file. Returns empty list
    on missing file / read error. NEVER raises."""
    try:
        if not path.exists():
            return []
        return [
            ln for ln in path.read_text(
                encoding="utf-8", errors="replace",
            ).splitlines()
            if ln.strip()
        ]
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[CoherenceWindowStore] read failed %s: %s",
            path, exc,
        )
        return []


# ---------------------------------------------------------------------------
# Internal: drift verdict serialization
# ---------------------------------------------------------------------------


def _verdict_from_dict(
    payload: Dict[str, Any],
) -> Optional[BehavioralDriftVerdict]:
    """Schema-tolerant reconstruction of a BehavioralDriftVerdict.
    Returns ``None`` on schema mismatch. NEVER raises."""
    try:
        if not isinstance(payload, dict):
            return None
        if (
            payload.get("schema_version")
            != COHERENCE_AUDITOR_SCHEMA_VERSION
        ):
            return None
        outcome_raw = payload.get("outcome")
        try:
            outcome = CoherenceOutcome(outcome_raw)
        except ValueError:
            return None
        sev_raw = payload.get("largest_severity", "none")
        try:
            severity = DriftSeverity(sev_raw)
        except ValueError:
            severity = DriftSeverity.NONE
        findings_raw = payload.get("findings") or []
        findings: List[BehavioralDriftFinding] = []
        for f_raw in findings_raw:
            if not isinstance(f_raw, dict):
                continue
            try:
                kind = BehavioralDriftKind(f_raw["kind"])
                f_sev = DriftSeverity(
                    f_raw.get("severity", "none"),
                )
                findings.append(BehavioralDriftFinding(
                    kind=kind,
                    severity=f_sev,
                    detail=str(f_raw.get("detail", "")),
                    delta_metric=float(
                        f_raw.get("delta_metric", 0.0),
                    ),
                    budget_metric=float(
                        f_raw.get("budget_metric", 0.0),
                    ),
                    prev_signature_id=f_raw.get(
                        "prev_signature_id",
                    ),
                    curr_signature_id=f_raw.get(
                        "curr_signature_id",
                    ),
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return BehavioralDriftVerdict(
            outcome=outcome,
            findings=tuple(findings),
            largest_severity=severity,
            drift_signature=str(
                payload.get("drift_signature", ""),
            ),
            detail=str(payload.get("detail", "")),
        )
    except Exception:  # noqa: BLE001 — defensive
        return None


# ---------------------------------------------------------------------------
# Public: record_signature
# ---------------------------------------------------------------------------


def record_signature(
    sig: BehavioralSignature,
    *,
    base_dir: Optional[Path] = None,
    max_signatures: Optional[int] = None,
) -> WindowOutcome:
    """Append ``sig`` to the bounded ring buffer. Trims oldest
    entries when count exceeds ``max_signatures``. Cross-process
    safe via ``flock_critical_section``. NEVER raises.

    Returns:
      * ``RECORDED``       — append succeeded, no rotation
      * ``WINDOW_ROTATED`` — append succeeded AND oldest evicted
      * ``FAILED``         — serialize error or unrecoverable
                             disk failure"""
    try:
        if not isinstance(sig, BehavioralSignature):
            return WindowOutcome.FAILED

        path = (
            (Path(base_dir) / _WINDOW_FILENAME)
            if base_dir is not None
            else coherence_window_path()
        )
        cap = (
            int(max_signatures) if max_signatures is not None
            else max_signatures_default()
        )

        try:
            line = json.dumps(
                sig.to_dict(), separators=(",", ":"),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[CoherenceWindowStore] signature serialize "
                "failed: %s", exc,
            )
            return WindowOutcome.FAILED

        with _INPROCESS_LOCK:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning(
                    "[CoherenceWindowStore] mkdir failed: %s", exc,
                )
                return WindowOutcome.FAILED

            rotated = False
            with flock_critical_section(path) as acquired:
                _ = acquired  # in-process RLock above already
                              # serializes within process; flock
                              # adds cross-process safety
                try:
                    existing = _read_jsonl_lines(path)
                    existing.append(line)
                    if len(existing) > cap:
                        existing = existing[-cap:]
                        rotated = True
                    _atomic_write(
                        path, "\n".join(existing) + "\n",
                    )
                except Exception as exc:  # noqa: BLE001 — defensive
                    logger.warning(
                        "[CoherenceWindowStore] append failed: "
                        "%s", exc,
                    )
                    return WindowOutcome.FAILED

            return (
                WindowOutcome.WINDOW_ROTATED if rotated
                else WindowOutcome.RECORDED
            )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[CoherenceWindowStore] record_signature raised: %s",
            exc,
        )
        return WindowOutcome.FAILED


# ---------------------------------------------------------------------------
# Public: read_window
# ---------------------------------------------------------------------------


def read_window(
    *,
    window_hours: Optional[int] = None,
    base_dir: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> WindowReadResult:
    """Read all signatures within the last ``window_hours``.
    Filters by signature's ``window_end_ts`` (not file mtime —
    content-accurate). Schema-mismatched / malformed lines
    silently dropped. NEVER raises.

    Returns:
      * ``READ_OK``    — at least one signature in window
      * ``READ_EMPTY`` — file missing / empty / no signatures
                         within time bound
      * ``FAILED``     — defensive sentinel"""
    try:
        path = (
            (Path(base_dir) / _WINDOW_FILENAME)
            if base_dir is not None
            else coherence_window_path()
        )
        hours = (
            int(window_hours) if window_hours is not None
            else window_hours_default()
        )
        # Defensive clamp on caller-supplied window_hours
        # (defaults already clamped via window_hours_default).
        # Floor 1h prevents zero-window degenerate.
        hours = max(1, hours)

        import time as _time
        ref_ts = (
            float(now_ts) if now_ts is not None
            else _time.time()
        )
        cutoff_ts = ref_ts - (hours * 3600.0)

        lines = _read_jsonl_lines(path)
        if not lines:
            return WindowReadResult(
                outcome=WindowOutcome.READ_EMPTY,
                detail=f"file empty or missing: {path}",
            )

        sigs: List[BehavioralSignature] = []
        for ln in lines:
            try:
                payload = json.loads(ln)
            except (json.JSONDecodeError, TypeError):
                continue
            sig = BehavioralSignature.from_dict(payload)
            if sig is None:
                continue
            if sig.window_end_ts < cutoff_ts:
                continue
            sigs.append(sig)

        if not sigs:
            return WindowReadResult(
                outcome=WindowOutcome.READ_EMPTY,
                detail=(
                    f"no signatures within last {hours}h "
                    f"(cutoff_ts={cutoff_ts:.2f})"
                ),
            )

        # Sort ascending by window_end_ts so callers can rely on
        # ordering. compute_behavioral_drift expects (prev, curr)
        # in chronological order.
        sigs.sort(key=lambda s: s.window_end_ts)

        return WindowReadResult(
            outcome=WindowOutcome.READ_OK,
            signatures=tuple(sigs),
            detail=f"{len(sigs)} signatures within last {hours}h",
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[CoherenceWindowStore] read_window raised: %s", exc,
        )
        return WindowReadResult(
            outcome=WindowOutcome.FAILED,
            detail=f"read_window raised: {exc!r}",
        )


# ---------------------------------------------------------------------------
# Public: record_drift_audit
# ---------------------------------------------------------------------------


def record_drift_audit(
    verdict: BehavioralDriftVerdict,
    *,
    base_dir: Optional[Path] = None,
) -> WindowOutcome:
    """Append a drift verdict to the audit log. APPEND-ONLY,
    NEVER ROTATES — per §8 immutable audit chain invariant.
    Cross-process safe via ``flock_append_line`` (Tier 1 #3).
    NEVER raises.

    Returns:
      * ``RECORDED`` — append succeeded
      * ``FAILED``   — serialize error or unrecoverable disk
                       failure"""
    try:
        if not isinstance(verdict, BehavioralDriftVerdict):
            return WindowOutcome.FAILED

        path = (
            (Path(base_dir) / _AUDIT_FILENAME)
            if base_dir is not None
            else coherence_audit_path()
        )

        try:
            payload = verdict.to_dict()
            # Augment with append timestamp so since_ts filtering
            # has a stable monotonic key (verdict itself doesn't
            # carry one — it's caller's responsibility to record
            # ts at write time).
            import time as _time
            payload["recorded_at_ts"] = _time.time()
            line = json.dumps(payload, separators=(",", ":"))
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[CoherenceWindowStore] verdict serialize "
                "failed: %s", exc,
            )
            return WindowOutcome.FAILED

        # flock_append_line handles parent mkdir + cross-process
        # serialization atomically. Returns False on any failure.
        ok = flock_append_line(path, line)
        if not ok:
            return WindowOutcome.FAILED
        return WindowOutcome.RECORDED
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[CoherenceWindowStore] record_drift_audit raised: %s",
            exc,
        )
        return WindowOutcome.FAILED


# ---------------------------------------------------------------------------
# Public: read_drift_audit
# ---------------------------------------------------------------------------


def read_drift_audit(
    *,
    since_ts: float = 0.0,
    base_dir: Optional[Path] = None,
    limit: Optional[int] = None,
) -> AuditReadResult:
    """Read drift audit verdicts with ``recorded_at_ts >=
    since_ts``. Schema-mismatched lines silently dropped. NEVER
    raises.

    Returns:
      * ``READ_OK``    — at least one verdict in range
      * ``READ_EMPTY`` — file missing / empty / no verdicts in
                         range
      * ``FAILED``     — defensive sentinel"""
    try:
        path = (
            (Path(base_dir) / _AUDIT_FILENAME)
            if base_dir is not None
            else coherence_audit_path()
        )

        lines = _read_jsonl_lines(path)
        if not lines:
            return AuditReadResult(
                outcome=WindowOutcome.READ_EMPTY,
                detail=f"file empty or missing: {path}",
            )

        verdicts_with_ts: List[
            Tuple[float, BehavioralDriftVerdict]
        ] = []
        for ln in lines:
            try:
                payload = json.loads(ln)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            try:
                ts = float(payload.get("recorded_at_ts", 0.0))
            except (TypeError, ValueError):
                continue
            if ts < since_ts:
                continue
            v = _verdict_from_dict(payload)
            if v is None:
                continue
            verdicts_with_ts.append((ts, v))

        if not verdicts_with_ts:
            return AuditReadResult(
                outcome=WindowOutcome.READ_EMPTY,
                detail=(
                    f"no verdicts since ts={since_ts:.2f}"
                ),
            )

        # Sort ascending by recorded_at_ts (chronological).
        verdicts_with_ts.sort(key=lambda pair: pair[0])
        verdicts = [v for _ts, v in verdicts_with_ts]
        if limit is not None and limit >= 0:
            verdicts = verdicts[-limit:]

        return AuditReadResult(
            outcome=WindowOutcome.READ_OK,
            verdicts=tuple(verdicts),
            detail=(
                f"{len(verdicts)} verdicts since "
                f"ts={since_ts:.2f}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[CoherenceWindowStore] read_drift_audit raised: %s",
            exc,
        )
        return AuditReadResult(
            outcome=WindowOutcome.FAILED,
            detail=f"read_drift_audit raised: {exc!r}",
        )


# ---------------------------------------------------------------------------
# Test helper — reset in-process lock state (test-only)
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    """Reset the in-process RLock. Test-only — production never
    calls this."""
    global _INPROCESS_LOCK  # noqa: PLW0603 — test-only state
    _INPROCESS_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "AuditReadResult",
    "COHERENCE_WINDOW_STORE_SCHEMA_VERSION",
    "WindowOutcome",
    "WindowReadResult",
    "coherence_audit_path",
    "coherence_base_dir",
    "coherence_window_path",
    "max_signatures_default",
    "read_drift_audit",
    "read_window",
    "record_drift_audit",
    "record_signature",
    "window_hours_default",
]
