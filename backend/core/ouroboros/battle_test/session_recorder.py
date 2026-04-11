"""SessionRecorder — collects all session data, produces a terminal summary, and persists JSON.

Tracks operations attempted, completed, failed, cancelled, and queued
(APPROVAL_REQUIRED).  Queued operations are also written to a separate
``review_queue.jsonl`` so humans can review them after the session.

Output files (written by :meth:`save_summary`):
  - ``{output_dir}/summary.json``       — full session statistics
  - ``{output_dir}/review_queue.jsonl`` — one JSON object per queued op
                                          (only created if queued ops exist)
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class SessionRecorder:
    """Collect session data and produce terminal / JSON summaries.

    Parameters
    ----------
    session_id:
        Unique identifier for this battle test session
        (e.g. ``"bt-2026-04-06-143022"``).
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._started_at: float = time.time()

        # Counters
        self._attempted: int = 0
        self._completed: int = 0
        self._failed: int = 0
        self._cancelled: int = 0
        self._queued: int = 0

        # Per-sensor and per-technique counts
        self._sensor_counts: Dict[str, int] = defaultdict(int)
        self._technique_counts: Dict[str, int] = defaultdict(int)

        # Full operation log
        self._operations: List[Dict[str, Any]] = []

        # Ops that need human review (status == "queued")
        self._review_queue: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def stats(self) -> Dict[str, int]:
        """Return a snapshot of counters keyed by status name."""
        return {
            "attempted": self._attempted,
            "completed": self._completed,
            "failed": self._failed,
            "cancelled": self._cancelled,
            "queued": self._queued,
        }

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_operation(
        self,
        op_id: str,
        status: str,
        sensor: str,
        technique: str,
        composite_score: float,
        elapsed_s: float,
        provider: str = "",
        cost_usd: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        tool_calls: int = 0,
        files_changed: int = 0,
    ) -> None:
        """Record a completed or queued operation with cost data.

        Parameters
        ----------
        op_id:
            Unique operation identifier.
        status:
            One of ``"completed"``, ``"failed"``, ``"cancelled"``, or
            ``"queued"`` (APPROVAL_REQUIRED).
        sensor:
            Name of the intake sensor that generated the operation.
        technique:
            Primary RSI technique applied during GENERATE phase.
        composite_score:
            Composite RSI score for this operation (lower = better).
        elapsed_s:
            Wall-clock seconds from enqueue to terminal state.
        provider:
            Provider used (doubleword-397b, claude-api, etc.)
        cost_usd:
            Estimated cost for this operation in USD.
        input_tokens:
            Total input tokens consumed.
        output_tokens:
            Total output tokens generated.
        cached_tokens:
            Input tokens served from prompt cache (90% cheaper).
        tool_calls:
            Number of Venom tool calls during generation.
        files_changed:
            Number of files modified in APPLY phase.
        """
        entry: Dict[str, Any] = {
            "op_id": op_id,
            "status": status,
            "sensor": sensor,
            "technique": technique,
            "composite_score": composite_score,
            "elapsed_s": elapsed_s,
            "recorded_at": time.time(),
            "provider": provider,
            "cost_usd": cost_usd,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "tool_calls": tool_calls,
            "files_changed": files_changed,
        }

        self._operations.append(entry)
        self._attempted += 1
        self._sensor_counts[sensor] += 1
        self._technique_counts[technique] += 1

        if status == "completed":
            self._completed += 1
        elif status == "failed":
            self._failed += 1
        elif status == "cancelled":
            self._cancelled += 1
        elif status == "queued":
            self._queued += 1
            self._review_queue.append(entry)

    # ------------------------------------------------------------------
    # Aggregation helpers
    # ------------------------------------------------------------------

    def top_sensors(self, n: int = 5) -> List[Tuple[str, int]]:
        """Return the top *n* sensors sorted descending by operation count.

        Returns
        -------
        List of ``(sensor_name, count)`` tuples.
        """
        sorted_sensors = sorted(
            self._sensor_counts.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )
        return sorted_sensors[:n]

    def top_techniques(self, n: int = 5) -> List[Tuple[str, int]]:
        """Return the top *n* techniques sorted descending by use count.

        Returns
        -------
        List of ``(technique_name, count)`` tuples.
        """
        sorted_techniques = sorted(
            self._technique_counts.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )
        return sorted_techniques[:n]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_summary(
        self,
        output_dir: Path,
        stop_reason: str,
        duration_s: float,
        cost_total: float,
        cost_breakdown: Dict[str, float],
        branch_stats: Dict[str, Any],
        convergence_state: str,
        convergence_slope: float,
        convergence_r2: float,
        strategic_drift: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """Write ``summary.json`` and (if any) ``review_queue.jsonl`` to *output_dir*.

        Parameters
        ----------
        output_dir:
            Directory in which to write output files.  Created if absent.
        stop_reason:
            Human-readable reason the session ended.
        duration_s:
            Total session wall-clock time in seconds.
        cost_total:
            Total API cost in USD.
        cost_breakdown:
            Per-provider cost map (e.g. ``{"anthropic": 0.48}``).
        branch_stats:
            Dict with branch metadata (``branch``, ``commits``, ``files``, …).
        convergence_state:
            Convergence classification string (e.g. ``"IMPROVING"``).
        convergence_slope:
            Linear-regression slope from the convergence tracker.
        convergence_r2:
            R² of the logarithmic fit.

        Returns
        -------
        Path
            Absolute path to the written ``summary.json``.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        summary: Dict[str, Any] = {
            "session_id": self._session_id,
            "stop_reason": stop_reason,
            "duration_s": duration_s,
            "stats": self.stats,
            "cost_total": cost_total,
            "cost_breakdown": cost_breakdown,
            "branch_stats": branch_stats,
            "convergence_state": convergence_state,
            "convergence_slope": convergence_slope,
            "convergence_r2": convergence_r2,
            "top_sensors": self.top_sensors(),
            "top_techniques": self.top_techniques(),
            "operations": self._operations,
        }
        if strategic_drift is not None:
            summary["strategic_drift"] = strategic_drift

        summary_path = output_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, indent=2))

        # Write review_queue.jsonl only when there are queued operations
        if self._review_queue:
            review_path = output_dir / "review_queue.jsonl"
            lines = [json.dumps(entry) for entry in self._review_queue]
            review_path.write_text("\n".join(lines) + "\n")

        return summary_path

    # ------------------------------------------------------------------
    # Terminal summary
    # ------------------------------------------------------------------

    def format_terminal_summary(
        self,
        stop_reason: str,
        duration_s: float,
        cost_total: float,
        cost_breakdown: Dict[str, float],
        branch_name: str,
        branch_stats: Dict[str, Any],
        convergence_state: str,
        convergence_slope: float,
        convergence_r2: float,
    ) -> str:
        """Build a human-readable terminal summary string.

        Parameters
        ----------
        stop_reason:
            Human-readable reason the session ended.
        duration_s:
            Total session wall-clock time in seconds.
        cost_total:
            Total API cost in USD.
        cost_breakdown:
            Per-provider cost map.
        branch_name:
            Full name of the accumulation branch.
        branch_stats:
            Dict with branch metadata (``commits``, ``files``, …).
        convergence_state:
            Convergence classification string.
        convergence_slope:
            Linear-regression slope.
        convergence_r2:
            R² of the logarithmic fit.

        Returns
        -------
        str
            Multi-line formatted summary ready for ``print()``.
        """
        border = "=" * 60
        minutes = int(duration_s) // 60
        seconds = int(duration_s) % 60

        stats = self.stats
        attempted = stats["attempted"]

        def pct(n: int) -> str:
            if attempted == 0:
                return "0.0%"
            return f"{100.0 * n / attempted:.1f}%"

        # --- Build convergence recommendation ---
        convergence_reco = _convergence_recommendation(convergence_state)

        # --- Cost lines ---
        cost_lines = []
        for provider, amount in cost_breakdown.items():
            cost_lines.append(f"  {provider.capitalize():12s} ${amount:.2f}")
        cost_lines.append(f"  {'Total':12s} ${cost_total:.2f}")

        # --- Top techniques ---
        technique_lines = []
        for i, (name, count) in enumerate(self.top_techniques(n=5), start=1):
            technique_lines.append(f"  {i}. {name:<24s} ({count} ops)")

        # --- Top sensors ---
        sensor_lines = []
        for i, (name, count) in enumerate(self.top_sensors(n=5), start=1):
            sensor_lines.append(f"  {i}. {name:<30s} {count} operations")

        # --- Branch section ---
        commits = branch_stats.get("commits", 0)
        files = branch_stats.get("files", 0)
        insertions = branch_stats.get("insertions", 0)
        deletions = branch_stats.get("deletions", 0)

        branch_detail = [f"  Branch:     {branch_name}"]
        branch_detail.append(f"  Commits:    {commits}")
        branch_detail.append(f"  Files:      {files} changed")
        if insertions or deletions:
            branch_detail.append(f"  Insertions: +{insertions:,}")
            branch_detail.append(f"  Deletions:  -{deletions:,}")

        lines = [
            border,
            "  OUROBOROS BATTLE TEST — SESSION COMPLETE",
            border,
            f"  Session ID:    {self._session_id}",
            f"  Duration:      {minutes}m {seconds}s",
            f"  Stop reason:   {stop_reason}",
            "",
            "  OPERATIONS",
            "  ----------",
            f"  Attempted:     {attempted}",
            f"  Completed:     {stats['completed']}  ({pct(stats['completed'])})",
            f"  Failed:        {stats['failed']}   ({pct(stats['failed'])})",
            f"  Cancelled:     {stats['cancelled']}   ({pct(stats['cancelled'])})",
            f"  Queued (approval):  {stats['queued']}  (see review_queue.jsonl)",
            "",
            "  CONVERGENCE",
            "  -----------",
            f"  State:         {convergence_state}",
            f"  Slope:         {convergence_slope:.4f}",
            f"  R\u00b2 (log fit):  {convergence_r2:.2f}",
            f"  Recommendation: {convergence_reco}",
            "",
            "  COST",
            "  ----",
        ]
        lines.extend(cost_lines)

        if technique_lines:
            lines += [
                "",
                "  TOP TECHNIQUES",
                "  --------------",
            ]
            lines.extend(technique_lines)

        if sensor_lines:
            lines += [
                "",
                "  TOP SENSORS",
                "  -----------",
            ]
            lines.extend(sensor_lines)

        lines += [
            "",
            "  BRANCH",
            "  ------",
        ]
        lines.extend(branch_detail)

        lines += [
            "",
            "  Next steps:",
            f"    git diff main..{branch_name}",
            "    jupyter notebook notebooks/ouroboros_battle_test_analysis.ipynb",
            "",
            border,
        ]

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _convergence_recommendation(state: str) -> str:
    """Return a short recommendation string for *state*."""
    recommendations: Dict[str, str] = {
        "LOGARITHMIC": "Excellent — pipeline is converging logarithmically. Near optimum.",
        "IMPROVING": "Pipeline is converging. Continue current strategy.",
        "PLATEAUED": "Scores have plateaued. Consider diversifying techniques.",
        "OSCILLATING": "Scores are oscillating. Reduce learning rate or add constraints.",
        "DEGRADING": "Scores are degrading. Investigate failure patterns immediately.",
        "INSUFFICIENT_DATA": "Not enough data to classify convergence. Run more operations.",
    }
    return recommendations.get(state, "Unknown convergence state.")
