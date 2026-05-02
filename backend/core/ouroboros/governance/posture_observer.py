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
from typing import Any, Callable, Dict, List, Optional, Tuple

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


_CONV_COMMIT_RE = re.compile(
    r"^(?P<type>feat|fix|refactor|test|docs|chore|perf|style|build|ci|revert)"
    r"(?:\([^)]+\))?!?:",
    re.IGNORECASE,
)


# Callable the wiring layer can inject to surface in-flight op count.
OpenOpsProvider = Callable[[], int]


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

    def _git_subjects(self, n: int) -> List[str]:
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

    def recent_summaries(self, window_h: int) -> List[Dict[str, Any]]:
        """Parse ``.ouroboros/sessions/*/summary.json`` within window."""
        sessions_dir = self._root / ".ouroboros" / "sessions"
        if not sessions_dir.exists():
            return []
        cutoff = time.time() - (window_h * 3600)
        out: List[Dict[str, Any]] = []
        try:
            for sess in sessions_dir.iterdir():
                if not sess.is_dir():
                    continue
                summary = sess / "summary.json"
                if not summary.exists():
                    continue
                try:
                    mtime = summary.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                try:
                    out.append(json.loads(summary.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        except OSError:
            return []
        return out

    def postmortem_failure_rate(self) -> float:
        summaries = self.recent_summaries(postmortem_window_h())
        if not summaries:
            return 0.0
        total_ops = 0
        failed_ops = 0
        for s in summaries:
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

    def iron_gate_reject_rate(self) -> float:
        summaries = self.recent_summaries(24)
        if not summaries:
            return 0.0
        total = 0
        rejects = 0
        for s in summaries:
            events = s.get("event_counts") or {}
            try:
                total += int(events.get("generate_total", 0))
                rejects += int(events.get("iron_gate_reject", 0))
            except (TypeError, ValueError):
                continue
        if total == 0:
            return 0.0
        return min(1.0, rejects / total)

    def l2_repair_rate(self) -> float:
        summaries = self.recent_summaries(24)
        if not summaries:
            return 0.0
        total = 0
        repairs = 0
        for s in summaries:
            events = s.get("event_counts") or {}
            try:
                total += int(events.get("apply_total", 0))
                repairs += int(events.get("l2_invoked", 0))
            except (TypeError, ValueError):
                continue
        if total == 0:
            return 0.0
        return min(1.0, repairs / total)

    def session_lessons_infra_ratio(self) -> float:
        summaries = self.recent_summaries(postmortem_window_h())
        if not summaries:
            return 0.0
        total = 0
        infra = 0
        for s in summaries:
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
        path = self._root / ".jarvis" / "cost_state.json"
        if not path.exists():
            return 0.0
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
            return 0.0
        return min(1.0, max(0.0, spent / cap))

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
        ratios = self.commit_ratios()
        base = baseline_bundle()
        return SignalBundle(
            feat_ratio=ratios["feat"],
            fix_ratio=ratios["fix"],
            refactor_ratio=ratios["refactor"],
            test_docs_ratio=ratios["test_docs"],
            postmortem_failure_rate=self.postmortem_failure_rate(),
            iron_gate_reject_rate=self.iron_gate_reject_rate(),
            l2_repair_rate=self.l2_repair_rate(),
            open_ops_normalized=self.open_ops_normalized(),
            session_lessons_infra_ratio=self.session_lessons_infra_ratio(),
            time_since_last_graduation_inv=self.time_since_last_graduation_inv(),
            cost_burn_normalized=self.cost_burn_normalized(),
            worktree_orphan_count=self.worktree_orphan_count(),
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

    def _read_lss_one_liner(self) -> str:
        """Best-effort read of the most-recent LastSessionSummary one-liner.

        Returns ``""`` when LSS is unavailable, the helper raises, or no
        prior session exists. Never raises — the arc-context branch is
        observability + small bounded nudge only."""
        try:
            from backend.core.ouroboros.governance.last_session_summary import (
                get_default_summary,
            )
            lss = get_default_summary(self._root)
            line = lss.format_for_prompt() or ""
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
        bundle = await self._collect_with_timeout()
        if bundle is None:
            return None
        # P0.5 Slice 2 — build arc-context (best-effort, never raises) and
        # pass to inferrer. Helper is observability-only by default; score
        # adjustment fires only when JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED=true.
        arc_ctx = None
        try:
            lss_one_liner = self._read_lss_one_liner()
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

        if promote:
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
                if self._on_change is not None:
                    try:
                        self._on_change(to_persist, previous)
                    except Exception:
                        logger.debug("[PostureObserver] on_change hook raised", exc_info=True)
            else:
                self._store.write_current(to_persist)
            self._cycles_ok += 1
        else:
            self._cycles_skipped_hysteresis += 1

        return to_persist

    async def _collect_with_timeout(self) -> Optional[SignalBundle]:
        """Run the synchronous collector with a timeout guard.

        Collectors are IO-bound (subprocess / file read); wrap in
        ``asyncio.to_thread`` + ``wait_for`` so one slow collector can't
        freeze the observer loop.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._collector.build_bundle),
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
    "reset_default_observer",
    "reset_default_store",
]
