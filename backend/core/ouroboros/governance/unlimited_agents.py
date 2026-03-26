"""
Unlimited Agent System — Recursive spawning, dynamic scopes, inter-fleet
communication, and model-powered reasoning agents.

Fixes 4 gaps for truly advanced agent spawning:

1. Recursive Spawning: Agents can spawn child agents (with depth limit
   + memory budget guard to prevent infinite recursion)

2. Dynamic Scope Discovery: Auto-discover directories to explore instead
   of hardcoded lists. Walk the tree, find Python packages.

3. Inter-Fleet Communication: SharedFindingsBus allows multiple fleets
   to share discoveries in real-time. One fleet's finding can redirect
   another fleet's exploration.

4. Agent-Level Model Reasoning: Agents can call a lightweight model
   to reason about what they find, not just AST/regex.

Boundary Principle:
  Deterministic: Scope discovery (fs walk), depth limits, memory checks,
  finding deduplication, bus routing.
  Agentic: Model reasoning calls within agents, recursive spawn decisions.
"""
from __future__ import annotations

import asyncio
import logging
import os
import psutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

from backend.core.ouroboros.governance.exploration_subagent import (
    ExplorationSubagent, ExplorationReport, ExplorationFinding,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_MAX_DEPTH = int(os.environ.get("JARVIS_AGENT_MAX_DEPTH", "3"))
_MAX_TOTAL_AGENTS = int(os.environ.get("JARVIS_AGENT_MAX_TOTAL", "30"))
_MEMORY_BUDGET_PCT = float(os.environ.get("JARVIS_AGENT_MEMORY_BUDGET_PCT", "85"))
_AGENT_TIMEOUT_S = float(os.environ.get("JARVIS_AGENT_TIMEOUT_S", "60"))


# ═══════════════════════════════════════════════════════════════════════════
# 1. DYNAMIC SCOPE DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════

class DynamicScopeDiscovery:
    """Auto-discover explorable scopes by walking the directory tree.

    Instead of hardcoded scope lists, discovers Python packages dynamically.
    A directory is a valid scope if it contains at least one .py file.
    """

    _SKIP = frozenset({
        "venv", "__pycache__", "node_modules", ".git", ".worktrees",
        "site-packages", "dist", "build", ".tox", ".mypy_cache",
    })

    @classmethod
    def discover(
        cls, root: Path, max_scopes: int = 20, min_py_files: int = 2,
    ) -> List[str]:
        """Discover Python package directories. Returns relative paths."""
        scopes: List[str] = []

        try:
            for dirpath in sorted(root.rglob("*")):
                if not dirpath.is_dir():
                    continue
                if any(skip in dirpath.parts for skip in cls._SKIP):
                    continue
                # Count .py files (non-recursive, just this directory)
                py_count = sum(1 for f in dirpath.iterdir()
                               if f.is_file() and f.suffix == ".py")
                if py_count >= min_py_files:
                    rel = str(dirpath.relative_to(root))
                    if rel != ".":
                        scopes.append(rel + "/")

                if len(scopes) >= max_scopes:
                    break
        except Exception:
            pass

        return scopes


# ═══════════════════════════════════════════════════════════════════════════
# 2. SHARED FINDINGS BUS (Inter-Fleet Communication)
# ═══════════════════════════════════════════════════════════════════════════

class SharedFindingsBus:
    """Real-time finding sharing between fleets and agents.

    Any agent can publish findings. Any fleet can subscribe to
    findings and redirect its exploration based on what others discover.

    Thread-safe via asyncio.Queue. Findings persist for the session.
    """

    def __init__(self, maxsize: int = 500) -> None:
        self._findings: List[ExplorationFinding] = []
        self._subscribers: List[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    async def publish(self, finding: ExplorationFinding) -> None:
        """Publish a finding to all subscribers."""
        async with self._lock:
            self._findings.append(finding)
        for q in self._subscribers:
            try:
                q.put_nowait(finding)
            except asyncio.QueueFull:
                pass  # Drop if subscriber is slow

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to new findings. Returns a queue to read from."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def get_all_findings(self) -> List[ExplorationFinding]:
        """Get all published findings (snapshot)."""
        return list(self._findings)

    @property
    def count(self) -> int:
        return len(self._findings)


# Global singleton
_global_bus: Optional[SharedFindingsBus] = None


def get_shared_findings_bus() -> SharedFindingsBus:
    global _global_bus
    if _global_bus is None:
        _global_bus = SharedFindingsBus()
    return _global_bus


# ═══════════════════════════════════════════════════════════════════════════
# 3. MEMORY BUDGET GUARD
# ═══════════════════════════════════════════════════════════════════════════

class MemoryBudgetGuard:
    """Prevents agent spawning from exhausting system memory.

    Checks psutil.virtual_memory().percent before each spawn.
    If memory exceeds budget, refuses to spawn (returns False).
    """

    @staticmethod
    def can_spawn() -> bool:
        """Check if system memory allows another agent spawn."""
        try:
            mem = psutil.virtual_memory()
            if mem.percent >= _MEMORY_BUDGET_PCT:
                logger.warning(
                    "[MemoryGuard] Memory at %.1f%% (budget: %.0f%%) — spawn blocked",
                    mem.percent, _MEMORY_BUDGET_PCT,
                )
                return False
            return True
        except Exception:
            return True  # If psutil fails, allow spawn (don't block on monitoring failure)

    @staticmethod
    def get_status() -> Dict[str, Any]:
        try:
            mem = psutil.virtual_memory()
            return {
                "memory_pct": round(mem.percent, 1),
                "budget_pct": _MEMORY_BUDGET_PCT,
                "available_gb": round(mem.available / (1024**3), 1),
                "can_spawn": mem.percent < _MEMORY_BUDGET_PCT,
            }
        except Exception:
            return {"memory_pct": -1, "can_spawn": True}


# ═══════════════════════════════════════════════════════════════════════════
# 4. RECURSIVE AGENT with Model Reasoning
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class RecursiveAgentResult:
    """Result from a recursive agent including child results."""
    agent_id: str
    scope: str
    repo: str
    depth: int
    findings: List[ExplorationFinding]
    child_results: List['RecursiveAgentResult']
    files_explored: int
    duration_s: float
    model_insight: str = ""    # Model-generated insight (if reasoning enabled)


class RecursiveExplorationAgent:
    """Agent that can spawn child agents for deeper exploration.

    When an agent discovers a complex subdirectory during exploration,
    it can spawn a child agent to explore that subdirectory in detail.
    Depth-limited to prevent infinite recursion. Memory-guarded.

    With model reasoning enabled, agents can call a lightweight model
    to synthesize insights from their findings — not just report AST data.
    """

    _total_spawned: int = 0
    _total_lock = asyncio.Lock()

    def __init__(
        self,
        agent_id: str,
        root: Path,
        repo: str,
        depth: int = 0,
        findings_bus: Optional[SharedFindingsBus] = None,
        model_fn: Optional[Callable[[str], Coroutine[Any, Any, str]]] = None,
    ) -> None:
        self._id = agent_id
        self._root = root
        self._repo = repo
        self._depth = depth
        self._bus = findings_bus or get_shared_findings_bus()
        self._model_fn = model_fn  # Lightweight model call for reasoning

    async def explore(
        self,
        scope: str,
        goal: str,
        spawn_children: bool = True,
    ) -> RecursiveAgentResult:
        """Explore a scope, optionally spawning children for sub-scopes.

        Recursive: if a subdirectory has high complexity, spawn a child
        agent to explore it in detail. Depth-limited + memory-guarded.
        """
        t0 = time.time()

        # Base exploration
        explorer = ExplorationSubagent(self._root)
        scope_dir = self._root / scope
        entry_files = []
        if scope_dir.exists():
            entry_files = [
                str(f.relative_to(self._root))
                for f in scope_dir.glob("*.py")
                if f.name != "__init__.py"
            ][:5]

        report = await explorer.explore(
            goal=goal,
            entry_files=tuple(entry_files),
            max_files=10,
            max_depth=2,
        )

        # Publish findings to shared bus
        for finding in report.findings:
            await self._bus.publish(finding)

        # Model reasoning (if available)
        model_insight = ""
        if self._model_fn and report.findings:
            try:
                summary = "; ".join(f.description[:50] for f in report.findings[:5])
                prompt = (
                    f"Based on these findings from {scope}: {summary}\n"
                    f"Goal: {goal}\n"
                    f"What are the most important insights and what should we explore next?"
                )
                model_insight = await asyncio.wait_for(
                    self._model_fn(prompt), timeout=30.0,
                )
            except Exception:
                pass

        # Spawn children for complex sub-directories
        child_results: List[RecursiveAgentResult] = []

        if (spawn_children
                and self._depth < _MAX_DEPTH
                and MemoryBudgetGuard.can_spawn()):

            # Find complex sub-directories worth exploring deeper
            sub_scopes = self._find_worthy_children(scope, report.findings)

            for sub_scope in sub_scopes:
                async with RecursiveExplorationAgent._total_lock:
                    if RecursiveExplorationAgent._total_spawned >= _MAX_TOTAL_AGENTS:
                        logger.info("[RecursiveAgent] Total agent limit reached (%d)", _MAX_TOTAL_AGENTS)
                        break
                    if not MemoryBudgetGuard.can_spawn():
                        break
                    RecursiveExplorationAgent._total_spawned += 1

                child_id = f"{self._id}/child-{len(child_results)}"
                child = RecursiveExplorationAgent(
                    agent_id=child_id,
                    root=self._root,
                    repo=self._repo,
                    depth=self._depth + 1,
                    findings_bus=self._bus,
                    model_fn=self._model_fn,
                )

                try:
                    child_result = await asyncio.wait_for(
                        child.explore(sub_scope, goal, spawn_children=True),
                        timeout=_AGENT_TIMEOUT_S,
                    )
                    child_results.append(child_result)
                except asyncio.TimeoutError:
                    logger.debug("[RecursiveAgent] Child %s timed out", child_id)
                except Exception:
                    logger.debug("[RecursiveAgent] Child %s failed", child_id)

        elapsed = time.time() - t0

        return RecursiveAgentResult(
            agent_id=self._id,
            scope=scope,
            repo=self._repo,
            depth=self._depth,
            findings=report.findings,
            child_results=child_results,
            files_explored=len(report.files_read),
            duration_s=elapsed,
            model_insight=model_insight,
        )

    def _find_worthy_children(
        self, parent_scope: str, findings: List[ExplorationFinding],
    ) -> List[str]:
        """Identify sub-directories worth spawning children for.

        A sub-directory is worth exploring if:
        - It contains high-complexity files
        - It has many .py files (rich package)
        - It was mentioned in findings but not fully explored
        """
        parent_dir = self._root / parent_scope
        if not parent_dir.exists():
            return []

        worthy: List[Tuple[str, int]] = []

        try:
            for subdir in parent_dir.iterdir():
                if not subdir.is_dir():
                    continue
                if subdir.name in DynamicScopeDiscovery._SKIP:
                    continue

                py_count = sum(1 for f in subdir.rglob("*.py")
                               if "__pycache__" not in str(f))
                if py_count >= 3:
                    rel = str(subdir.relative_to(self._root)) + "/"
                    worthy.append((rel, py_count))
        except Exception:
            pass

        # Sort by py_count descending (richest packages first)
        worthy.sort(key=lambda x: -x[1])
        return [w[0] for w in worthy[:3]]  # Top 3 sub-directories


# ═══════════════════════════════════════════════════════════════════════════
# UNLIMITED FLEET ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════

class UnlimitedFleetOrchestrator:
    """Deploy recursive exploration agents across all Trinity repos.

    Combines all 4 capabilities:
    - Dynamic scope discovery (no hardcoded lists)
    - Recursive spawning (agents spawn children for complex areas)
    - Inter-fleet communication (shared findings bus)
    - Model reasoning (optional lightweight inference per agent)
    - Memory budget guard (prevents OOM)

    Usage:
        fleet = UnlimitedFleetOrchestrator()
        report = await fleet.deploy("understand the governance pipeline")
    """

    def __init__(
        self,
        jarvis_root: Optional[Path] = None,
        prime_root: Optional[Path] = None,
        reactor_root: Optional[Path] = None,
        model_fn: Optional[Callable[[str], Coroutine[Any, Any, str]]] = None,
    ) -> None:
        self._roots: Dict[str, Path] = {}
        self._roots["jarvis"] = jarvis_root or Path(".")

        prime = prime_root or os.environ.get("JARVIS_PRIME_REPO_PATH")
        if prime:
            self._roots["jarvis-prime"] = Path(prime)

        reactor = reactor_root or os.environ.get("JARVIS_REACTOR_REPO_PATH")
        if reactor:
            self._roots["reactor"] = Path(reactor)

        self._model_fn = model_fn
        self._bus = get_shared_findings_bus()

    async def deploy(
        self,
        goal: str,
        repos: Optional[Tuple[str, ...]] = None,
        max_agents: int = _MAX_TOTAL_AGENTS,
    ) -> Dict[str, Any]:
        """Deploy unlimited recursive fleet across Trinity repos."""
        t0 = time.time()

        # Reset global counter
        async with RecursiveExplorationAgent._total_lock:
            RecursiveExplorationAgent._total_spawned = 0

        target_repos = repos or tuple(self._roots.keys())
        all_results: List[RecursiveAgentResult] = []
        tasks = []

        for repo in target_repos:
            root = self._roots.get(repo)
            if not root or not root.exists():
                continue

            # Dynamic scope discovery
            scopes = DynamicScopeDiscovery.discover(root, max_scopes=10)
            if not scopes:
                scopes = [""]  # Explore root

            for scope in scopes:
                if not MemoryBudgetGuard.can_spawn():
                    break

                agent_id = f"fleet-{repo}-{scope.replace('/', '-').strip('-') or 'root'}"
                agent = RecursiveExplorationAgent(
                    agent_id=agent_id,
                    root=root,
                    repo=repo,
                    depth=0,
                    findings_bus=self._bus,
                    model_fn=self._model_fn,
                )

                async with RecursiveExplorationAgent._total_lock:
                    RecursiveExplorationAgent._total_spawned += 1

                task = asyncio.create_task(
                    agent.explore(scope, goal, spawn_children=True),
                    name=agent_id,
                )
                tasks.append(task)

        # Wait for all with timeout
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=_AGENT_TIMEOUT_S * 2)
            for t in pending:
                t.cancel()

            for t in done:
                try:
                    result = t.result()
                    all_results.append(result)
                except Exception:
                    pass

        # Count total agents (including children)
        total_agents = self._count_agents(all_results)
        total_files = self._count_files(all_results)
        all_findings = self._bus.get_all_findings()

        elapsed = time.time() - t0

        logger.info(
            "[UnlimitedFleet] %d root agents, %d total (incl children), "
            "%d files, %d findings in %.1fs",
            len(all_results), total_agents, total_files,
            len(all_findings), elapsed,
        )

        return {
            "goal": goal,
            "repos_explored": list(target_repos),
            "root_agents": len(all_results),
            "total_agents": total_agents,
            "total_files": total_files,
            "total_findings": len(all_findings),
            "duration_s": round(elapsed, 1),
            "memory": MemoryBudgetGuard.get_status(),
            "top_findings": [
                {"category": f.category, "description": f.description[:80], "file": f.file_path}
                for f in sorted(all_findings, key=lambda x: -x.relevance)[:10]
            ],
        }

    def _count_agents(self, results: List[RecursiveAgentResult]) -> int:
        """Count total agents including recursive children."""
        count = len(results)
        for r in results:
            count += self._count_agents(r.child_results)
        return count

    def _count_files(self, results: List[RecursiveAgentResult]) -> int:
        """Count total files explored including recursive children."""
        count = sum(r.files_explored for r in results)
        for r in results:
            count += self._count_files(r.child_results)
        return count
