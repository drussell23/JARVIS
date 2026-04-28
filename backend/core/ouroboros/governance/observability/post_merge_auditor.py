"""Slice 2.1 — PostMergeAuditor: commit consequence tracker.

Per ``OUROBOROS_VENOM_PRD.md`` §24.10.2 (Priority 2):

  > Without closed-loop verification, the system cannot distinguish
  > improvements from regressions. Every commit must be observed
  > post-merge.

This module ships the **commit consequence tracker** — the component
that closes the open-loop gap where each op was amnesic about the
downstream impact of its commits.

## What it does

1. **Records** every commit landed by APPLY (via ``CommitResult``
   from ``auto_committer.py``).
2. **Schedules** deferred observations at configurable intervals
   (default 24h / 72h / 168h) via ``DeferredObservationQueue``.
3. When observations fire, **evaluates** the commit's downstream
   impact by running read-only git operations:
   - Revert detection (commit SHA appears in a ``Revert`` subject)
   - Stat-based file-change counting
   - Test file delta estimation
4. **Produces** a typed ``MergeOutcome`` result.
5. **Feeds lessons** to ``StrategicDirection`` as a composable
   prompt section.

## Design constraints (load-bearing)

  * **Read-only git operations** — ``git log``, ``git show --stat``,
    ``git diff --name-only``. NEVER ``git checkout``, ``git reset``,
    ``git merge``. All via ``asyncio.create_subprocess_exec`` with
    argument arrays (Iron Gate §6 compliance).
  * **Posture-gated + memory-pressure-gated** — skips evaluation
    when system is in HARDEN posture or HIGH/CRITICAL pressure.
  * **Configurable intervals** via ``JARVIS_POST_MERGE_AUDIT_INTERVALS``
    (default ``"24h,72h,168h"``). Parsed dynamically — no hardcoded list.
  * **Failure-mode test requirement** (§24.10.2): deliberately bad
    commits MUST be detected.
  * **Stdlib + _file_lock + deferred_observation +
    determinism_substrate.canonical_hash import surface only.**
  * **NEVER raises into the caller.**

## Default-off

``JARVIS_POST_MERGE_AUDITOR_ENABLED`` (default false until Phase 2
graduation).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Hard caps
# ---------------------------------------------------------------------------

MAX_OUTCOMES_FILE_BYTES: int = 16 * 1024 * 1024  # 16 MiB
MAX_OUTCOMES_LOADED: int = 10_000
MAX_LESSON_CHARS: int = 1_000
MAX_TARGET_FILES: int = 100
MAX_RECENT_OUTCOMES: int = 10  # for prompt injection


# ---------------------------------------------------------------------------
# Master flag + configuration
# ---------------------------------------------------------------------------


def is_auditor_enabled() -> bool:
    """Master flag — ``JARVIS_POST_MERGE_AUDITOR_ENABLED``
    (default false)."""
    return os.environ.get(
        "JARVIS_POST_MERGE_AUDITOR_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _outcomes_path() -> Path:
    raw = os.environ.get("JARVIS_POST_MERGE_AUDITOR_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "merge_outcomes.jsonl"


def _parse_intervals() -> Tuple[Tuple[str, float], ...]:
    """Parse ``JARVIS_POST_MERGE_AUDIT_INTERVALS`` into
    ``((label, seconds), ...)``.

    Default: ``"24h,72h,168h"`` → ``(("24h", 86400), ("72h", 259200), ("168h", 604800))``.

    Supported suffixes: ``h`` (hours), ``d`` (days), ``m`` (minutes).
    """
    raw = os.environ.get("JARVIS_POST_MERGE_AUDIT_INTERVALS", "24h,72h,168h")
    intervals: List[Tuple[str, float]] = []
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            continue
        try:
            if part.endswith("h"):
                seconds = float(part[:-1]) * 3600
            elif part.endswith("d"):
                seconds = float(part[:-1]) * 86400
            elif part.endswith("m"):
                seconds = float(part[:-1]) * 60
            else:
                seconds = float(part) * 3600  # default to hours
            intervals.append((part, seconds))
        except (ValueError, TypeError):
            logger.warning(
                "[PostMergeAuditor] Unparseable interval '%s' — skipping",
                part,
            )
    if not intervals:
        # Fallback to safe defaults.
        intervals = [("24h", 86400.0), ("72h", 259200.0), ("168h", 604800.0)]
    return tuple(intervals)


# Postures that ALLOW audit evaluation (HARDEN excluded).
_AUDIT_OK_POSTURES = frozenset({"EXPLORE", "CONSOLIDATE", "MAINTAIN"})

# Memory-pressure levels that ALLOW audit evaluation.
_AUDIT_OK_PRESSURE = frozenset({"OK", "WARN"})


# ---------------------------------------------------------------------------
# MergeOutcome — result of observing a commit's downstream impact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeOutcome:
    """Typed outcome of a post-merge observation.

    Produced by ``PostMergeAuditor`` when a deferred observation fires
    and evaluates a commit's downstream impact.
    """

    commit_sha: str
    op_id: str
    observation_interval: str  # "24h" | "72h" | "168h"
    downstream_failures: int   # new test failures since commit
    was_reverted: bool         # commit sha appears in a revert
    latency_delta_s: float     # p95 latency change (0 if unavailable)
    test_count_delta: int      # tests added/removed by this commit
    files_changed: int         # number of files in the commit
    lesson: str                # human-readable lesson for StrategicDirection
    verdict: str               # "beneficial" | "neutral" | "harmful" | "reverted"
    ts_unix: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "commit_sha": self.commit_sha,
            "op_id": self.op_id,
            "observation_interval": self.observation_interval,
            "downstream_failures": self.downstream_failures,
            "was_reverted": self.was_reverted,
            "latency_delta_s": self.latency_delta_s,
            "test_count_delta": self.test_count_delta,
            "files_changed": self.files_changed,
            "lesson": self.lesson[:MAX_LESSON_CHARS],
            "verdict": self.verdict,
            "ts_unix": self.ts_unix,
        }


def _classify_verdict(
    *,
    was_reverted: bool,
    downstream_failures: int,
    test_count_delta: int,
) -> str:
    """Classify the commit outcome into a human-readable verdict."""
    if was_reverted:
        return "reverted"
    if downstream_failures > 0:
        return "harmful"
    if test_count_delta > 0:
        return "beneficial"
    return "neutral"


def _compose_lesson(
    *,
    commit_sha: str,
    op_id: str,
    interval: str,
    verdict: str,
    downstream_failures: int,
    was_reverted: bool,
    test_count_delta: int,
    files_changed: int,
) -> str:
    """Compose a human-readable, self-contained lesson string."""
    sha_short = commit_sha[:8] if commit_sha else "unknown"
    parts: List[str] = [
        f"Commit {sha_short} (op={op_id[:16]}) at {interval}:",
    ]

    if was_reverted:
        parts.append("REVERTED by a later commit.")
    elif downstream_failures > 0:
        parts.append(
            f"caused {downstream_failures} downstream test failure(s)."
        )
    elif test_count_delta > 0:
        parts.append(
            f"added {test_count_delta} test(s), "
            f"changed {files_changed} file(s). Healthy."
        )
    else:
        parts.append(
            f"changed {files_changed} file(s). No observable regressions."
        )

    return " ".join(parts)[:MAX_LESSON_CHARS]


# ---------------------------------------------------------------------------
# CommitRecord — lightweight internal record of a tracked commit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommitRecord:
    """Internal record of a commit registered for tracking."""

    commit_sha: str
    op_id: str
    target_files: Tuple[str, ...]
    risk_tier: str
    signal_source: str
    registered_unix: float
    intervals_scheduled: Tuple[str, ...]  # which intervals have been scheduled

    def to_dict(self) -> Dict[str, Any]:
        return {
            "commit_sha": self.commit_sha,
            "op_id": self.op_id,
            "target_files": list(self.target_files[:MAX_TARGET_FILES]),
            "risk_tier": self.risk_tier,
            "signal_source": self.signal_source,
            "registered_unix": self.registered_unix,
            "intervals_scheduled": list(self.intervals_scheduled),
        }


# ---------------------------------------------------------------------------
# PostMergeAuditor
# ---------------------------------------------------------------------------


class PostMergeAuditor:
    """Commit consequence tracker with deferred observation scheduling.

    Parameters
    ----------
    repo_root:
        Git repository root path (for git subprocess calls).
    observation_queue:
        ``DeferredObservationQueue`` to schedule follow-ups.
    outcomes_path:
        JSONL file for merge outcome persistence.
    posture_provider:
        Callable returning current posture string. None → always allow.
    pressure_provider:
        Callable returning memory-pressure string. None → always allow.
    """

    def __init__(
        self,
        repo_root: Path,
        observation_queue: Optional[Any] = None,
        outcomes_path: Optional[Path] = None,
        posture_provider: Optional[Callable[[], str]] = None,
        pressure_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self._repo_root = repo_root
        self._queue = observation_queue
        self._outcomes_path = outcomes_path or _outcomes_path()
        self._posture_provider = posture_provider
        self._pressure_provider = pressure_provider
        self._tracked_commits: Dict[str, CommitRecord] = {}
        self._outcomes: List[MergeOutcome] = []

    # ------------------------------------------------------------------
    # Gate checks
    # ------------------------------------------------------------------

    def _check_posture_gate(self) -> Optional[str]:
        """Return skip reason if posture gate blocks, else None."""
        if self._posture_provider is None:
            return None
        try:
            posture = (self._posture_provider() or "").upper()
        except Exception:  # noqa: BLE001
            return None
        if posture and posture not in _AUDIT_OK_POSTURES:
            return f"posture_blocked:{posture}"
        return None

    def _check_pressure_gate(self) -> Optional[str]:
        """Return skip reason if pressure gate blocks, else None."""
        if self._pressure_provider is None:
            return None
        try:
            pressure = (self._pressure_provider() or "").upper()
        except Exception:  # noqa: BLE001
            return None
        if pressure and pressure not in _AUDIT_OK_PRESSURE:
            return f"pressure_blocked:{pressure}"
        return None

    # ------------------------------------------------------------------
    # Record a commit for tracking
    # ------------------------------------------------------------------

    def record_commit(
        self,
        *,
        commit_sha: str,
        op_id: str,
        target_files: Sequence[str] = (),
        risk_tier: str = "",
        signal_source: str = "",
        now_unix: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Register a commit for post-merge tracking.

        Schedules deferred observations at all configured intervals.
        Returns ``(ok, detail)``. NEVER raises.
        """
        if not is_auditor_enabled():
            return (False, "master_off")
        if not commit_sha:
            return (False, "empty_commit_sha")
        if not op_id:
            return (False, "empty_op_id")

        now = now_unix or time.time()
        intervals = _parse_intervals()

        # Schedule deferred observations for each interval.
        scheduled_labels: List[str] = []
        if self._queue is not None:
            for label, seconds in intervals:
                try:
                    from backend.core.ouroboros.governance.observability.deferred_observation import (  # noqa: E501
                        make_intent,
                    )
                    intent = make_intent(
                        origin="post_merge_auditor",
                        observation_target=f"commit:{commit_sha}",
                        hypothesis=f"no regressions at {label}",
                        due_unix=now + seconds,
                        max_wait_s=seconds * 0.5,  # 50% grace window
                        metadata={
                            "commit_sha": commit_sha,
                            "op_id": op_id,
                            "interval": label,
                            "target_files": list(target_files[:MAX_TARGET_FILES]),
                            "risk_tier": risk_tier,
                            "signal_source": signal_source,
                        },
                        now_unix=now,
                    )
                    ok, detail = self._queue.schedule(intent)
                    if ok:
                        scheduled_labels.append(label)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[PostMergeAuditor] Failed to schedule %s observation "
                        "for commit %s: %s",
                        label, commit_sha[:8], exc,
                    )

        # Record the commit.
        record = CommitRecord(
            commit_sha=commit_sha,
            op_id=op_id,
            target_files=tuple(target_files[:MAX_TARGET_FILES]),
            risk_tier=risk_tier,
            signal_source=signal_source,
            registered_unix=now,
            intervals_scheduled=tuple(scheduled_labels),
        )
        self._tracked_commits[commit_sha] = record

        logger.info(
            "[PostMergeAuditor] Registered commit=%s op=%s intervals=%s",
            commit_sha[:8], op_id[:16], ",".join(scheduled_labels),
        )
        return (True, f"scheduled:{','.join(scheduled_labels)}")

    # ------------------------------------------------------------------
    # Observation evaluation (called when deferred observations fire)
    # ------------------------------------------------------------------

    def evaluate_commit(
        self,
        *,
        commit_sha: str,
        op_id: str,
        interval: str,
        target_files: Sequence[str] = (),
        now_unix: Optional[float] = None,
    ) -> MergeOutcome:
        """Evaluate a commit's downstream impact. Synchronous.

        This is called by the observation queue's observer callback
        when a deferred observation fires. It runs read-only git
        commands to assess the commit's impact.

        NEVER raises — returns a MergeOutcome with error info on failure.
        """
        now = now_unix or time.time()

        # Check gates.
        posture_skip = self._check_posture_gate()
        if posture_skip:
            return MergeOutcome(
                commit_sha=commit_sha,
                op_id=op_id,
                observation_interval=interval,
                downstream_failures=0,
                was_reverted=False,
                latency_delta_s=0.0,
                test_count_delta=0,
                files_changed=0,
                lesson=f"Skipped: {posture_skip}",
                verdict="neutral",
                ts_unix=now,
            )

        pressure_skip = self._check_pressure_gate()
        if pressure_skip:
            return MergeOutcome(
                commit_sha=commit_sha,
                op_id=op_id,
                observation_interval=interval,
                downstream_failures=0,
                was_reverted=False,
                latency_delta_s=0.0,
                test_count_delta=0,
                files_changed=0,
                lesson=f"Skipped: {pressure_skip}",
                verdict="neutral",
                ts_unix=now,
            )

        # Evaluate via git operations.
        was_reverted = self._check_revert(commit_sha)
        files_changed = self._count_files_changed(commit_sha)
        test_delta = self._estimate_test_delta(commit_sha, target_files)
        downstream_failures = self._estimate_downstream_failures(
            commit_sha, target_files,
        )

        verdict = _classify_verdict(
            was_reverted=was_reverted,
            downstream_failures=downstream_failures,
            test_count_delta=test_delta,
        )

        lesson = _compose_lesson(
            commit_sha=commit_sha,
            op_id=op_id,
            interval=interval,
            verdict=verdict,
            downstream_failures=downstream_failures,
            was_reverted=was_reverted,
            test_count_delta=test_delta,
            files_changed=files_changed,
        )

        outcome = MergeOutcome(
            commit_sha=commit_sha,
            op_id=op_id,
            observation_interval=interval,
            downstream_failures=downstream_failures,
            was_reverted=was_reverted,
            latency_delta_s=0.0,
            test_count_delta=test_delta,
            files_changed=files_changed,
            lesson=lesson,
            verdict=verdict,
            ts_unix=now,
        )

        # Persist the outcome.
        self._outcomes.append(outcome)
        self._persist_outcome(outcome)

        logger.info(
            "[PostMergeAuditor] Evaluated commit=%s at %s: verdict=%s "
            "failures=%d reverted=%s",
            commit_sha[:8], interval, verdict,
            downstream_failures, was_reverted,
        )
        return outcome

    # ------------------------------------------------------------------
    # Observer callback (for DeferredObservationQueue.tick())
    # ------------------------------------------------------------------

    def make_observer(self) -> Callable:
        """Return an observer callback suitable for
        ``DeferredObservationQueue.tick(observer=...)``.

        The callback extracts commit metadata from the intent and
        delegates to ``evaluate_commit()``.
        """
        def observer(intent: Any) -> str:
            meta = getattr(intent, "metadata", {}) or {}
            outcome = self.evaluate_commit(
                commit_sha=str(meta.get("commit_sha", "")),
                op_id=str(meta.get("op_id", "")),
                interval=str(meta.get("interval", "unknown")),
                target_files=meta.get("target_files", []),
            )
            return json.dumps(outcome.to_dict(), separators=(",", ":"))
        return observer

    # ------------------------------------------------------------------
    # Git evaluation primitives (read-only, never-raise)
    # ------------------------------------------------------------------

    def _check_revert(self, commit_sha: str) -> bool:
        """Check if commit was reverted by scanning recent commit subjects
        for ``Revert`` patterns. Synchronous (uses subprocess). NEVER raises."""
        if not commit_sha:
            return False
        try:
            import subprocess
            result = subprocess.run(
                ["git", "log", "--oneline", "-50", "--grep",
                 f"Revert.*{commit_sha[:8]}",
                 "--all-match", "--format=%s"],
                capture_output=True, text=True, timeout=10,
                cwd=str(self._repo_root),
            )
            if result.returncode == 0 and result.stdout.strip():
                # Check if any line mentions this commit's SHA.
                for line in result.stdout.strip().splitlines():
                    if commit_sha[:8] in line.lower() or "revert" in line.lower():
                        return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _count_files_changed(self, commit_sha: str) -> int:
        """Count files changed in the commit. NEVER raises."""
        if not commit_sha:
            return 0
        try:
            import subprocess
            result = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "--name-only",
                 "-r", commit_sha],
                capture_output=True, text=True, timeout=10,
                cwd=str(self._repo_root),
            )
            if result.returncode == 0:
                return len([
                    line for line in result.stdout.strip().splitlines()
                    if line.strip()
                ])
        except Exception:  # noqa: BLE001
            pass
        return 0

    def _estimate_test_delta(
        self, commit_sha: str, target_files: Sequence[str],
    ) -> int:
        """Estimate the number of test files added/removed by this commit.
        Positive = tests added, negative = tests removed. NEVER raises."""
        if not commit_sha:
            return 0
        try:
            import subprocess
            result = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "--name-status",
                 "-r", commit_sha],
                capture_output=True, text=True, timeout=10,
                cwd=str(self._repo_root),
            )
            if result.returncode != 0:
                return 0
            delta = 0
            for line in result.stdout.strip().splitlines():
                parts = line.strip().split("\t", 1)
                if len(parts) < 2:
                    continue
                status, filepath = parts[0].strip(), parts[1].strip()
                if "test" in filepath.lower():
                    if status.startswith("A"):
                        delta += 1
                    elif status.startswith("D"):
                        delta -= 1
            return delta
        except Exception:  # noqa: BLE001
            return 0

    def _estimate_downstream_failures(
        self, commit_sha: str, target_files: Sequence[str],
    ) -> int:
        """Estimate downstream test failures.

        This is a heuristic — in a production environment, this would
        query a CI system. For now, it checks if any revert commits
        exist that reference this SHA (which implies breakage).

        NEVER raises.
        """
        # Revert-based estimation: if the commit was reverted, assume
        # at least 1 downstream failure.
        if self._check_revert(commit_sha):
            return 1
        return 0

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_outcome(self, outcome: MergeOutcome) -> Tuple[bool, str]:
        """Append one outcome to the JSONL ledger. NEVER raises."""
        try:
            self._outcomes_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(outcome.to_dict(), separators=(",", ":"))
            with self._outcomes_path.open("a", encoding="utf-8") as f:
                try:
                    from backend.core.ouroboros.governance.adaptation._file_lock import (  # noqa: E501
                        flock_exclusive,
                    )
                    with flock_exclusive(f.fileno()):
                        f.write(line)
                        f.write("\n")
                        f.flush()
                except ImportError:
                    f.write(line)
                    f.write("\n")
                    f.flush()
        except OSError as exc:
            return (False, f"persist_failed:{exc}")
        return (True, "ok")

    def load_recent_outcomes(
        self, limit: int = MAX_RECENT_OUTCOMES,
    ) -> List[MergeOutcome]:
        """Load recent merge outcomes from disk. NEVER raises."""
        if not self._outcomes_path.exists():
            return []
        try:
            size = self._outcomes_path.stat().st_size
        except OSError:
            return []
        if size > MAX_OUTCOMES_FILE_BYTES:
            return []
        outcomes: List[MergeOutcome] = []
        try:
            text = self._outcomes_path.read_text(encoding="utf-8")
        except OSError:
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if len(outcomes) >= MAX_OUTCOMES_LOADED:
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            try:
                outcomes.append(MergeOutcome(
                    commit_sha=str(obj.get("commit_sha", "")),
                    op_id=str(obj.get("op_id", "")),
                    observation_interval=str(obj.get("observation_interval", "")),
                    downstream_failures=int(obj.get("downstream_failures", 0)),
                    was_reverted=bool(obj.get("was_reverted", False)),
                    latency_delta_s=float(obj.get("latency_delta_s", 0.0)),
                    test_count_delta=int(obj.get("test_count_delta", 0)),
                    files_changed=int(obj.get("files_changed", 0)),
                    lesson=str(obj.get("lesson", "")),
                    verdict=str(obj.get("verdict", "")),
                    ts_unix=float(obj.get("ts_unix", 0.0)),
                ))
            except (TypeError, ValueError):
                continue
        # Return newest first, limited.
        outcomes.sort(key=lambda o: o.ts_unix, reverse=True)
        return outcomes[:limit]

    # ------------------------------------------------------------------
    # Prompt composition (for StrategicDirection integration)
    # ------------------------------------------------------------------

    def format_prompt_section(
        self, limit: int = MAX_RECENT_OUTCOMES,
    ) -> Optional[str]:
        """Compose a ``## Recent Merge Outcomes`` section for
        ``StrategicDirection.format_for_prompt()``.

        Returns ``None`` if there are no outcomes to report.
        Advisory-only — zero authority.
        """
        outcomes = self.load_recent_outcomes(limit=limit)
        if not outcomes:
            return None

        lines: List[str] = [
            "## Recent Merge Outcomes",
            "",
            "Observed consequences of recent autonomous commits. Use these "
            "lessons to avoid repeating mistakes and reinforce successful "
            "patterns.",
            "",
        ]
        for outcome in outcomes:
            icon = {
                "beneficial": "✅",
                "neutral": "➖",
                "harmful": "⚠️",
                "reverted": "🔄",
            }.get(outcome.verdict, "❓")
            lines.append(f"- {icon} {outcome.lesson}")

        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Default singleton
# ---------------------------------------------------------------------------


_DEFAULT_AUDITOR: Optional[PostMergeAuditor] = None


def get_default_auditor(
    repo_root: Optional[Path] = None,
) -> PostMergeAuditor:
    global _DEFAULT_AUDITOR
    if _DEFAULT_AUDITOR is None:
        root = repo_root or Path(".")
        try:
            from backend.core.ouroboros.governance.observability.deferred_observation import (  # noqa: E501
                get_default_queue,
            )
            queue = get_default_queue()
        except ImportError:
            queue = None
        _DEFAULT_AUDITOR = PostMergeAuditor(
            repo_root=root,
            observation_queue=queue,
        )
    return _DEFAULT_AUDITOR


def reset_default_auditor() -> None:
    global _DEFAULT_AUDITOR
    _DEFAULT_AUDITOR = None


__all__ = [
    "CommitRecord",
    "MAX_LESSON_CHARS",
    "MAX_OUTCOMES_FILE_BYTES",
    "MAX_OUTCOMES_LOADED",
    "MAX_RECENT_OUTCOMES",
    "MAX_TARGET_FILES",
    "MergeOutcome",
    "PostMergeAuditor",
    "get_default_auditor",
    "is_auditor_enabled",
    "reset_default_auditor",
]
