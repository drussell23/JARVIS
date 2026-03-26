"""
Pipeline Hooks — Post-COMPLETE and cross-cutting pipeline enhancements.

P1 End-to-End gaps:
  1. GitHubIssueCloser: Auto-close issues after fix (gh CLI, argv-based)
  2. UnifiedCostAggregator: Single view across all providers
  3. VisualTokenGuard: Rate limiter for visual comprehension

All deterministic. No model inference.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. GitHub Issue Auto-Closer
# ---------------------------------------------------------------------------

class GitHubIssueCloser:
    """Auto-close GitHub issues after Ouroboros fixes them. Uses gh CLI (argv)."""

    _ENABLED = os.environ.get(
        "JARVIS_GITHUB_AUTO_CLOSE", "true"
    ).lower() in ("true", "1", "yes")

    @classmethod
    async def close_if_applicable(
        cls, op_id: str, evidence: Dict[str, Any], commit_sha: str = "",
    ) -> bool:
        """Close the issue if operation resolved a GitHub issue."""
        if not cls._ENABLED:
            return False
        if evidence.get("category") != "github_issue":
            return False
        if not evidence.get("auto_resolvable", False):
            return False

        repo_full = evidence.get("repo_full", "")
        issue_number = evidence.get("issue_number", 0)
        if not repo_full or not issue_number:
            return False

        try:
            comment = (
                f"Automatically resolved by Ouroboros governance pipeline.\n"
                f"Operation: `{op_id}`"
            )
            if commit_sha:
                comment += f"\nCommit: `{commit_sha}`"

            # Comment on the issue (argv, no shell)
            proc = await asyncio.create_subprocess_exec(
                "gh", "issue", "comment", str(issue_number),
                "--repo", repo_full, "--body", comment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15.0)

            # Close the issue (argv, no shell)
            proc2 = await asyncio.create_subprocess_exec(
                "gh", "issue", "close", str(issue_number),
                "--repo", repo_full, "--reason", "completed",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc2.communicate(), timeout=15.0)

            if proc2.returncode == 0:
                logger.info(
                    "[GitHubIssueCloser] Closed #%d in %s (op=%s)",
                    issue_number, repo_full, op_id,
                )
                return True
            return False

        except (asyncio.TimeoutError, Exception) as exc:
            logger.debug("[GitHubIssueCloser] Error: %s", exc)
            return False


# ---------------------------------------------------------------------------
# 2. Unified Cost Aggregator
# ---------------------------------------------------------------------------

@dataclass
class ProviderCostSnapshot:
    provider: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_requests: int = 0


@dataclass
class UnifiedCostReport:
    providers: List[ProviderCostSnapshot]
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_requests: int = 0
    report_time: float = field(default_factory=time.time)


class UnifiedCostAggregator:
    """Single view of inference spend across all providers."""

    def __init__(self) -> None:
        self._providers: List[Any] = []

    def register_provider(self, provider: Any) -> None:
        if hasattr(provider, "get_stats"):
            self._providers.append(provider)

    def generate_report(self) -> UnifiedCostReport:
        snapshots = []
        total_cost = 0.0
        total_tokens = 0
        total_requests = 0

        for provider in self._providers:
            try:
                stats = provider.get_stats()
                snap = ProviderCostSnapshot(
                    provider=stats.get("provider", "unknown"),
                    total_input_tokens=stats.get("total_input_tokens", 0),
                    total_output_tokens=stats.get("total_output_tokens", 0),
                    total_cost_usd=stats.get("total_cost_usd", 0.0),
                    total_requests=stats.get("total_batches", 0) + stats.get("total_requests", 0),
                )
                snapshots.append(snap)
                total_cost += snap.total_cost_usd
                total_tokens += snap.total_input_tokens + snap.total_output_tokens
                total_requests += snap.total_requests
            except Exception:
                pass

        return UnifiedCostReport(
            providers=snapshots,
            total_cost_usd=round(total_cost, 6),
            total_tokens=total_tokens,
            total_requests=total_requests,
        )

    def format_report(self) -> str:
        report = self.generate_report()
        lines = [
            f"Unified Cost: ${report.total_cost_usd:.4f} total, "
            f"{report.total_tokens:,} tokens, {report.total_requests} requests"
        ]
        for snap in report.providers:
            lines.append(
                f"  {snap.provider}: ${snap.total_cost_usd:.4f} "
                f"({snap.total_input_tokens + snap.total_output_tokens:,} tok)"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. Visual Token Guard
# ---------------------------------------------------------------------------

class VisualTokenGuard:
    """Rate limiter for visual comprehension. Prevents token burn."""

    def __init__(
        self,
        max_calls_per_hour: int = 10,
        max_daily_cost_usd: float = 1.0,
    ) -> None:
        self._max_per_hour = int(
            os.environ.get("JARVIS_VISUAL_MAX_PER_HOUR", str(max_calls_per_hour))
        )
        self._max_daily = float(
            os.environ.get("JARVIS_VISUAL_MAX_DAILY_USD", str(max_daily_cost_usd))
        )
        self._calls: List[float] = []
        self._daily_cost: float = 0.0
        self._day_start: float = time.time()

    def can_analyze(self) -> bool:
        now = time.time()
        if now - self._day_start > 86400:
            self._daily_cost = 0.0
            self._day_start = now
        self._calls = [t for t in self._calls if now - t < 3600]

        if len(self._calls) >= self._max_per_hour:
            return False
        if self._daily_cost >= self._max_daily:
            return False
        return True

    def record_call(self, cost_usd: float = 0.02) -> None:
        self._calls.append(time.time())
        self._daily_cost += cost_usd

    def get_status(self) -> Dict[str, Any]:
        now = time.time()
        recent = [t for t in self._calls if now - t < 3600]
        return {
            "calls_this_hour": len(recent),
            "max_per_hour": self._max_per_hour,
            "daily_cost_usd": round(self._daily_cost, 4),
            "max_daily_usd": self._max_daily,
        }
