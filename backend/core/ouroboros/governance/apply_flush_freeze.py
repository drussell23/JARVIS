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
_MAX_MUTATIONS_ENV = "JARVIS_APPLY_FREEZE_MAX_MUTATIONS"
_DIFF_MAX_BYTES_ENV = "JARVIS_APPLY_FREEZE_DIFF_MAX_BYTES"
_ARTIFACT_DIR_ENV = "JARVIS_APPLY_FREEZE_ARTIFACT_DIR"
_GIT_TIMEOUT_ENV = "JARVIS_APPLY_FREEZE_GIT_TIMEOUT_S"

#: The ChangeEngine denial marker (grep-discoverable, mirrors the
#: generation_fence / mutation_budget POLICY_DENIED taxonomy).
FROZEN_DENIAL_REASON = "POLICY_DENIED reason=apply_frozen_pending_review"


def flush_freeze_enabled() -> bool:
    """Master flag — default FALSE (supervised soaks opt in)."""
    raw = os.environ.get(_ENABLED_ENV, "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _max_mutations() -> int:
    try:
        return max(1, int(os.environ.get(_MAX_MUTATIONS_ENV, "1").strip()))
    except (TypeError, ValueError):
        return 1


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
    "mutations_flushed": 0,
    "frozen": False,
    "frozen_reason": "",
    "frozen_at_monotonic": 0.0,
    "last_op_id": "",
    "last_diff_sha8": "",
    "last_artifact": "",
}


def is_frozen() -> bool:
    """True once the mutation budget is spent — consulted by
    ``ChangeEngine.execute`` BEFORE any byte is staged. Thread-safe;
    NEVER raises."""
    try:
        with _LOCK:
            return bool(_STATE["frozen"])
    except Exception:  # noqa: BLE001
        return False


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
            mutations_flushed=0, frozen=False, frozen_reason="",
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
        # 4. Freeze — the latch closes NO MATTER what degraded above.
        try:
            with _LOCK:
                _STATE["mutations_flushed"] = (
                    int(_STATE["mutations_flushed"]) + 1
                )
                _STATE["last_op_id"] = op_id
                _STATE["last_diff_sha8"] = summary["diff_sha8"]
                _STATE["last_artifact"] = summary["artifact"]
                if _STATE["mutations_flushed"] >= _max_mutations():
                    _STATE["frozen"] = True
                    _STATE["frozen_reason"] = (
                        f"apply_mutation_budget_spent "
                        f"({_STATE['mutations_flushed']}/"
                        f"{_max_mutations()}) op={op_id}"
                    )
                    _STATE["frozen_at_monotonic"] = time.monotonic()
                summary["frozen"] = bool(_STATE["frozen"])
            if summary["frozen"]:
                logger.warning(
                    "[ApplyFlushFreeze] MUTATIONS PARKED — %s; every "
                    "subsequent ChangeEngine.execute short-circuits to "
                    "'%s' pending human review",
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
                "existing HiveEmitter edge, and — after "
                "JARVIS_APPLY_FREEZE_MAX_MUTATIONS (default 1) mutations — "
                "PARKS all further mutations (POLICY_DENIED "
                "reason=apply_frozen_pending_review) pending human review. "
                "Default OFF; supervised validation soaks opt in."
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
    "FROZEN_DENIAL_REASON",
    "capture_mutation_diff",
    "flush_and_freeze",
    "flush_freeze_enabled",
    "freeze_snapshot",
    "is_frozen",
    "register_flags",
]
