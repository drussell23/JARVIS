"""PostureObserver — periodic async signal collector + hysteresis gate.

Owns the lifecycle of the DirectionInferrer in production: wake every
``JARVIS_POSTURE_OBSERVER_INTERVAL_S`` (default 300s), collect the 12
signals from their authoritative sources, infer a new reading, apply
the hysteresis gate, and persist through PostureStore.

Signal collection is defensively wrapped — a failed collector yields
the documented baseline (typically 0.0) rather than blocking the cycle.
The observer never blocks the main loop: every collector is guarded by
``asyncio.wait_for`` (``JARVIS_POSTURE_COLLECTOR_TIMEOUT_S`` default 30s).

Hysteresis:
  A new reading replaces ``current`` only when ONE of:
  (a) ``JARVIS_POSTURE_HYSTERESIS_WINDOW_S`` has elapsed since the last
      *change* (not the last *reading*) — default 900s / 15min;
  (b) the new reading's confidence exceeds 0.75 (high-confidence bypass);
  (c) an operator override is active (override supersedes inference).
  Otherwise the reading lands in history but current stays pinned.

Authority invariant (grep-pinned in Slice 4):
  Imports nothing from ``orchestrator`` / ``policy`` / ``iron_gate`` /
  ``risk_tier`` / ``change_engine`` / ``candidate_generator`` / ``gate``.

Signal collectors in v1 — honest scope:
  * ``feat_ratio`` / ``fix_ratio`` / ``refactor_ratio`` / ``test_docs_ratio``
    — derived from ``git log`` Conventional-Commit parsing (window via
    ``JARVIS_POSTURE_SIGNAL_COMMIT_WINDOW``, default 50)
  * ``postmortem_failure_rate`` — parsed from recent
    ``.ouroboros/sessions/*/summary.json`` files
  * ``iron_gate_reject_rate``, ``l2_repair_rate`` — read from
    ``.ouroboros/sessions/*/summary.json`` event_counts when present;
    0.0 when absent (cold start)
  * ``session_lessons_infra_ratio`` — parsed from ``session_lessons``
    field in the most recent summary.json when present; 0.0 otherwise
  * ``open_ops_normalized`` — snapshotted from an injected
    ``open_ops_provider`` callable at the wiring layer; 0.0 when
    unwired (Slice 2 ships the hook, GovernedLoopService wires it later)
  * ``time_since_last_graduation_inv`` — grep for
    ``graduate.*JARVIS_`` in recent git log subjects → 1/(hours_since+1)
  * ``cost_burn_normalized`` — reads CostGovernor daily state if present
    at ``.jarvis/cost_state.json``, else 0.0
  * ``worktree_orphan_count`` — counts ``unit-*`` dirs under
    ``JARVIS_WORKTREE_BASE`` if configured, else 0

This is Slice 2's honest scope: real signals where the source is
authoritative, documented baselines where it isn't. Slice 5 (hardening)
revisits the stub signals with real providers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from backend.core.ouroboros.governance.arc_context import (
    build_arc_context,
)
from backend.core.ouroboros.governance.direction_inferrer import (
    DirectionInferrer,
    arc_context_enabled as _arc_context_enabled,
    is_enabled as _inferrer_enabled,
)
from backend.core.ouroboros.governance.posture import (
    Posture,
    PostureReading,
    SignalBundle,
    baseline_bundle,
)
from backend.core.ouroboros.governance.posture_store import (
    OverrideRecord,
    PostureStore,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------


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


def observer_interval_s() -> float:
    return float(_env_int("JARVIS_POSTURE_OBSERVER_INTERVAL_S", 300, minimum=5))


def collector_timeout_s() -> float:
    return _env_float("JARVIS_POSTURE_COLLECTOR_TIMEOUT_S", 30.0, minimum=0.5)


def hysteresis_window_s() -> float:
    return float(_env_int("JARVIS_POSTURE_HYSTERESIS_WINDOW_S", 900, minimum=0))


def high_confidence_bypass() -> float:
    return _env_float("JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS", 0.75, minimum=0.0)


def commit_window() -> int:
    return _env_int("JARVIS_POSTURE_SIGNAL_COMMIT_WINDOW", 50, minimum=1)


def postmortem_window_h() -> int:
    return _env_int("JARVIS_POSTURE_SIGNAL_POSTMORTEM_WINDOW_H", 48, minimum=1)


def override_max_h() -> int:
    return _env_int("JARVIS_POSTURE_OVERRIDE_MAX_H", 24, minimum=1)


def recent_summaries_max() -> int:
    """Upper bound on how many ``summary.json`` files a single posture
    cycle scans + parses (IMPORTANT 2). The four summary-derived raters
    (postmortem_failure_rate / iron_gate_reject_rate / l2_repair_rate /
    session_lessons_infra_ratio) previously re-scanned + re-parsed the
    *entire* ``.ouroboros/sessions/*`` tree FOUR times per cycle with no
    shared memo and no count bound — the same unbounded-scan pathology
    class as the cost_burn bug. Bounding to the newest-N by mtime (env,
    default 50) means a session-count spike (or one enormous summary.json)
    can no longer reintroduce the GIL-hold freeze. Newest-N is selected
    BEFORE parsing, so the parse budget itself is bounded, not just the
    result."""
    return _env_int("JARVIS_POSTURE_RECENT_SUMMARIES_MAX", 50, minimum=1)


def wholesale_offload_enabled() -> bool:
    """3rd starvation tier (Fix 2) — master switch for wholesale
    off-loop cycle execution. Default TRUE: the ENTIRE ``run_one_cycle``
    runs synchronously inside the shared ``cooperative_fs_io.offload``
    thread pool so the whole cadence tick is decoupled from the primary
    event loop in ONE dispatch. FALSE degrades to the legacy per-signal
    chunked-async path (``build_bundle_async`` → ``_offload_signal``,
    Tier-2b) for byte-identical-shape rollback."""
    raw = os.environ.get("JARVIS_POSTURE_WHOLESALE_OFFLOAD_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


_CONV_COMMIT_RE = re.compile(
    r"^(?P<type>feat|fix|refactor|test|docs|chore|perf|style|build|ci|revert)"
    r"(?:\([^)]+\))?!?:",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Slice 33 Arc 2 Phase 2 — dedicated filesystem signal executor
# ---------------------------------------------------------------------------
#
# Closes the v28 (bt-2026-05-27-235042) LoopSink-confirmed sink:
#   posture.signal.postmortem_failure_rate blocked_ms=5322.57
#
# Root cause: 4 of the 9 signal collectors (postmortem_failure_rate,
# iron_gate_reject_rate, l2_repair_rate, session_lessons_infra_ratio)
# all iterate .ouroboros/sessions/*/summary.json synchronously. Under
# Arc 1, each runs in the DEFAULT ThreadPoolExecutor via asyncio.
# to_thread — contending with every other to_thread caller in the
# process (oracle file reads, oracle parse-result dispatches, etc.).
#
# Phase 2 routes these 4 specifically to a DEDICATED 2-worker
# ThreadPoolExecutor reserved for filesystem signal collection.
# Bounded (operators with more cores can raise via env). Lazy
# singleton (no startup cost when posture observer isn't active).

# Tier-2b (bt-iso-1783093701 LoopSink, 2nd starvation tier) — the bespoke
# 2-worker ``fs_signal_executor`` ThreadPoolExecutor that Slice 33 Arc 2
# Phase 2 / Slice 257 introduced was a THIRD divergent offload mechanism
# (alongside cooperative_fs_io's advisor-blast pool and the process pool).
# It is now KILLED and CONVERGED onto the single unified
# ``cooperative_fs_io.offload`` gateway: every fs-signal + git-subprocess
# collector routes through ``offload(cpu_bound=False)`` (thread path —
# ``subprocess.run(git log)`` releases the GIL while the child runs, and
# session-dir summary scans are IO-bound). One pool, one contract, no
# bespoke lifecycle to shut down. See ``build_bundle_async`` /
# ``commit_ratios_async``.
async def _offload_signal(fn: "Callable[..., Any]", *args: Any) -> Any:
    """Route a filesystem / git-subprocess signal collector through the
    unified ``cooperative_fs_io.offload`` substrate (thread pool).

    Fail-soft: on any substrate fault OR an ``OffloadError`` (the collector
    raised inside the worker), returns ``None`` so the caller can fall back
    to its own neutral default — the collector fault NEVER propagates into
    the posture cycle. If the substrate import itself fails, degrades to a
    bare ``asyncio.to_thread`` (still off-loop)."""
    try:
        from backend.core.ouroboros.governance.cooperative_fs_io import (
            offload,
            is_offload_error,
        )
    except Exception:  # noqa: BLE001 — substrate import fault
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception:  # noqa: BLE001
            return None
    result = await offload(fn, *args, cpu_bound=False)
    if is_offload_error(result):
        logger.debug(
            "[PostureObserver] signal collector OffloadError: %r", result,
        )
        return None
    return result


# Callable the wiring layer can inject to surface in-flight op count.
OpenOpsProvider = Callable[[], int]


class _CycleOutcome(NamedTuple):
    """Result of one cadence tick's decision phase (``_process_bundle``).

    CRITICAL fix — the loop-affine ``on_change`` callback must NOT run on
    the offload worker thread (in production it flows into
    ``StreamEventBroker.publish`` → ``asyncio.Queue.put_nowait`` →
    ``loop.call_soon``, which is NOT thread-safe from a foreign thread).
    So ``_process_bundle`` runs the pure/thread-safe decision (bundle +
    hysteresis + the thread-safe ``PostureStore`` file IO) and *returns*
    the on_change payload here instead of invoking it. The caller fires
    ``on_change`` ON THE LOOP after ``offload(...)`` resolves.

    ``on_change_args`` is ``None`` unless a real posture transition was
    promoted (and its write actually landed — a stale epoch-guarded cycle
    suppresses both the write and the callback)."""

    to_persist: Optional[PostureReading]
    on_change_args: Optional[Tuple[PostureReading, Optional[PostureReading]]] = None


# ---------------------------------------------------------------------------
# Signal collectors
# ---------------------------------------------------------------------------


class SignalCollector:
    """Read-only signal collection. Every method returns a documented
    baseline on failure; nothing raises to the observer loop."""

    def __init__(
        self,
        project_root: Path,
        *,
        open_ops_provider: Optional[OpenOpsProvider] = None,
    ) -> None:
        self._root = project_root.resolve()
        self._open_ops_provider = open_ops_provider
        # Slice 52 Phase 2 — reactive commit-ratio cache keyed by
        # (HEAD hash, window). Lets the 300s posture cycle skip the
        # 100-commit ``git log`` whenever HEAD has not advanced (the
        # common case). ``None`` until first computation.
        self._commit_ratios_cache: Optional[Tuple[str, int, Dict[str, float]]] = None
        # 3rd starvation tier (Fix 1) — memoized cost-burn read keyed by
        # the cost_state.json stat identity ``(st_mtime_ns, st_size)``.
        # The 300s cadence re-read + re-parsed this file EVERY cycle; if
        # the file grows into a rolling cost ledger, ``json.loads`` is
        # O(filesize) pure-Python CPU holding the GIL. Memoizing on stat
        # identity collapses the steady state to a single ``stat()``
        # (microseconds) whenever the file has not changed. ``None`` until
        # first read; reset to ``None`` on a missing/unreadable file so a
        # stale value can't be pinned.
        self._cost_burn_cache: Optional[Tuple[Tuple[int, int], float]] = None

    def _git_subjects(self, n: int) -> List[str]:
        """Legacy sync entry — retained for backwards compat with the
        sync ``commit_ratios`` / ``build_bundle`` path. Production
        chunked-async cycle uses :meth:`_git_subjects_async`."""
        try:
            result = subprocess.run(
                ["git", "log", f"-{n}", "--pretty=format:%s"],
                cwd=str(self._root), capture_output=True, text=True,
                timeout=5.0, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return []
        if result.returncode != 0:
            return []
        return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]

    def _git_head(self) -> str:
        """Sync ``git rev-parse HEAD`` — the cheap cache anchor.

        Slice 257 — paired with :meth:`_git_subjects` for the off-loop
        ``commit_ratios_async`` path. Runs in the dedicated
        ``fs_signal_executor`` thread, so even a slow fork (large
        multi-threaded process) blocks a worker, never the event loop.
        Returns "" on any failure so a stale value can't pin the cache.
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self._root), capture_output=True, text=True,
                timeout=2.0, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    async def _git_subjects_async(self, n: int) -> List[str]:
        """Slice 33 Arc 2 Phase 1 — async git subprocess.

        Replaces ``subprocess.run`` (which ties up a ThreadPool worker
        for the full duration even via ``asyncio.to_thread``) with
        ``asyncio.create_subprocess_exec`` which is genuinely
        non-blocking from asyncio's perspective. The cold-cache 18 s
        git log on a 29k-file repo no longer holds a thread-pool
        slot, freeing default-executor capacity for sibling work.

        Bounded by ``asyncio.wait_for(timeout=5.0)`` — on timeout the
        subprocess is killed cleanly. NEVER raises — returns ``[]``
        on any failure (timeout / git missing / nonzero exit /
        decode error).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "log", f"-{n}", "--pretty=format:%s",
                cwd=str(self._root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError):
            return []
        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=5.0,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
            return []
        except asyncio.CancelledError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            raise
        except Exception:  # noqa: BLE001 — defensive
            return []
        if proc.returncode != 0:
            return []
        try:
            text = stdout_bytes.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return []
        return [ln.strip() for ln in text.splitlines() if ln.strip()]

    async def _git_head_async(self) -> str:
        """Resolve current HEAD sha (``git rev-parse HEAD``), async + bounded.

        Slice 52 Phase 2 — a cheap (sub-tens-of-ms) anchor for the
        commit-ratio cache. Returns "" on any failure (no git / detached /
        timeout / nonzero exit) so callers treat HEAD as unresolvable and
        skip caching rather than pinning a stale value. NEVER raises.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                cwd=str(self._root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError):
            return ""
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            return ""
        except Exception:  # noqa: BLE001
            return ""
        if proc.returncode != 0:
            return ""
        try:
            return out.decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _compute_commit_ratios(subjects: List[str]) -> Dict[str, float]:
        """Pure ratio math over Conventional-Commit subjects (Slice 52
        — extracted so the cached async path and any future caller share
        one implementation)."""
        if not subjects:
            return {"feat": 0.0, "fix": 0.0, "refactor": 0.0, "test_docs": 0.0}
        counts = {"feat": 0, "fix": 0, "refactor": 0, "test": 0, "docs": 0}
        for subj in subjects:
            m = _CONV_COMMIT_RE.match(subj)
            if not m:
                continue
            ctype = m.group("type").lower()
            if ctype in counts:
                counts[ctype] += 1
        total = len(subjects)
        return {
            "feat": counts["feat"] / total,
            "fix": counts["fix"] / total,
            "refactor": counts["refactor"] / total,
            "test_docs": (counts["test"] + counts["docs"]) / total,
        }

    async def commit_ratios_async(self) -> Dict[str, float]:
        """Async commit ratios — git work offloaded to a worker thread.

        Slice 257 (bt-2026-06-16-052242, 108.9s loop wedge → near-fatal
        heartbeat-stale SIGKILL): the previous implementation resolved HEAD
        and the 100-commit ``git log`` via ``asyncio.create_subprocess_exec``,
        which forks/execs the child **on the event-loop thread**. From the
        large, multi-threaded organism process — concurrent with the Oracle
        process pool forking 16 cold-index workers — that fork blocked the
        loop synchronously for 33–108s (the ``git log`` query itself is
        0.14s; the cost is the fork, not the work). Because the block is
        synchronous and yield-less, the 30s collector ``wait_for`` could not
        cancel it and ``ControlPlaneStarvation`` stayed silent — the heartbeat
        froze and the 120s external watchdog SIGKILLed the session.

        Tier-2b converges this onto the single unified
        ``cooperative_fs_io.offload`` substrate (``_offload_signal``, thread
        path — ``subprocess.run(git ...)`` releases the GIL while the child
        runs, so the fork blocks a worker thread, never the loop; the
        Slice 257 off-loop invariant holds via the shared advisor-blast pool
        instead of the killed bespoke pool). ``subprocess.run`` timeouts
        (2s HEAD / 5s log) still bound the work; the Slice 52 HEAD-cache
        short-circuit is preserved.
        """
        # Tier-2b — converge onto the unified offload substrate (thread path:
        # ``subprocess.run(git ...)`` releases the GIL while the child runs,
        # so the fork/exec blocks a worker thread, never the loop — the
        # Slice 257 off-loop invariant is preserved via the shared
        # advisor-blast pool instead of the killed bespoke pool).
        window = commit_window()
        head = await _offload_signal(self._git_head)
        if head is None:
            head = ""
        cache = self._commit_ratios_cache
        if head and cache is not None and cache[0] == head and cache[1] == window:
            return dict(cache[2])
        subjects = await _offload_signal(self._git_subjects, window)
        if subjects is None:
            subjects = []
        ratios = self._compute_commit_ratios(subjects)
        if head:
            self._commit_ratios_cache = (head, window, dict(ratios))
        return ratios

    def commit_ratios(self) -> Dict[str, float]:
        """feat / fix / refactor / test+docs ratios over last N commits."""
        subjects = self._git_subjects(commit_window())
        if not subjects:
            return {"feat": 0.0, "fix": 0.0, "refactor": 0.0, "test_docs": 0.0}
        counts = {"feat": 0, "fix": 0, "refactor": 0, "test": 0, "docs": 0}
        for subj in subjects:
            m = _CONV_COMMIT_RE.match(subj)
            if not m:
                continue
            ctype = m.group("type").lower()
            if ctype in counts:
                counts[ctype] += 1
        total = len(subjects)
        return {
            "feat": counts["feat"] / total,
            "fix": counts["fix"] / total,
            "refactor": counts["refactor"] / total,
            "test_docs": (counts["test"] + counts["docs"]) / total,
        }

    # IMPORTANT 2 — the widest window any summary-derived rater needs.
    # postmortem_failure_rate / session_lessons_infra_ratio read
    # ``postmortem_window_h()`` (default 48h); iron_gate_reject_rate /
    # l2_repair_rate read a fixed 24h. A single per-cycle scan over the
    # WIDEST window is a strict superset — each rater re-filters the
    # shared list down to its own (narrower-or-equal) cutoff, so the
    # numbers are identical to the old per-rater scans, minus the 3×
    # redundant re-parse and the unbounded session count.
    def _summaries_widest_window_h(self) -> int:
        return max(postmortem_window_h(), 24)

    def scan_recent_summaries(
        self, window_h: int, limit: int,
    ) -> List[Tuple[float, Dict[str, Any]]]:
        """Scan + parse ``.ouroboros/sessions/*/summary.json`` within
        ``window_h``, bounded to the newest-``limit`` by mtime.

        IMPORTANT 2 — the newest-N bound is applied BEFORE parsing, so a
        session-count spike (thousands of stale session dirs) can force at
        most ``limit`` ``json.loads`` calls, never one-per-session. Returns
        ``(mtime, payload)`` pairs (mtime retained so a shared scan over the
        widest window can be re-filtered per-rater without a re-scan).
        Never raises."""
        sessions_dir = self._root / ".ouroboros" / "sessions"
        if not sessions_dir.exists():
            return []
        cutoff = time.time() - (window_h * 3600)
        candidates: List[Tuple[float, Path]] = []
        try:
            for sess in sessions_dir.iterdir():
                if not sess.is_dir():
                    continue
                summary = sess / "summary.json"
                try:
                    mtime = summary.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                candidates.append((mtime, summary))
        except OSError:
            return []
        # Bound the PARSE budget: keep only the newest-N by mtime, then
        # parse. A spike in session count can't blow up json.loads calls.
        candidates.sort(key=lambda t: t[0], reverse=True)
        out: List[Tuple[float, Dict[str, Any]]] = []
        for mtime, summary in candidates[: max(1, int(limit))]:
            try:
                out.append(
                    (mtime, json.loads(summary.read_text(encoding="utf-8")))
                )
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def recent_summaries(self, window_h: int) -> List[Dict[str, Any]]:
        """Backward-compatible payload-only view over
        :meth:`scan_recent_summaries` (now bounded to ``recent_summaries_max``
        newest sessions). Retained for any standalone caller/test."""
        return [
            payload
            for _mtime, payload in self.scan_recent_summaries(
                window_h, recent_summaries_max(),
            )
        ]

    @staticmethod
    def _filter_summaries_within(
        summaries: List[Tuple[float, Dict[str, Any]]], window_h: int,
    ) -> List[Dict[str, Any]]:
        """Re-filter a shared (widest-window) scan to a rater's own
        window cutoff — pure, no IO."""
        cutoff = time.time() - (window_h * 3600)
        return [payload for mtime, payload in summaries if mtime >= cutoff]

    def _resolve_summaries(
        self,
        shared: Optional[List[Tuple[float, Dict[str, Any]]]],
        window_h: int,
    ) -> List[Dict[str, Any]]:
        """Return the payloads a rater should score. When a per-cycle
        ``shared`` scan is provided, filter it (no IO — the scan already
        happened once for all four raters). Standalone (``shared is None``)
        falls back to a self-scan bounded to ``recent_summaries_max``."""
        if shared is None:
            return [
                payload
                for _mtime, payload in self.scan_recent_summaries(
                    window_h, recent_summaries_max(),
                )
            ]
        return self._filter_summaries_within(shared, window_h)

    def postmortem_failure_rate(
        self,
        summaries: Optional[List[Tuple[float, Dict[str, Any]]]] = None,
    ) -> float:
        rows = self._resolve_summaries(summaries, postmortem_window_h())
        if not rows:
            return 0.0
        total_ops = 0
        failed_ops = 0
        for s in rows:
            ops_digest = s.get("ops_digest") or {}
            try:
                attempted = int(ops_digest.get("attempted", 0))
                verified = int(ops_digest.get("verified", 0))
            except (TypeError, ValueError):
                continue
            if attempted > 0:
                total_ops += attempted
                failed_ops += max(0, attempted - verified)
        if total_ops == 0:
            return 0.0
        return min(1.0, failed_ops / total_ops)

    def iron_gate_reject_rate(
        self,
        summaries: Optional[List[Tuple[float, Dict[str, Any]]]] = None,
    ) -> float:
        rows = self._resolve_summaries(summaries, 24)
        if not rows:
            return 0.0
        total = 0
        rejects = 0
        for s in rows:
            events = s.get("event_counts") or {}
            try:
                total += int(events.get("generate_total", 0))
                rejects += int(events.get("iron_gate_reject", 0))
            except (TypeError, ValueError):
                continue
        if total == 0:
            return 0.0
        return min(1.0, rejects / total)

    def l2_repair_rate(
        self,
        summaries: Optional[List[Tuple[float, Dict[str, Any]]]] = None,
    ) -> float:
        rows = self._resolve_summaries(summaries, 24)
        if not rows:
            return 0.0
        total = 0
        repairs = 0
        for s in rows:
            events = s.get("event_counts") or {}
            try:
                total += int(events.get("apply_total", 0))
                repairs += int(events.get("l2_invoked", 0))
            except (TypeError, ValueError):
                continue
        if total == 0:
            return 0.0
        return min(1.0, repairs / total)

    def session_lessons_infra_ratio(
        self,
        summaries: Optional[List[Tuple[float, Dict[str, Any]]]] = None,
    ) -> float:
        rows = self._resolve_summaries(summaries, postmortem_window_h())
        if not rows:
            return 0.0
        total = 0
        infra = 0
        for s in rows:
            lessons = s.get("session_lessons") or []
            if not isinstance(lessons, list):
                continue
            for lesson in lessons:
                if not isinstance(lesson, dict):
                    continue
                total += 1
                tag = str(lesson.get("tag", "")).lower()
                if tag == "infra":
                    infra += 1
        if total == 0:
            return 0.0
        return infra / total

    def time_since_last_graduation_inv(self) -> float:
        subjects = self._git_subjects(200)
        if not subjects:
            return 0.0
        now = time.time()
        # Walk ``git log`` with timestamps to find the most recent
        # subject mentioning "graduate" or "GRADUATED".
        try:
            result = subprocess.run(
                ["git", "log", "-200", "--pretty=format:%ct %s"],
                cwd=str(self._root), capture_output=True, text=True,
                timeout=5.0, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return 0.0
        if result.returncode != 0:
            return 0.0
        for ln in result.stdout.splitlines():
            parts = ln.strip().split(maxsplit=1)
            if len(parts) != 2:
                continue
            ts_str, subject = parts
            if "graduate" in subject.lower() or "GRADUATED" in subject:
                try:
                    ts = float(ts_str)
                except ValueError:
                    continue
                hours = max(0.0, (now - ts) / 3600.0)
                return 1.0 / (hours + 1.0)
        return 0.0

    def open_ops_normalized(self) -> float:
        if self._open_ops_provider is None:
            return 0.0
        try:
            count = int(self._open_ops_provider())
        except Exception:
            return 0.0
        # 16 sensors — if every sensor has one in-flight op we're saturated.
        return min(1.0, max(0.0, count / 16.0))

    def cost_burn_normalized(self) -> float:
        """Fraction of today's cost cap consumed — a **bounded** read.

        3rd starvation tier (Fix 1): the daily cost state is a single
        JSON record, but the 300s cadence previously re-read + fully
        re-parsed it on EVERY cycle. When ``cost_state.json`` grows (a
        rolling ledger), ``json.loads`` is O(filesize) pure-Python CPU
        holding the GIL — the pathological scan. The fix reads the file's
        ``(st_mtime_ns, st_size)`` stat identity first (microseconds, no
        read) and returns the memoized value whenever the file is
        unchanged — so the steady-state cost is a single ``stat()``,
        never a re-parse. A real write (mtime/size change) invalidates
        the cache and re-parses exactly once. NEVER raises."""
        path = self._root / ".jarvis" / "cost_state.json"
        try:
            st = path.stat()
        except OSError:
            # Missing / unreadable — drop any stale memo, documented 0.0.
            self._cost_burn_cache = None
            return 0.0
        stat_key = (st.st_mtime_ns, st.st_size)
        cached = self._cost_burn_cache
        if cached is not None and cached[0] == stat_key:
            return cached[1]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0.0
        try:
            spent = float(payload.get("daily_spent_usd", 0.0))
            cap = float(payload.get("daily_cap_usd", 0.0))
        except (TypeError, ValueError):
            return 0.0
        if cap <= 0.0:
            value = 0.0
        else:
            value = min(1.0, max(0.0, spent / cap))
        self._cost_burn_cache = (stat_key, value)
        return value

    def worktree_orphan_count(self) -> int:
        base = os.environ.get("JARVIS_WORKTREE_BASE")
        if not base:
            return 0
        base_p = Path(base)
        if not base_p.exists():
            return 0
        try:
            return sum(
                1 for entry in base_p.iterdir()
                if entry.is_dir() and entry.name.startswith("unit-")
            )
        except OSError:
            return 0

    def build_bundle(self) -> SignalBundle:
        """Legacy sync entry — retained for backwards compatibility
        with tests and any call-site that needs a synchronous bundle.
        Production posture cycle uses :meth:`build_bundle_async`.

        IMPORTANT 2 — the four summary-derived raters share ONE bounded
        scan (widest window, newest-N) instead of each re-scanning +
        re-parsing the session tree. This is the path the default
        wholesale-offload cycle (``_run_one_cycle_sync``) uses, so the
        shared+bounded scan is exactly where the 4× redundant-scan freeze
        was most acute."""
        ratios = self.commit_ratios()
        shared = self.scan_recent_summaries(
            self._summaries_widest_window_h(), recent_summaries_max(),
        )
        base = baseline_bundle()
        return SignalBundle(
            feat_ratio=ratios["feat"],
            fix_ratio=ratios["fix"],
            refactor_ratio=ratios["refactor"],
            test_docs_ratio=ratios["test_docs"],
            postmortem_failure_rate=self.postmortem_failure_rate(shared),
            iron_gate_reject_rate=self.iron_gate_reject_rate(shared),
            l2_repair_rate=self.l2_repair_rate(shared),
            open_ops_normalized=self.open_ops_normalized(),
            session_lessons_infra_ratio=self.session_lessons_infra_ratio(shared),
            time_since_last_graduation_inv=self.time_since_last_graduation_inv(),
            cost_burn_normalized=self.cost_burn_normalized(),
            worktree_orphan_count=self.worktree_orphan_count(),
            commit_window=commit_window(),
            postmortem_window_h=postmortem_window_h(),
            schema_version=base.schema_version,
        )

    async def build_bundle_async(self) -> SignalBundle:
        """Chunked async signal collection — every collector off-loop.

        Closes v27 (bt-2026-05-27-232749) sink: ``build_bundle`` ran 12
        synchronous signal collectors sequentially in ONE blocking call,
        holding the GIL for 22.56 s on a cold session. Each collector now
        runs off-loop with explicit ``sleep(0)`` cooperative yields between
        them, so the 22.56 s monolithic block becomes short hops that each
        yield the loop a scheduling slot.

        Tier-2b (bt-iso-1783093701, 2nd starvation tier): every collector
        — the git-subprocess commit ratios AND the filesystem summary scans
        — is CONVERGED onto the single unified ``cooperative_fs_io.offload``
        gateway (via ``_offload_signal``). The prior code scattered dispatch
        across a bespoke ``fs_signal_executor`` pool and ad-hoc executor /
        thread calls; that third divergent offload mechanism is killed. One
        pool, one contract, one fail-soft path.
        """
        import asyncio as _asyncio_ls  # noqa: WPS433 — local alias
        from backend.core.ouroboros.telemetry.loop_sink import (
            sink_async as _ls_sink_async,
        )

        # Literal callsite labels (not f-strings) so AST pins +
        # production log greps see the exact string at the call site.
        # Slice 33 Arc 2 Phase 1 — commit_ratios uses async-native
        # subprocess (create_subprocess_exec) instead of to_thread
        # wrapping subprocess.run; no thread-pool slot consumed even
        # during cold-cache 18 s scans.
        async with _ls_sink_async("posture.signal.commit_ratios"):
            ratios = await self.commit_ratios_async()
        await _asyncio_ls.sleep(0)

        # IMPORTANT 2 — ONE bounded scan of .ouroboros/sessions/*/summary.json
        # per cycle (widest window, newest-N), shared across the four
        # summary-derived raters. Previously each of the 4 raters
        # (postmortem/iron_gate/l2_repair/session_lessons) re-scanned +
        # re-parsed the whole session tree independently — 4× redundant IO
        # + json.loads with no count bound (the cost_burn pathology class).
        # The scan is off-loop via the unified substrate; each rater is then
        # pure math over the already-parsed shared list (still dispatched
        # through offload so the per-signal off-loop contract + telemetry
        # labels are preserved).
        async with _ls_sink_async("posture.signal.recent_summaries_scan"):
            shared = await _offload_signal(
                self.scan_recent_summaries,
                self._summaries_widest_window_h(),
                recent_summaries_max(),
            )
            if shared is None:
                shared = []
        await _asyncio_ls.sleep(0)

        # Tier-2b — the 4 filesystem-bound signals
        # (postmortem/iron_gate/l2_repair/session_lessons) now score the
        # SHARED bounded scan (no re-scan) through the single unified
        # ``cooperative_fs_io.offload`` gateway (``_offload_signal`` →
        # advisor-blast thread pool). The bespoke ``fs_signal_executor`` and
        # scattered ``to_thread`` calls are gone — one pool, one contract.
        # Fail-soft: ``_offload_signal`` returns ``None`` on collector fault;
        # each signal falls back to its neutral 0.0 default below.
        async with _ls_sink_async("posture.signal.postmortem_failure_rate"):
            pm = await _offload_signal(self.postmortem_failure_rate, shared)
            if pm is None:
                pm = 0.0
        await _asyncio_ls.sleep(0)

        async with _ls_sink_async("posture.signal.iron_gate_reject_rate"):
            ig = await _offload_signal(self.iron_gate_reject_rate, shared)
            if ig is None:
                ig = 0.0
        await _asyncio_ls.sleep(0)

        async with _ls_sink_async("posture.signal.l2_repair_rate"):
            l2 = await _offload_signal(self.l2_repair_rate, shared)
            if l2 is None:
                l2 = 0.0
        await _asyncio_ls.sleep(0)

        async with _ls_sink_async("posture.signal.open_ops_normalized"):
            oo = await _offload_signal(self.open_ops_normalized)
            if oo is None:
                oo = 0.0
        await _asyncio_ls.sleep(0)

        async with _ls_sink_async("posture.signal.session_lessons_infra_ratio"):
            sl = await _offload_signal(self.session_lessons_infra_ratio, shared)
            if sl is None:
                sl = 0.0
        await _asyncio_ls.sleep(0)

        async with _ls_sink_async("posture.signal.time_since_last_graduation_inv"):
            ts = await _offload_signal(self.time_since_last_graduation_inv)
            if ts is None:
                ts = 0.0
        await _asyncio_ls.sleep(0)

        async with _ls_sink_async("posture.signal.cost_burn_normalized"):
            cb = await _offload_signal(self.cost_burn_normalized)
            if cb is None:
                cb = 0.0
        await _asyncio_ls.sleep(0)

        async with _ls_sink_async("posture.signal.worktree_orphan_count"):
            wo = await _offload_signal(self.worktree_orphan_count)
            if wo is None:
                wo = 0
        base = baseline_bundle()
        return SignalBundle(
            feat_ratio=ratios["feat"],
            fix_ratio=ratios["fix"],
            refactor_ratio=ratios["refactor"],
            test_docs_ratio=ratios["test_docs"],
            postmortem_failure_rate=pm,
            iron_gate_reject_rate=ig,
            l2_repair_rate=l2,
            open_ops_normalized=oo,
            session_lessons_infra_ratio=sl,
            time_since_last_graduation_inv=ts,
            cost_burn_normalized=cb,
            worktree_orphan_count=wo,
            commit_window=commit_window(),
            postmortem_window_h=postmortem_window_h(),
            schema_version=base.schema_version,
        )


# ---------------------------------------------------------------------------
# Override state — in-memory, persisted via audit log
# ---------------------------------------------------------------------------


class OverrideState:
    """Tracks the active operator override, if any. Time-bound.

    Not threadsafe with the observer loop — the observer reads it once
    per cycle; operators mutate via ``/posture override`` (single writer).
    """

    def __init__(self) -> None:
        self._posture: Optional[Posture] = None
        self._until: Optional[float] = None
        self._reason: str = ""
        self._who: str = ""
        self._set_at: Optional[float] = None

    def set(
        self,
        posture: Posture,
        *,
        duration_s: float,
        reason: str,
        who: str = "user",
    ) -> Tuple[float, float]:
        """Activate override. Duration is clamped to override_max_h.

        Returns ``(set_at, until)`` for the audit record.
        """
        max_s = override_max_h() * 3600
        clamped = max(0.0, min(duration_s, max_s))
        now = time.time()
        self._posture = posture
        self._set_at = now
        self._until = now + clamped
        self._reason = reason
        self._who = who
        return now, self._until

    def clear(self) -> None:
        self._posture = None
        self._until = None
        self._reason = ""
        self._who = ""
        self._set_at = None

    def active_posture(self) -> Optional[Posture]:
        """Return the override posture if still active, else clear+return None."""
        if self._posture is None or self._until is None:
            return None
        if time.time() >= self._until:
            # Expired — caller should emit an 'expired' audit record
            return None
        return self._posture

    def snapshot(self) -> Dict[str, Any]:
        return {
            "posture": self._posture.value if self._posture else None,
            "until": self._until,
            "reason": self._reason,
            "who": self._who,
            "set_at": self._set_at,
        }

    def is_expired(self) -> bool:
        if self._posture is None or self._until is None:
            return False
        return time.time() >= self._until


# ---------------------------------------------------------------------------
# PostureObserver — the periodic task
# ---------------------------------------------------------------------------


class PostureObserver:
    """Periodic signal collection + inference + hysteresis + persistence.

    Lifecycle:
      * ``start()`` — spawns the async task
      * ``stop()``  — cancels the task and awaits cleanup
      * ``run_one_cycle()`` — public for tests (no sleep between cycles)

    The observer never blocks the main loop. A failed cycle increments
    ``cycles_failed`` but leaves the task running.
    """

    def __init__(
        self,
        project_root: Path,
        store: PostureStore,
        *,
        inferrer: Optional[DirectionInferrer] = None,
        collector: Optional[SignalCollector] = None,
        override_state: Optional[OverrideState] = None,
        on_change: Optional[Callable[[PostureReading, Optional[PostureReading]], Any]] = None,
    ) -> None:
        self._root = Path(project_root).resolve()
        self._store = store
        self._inferrer = inferrer or DirectionInferrer()
        self._collector = collector or SignalCollector(self._root)
        self._override = override_state or OverrideState()
        self._on_change = on_change
        self._task: Optional[asyncio.Task[Any]] = None
        self._stop_event = asyncio.Event()
        self._cycles_ok = 0
        self._cycles_failed = 0
        self._cycles_skipped_hysteresis = 0
        # Q3 Slice 2 — hydrate from durable side-car so the hysteresis
        # window survives process restarts. Cold start / missing /
        # corrupt / posture-mismatched marker yields None, in which case
        # the cycle's hysteresis check falls back to the legacy
        # ``previous.inferred_at`` proxy (backward-compat behavior).
        self._last_change_at: Optional[float] = self._hydrate_last_change_at()
        # Tier 1 #2 — task-death detection heartbeats. Updated on
        # every cycle so consumers can detect a dead/hung observer
        # task before reading frozen state. Posture health module
        # (posture_health.py) consumes these.
        self._last_cycle_attempt_at_unix: Optional[float] = None
        self._last_cycle_ok_at_unix: Optional[float] = None
        self._consecutive_cycle_failures: int = 0
        # IMPORTANT 3 — monotonic cycle epoch. Incremented on the LOOP
        # thread at the start of every offloaded cycle. The offload worker
        # captures its epoch and re-checks it right before ``write_current``;
        # if a newer cycle has since advanced the epoch (the timed-out
        # cycle whose worker kept running), the stale worker no-ops its
        # write so a late cycle-N reading can't clobber cycle-N+1's newer
        # one. Only ever written on the loop thread (single writer);
        # workers read it under the GIL (atomic int read). ``0`` sentinel
        # (never used as a live epoch — first cycle is ``1``).
        self._cycle_epoch: int = 0

    # ---- Q3 Slice 2 — durable hysteresis state hydration ---------------

    def _hydrate_last_change_at(self) -> Optional[float]:
        """Read the change-marker side-car (paired with ``current``) so a
        process restart doesn't lose hysteresis state. The marker is
        rejected if its recorded posture doesn't match ``current.posture``
        — that filters out legacy observers that wrote ``current`` without
        the marker, plus any partial-write or operator-tampering scenario.
        Failure modes ALL fall through to ``None`` so the legacy
        ``previous.inferred_at`` proxy still kicks in. Never raises."""
        try:
            current = self._store.load_current()
            if current is None:
                return None
            return self._store.load_change_marker_at(
                expected_posture=current.posture,
            )
        except Exception:  # noqa: BLE001 — defensive at boot
            logger.debug(
                "[PostureObserver] hydrate_last_change_at failed",
                exc_info=True,
            )
            return None

    # ---- lifecycle --------------------------------------------------------

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---- Tier 1 #2 — task-death detection -------------------------------

    def task_health_snapshot(self) -> Dict[str, Any]:
        """Read-only snapshot of observer task health for the
        ``posture_health`` module's classifier. Returns the four
        heartbeat fields + lifecycle predicates. NEVER raises.

        Consumers should NOT classify health themselves — the
        classifier in ``posture_health.evaluate_observer_health``
        owns the policy (DEGRADED threshold, env knobs, sentinel
        handling). This method just exposes the raw signals."""
        try:
            return {
                "is_running": self.is_running(),
                "task_done": (
                    self._task is not None and self._task.done()
                ),
                "task_started": self._task is not None,
                "last_cycle_attempt_at_unix": (
                    self._last_cycle_attempt_at_unix
                ),
                "last_cycle_ok_at_unix": self._last_cycle_ok_at_unix,
                "consecutive_cycle_failures": (
                    self._consecutive_cycle_failures
                ),
                "cycles_ok": self._cycles_ok,
                "cycles_failed": self._cycles_failed,
            }
        except Exception:  # noqa: BLE001 — defensive
            return {
                "is_running": False,
                "task_done": False,
                "task_started": False,
                "last_cycle_attempt_at_unix": None,
                "last_cycle_ok_at_unix": None,
                "consecutive_cycle_failures": 0,
                "cycles_ok": 0,
                "cycles_failed": 0,
            }

    def start(self) -> None:
        if not _inferrer_enabled():
            logger.info("[PostureObserver] master flag off; not starting")
            return
        if self.is_running():
            return
        self._stop_event.clear()
        self._task = asyncio.get_event_loop().create_task(self._run_forever())
        logger.info(
            "[PostureObserver] started interval=%.1fs window=%.1fs",
            observer_interval_s(), hysteresis_window_s(),
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ---- arc-context input (P0.5 Slice 2) ---------------------------------

    async def _read_lss_one_liner(self) -> str:
        """Best-effort read of the most-recent LastSessionSummary one-liner.

        Returns ``""`` when LSS is unavailable, the helper raises, or no
        prior session exists. Never raises — the arc-context branch is
        observability + small bounded nudge only."""
        try:
            from backend.core.ouroboros.governance.last_session_summary import (
                get_default_summary,
            )
            lss = get_default_summary(self._root)
            line = await lss.format_for_prompt() or ""
            return str(line)
        except Exception:
            return ""

    def _read_lss_one_liner_sync(self) -> str:
        """Synchronous sibling of :meth:`_read_lss_one_liner` for the
        wholesale-offload path (Fix 2), which runs entirely inside a
        worker thread with no event loop. Uses LSS's sync formatter.
        Never raises — arc-context is a bounded best-effort nudge."""
        try:
            from backend.core.ouroboros.governance.last_session_summary import (
                get_default_summary,
            )
            lss = get_default_summary(self._root)
            line = lss.format_for_prompt_sync() or ""
            return str(line)
        except Exception:
            return ""

    # ---- main loop --------------------------------------------------------

    async def _run_forever(self) -> None:
        interval = observer_interval_s()
        while not self._stop_event.is_set():
            # Tier 1 #2 — record cycle attempt before run for hung-
            # cycle detection (run_one_cycle has no internal timeout
            # so it could block indefinitely on a stuck collector).
            self._last_cycle_attempt_at_unix = time.time()
            try:
                await self.run_one_cycle()
                # Tier 1 #2 — successful cycle resets the failure
                # counter and updates the OK heartbeat. Consumers
                # use last_cycle_ok_at_unix to detect DEGRADED state.
                self._last_cycle_ok_at_unix = time.time()
                self._consecutive_cycle_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._cycles_failed += 1
                self._consecutive_cycle_failures += 1
                logger.exception("[PostureObserver] cycle_failed")
            # Sleep-or-stop
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # ---- one cycle --------------------------------------------------------

    async def run_one_cycle(self) -> Optional[PostureReading]:
        """Collect signals, infer, hysteresis-gate, persist. Returns the
        reading that was persisted (or None if collection timed out)."""
        # Slice 33 Arc 0 — diagnostic only. Periodic posture cycle
        # ~5 min cadence (env: JARVIS_POSTURE_OBSERVER_INTERVAL_S).
        # If it shows in the v27 leaderboard, posture collection or
        # inference is the sink.
        from backend.core.ouroboros.telemetry.loop_sink import (
            sink_async as _ls_sink_async,
        )
        async with _ls_sink_async("posture_observer.run_one_cycle"):
            if wholesale_offload_enabled():
                return await self._run_one_cycle_offloaded()
            return await self._run_one_cycle_impl()

    # ---- Fix 2 — wholesale off-loop cycle -------------------------------

    async def _run_one_cycle_offloaded(self) -> Optional[PostureReading]:
        """Run the ENTIRE cadence tick off the primary event loop.

        3rd starvation tier (Fix 2): the whole synchronous cycle
        (``_run_one_cycle_sync`` — collect + arc-context + infer +
        hysteresis + persist) is dispatched in ONE call through the
        shared ``cooperative_fs_io.offload`` substrate. THREAD pool
        (``cpu_bound=False``) — NOT a process pool — because the cycle
        touches live in-memory state a separate process could neither
        read nor marshal mutations back from: the injected
        ``open_ops_provider`` callable, the ``_commit_ratios_cache`` /
        ``_cost_burn_cache`` memos, the ``PostureStore`` (its
        ``threading.Lock`` + atomic temp+rename writer), the
        ``DirectionInferrer``, the ``OverrideState`` and the
        ``on_change`` orchestrator callback. Fix 1 removes the GIL-hold
        pathology, so the thread path genuinely frees the loop.

        RACES: the cycle's only externally-visible effect — the flip of
        ``current`` — lands via ``PostureStore.write_current`` under the
        store's lock + atomic ``os.replace``. A concurrent reader
        (``load_current``) takes the same lock and reads an atomically
        renamed file, so it always sees a prior-or-complete immutable
        ``PostureReading``, never a partial bundle. The offloaded thread
        never re-acquires a lock the caller holds (the loop holds none of
        the store's locks), so no deadlock.

        FAIL-SOFT: ``offload`` traps any runtime raise and returns an
        ``OffloadError`` (never re-raised); a wall-clock timeout leaves
        the prior bundle untouched. Either way we bump ``cycles_failed``,
        keep the prior posture, and return ``None`` — never raising into
        the cadence loop or the orchestrator.
        """
        # NOTE (MINOR): the off-loop guarantee here depends on the
        # ``cooperative_fs_io`` master switch ``JARVIS_COOPERATIVE_FS_IO_ENABLED``.
        # When THAT master is OFF, ``offload(...)`` degrades to running the
        # whole sync cycle INLINE on the loop (its documented byte-identical
        # rollback) — reintroducing the very starvation this tier closes,
        # even while ``JARVIS_POSTURE_WHOLESALE_OFFLOAD_ENABLED`` still
        # reports on. Wholesale-offload is a consumer of the substrate, not
        # a second dispatch mechanism; both masters must be on for the loop
        # to actually stay free.
        from backend.core.ouroboros.governance.cooperative_fs_io import (
            offload as _offload,
            is_offload_error as _is_offload_error,
        )
        # IMPORTANT 3 — stamp this cycle's epoch on the LOOP thread BEFORE
        # dispatch. A prior timed-out cycle whose worker is still running
        # will see this newer value at its write and no-op (stale-write
        # guard). Single writer (loop thread), so the increment is race-free.
        self._cycle_epoch += 1
        my_epoch = self._cycle_epoch
        try:
            result = await asyncio.wait_for(
                _offload(self._run_one_cycle_sync, my_epoch, cpu_bound=False),
                timeout=collector_timeout_s(),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[PostureObserver] offloaded cycle timeout after %.1fs",
                collector_timeout_s(),
            )
            self._cycles_failed += 1
            return None
        if _is_offload_error(result):
            logger.debug(
                "[PostureObserver] offloaded cycle fail-soft: %r", result,
            )
            self._cycles_failed += 1
            return None
        if result is None:
            return None
        # CRITICAL — fire the loop-affine ``on_change`` callback HERE, back
        # on the loop thread, NOT inside the offload worker. The decision
        # (whether a real transition landed) was computed thread-safely in
        # ``_process_bundle`` and returned in ``result.on_change_args``.
        self._fire_on_change(result.on_change_args)
        return result.to_persist

    def _fire_on_change(
        self,
        on_change_args: Optional[Tuple[PostureReading, Optional[PostureReading]]],
    ) -> None:
        """Invoke the injected ``on_change`` callback ON THE CURRENT
        (loop) thread. MUST only ever be called from the event loop — in
        production it reaches ``loop.call_soon`` via StreamEventBroker,
        which is not thread-safe off-loop. Fail-soft: a raising hook never
        propagates into the cadence loop."""
        if on_change_args is None or self._on_change is None:
            return
        to_persist, previous = on_change_args
        try:
            self._on_change(to_persist, previous)
        except Exception:
            logger.debug("[PostureObserver] on_change hook raised", exc_info=True)

    def _run_one_cycle_sync(
        self, epoch: Optional[int] = None,
    ) -> Optional[_CycleOutcome]:
        """Fully synchronous cadence tick — runs inside the offload
        worker thread (Fix 2). Uses the legacy sync ``build_bundle``
        (each collector's own subprocess/fs timeouts bound the work) and
        shares the ``_process_bundle`` tail with the async path. NEVER
        called on the event loop directly; blocking here blocks a worker
        thread, never the loop. Returns a :class:`_CycleOutcome` so the
        loop-affine ``on_change`` can be marshalled back to the loop by
        the caller (the callback is NOT invoked in this thread)."""
        bundle = self._collector.build_bundle()
        if bundle is None:
            return None
        lss_one_liner = self._read_lss_one_liner_sync()
        return self._process_bundle(
            bundle, lss_one_liner=lss_one_liner, epoch=epoch,
        )

    async def _run_one_cycle_impl(self) -> Optional[PostureReading]:
        """Legacy async cadence tick (master-off rollback). Per-signal
        chunked-async collect via ``build_bundle_async`` (Tier-2b
        ``_offload_signal``), then the shared sync ``_process_bundle``
        tail. Runs entirely ON the loop, so ``on_change`` fires inline
        here (still on the loop) — byte-behavior-identical to the pre-fix
        legacy path. ``epoch=None`` disables the stale-write guard (this
        path is fully awaited, no overlapping cycles to race)."""
        bundle = await self._collect_with_timeout()
        if bundle is None:
            return None
        lss_one_liner = await self._read_lss_one_liner()
        outcome = self._process_bundle(
            bundle, lss_one_liner=lss_one_liner, epoch=None,
        )
        if outcome is None:
            return None
        self._fire_on_change(outcome.on_change_args)
        return outcome.to_persist

    def _process_bundle(
        self, bundle: SignalBundle, *, lss_one_liner: str = "",
        epoch: Optional[int] = None,
    ) -> Optional[_CycleOutcome]:
        """Shared SYNC tail — arc-context, infer, hysteresis, persist.

        Purely synchronous and THREAD-SAFE: called either off-loop inside
        the offload worker thread (Fix 2 default path) or inline after the
        legacy async collect. All shared-state mutation happens here under
        the ``PostureStore`` lock + atomic writes (see
        ``_run_one_cycle_offloaded`` for the race/atomicity contract).

        CRITICAL — it does NOT invoke the loop-affine ``on_change`` callback
        (that would run on the worker thread → non-thread-safe
        ``loop.call_soon`` in production). Instead it computes the decision
        and returns the callback payload in :class:`_CycleOutcome`; the
        caller fires it back on the loop.

        IMPORTANT 3 — when ``epoch`` is provided (offloaded path), the
        ``write_current`` is guarded: if a newer cycle has advanced
        ``self._cycle_epoch`` since this cycle was dispatched, this stale
        cycle no-ops its write (and marshals no ``on_change``) so it can't
        clobber the newer reading. ``epoch=None`` (legacy inline path) has
        no overlap and skips the guard entirely."""
        # P0.5 Slice 2 — build arc-context (best-effort, never raises) and
        # pass to inferrer. Helper is observability-only by default; score
        # adjustment fires only when JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED=true.
        arc_ctx = None
        try:
            arc_ctx = build_arc_context(self._root, lss_one_liner=lss_one_liner)
        except Exception:
            logger.debug("[PostureObserver] arc_context build skipped", exc_info=True)
        reading = self._inferrer.infer(bundle, arc_context=arc_ctx)
        # Single observability line for the arc-context state per cycle.
        if arc_ctx is not None:
            logger.info(
                "[PostureObserver] arc_context=%s applied=%s",
                json.dumps(arc_ctx.to_log_dict(), sort_keys=True),
                _arc_context_enabled(),
            )

        # Append to history regardless of hysteresis (we want the raw
        # signal trail; hysteresis only masks `current`).
        self._store.append_history(reading)

        # Check for override expiry first — emit audit if applicable.
        if self._override.is_expired():
            snap = self._override.snapshot()
            self._store.append_audit(
                OverrideRecord(
                    event="expired",
                    posture=Posture.from_str(snap["posture"]) if snap["posture"] else None,
                    who=snap.get("who", "user"),
                    at=time.time(),
                    until=snap.get("until"),
                    reason=snap.get("reason", ""),
                )
            )
            self._override.clear()

        # Override wins — current is a synthetic reading reflecting the
        # overridden posture, but original evidence preserved so
        # `/posture explain` still shows the underlying signals.
        active = self._override.active_posture()
        if active is not None:
            # Current reflects override posture; underlying inference stays
            # in history for observability.
            to_persist = reading  # keep original signal evidence
        else:
            to_persist = reading

        # Hysteresis check — does the new reading get promoted to
        # ``current``?
        previous = self._store.load_current()
        now = time.time()
        window = hysteresis_window_s()
        bypass = high_confidence_bypass()

        promote = False
        if previous is None:
            promote = True  # cold start always promotes
        elif active is not None:
            promote = True  # override always refreshes current
        elif to_persist.posture is previous.posture:
            # Same posture → refresh current (carries new confidence)
            promote = True
        elif reading.confidence >= bypass:
            promote = True
        elif self._last_change_at is None:
            # No prior change recorded yet — use previous.inferred_at as
            # a proxy; promote if window elapsed.
            if now - previous.inferred_at >= window:
                promote = True
        else:
            if now - self._last_change_at >= window:
                promote = True

        on_change_args: Optional[
            Tuple[PostureReading, Optional[PostureReading]]
        ] = None
        if promote:
            # IMPORTANT 3 — stale-write guard. A cycle that timed out on the
            # loop keeps running in its worker; if a newer cycle has since
            # advanced the epoch, this stale cycle must NOT write (it would
            # clobber the newer reading) and must NOT marshal on_change.
            if epoch is not None and epoch != self._cycle_epoch:
                logger.debug(
                    "[PostureObserver] stale cycle epoch=%s current=%s — "
                    "suppressing write + on_change", epoch, self._cycle_epoch,
                )
                return _CycleOutcome(to_persist=to_persist, on_change_args=None)
            # Q3 Slice 2 — pair the marker write with current ONLY on real
            # posture transitions. Same-posture refreshes pass marker=None
            # so the side-car retains the timestamp at which this posture
            # actually became authoritative — that's the value we want on
            # restart, not the most recent reading time.
            is_change = (
                previous is None
                or previous.posture is not to_persist.posture
            )
            if is_change:
                self._last_change_at = now
                self._store.write_current(to_persist, change_marker_at=now)
                # CRITICAL — defer the loop-affine callback to the caller
                # (fired on the loop, never on this possibly-worker thread).
                on_change_args = (to_persist, previous)
            else:
                self._store.write_current(to_persist)
            self._cycles_ok += 1
        else:
            self._cycles_skipped_hysteresis += 1

        return _CycleOutcome(
            to_persist=to_persist, on_change_args=on_change_args,
        )

    async def _collect_with_timeout(self) -> Optional[SignalBundle]:
        """Run the collector with a timeout guard.

        Slice 33 Arc 1 (v27 LoopSink-confirmed fix): uses the
        chunked-async ``build_bundle_async()`` which dispatches each
        of the 12 individual signal collectors via separate
        ``asyncio.to_thread`` calls with explicit cooperative yields
        between them. Closes the 22.56 s monolithic GIL hold v27
        named as the dominant on-loop sink.

        Legacy synchronous ``build_bundle`` path is preserved on the
        collector for backwards compatibility but no longer used in
        the production cycle.
        """
        try:
            return await asyncio.wait_for(
                self._collector.build_bundle_async(),
                timeout=collector_timeout_s(),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[PostureObserver] collector timeout after %.1fs",
                collector_timeout_s(),
            )
            self._cycles_failed += 1
            return None

    # ---- diagnostics ------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        return {
            "running": self.is_running(),
            "cycles_ok": self._cycles_ok,
            "cycles_failed": self._cycles_failed,
            "cycles_skipped_hysteresis": self._cycles_skipped_hysteresis,
            "last_change_at": self._last_change_at,
            "override_active": self._override.active_posture() is not None,
            "interval_s": observer_interval_s(),
            "hysteresis_window_s": hysteresis_window_s(),
        }


# ---------------------------------------------------------------------------
# Module-level singletons for ease-of-integration
# ---------------------------------------------------------------------------


import threading as _threading  # noqa: E402  — late alias for singleton guard
# RLock (reentrant) because get_default_observer() acquires this lock
# and then calls get_default_store() which acquires it again. A plain
# threading.Lock would deadlock on that recursive acquisition — bug
# surfaced by Slice 5 Arc A integration tests on 2026-04-21.
_singleton_guard = _threading.RLock()
_singleton_observer: Optional[PostureObserver] = None
_singleton_store: Optional[PostureStore] = None


def get_default_store(base_dir: Optional[Path] = None) -> PostureStore:
    global _singleton_store
    with _singleton_guard:
        if _singleton_store is None:
            root = base_dir or Path.cwd() / ".jarvis"
            _singleton_store = PostureStore(root)
        return _singleton_store


def reset_default_store() -> None:
    global _singleton_store
    with _singleton_guard:
        _singleton_store = None


def get_default_observer(
    project_root: Optional[Path] = None,
) -> PostureObserver:
    global _singleton_observer
    with _singleton_guard:
        if _singleton_observer is None:
            root = project_root or Path.cwd()
            store = get_default_store(root / ".jarvis")
            _singleton_observer = PostureObserver(root, store)
        return _singleton_observer


def reset_default_observer() -> None:
    global _singleton_observer
    with _singleton_guard:
        _singleton_observer = None


__all__ = [
    "OverrideState",
    "PostureObserver",
    "SignalCollector",
    "collector_timeout_s",
    "commit_window",
    "get_default_observer",
    "get_default_store",
    "high_confidence_bypass",
    "hysteresis_window_s",
    "observer_interval_s",
    "override_max_h",
    "postmortem_window_h",
    "recent_summaries_max",
    "reset_default_observer",
    "reset_default_store",
    "wholesale_offload_enabled",
]
