"""
ExplorationFleet — Spawn an army of exploration agents across Trinity repos.

Like Claude Code's parallel Explore subagents: spawn N independent
exploration agents, each searching different areas simultaneously,
then synthesize findings into a unified report.

Covers all 3 Trinity repos: JARVIS, J-Prime, Reactor Core.

Architecture:
  ExplorationFleet.deploy(goal, strategy)
    ├─ Agent 1: JARVIS/backend/core/ (asyncio.Task)
    ├─ Agent 2: JARVIS/backend/vision/ (asyncio.Task)
    ├─ Agent 3: JARVIS/backend/voice/ (asyncio.Task)
    ├─ Agent 4: J-Prime/reasoning/ (asyncio.Task)
    ├─ Agent 5: Reactor/training/ (asyncio.Task)
    └─ Coordinator: merge findings, deduplicate, rank

Boundary Principle:
  Deterministic: Task spawning, finding deduplication, ranking.
  Agentic: What each agent explores (goal-driven file discovery).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.exploration_subagent import (
    ExplorationSubagent, ExplorationReport, ExplorationFinding,
)

logger = logging.getLogger(__name__)

_MAX_AGENTS = int(os.environ.get("JARVIS_FLEET_MAX_AGENTS", "8"))
_FLEET_TIMEOUT_S = float(os.environ.get("JARVIS_FLEET_TIMEOUT_S", "120"))


@dataclass
class FleetAgent:
    """One agent in the exploration fleet."""
    agent_id: str
    repo: str                  # "jarvis", "jarvis-prime", "reactor"
    scope: str                 # Directory scope (e.g., "backend/core/")
    status: str = "pending"    # pending, exploring, completed, failed
    report: Optional[ExplorationReport] = None


@dataclass
class FleetReport:
    """Merged report from all fleet agents."""
    goal: str
    agents_deployed: int
    agents_completed: int
    agents_failed: int
    total_files_explored: int
    total_findings: int
    findings: List[ExplorationFinding]
    per_repo_summary: Dict[str, str]
    duration_s: float
    synthesis: str = ""


# Default exploration scopes per repo
_JARVIS_SCOPES = [
    "backend/core/",
    "backend/core/ouroboros/governance/",
    "backend/vision/",
    "backend/voice/",
    "backend/intelligence/",
    "backend/ghost_hands/",
    "backend/neural_mesh/agents/",
    "backend/core_contexts/",
]

_PRIME_SCOPES = [
    "reasoning/",
    "knowledge/",
]

_REACTOR_SCOPES = [
    "backend/training/",
]


class ExplorationFleet:
    """Spawn an army of exploration agents across all Trinity repos.

    Usage:
        fleet = ExplorationFleet()
        report = await fleet.deploy(
            goal="understand the voice authentication flow",
            repos=("jarvis", "jarvis-prime"),
        )
        # report.findings has merged, deduplicated, ranked findings
        # report.synthesis has a unified summary
    """

    def __init__(
        self,
        jarvis_root: Optional[Path] = None,
        prime_root: Optional[Path] = None,
        reactor_root: Optional[Path] = None,
    ) -> None:
        self._roots: Dict[str, Path] = {}

        # JARVIS repo (this repo)
        self._roots["jarvis"] = jarvis_root or Path(".")

        # J-Prime repo
        prime_path = prime_root or os.environ.get("JARVIS_PRIME_REPO_PATH")
        if prime_path:
            self._roots["jarvis-prime"] = Path(prime_path)

        # Reactor repo
        reactor_path = reactor_root or os.environ.get("JARVIS_REACTOR_REPO_PATH")
        if reactor_path:
            self._roots["reactor"] = Path(reactor_path)

    async def deploy(
        self,
        goal: str,
        repos: Optional[Tuple[str, ...]] = None,
        max_agents: int = _MAX_AGENTS,
    ) -> FleetReport:
        """Deploy the exploration fleet across repos.

        Spawns one ExplorationSubagent per scope, runs them in parallel,
        merges findings, and returns a unified report.
        """
        t0 = time.time()

        # Determine which repos to explore
        target_repos = repos or tuple(self._roots.keys())

        # Build agent assignments
        agents: List[FleetAgent] = []
        for repo in target_repos:
            root = self._roots.get(repo)
            if not root or not root.exists():
                continue

            scopes = self._get_scopes_for_repo(repo)
            # Filter to scopes that exist
            for scope in scopes:
                if (root / scope).exists():
                    agents.append(FleetAgent(
                        agent_id=f"fleet-{repo}-{scope.replace('/', '-').strip('-')}",
                        repo=repo,
                        scope=scope,
                    ))

        # Cap agent count
        agents = agents[:max_agents]

        if not agents:
            return FleetReport(
                goal=goal, agents_deployed=0, agents_completed=0,
                agents_failed=0, total_files_explored=0, total_findings=0,
                findings=[], per_repo_summary={}, duration_s=0,
                synthesis="No repos available for exploration.",
            )

        logger.info(
            "[Fleet] Deploying %d agents across %d repos for: %s",
            len(agents), len(target_repos), goal[:50],
        )

        # Spawn all agents in parallel
        tasks = []
        for agent in agents:
            task = asyncio.create_task(
                self._run_agent(agent, goal),
                name=agent.agent_id,
            )
            tasks.append((agent, task))

        # Wait for all with timeout
        done, pending = await asyncio.wait(
            [t for _, t in tasks],
            timeout=_FLEET_TIMEOUT_S,
        )

        # Cancel timed-out agents
        for task in pending:
            task.cancel()

        # Collect results
        all_findings: List[ExplorationFinding] = []
        total_files = 0
        completed = 0
        failed = 0
        per_repo: Dict[str, List[str]] = {}

        for agent, task in tasks:
            if task.done() and not task.cancelled():
                try:
                    task.result()  # Raise if exception
                    if agent.report:
                        all_findings.extend(agent.report.findings)
                        total_files += len(agent.report.files_read)
                        per_repo.setdefault(agent.repo, []).append(
                            f"{agent.scope}: {len(agent.report.findings)} findings"
                        )
                        completed += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
            else:
                failed += 1

        # Deduplicate findings
        deduped = self._deduplicate_findings(all_findings)

        # Rank by relevance
        deduped.sort(key=lambda f: -f.relevance)

        # Generate per-repo summaries
        repo_summaries = {
            repo: "; ".join(items)
            for repo, items in per_repo.items()
        }

        # Synthesize
        synthesis = self._synthesize(goal, deduped, total_files, completed)

        elapsed = time.time() - t0

        report = FleetReport(
            goal=goal,
            agents_deployed=len(agents),
            agents_completed=completed,
            agents_failed=failed,
            total_files_explored=total_files,
            total_findings=len(deduped),
            findings=deduped[:50],  # Top 50
            per_repo_summary=repo_summaries,
            duration_s=elapsed,
            synthesis=synthesis,
        )

        logger.info(
            "[Fleet] Complete: %d agents, %d files, %d findings in %.1fs",
            completed, total_files, len(deduped), elapsed,
        )
        return report

    async def _run_agent(self, agent: FleetAgent, goal: str) -> None:
        """Run one exploration agent. Updates agent.report on completion."""
        agent.status = "exploring"
        root = self._roots.get(agent.repo)
        if not root:
            agent.status = "failed"
            return

        try:
            explorer = ExplorationSubagent(root)

            # Find entry files in the scope
            scope_dir = root / agent.scope
            entry_files = []
            if scope_dir.exists():
                for py in scope_dir.glob("*.py"):
                    if py.name != "__init__.py":
                        entry_files.append(
                            str(py.relative_to(root))
                        )
                entry_files = entry_files[:5]  # Cap per agent

            report = await explorer.explore(
                goal=goal,
                entry_files=tuple(entry_files),
                max_files=10,
                max_depth=2,
            )
            agent.report = report
            agent.status = "completed"

        except Exception as exc:
            agent.status = "failed"
            logger.debug("[Fleet] Agent %s failed: %s", agent.agent_id, exc)

    def _get_scopes_for_repo(self, repo: str) -> List[str]:
        """Get exploration scopes for a repo. Deterministic."""
        if repo == "jarvis":
            return _JARVIS_SCOPES
        if repo == "jarvis-prime":
            return _PRIME_SCOPES
        if repo == "reactor":
            return _REACTOR_SCOPES
        return [""]

    @staticmethod
    def _deduplicate_findings(
        findings: List[ExplorationFinding],
    ) -> List[ExplorationFinding]:
        """Remove duplicate findings (same file + category + description prefix)."""
        seen: set[str] = set()
        deduped: List[ExplorationFinding] = []
        for f in findings:
            key = f"{f.file_path}:{f.category}:{f.description[:50]}"
            if key not in seen:
                seen.add(key)
                deduped.append(f)
        return deduped

    @staticmethod
    def _synthesize(
        goal: str, findings: List[ExplorationFinding],
        total_files: int, agents_completed: int,
    ) -> str:
        """Generate a unified synthesis from all agent findings."""
        if not findings:
            return "No findings from exploration."

        parts = [
            f"Fleet exploration for '{goal}': "
            f"{agents_completed} agents explored {total_files} files, "
            f"producing {len(findings)} unique findings."
        ]

        # Count by category
        cats: Dict[str, int] = {}
        for f in findings:
            cats[f.category] = cats.get(f.category, 0) + 1

        cat_str = ", ".join(f"{c}={n}" for c, n in sorted(cats.items(), key=lambda x: -x[1]))
        parts.append(f"Categories: {cat_str}.")

        # Top findings
        top = findings[:3]
        if top:
            parts.append("Top findings: " + "; ".join(f.description[:60] for f in top))

        return " ".join(parts)

    def format_for_prompt(self, report: FleetReport) -> str:
        """Format fleet report for generation prompt injection."""
        if not report.findings:
            return ""

        lines = [
            f"## Fleet Exploration: {report.goal}",
            f"{report.agents_completed} agents, {report.total_files_explored} files, "
            f"{report.total_findings} findings in {report.duration_s:.1f}s",
            "",
        ]

        # Per-repo breakdown
        for repo, summary in report.per_repo_summary.items():
            lines.append(f"**{repo}:** {summary}")
        lines.append("")

        # Top findings
        for f in report.findings[:10]:
            lines.append(f"- [{f.category}] {f.description}")

        if report.synthesis:
            lines.append(f"\n**Synthesis:** {report.synthesis}")

        return "\n".join(lines)
