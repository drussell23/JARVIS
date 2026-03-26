"""
Hybrid Teammate Executor — Routes work to coroutines or isolated subprocesses.
================================================================================

Routes each work unit to the appropriate execution environment based on its
risk profile.  Cognitive tasks (research, review, analysis) run as lightweight
asyncio coroutines in the same event loop.  Mutation tasks (code generation,
test execution, file writes) run in fully isolated subprocesses with JSON-line
IPC.

Architecture::

    ┌─────────────────────────────────────────────────────────────────────┐
    │                   HybridTeammateExecutor                           │
    │                                                                    │
    │   work_unit ──▸ evaluate_risk() ──▸ WorkUnitRiskProfile            │
    │                                       │                            │
    │              ┌────────────────────────┐│┌───────────────────────┐   │
    │              │   isolation_mode ==    │││  isolation_mode ==    │   │
    │              │     "coroutine"        │││    "subprocess"       │   │
    │              │                        │││                       │   │
    │              │  ┌──────────────────┐  │││  ┌─────────────────┐  │   │
    │              │  │ _CoroutineRunner │  │││  │ _SubprocessRunner│  │   │
    │              │  │   (same loop)    │  │││  │ (isolated proc)  │  │   │
    │              │  │                  │  │││  │                  │  │   │
    │              │  │  read_file       │  │││  │ stdin: JSON work │  │   │
    │              │  │  search_code     │  │││  │ stdout: JSON     │  │   │
    │              │  │  list_symbols    │  │││  │   findings/      │  │   │
    │              │  │                  │  │││  │   progress/      │  │   │
    │              │  │  SharedFindings  │  │││  │   result         │  │   │
    │              │  │  Bus publish     │  │││  │                  │  │   │
    │              │  └──────────────────┘  │││  │ MemoryBudget     │  │   │
    │              │                        │││  │ Guard gated      │  │   │
    │              └────────────────────────┘│└───────────────────────┘   │
    │                                       │                            │
    │                          WorkUnitResult ◂──────────────────────────┘│
    └─────────────────────────────────────────────────────────────────────┘

Boundary Principle:
  Deterministic: Risk classification (role/phase/file-extension rules),
  isolation mode selection, subprocess lifecycle management, timeout enforcement,
  memory budget checks, JSON-line protocol parsing.
  Agentic: The actual work performed inside coroutines and subprocesses —
  file exploration strategy, code analysis, patch generation.
"""
from __future__ import annotations

import asyncio
import ast
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (all via env vars with sane defaults)
# ---------------------------------------------------------------------------

_SUBPROCESS_TIMEOUT_S = float(
    os.environ.get("JARVIS_SUBPROCESS_AGENT_TIMEOUT_S", "120")
)
_COROUTINE_TIMEOUT_S = float(
    os.environ.get("JARVIS_COROUTINE_AGENT_TIMEOUT_S", "60")
)
_PROJECT_ROOT = Path(
    os.environ.get("JARVIS_REPO_PATH", ".")
).resolve()
_WORKER_MODULE = os.environ.get(
    "JARVIS_ISOLATED_WORKER_MODULE",
    "backend.core.ouroboros.governance.isolated_agent_worker",
)
_PYTHON_BIN = os.environ.get("JARVIS_PYTHON_BIN", "python3")

# File extensions considered mutable source code
_MUTABLE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".rs", ".go", ".js", ".ts", ".tsx", ".jsx",
    ".java", ".c", ".cpp", ".h", ".hpp", ".rb", ".swift",
})

# Phases that produce mutations
_MUTATION_PHASES: frozenset[str] = frozenset({
    "generate", "apply", "generate_retry",
})

# Roles that are inherently read-only
_READONLY_ROLES: frozenset[str] = frozenset({
    "researcher", "reviewer", "analyst", "explorer",
})

# Roles that may mutate when in mutation phases
_WORKER_ROLES: frozenset[str] = frozenset({
    "worker", "implementer", "fixer", "generator",
})

# Operation types that require execution
_EXECUTION_OP_TYPES: frozenset[str] = frozenset({
    "code_generation", "test_execution", "script_run",
    "patch_apply", "refactor", "build",
})


# ═══════════════════════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class WorkUnitRiskProfile:
    """Risk classification for a single work unit.

    Determines whether the unit runs as a lightweight coroutine (read-only,
    same event loop) or an isolated subprocess (mutation-capable, sandboxed).
    """

    requires_execution: bool = False
    """Work unit runs code, subprocess, or shell command."""

    mutates_state: bool = False
    """Work unit writes files, modifies git, or alters system state."""

    isolation_mode: str = "coroutine"
    """Execution environment: ``"coroutine"`` or ``"subprocess"``."""

    risk_signals: List[str] = field(default_factory=list)
    """Human-readable reasons for the classification decision."""


@dataclass
class WorkUnitResult:
    """Result from executing a work unit in either isolation mode."""

    success: bool
    findings: List[Dict[str, Any]] = field(default_factory=list)
    patches: List[Dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0
    isolation_mode: str = "coroutine"
    error: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# Hybrid Teammate Executor
# ═══════════════════════════════════════════════════════════════════════════


class HybridTeammateExecutor:
    """Routes work units to coroutines or isolated subprocesses.

    The executor is stateless — all decisions are deterministic functions of
    the work unit dict.  No model calls, no network access, no randomness.

    Usage::

        executor = HybridTeammateExecutor(project_root=Path("."))
        profile = executor.evaluate_risk(work_unit)
        result = await executor.execute(work_unit, profile, goal="fix auth bug")
    """

    def __init__(
        self,
        project_root: Optional[Path] = None,
        findings_bus: Optional[Any] = None,
    ) -> None:
        self._root = (project_root or _PROJECT_ROOT).resolve()
        self._findings_bus = findings_bus  # Optional SharedFindingsBus

    # ------------------------------------------------------------------
    # Risk Evaluation (deterministic — no model calls)
    # ------------------------------------------------------------------

    def evaluate_risk(self, work_unit: Dict[str, Any]) -> WorkUnitRiskProfile:
        """Classify a work unit's risk profile deterministically.

        Decision tree:
          1. Role-based: read-only roles always get coroutine mode.
          2. Phase-based: mutation phases with worker roles get subprocess.
          3. Operation-type-based: execution ops get subprocess.
          4. File-based: mutable source files in mutation phases get subprocess.
          5. Default: coroutine (least privilege).
        """
        signals: List[str] = []
        requires_execution = False
        mutates_state = False

        role = str(work_unit.get("role", "")).lower().strip()
        phase = str(work_unit.get("phase", "")).lower().strip()
        operation_type = str(work_unit.get("operation_type", "")).lower().strip()
        target_files: List[str] = work_unit.get("target_files", []) or []

        # --- Rule 1: Read-only roles always get coroutine ---
        if role in _READONLY_ROLES:
            signals.append(f"role={role!r} is read-only → coroutine")
            return WorkUnitRiskProfile(
                requires_execution=False,
                mutates_state=False,
                isolation_mode="coroutine",
                risk_signals=signals,
            )

        # --- Rule 2: Worker roles in mutation phases ---
        if role in _WORKER_ROLES and phase in _MUTATION_PHASES:
            requires_execution = True
            mutates_state = True
            signals.append(
                f"role={role!r} in phase={phase!r} → requires execution + mutation"
            )

        # --- Rule 3: Execution operation types ---
        if operation_type in _EXECUTION_OP_TYPES:
            requires_execution = True
            signals.append(
                f"operation_type={operation_type!r} → requires execution"
            )

        # --- Rule 4: Mutable source files in mutation phases ---
        if target_files and phase in _MUTATION_PHASES:
            mutable_targets = [
                f for f in target_files
                if Path(f).suffix.lower() in _MUTABLE_EXTENSIONS
            ]
            if mutable_targets:
                mutates_state = True
                signals.append(
                    f"{len(mutable_targets)} mutable source file(s) "
                    f"in phase={phase!r} → mutates state"
                )

        # --- Isolation decision ---
        if requires_execution or mutates_state:
            isolation_mode = "subprocess"
            signals.append("requires_execution OR mutates_state → subprocess")
        else:
            isolation_mode = "coroutine"
            if not signals:
                signals.append("no mutation signals detected → coroutine (default)")

        return WorkUnitRiskProfile(
            requires_execution=requires_execution,
            mutates_state=mutates_state,
            isolation_mode=isolation_mode,
            risk_signals=signals,
        )

    # ------------------------------------------------------------------
    # Execution Router
    # ------------------------------------------------------------------

    async def execute(
        self,
        work_unit: Dict[str, Any],
        profile: WorkUnitRiskProfile,
        goal: str,
        on_finding: Optional[Callable[[Dict[str, Any]], Coroutine]] = None,
    ) -> WorkUnitResult:
        """Execute a work unit using the appropriate isolation mode.

        Routes to :class:`_CoroutineRunner` or :class:`_SubprocessRunner`
        based on the risk profile's ``isolation_mode``.
        """
        logger.info(
            "[HybridExecutor] Executing work unit role=%s phase=%s "
            "isolation=%s signals=%s",
            work_unit.get("role", "?"),
            work_unit.get("phase", "?"),
            profile.isolation_mode,
            profile.risk_signals,
        )

        if profile.isolation_mode == "subprocess":
            runner = _SubprocessRunner(
                project_root=self._root,
            )
            return await runner.run(work_unit, goal)

        # Default: coroutine
        runner_co = _CoroutineRunner(
            project_root=self._root,
            findings_bus=self._findings_bus,
            on_finding=on_finding,
        )
        return await runner_co.run(work_unit, goal)


# ═══════════════════════════════════════════════════════════════════════════
# Coroutine Runner (same event loop, read-only tools)
# ═══════════════════════════════════════════════════════════════════════════


class _CoroutineRunner:
    """Runs cognitive work as an asyncio task in the current event loop.

    Has access to read-only tools only:
      - ``read_file``: read file contents
      - ``search_code``: regex search across files
      - ``list_symbols``: AST-based function/class listing

    Publishes findings to SharedFindingsBus if provided.
    """

    def __init__(
        self,
        project_root: Path,
        findings_bus: Optional[Any] = None,
        on_finding: Optional[Callable[[Dict[str, Any]], Coroutine]] = None,
    ) -> None:
        self._root = project_root
        self._findings_bus = findings_bus
        self._on_finding = on_finding

    async def run(
        self,
        work_unit: Dict[str, Any],
        goal: str,
    ) -> WorkUnitResult:
        """Execute the work unit as an asyncio coroutine."""
        t0 = time.monotonic()
        findings: List[Dict[str, Any]] = []
        patches: List[Dict[str, Any]] = []

        try:
            result = await asyncio.wait_for(
                asyncio.shield(self._do_work(work_unit, goal, findings)),
                timeout=_COROUTINE_TIMEOUT_S,
            )
            duration = time.monotonic() - t0
            return WorkUnitResult(
                success=True,
                findings=findings,
                patches=patches,
                duration_s=round(duration, 3),
                isolation_mode="coroutine",
            )
        except asyncio.TimeoutError:
            duration = time.monotonic() - t0
            logger.warning(
                "[CoroutineRunner] Timed out after %.1fs (limit %.0fs)",
                duration, _COROUTINE_TIMEOUT_S,
            )
            return WorkUnitResult(
                success=False,
                findings=findings,
                duration_s=round(duration, 3),
                isolation_mode="coroutine",
                error=f"Coroutine timed out after {duration:.1f}s",
            )
        except asyncio.CancelledError:
            raise  # Never swallow cancellation
        except Exception as exc:
            duration = time.monotonic() - t0
            logger.exception("[CoroutineRunner] Work unit failed: %s", exc)
            return WorkUnitResult(
                success=False,
                findings=findings,
                duration_s=round(duration, 3),
                isolation_mode="coroutine",
                error=str(exc),
            )

    async def _do_work(
        self,
        work_unit: Dict[str, Any],
        goal: str,
        findings: List[Dict[str, Any]],
    ) -> None:
        """Perform the actual cognitive work (exploration, review, analysis)."""
        target_files: List[str] = work_unit.get("target_files", []) or []
        search_terms: List[str] = work_unit.get("search_terms", []) or []
        task_goal: str = work_unit.get("goal", goal)

        # Phase 1: Read target files and collect information
        for rel_path in target_files:
            content = await self._read_file(rel_path)
            if content is not None:
                symbols = self._list_symbols(content, rel_path)
                finding = {
                    "type": "file_analysis",
                    "file": rel_path,
                    "symbols": symbols,
                    "line_count": content.count("\n") + 1,
                    "goal": task_goal,
                }
                findings.append(finding)
                await self._publish_finding(finding)

        # Phase 2: Search for relevant code patterns
        for term in search_terms:
            matches = await self._search_code(term)
            if matches:
                finding = {
                    "type": "search_result",
                    "query": term,
                    "matches": matches[:20],  # Cap to avoid bloat
                    "total_matches": len(matches),
                }
                findings.append(finding)
                await self._publish_finding(finding)

        # Phase 3: If no specific targets, explore from goal context
        if not target_files and not search_terms:
            # Extract potential search terms from the goal
            goal_terms = self._extract_search_terms(task_goal)
            for term in goal_terms[:3]:
                matches = await self._search_code(term)
                if matches:
                    finding = {
                        "type": "goal_search",
                        "query": term,
                        "matches": matches[:10],
                        "total_matches": len(matches),
                    }
                    findings.append(finding)
                    await self._publish_finding(finding)

    # --- Read-only tools ---

    async def _read_file(self, rel_path: str) -> Optional[str]:
        """Read a file relative to project root. Returns None on error."""
        full = self._root / rel_path
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, full.read_text, "utf-8",
            )
        except Exception as exc:
            logger.debug("[CoroutineRunner] Cannot read %s: %s", rel_path, exc)
            return None

    async def _search_code(
        self,
        pattern: str,
        max_results: int = 50,
    ) -> List[Dict[str, Any]]:
        """Regex search across Python files in the project."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._search_code_sync, pattern, max_results,
        )

    def _search_code_sync(
        self,
        pattern: str,
        max_results: int,
    ) -> List[Dict[str, Any]]:
        """Synchronous regex search (runs in executor)."""
        matches: List[Dict[str, Any]] = []
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return matches

        for py_file in self._root.rglob("*.py"):
            if len(matches) >= max_results:
                break
            # Skip known non-source directories
            parts = py_file.parts
            if any(
                skip in parts
                for skip in ("venv", "__pycache__", "node_modules", ".git", ".tox")
            ):
                continue
            try:
                text = py_file.read_text("utf-8", errors="replace")
                for i, line in enumerate(text.splitlines(), 1):
                    if compiled.search(line):
                        matches.append({
                            "file": str(py_file.relative_to(self._root)),
                            "line": i,
                            "text": line.strip()[:200],
                        })
                        if len(matches) >= max_results:
                            break
            except Exception:
                continue
        return matches

    @staticmethod
    def _list_symbols(content: str, file_path: str) -> List[Dict[str, str]]:
        """Extract function and class names via AST."""
        symbols: List[Dict[str, str]] = []
        try:
            tree = ast.parse(content, filename=file_path)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    symbols.append({"type": "function", "name": node.name, "line": str(node.lineno)})
                elif isinstance(node, ast.AsyncFunctionDef):
                    symbols.append({"type": "async_function", "name": node.name, "line": str(node.lineno)})
                elif isinstance(node, ast.ClassDef):
                    symbols.append({"type": "class", "name": node.name, "line": str(node.lineno)})
        except SyntaxError:
            pass
        return symbols

    @staticmethod
    def _extract_search_terms(goal: str) -> List[str]:
        """Extract likely code search terms from a natural-language goal."""
        # Pull out quoted terms, CamelCase words, snake_case identifiers
        terms: List[str] = []
        # Quoted terms
        for match in re.finditer(r'"([^"]+)"|\'([^\']+)\'', goal):
            terms.append(match.group(1) or match.group(2))
        # CamelCase words (likely class names)
        for match in re.finditer(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b', goal):
            terms.append(match.group())
        # snake_case identifiers
        for match in re.finditer(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b', goal):
            terms.append(match.group())
        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: List[str] = []
        for t in terms:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        return deduped

    async def _publish_finding(self, finding: Dict[str, Any]) -> None:
        """Publish to SharedFindingsBus and/or on_finding callback."""
        if self._findings_bus is not None:
            try:
                # SharedFindingsBus expects ExplorationFinding, but we publish
                # raw dicts via its publish() — caller wraps if needed
                from backend.core.ouroboros.governance.exploration_subagent import (
                    ExplorationFinding,
                )
                ef = ExplorationFinding(
                    category=finding.get("type", "unknown"),
                    description=json.dumps(finding, default=str)[:500],
                    file_path=finding.get("file", ""),
                )
                await self._findings_bus.publish(ef)
            except Exception:
                pass  # Best-effort — never block work on bus failures

        if self._on_finding is not None:
            try:
                await self._on_finding(finding)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# Subprocess Runner (isolated process, mutation-capable)
# ═══════════════════════════════════════════════════════════════════════════


class _SubprocessRunner:
    """Runs work in an isolated subprocess via JSON-line IPC.

    Spawns ``python3 -m backend.core.ouroboros.governance.isolated_agent_worker``
    as a child process.  Communication protocol:

    **stdin** (parent → child): Single JSON line containing the work unit.

    **stdout** (child → parent): JSON lines, one per event::

        {"type": "finding", "data": {...}}
        {"type": "progress", "pct": 50, "message": "..."}
        {"type": "result", "success": true, "findings": [...], "patches": [...]}

    **stderr** (child → parent): Logged at DEBUG level.

    Guards:
      - ``MemoryBudgetGuard.can_spawn()`` checked before launch.
      - ``asyncio.wait_for()`` enforces timeout.
      - Process is killed (SIGKILL) on timeout.
    """

    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    async def run(
        self,
        work_unit: Dict[str, Any],
        goal: str,
    ) -> WorkUnitResult:
        """Spawn an isolated subprocess and collect results."""
        t0 = time.monotonic()

        # --- Memory budget guard ---
        if not self._check_memory_budget():
            return WorkUnitResult(
                success=False,
                duration_s=0.0,
                isolation_mode="subprocess",
                error="Memory budget exceeded — subprocess spawn blocked",
            )

        # --- Prepare payload ---
        payload = {
            "work_unit": work_unit,
            "goal": goal,
            "project_root": str(self._root),
        }
        payload_bytes = (json.dumps(payload, default=str) + "\n").encode("utf-8")

        # --- Spawn subprocess ---
        findings: List[Dict[str, Any]] = []
        patches: List[Dict[str, Any]] = []
        error: Optional[str] = None
        success = False

        try:
            proc = await asyncio.create_subprocess_exec(
                _PYTHON_BIN, "-m", _WORKER_MODULE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._root),
            )
            logger.info(
                "[SubprocessRunner] Spawned worker pid=%s timeout=%.0fs",
                proc.pid, _SUBPROCESS_TIMEOUT_S,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=payload_bytes),
                    timeout=_SUBPROCESS_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                # Kill the timed-out process
                logger.warning(
                    "[SubprocessRunner] Worker pid=%s timed out after %.0fs — killing",
                    proc.pid, _SUBPROCESS_TIMEOUT_S,
                )
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
                duration = time.monotonic() - t0
                return WorkUnitResult(
                    success=False,
                    findings=findings,
                    duration_s=round(duration, 3),
                    isolation_mode="subprocess",
                    error=f"Subprocess timed out after {_SUBPROCESS_TIMEOUT_S:.0f}s",
                )

            # --- Parse stdout JSON lines ---
            if stderr_bytes:
                for line in stderr_bytes.decode("utf-8", errors="replace").splitlines():
                    if line.strip():
                        logger.debug("[SubprocessRunner:stderr] %s", line.rstrip())

            if stdout_bytes:
                for raw_line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug(
                            "[SubprocessRunner] Non-JSON stdout line: %s",
                            line[:200],
                        )
                        continue

                    msg_type = msg.get("type", "")
                    if msg_type == "finding":
                        findings.append(msg.get("data", msg))
                    elif msg_type == "progress":
                        logger.debug(
                            "[SubprocessRunner] Progress: %s%% — %s",
                            msg.get("pct", "?"),
                            msg.get("message", ""),
                        )
                    elif msg_type == "result":
                        success = msg.get("success", False)
                        findings.extend(msg.get("findings", []))
                        patches.extend(msg.get("patches", []))
                        if msg.get("error"):
                            error = msg["error"]
                    else:
                        logger.debug(
                            "[SubprocessRunner] Unknown message type: %s",
                            msg_type,
                        )

            # If no explicit result message, infer from exit code
            if proc.returncode == 0 and not error:
                success = True
            elif proc.returncode != 0 and not error:
                error = f"Subprocess exited with code {proc.returncode}"

        except asyncio.CancelledError:
            raise  # Never swallow cancellation
        except Exception as exc:
            logger.exception("[SubprocessRunner] Failed to run worker: %s", exc)
            error = f"Subprocess launch/communication failed: {exc}"

        duration = time.monotonic() - t0
        return WorkUnitResult(
            success=success,
            findings=findings,
            patches=patches,
            duration_s=round(duration, 3),
            isolation_mode="subprocess",
            error=error,
        )

    @staticmethod
    def _check_memory_budget() -> bool:
        """Check if MemoryBudgetGuard allows subprocess spawning."""
        try:
            from backend.core.ouroboros.governance.unlimited_agents import (
                MemoryBudgetGuard,
            )
            can = MemoryBudgetGuard.can_spawn()
            if not can:
                logger.warning(
                    "[SubprocessRunner] MemoryBudgetGuard blocked subprocess spawn"
                )
            return can
        except ImportError:
            # If unlimited_agents isn't available, allow spawn
            logger.debug(
                "[SubprocessRunner] MemoryBudgetGuard not available — allowing spawn"
            )
            return True
