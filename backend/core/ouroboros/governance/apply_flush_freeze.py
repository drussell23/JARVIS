"""Atomic Flush & Freeze — post-APPLY mutation telemetry + park (2026-07-21).

The Autonomous Cascade Trap: the instant a governed APPLY lands on disk, the
host reacts — FS-watching sensors fire on the mutation, dev-server hot-reload
watchers may recycle the serving loop, and an autonomous generation loop can
begin stacking FURTHER mutations on top of an un-reviewed one. If the process
is recycled mid-emission, the outbound telemetry describing the mutation is
severed and the operator learns nothing about what changed.

This module is the deterministic countermeasure, engaged at the ChangeEngine
success seam (post-2PC-VERIFY, post-APPLIED ledger record — a write that is
provably durable, never one that may still roll back):

1. **Capture** — the mutation's ``git diff`` via an async subprocess against
   the write root (untracked new files captured via ``git diff --no-index``),
   byte-bounded.
2. **Persist** — the diff lands as a durable artifact file AND is logged in
   full (bounded) to the session ``debug.log``, so a severed socket can never
   lose the record.
3. **Flush** — the diff metadata is serialized into a high-priority
   ``actor_edge`` envelope through the EXISTING ``HiveEmitter``
   (``hive_emit`` + ``hive_flush`` — the zero-authority emission edge; DRY,
   no parallel transport) and the enqueue is DEMONSTRATED via the emitter's
   ``stats["emitted"]`` delta before the engine proceeds.
4. **Freeze** — once ``JARVIS_APPLY_FREEZE_MAX_MUTATIONS`` (default 1)
   successful mutations have flushed, the process-wide freeze latch closes:
   every subsequent ``ChangeEngine.execute`` short-circuits to
   ``POLICY_DENIED reason=apply_frozen_pending_review`` BEFORE any byte is
   staged (the ``generation_fence.is_fenced()`` idiom — same chokepoint,
   same shape). The cascade is structurally impossible; a human reviews the
   parked diff.

Master ``JARVIS_APPLY_FLUSH_FREEZE_ENABLED`` — default **false** (production
autonomy is not silently crippled); supervised validation soaks opt in at
launch. All knobs env-tunable; no hardcoding. Telemetry/persistence failures
are fail-soft but the freeze latch still closes — observability may degrade,
containment never does.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger(__name__)


_ENABLED_ENV = "JARVIS_APPLY_FLUSH_FREEZE_ENABLED"
_MAX_OPS_ENV = "JARVIS_APPLY_FREEZE_MAX_OPS"
_MAX_FILES_PER_OP_ENV = "JARVIS_APPLY_FREEZE_MAX_FILES_PER_OP"
_DIFF_MAX_BYTES_ENV = "JARVIS_APPLY_FREEZE_DIFF_MAX_BYTES"
_ARTIFACT_DIR_ENV = "JARVIS_APPLY_FREEZE_ARTIFACT_DIR"
_GIT_TIMEOUT_ENV = "JARVIS_APPLY_FREEZE_GIT_TIMEOUT_S"

#: The ChangeEngine denial markers (grep-discoverable, mirror the
#: generation_fence / mutation_budget POLICY_DENIED taxonomy).
FROZEN_DENIAL_REASON = "POLICY_DENIED reason=apply_frozen_pending_review"
FANOUT_DENIAL_REASON = "POLICY_DENIED reason=apply_fanout_ceiling"


def flush_freeze_enabled() -> bool:
    """Master flag — default FALSE (supervised soaks opt in)."""
    raw = os.environ.get(_ENABLED_ENV, "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _max_ops() -> int:
    """``JARVIS_APPLY_FREEZE_MAX_OPS`` — default 1. How many DISTINCT
    base operations may acquire the latch token before the hard freeze.

    Transactional Op-Scoping (2026-07-22): the original per-FILE
    mutation budget collided with multi-file 2PC atomicity — soak
    bt-2026-07-22-022146 landed+verified file 1 of a 2-file op, then
    the budget denied file 2 and batch rollback correctly reverted
    the whole transaction. The latch now aligns with the operation
    boundary: files are siblings of a transaction, ops are the unit
    the budget counts.
    """
    try:
        return max(1, int(os.environ.get(_MAX_OPS_ENV, "1").strip()))
    except (TypeError, ValueError):
        return 1


def max_files_per_op() -> int:
    """``JARVIS_APPLY_FREEZE_MAX_FILES_PER_OP`` — default 5. Bounded
    Fan-Out: the ceiling on sibling files a single op-scoped token
    admits. Prevents a runaway mono-op from tunneling unlimited writes
    through one authorization; files beyond the ceiling are rejected
    pre-write (``FANOUT_DENIAL_REASON``), and the orchestrator's
    multi-file batch path rejects oversized transactions BEFORE the
    first byte so 2PC never starts a doomed batch.
    """
    try:
        return max(1, int(
            os.environ.get(_MAX_FILES_PER_OP_ENV, "5").strip()
        ))
    except (TypeError, ValueError):
        return 5


def base_op_id(op_id: str) -> str:
    """The transaction key: the per-file suffix the orchestrator's
    multi-file path appends (``{op_id}::{idx:02d}``) is stripped so
    every sibling file of one atomic candidate shares one latch token."""
    return (op_id or "").split("::", 1)[0]


def _diff_max_bytes() -> int:
    try:
        return max(1024, int(
            os.environ.get(_DIFF_MAX_BYTES_ENV, "262144").strip()
        ))
    except (TypeError, ValueError):
        return 262_144


def _git_timeout_s() -> float:
    try:
        return max(1.0, float(
            os.environ.get(_GIT_TIMEOUT_ENV, "15").strip()
        ))
    except (TypeError, ValueError):
        return 15.0


def _artifact_dir() -> Path:
    raw = os.environ.get(_ARTIFACT_DIR_ENV, "").strip()
    return Path(raw) if raw else Path(".jarvis") / "apply_freeze"


# ---------------------------------------------------------------------------
# Freeze latch — process-wide, thread-safe
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {
    # Transactional Op-Scoping (2026-07-22): the latch is OWNED by a base
    # operation, not spent by a file count. ``owner_op_base`` is the
    # transaction currently holding the token; its siblings pass (up to
    # the fan-out ceiling); every other base op is denied. ``ops_acquired``
    # counts distinct owners toward JARVIS_APPLY_FREEZE_MAX_OPS.
    "owner_op_base": "",
    "owner_files_flushed": 0,
    "ops_acquired": 0,
    "mutations_flushed": 0,     # global per-file counter (observability)
    "frozen_reason": "",
    "frozen_at_monotonic": 0.0,
    "last_op_id": "",
    "last_diff_sha8": "",
    "last_artifact": "",
}


def is_frozen(op_id: str = "") -> bool:
    """Op-aware latch consult — ``ChangeEngine.execute`` calls this
    BEFORE any byte is staged, passing the (per-file) op_id.

    Semantics (Transactional Op-Scoping, 2026-07-22):

    * Latch unowned → open for everyone (``False``).
    * Owned + caller is a SIBLING of the owner (same ``base_op_id``) →
      pass while the owner is under the Bounded Fan-Out ceiling — the
      multi-file 2PC atomicity contract completes; at/over the ceiling
      the sibling is denied (runaway mono-op containment).
    * Owned + caller is a DIFFERENT base op → denied IF the op budget
      (``JARVIS_APPLY_FREEZE_MAX_OPS``, default 1) is spent; under a
      multi-op budget the new op may take over the token at flush time,
      so the consult passes it through.
    * No/empty op_id (legacy callers) → conservative: frozen whenever
      the latch is owned and the budget is spent.

    Thread-safe; NEVER raises (fail-open on internal error — the rest
    of the cage still stands; fail-closed here would wedge every APPLY
    on a telemetry-layer bug).
    """
    try:
        caller_base = base_op_id(op_id)
        with _LOCK:
            owner = _STATE["owner_op_base"]
            if not owner:
                return False
            if caller_base and caller_base == owner:
                return (
                    int(_STATE["owner_files_flushed"]) >= max_files_per_op()
                )
            return int(_STATE["ops_acquired"]) >= _max_ops()
    except Exception:  # noqa: BLE001
        return False


def denial_reason(op_id: str = "") -> str:
    """The taxonomy-correct POLICY_DENIED string for a denied consult:
    a sibling over the ceiling gets the fan-out reason; a foreign op
    gets the frozen-pending-review reason. NEVER raises."""
    try:
        caller_base = base_op_id(op_id)
        with _LOCK:
            if (
                caller_base
                and caller_base == _STATE["owner_op_base"]
                and int(_STATE["owner_files_flushed"]) >= max_files_per_op()
            ):
                return FANOUT_DENIAL_REASON
    except Exception:  # noqa: BLE001
        pass
    return FROZEN_DENIAL_REASON


def freeze_snapshot() -> Dict[str, Any]:
    """Observability copy of the latch state. NEVER raises."""
    try:
        with _LOCK:
            return dict(_STATE)
    except Exception:  # noqa: BLE001
        return {}


def _reset_for_tests() -> None:
    with _LOCK:
        _STATE.update(
            owner_op_base="", owner_files_flushed=0, ops_acquired=0,
            mutations_flushed=0, frozen_reason="",
            frozen_at_monotonic=0.0, last_op_id="", last_diff_sha8="",
            last_artifact="",
        )


# ---------------------------------------------------------------------------
# Diff capture — async subprocess, byte-bounded, untracked-aware
# ---------------------------------------------------------------------------

async def _run_git(args: Sequence[str], cwd: Path) -> "tuple[int, str]":
    """Bounded async git invocation. Returns (rc, stdout). NEVER raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_git_timeout_s(),
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return 124, ""
        return proc.returncode or 0, out.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — capture is fail-soft
        return 125, ""


async def capture_mutation_diff(
    write_root: Path, target_paths: Sequence[str],
) -> str:
    """The mutation's git diff against ``write_root``, byte-bounded.

    Tracked modifications come from ``git diff -- <targets>``; a target the
    index has never seen (a genuinely NEW file) yields nothing there, so it
    is captured via ``git diff --no-index /dev/null <target>`` (rc=1 on
    difference is SUCCESS for no-index). Read-only: never touches the index,
    the working tree, or HEAD. NEVER raises; returns "" when nothing can be
    captured (no git, no repo, timeout).
    """
    rels = [str(p) for p in target_paths if str(p).strip()]
    if not rels:
        return ""
    cap = _diff_max_bytes()
    _rc, tracked = await _run_git(["diff", "--", *rels], write_root)
    chunks = [tracked] if tracked.strip() else []
    for rel in rels:
        if any(f"b/{rel}" in c or rel in c for c in chunks):
            continue
        candidate = write_root / rel
        try:
            if not candidate.is_file():
                continue
        except OSError:
            continue
        _rc2, untracked = await _run_git(
            ["diff", "--no-index", "--", os.devnull, rel], write_root,
        )
        if untracked.strip():
            chunks.append(untracked)
    diff = "\n".join(chunks)
    if len(diff.encode("utf-8", errors="replace")) > cap:
        diff = diff.encode("utf-8", errors="replace")[:cap].decode(
            "utf-8", errors="replace",
        ) + "\n... [diff truncated at byte cap]"
    return diff


# ---------------------------------------------------------------------------
# The interceptor — capture → persist → flush → freeze
# ---------------------------------------------------------------------------

async def flush_and_freeze(
    op_id: str,
    target_paths: Sequence[str],
    write_root: Path,
    *,
    goal: str = "",
) -> Dict[str, Any]:
    """Run the full Atomic Flush & Freeze sequence for one successful APPLY.

    Called by ``ChangeEngine.execute`` at the success seam (post-2PC-VERIFY,
    post-APPLIED ledger). Every stage is fail-soft EXCEPT the latch: the
    freeze closes even when capture/persist/flush degrade — containment
    never depends on observability succeeding. Returns a scalar summary
    (also logged) for the caller's ledger/telemetry. NEVER raises.
    """
    summary: Dict[str, Any] = {
        "op_id": op_id, "captured": False, "flushed": False,
        "frozen": False, "artifact": "", "diff_sha8": "", "diff_lines": 0,
    }
    try:
        # 1. Capture.
        diff = await capture_mutation_diff(write_root, target_paths)
        diff_sha8 = hashlib.sha256(
            diff.encode("utf-8", errors="replace"),
        ).hexdigest()[:8] if diff else ""
        summary["captured"] = bool(diff)
        summary["diff_sha8"] = diff_sha8
        summary["diff_lines"] = diff.count("\n") if diff else 0

        # 2. Persist — durable artifact + full (bounded) debug.log record so
        #    a severed socket can never lose the mutation evidence.
        artifact_path = ""
        if diff:
            try:
                adir = _artifact_dir()
                adir.mkdir(parents=True, exist_ok=True)
                artifact = adir / f"{op_id}.patch"
                artifact.write_text(diff, encoding="utf-8")
                artifact_path = str(artifact)
            except Exception:  # noqa: BLE001 — persistence is fail-soft
                logger.debug(
                    "[ApplyFlushFreeze] artifact persist failed op=%s",
                    op_id, exc_info=True,
                )
            logger.info(
                "[ApplyFlushFreeze] APPLY MUTATION DIFF op=%s files=%s "
                "sha8=%s lines=%d artifact=%s\n%s",
                op_id, ",".join(str(p) for p in target_paths), diff_sha8,
                summary["diff_lines"], artifact_path or "-", diff,
            )
        summary["artifact"] = artifact_path

        # 3. Flush — high-priority envelope through the EXISTING HiveEmitter
        #    edge; the enqueue is demonstrated via the stats delta.
        try:
            from backend.api.hive_emitter import (
                get_default_emitter, hive_emit, hive_flush,
            )
            _before = int(get_default_emitter().stats.get("emitted", 0))
            hive_emit(
                actor_id="change_engine",
                subsystem="governance",
                intent="apply_mutation_frozen",
                summary=(
                    f"APPLY {op_id[:16]}: {len(list(target_paths))} file(s) "
                    f"mutated (diff sha8={diff_sha8 or '-'}, "
                    f"lines={summary['diff_lines']}) — mutations parked "
                    f"pending human review"
                ),
                severity="warn",
                trace_id=op_id,
                detail={
                    "files": ",".join(str(p) for p in target_paths)[:120],
                    "diff_sha8": diff_sha8,
                    "diff_lines": summary["diff_lines"],
                    "artifact": artifact_path[:120],
                    "goal": goal[:120],
                },
            )
            hive_flush("change_engine", "apply_mutation_frozen")
            _after = int(get_default_emitter().stats.get("emitted", 0))
            summary["flushed"] = _after > _before
            logger.info(
                "[ApplyFlushFreeze] telemetry %s op=%s "
                "(emitter stats emitted %d→%d)",
                "ENQUEUED+FLUSHED" if summary["flushed"]
                else "NOT CONFIRMED (emitter disabled/no-loop)",
                op_id, _before, _after,
            )
        except Exception:  # noqa: BLE001 — telemetry is fail-soft
            logger.warning(
                "[ApplyFlushFreeze] hive flush failed op=%s — freeze "
                "proceeds regardless", op_id, exc_info=True,
            )
    finally:
        # 4. Freeze — op-scoped token acquisition (Transactional
        #    Op-Scoping, 2026-07-22). The latch closes to NEW base ops
        #    NO MATTER what degraded above; siblings of the owning
        #    transaction keep passing (up to the fan-out ceiling) so
        #    the multi-file 2PC atomicity contract can complete.
        try:
            caller_base = base_op_id(op_id)
            newly_parked = False
            with _LOCK:
                _STATE["mutations_flushed"] = (
                    int(_STATE["mutations_flushed"]) + 1
                )
                _STATE["last_op_id"] = op_id
                _STATE["last_diff_sha8"] = summary["diff_sha8"]
                _STATE["last_artifact"] = summary["artifact"]
                owner = _STATE["owner_op_base"]
                if owner == caller_base and owner:
                    _STATE["owner_files_flushed"] = (
                        int(_STATE["owner_files_flushed"]) + 1
                    )
                else:
                    # First landed file of a new transaction — acquire
                    # (or, under a multi-op budget, take over) the token.
                    _STATE["owner_op_base"] = caller_base
                    _STATE["owner_files_flushed"] = 1
                    _STATE["ops_acquired"] = (
                        int(_STATE["ops_acquired"]) + 1
                    )
                    if int(_STATE["ops_acquired"]) >= _max_ops():
                        _STATE["frozen_reason"] = (
                            f"apply_op_budget_spent "
                            f"({_STATE['ops_acquired']}/{_max_ops()}) "
                            f"owner={caller_base} — siblings admitted "
                            f"up to fan-out {max_files_per_op()}"
                        )
                        _STATE["frozen_at_monotonic"] = time.monotonic()
                        newly_parked = True
                summary["frozen"] = (
                    int(_STATE["ops_acquired"]) >= _max_ops()
                )
            if newly_parked:
                logger.warning(
                    "[ApplyFlushFreeze] MUTATIONS PARKED (op-scoped) — "
                    "%s; every NEW base op short-circuits to '%s' "
                    "pending human review; owner siblings complete the "
                    "2PC transaction up to the fan-out ceiling",
                    freeze_snapshot().get("frozen_reason", ""),
                    FROZEN_DENIAL_REASON,
                )
        except Exception:  # noqa: BLE001 — never break the APPLY result
            logger.error(
                "[ApplyFlushFreeze] latch update failed op=%s",
                op_id, exc_info=True,
            )
    return summary


def register_flags(registry: Any) -> int:
    """FlagRegistry self-registration (repo discoverability idiom).
    Returns count registered. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except ImportError:
        return 0
    specs = [
        FlagSpec(
            name=_ENABLED_ENV, type=FlagType.BOOL, default=False,
            description=(
                "Atomic Flush & Freeze master. When ON, every successful "
                "ChangeEngine APPLY captures its git diff, persists it, "
                "flushes a high-priority HiveTelemetryEnvelope through the "
                "existing HiveEmitter edge, and acquires the op-scoped "
                "latch token: after JARVIS_APPLY_FREEZE_MAX_OPS (default 1) "
                "distinct base operations, all NEW ops are PARKED "
                "(POLICY_DENIED reason=apply_frozen_pending_review) while "
                "siblings of the owning transaction complete their 2PC "
                "batch up to JARVIS_APPLY_FREEZE_MAX_FILES_PER_OP "
                "(default 5; over-ceiling = apply_fanout_ceiling, and "
                "oversized batches are rejected pre-write). Default OFF; "
                "supervised validation soaks opt in."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/apply_flush_freeze.py"
            ),
            example="true",
            since="Atomic Flush & Freeze (2026-07-21)",
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            pass
    return count


__all__ = [
    "FANOUT_DENIAL_REASON",
    "FROZEN_DENIAL_REASON",
    "base_op_id",
    "capture_mutation_diff",
    "denial_reason",
    "flush_and_freeze",
    "flush_freeze_enabled",
    "freeze_snapshot",
    "is_frozen",
    "max_files_per_op",
    "register_flags",
]
