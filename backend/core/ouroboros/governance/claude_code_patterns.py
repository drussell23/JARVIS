"""
Claude Code Advanced Patterns — 3 patterns from Claude Code documentation.

1. Explore-First: Read-only reconnaissance before modification
2. Batch Decomposition: Large changes -> parallel PRs with worktrees
3. Path-Scoped Skills: Glob-based rule activation

All subprocess calls are argv-based (no shell). Deterministic infrastructure.
"""
from __future__ import annotations

import asyncio
import ast
import fnmatch
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 1. EXPLORE-FIRST (context:fork + agent:Explore)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ExplorationReport:
    findings: str
    files_examined: List[str]
    risks: List[str]
    duration_s: float = 0.0


class ExploreFirstOrchestrator:
    """Read-only exploration before modification. AST + import tracing."""

    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    async def explore(self, target_files: Tuple[str, ...], description: str) -> ExplorationReport:
        t0 = time.monotonic()
        examined, risks, parts = [], [], []

        for rel in target_files:
            fp = self._root / rel
            if not fp.exists() or not rel.endswith(".py"):
                continue
            try:
                src = fp.read_text()
                examined.append(rel)
                tree = ast.parse(src)
                imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
                lines = len(src.split("\n"))
                test_exists = any((self._root / "tests" / f"test_{Path(rel).stem}.py").exists()
                                  for _ in [1])
                if not test_exists: risks.append(f"{rel}: no tests")
                if lines > 500: risks.append(f"{rel}: {lines} lines")
                parts.append(f"- {rel}: {lines}L, imports={len(imports)}, tests={'yes' if test_exists else 'NO'}")
            except Exception:
                pass

        return ExplorationReport(
            findings="\n".join(parts), files_examined=examined,
            risks=risks, duration_s=time.monotonic() - t0,
        )

    def format_for_prompt(self, r: ExplorationReport) -> str:
        if not r.findings: return ""
        lines = [f"## Exploration ({len(r.files_examined)} files, {r.duration_s:.1f}s)"]
        if r.risks:
            lines.append("**Risks:** " + "; ".join(r.risks))
        lines.append(r.findings)
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# 2. BATCH DECOMPOSITION TO PARALLEL PRs
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class BatchUnit:
    unit_id: str
    goal: str
    target_files: Tuple[str, ...]
    branch_name: str = ""
    pr_url: str = ""
    status: str = "pending"


class BatchDecomposer:
    """Decompose large changes into parallel work units with PRs. Argv-based."""

    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    def decompose_by_directory(self, target_files: Tuple[str, ...], goal: str) -> List[BatchUnit]:
        groups: Dict[str, List[str]] = {}
        for f in target_files:
            groups.setdefault(str(Path(f).parent), []).append(f)
        return [
            BatchUnit(
                unit_id=f"batch-{i:03d}", goal=f"{goal} in {d}/",
                target_files=tuple(files),
                branch_name=f"ouroboros/batch/{d.replace('/', '-')}-{i}",
            )
            for i, (d, files) in enumerate(groups.items())
        ]

    async def create_worktree(self, unit: BatchUnit) -> Optional[Path]:
        wt = self._root / ".worktrees" / unit.unit_id
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "add", "-b", unit.branch_name, str(wt),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(self._root),
            )
            await asyncio.wait_for(proc.communicate(), timeout=30.0)
            return wt if proc.returncode == 0 else None
        except Exception:
            return None

    async def create_pr(self, unit: BatchUnit) -> Optional[str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "pr", "create",
                "--title", f"[Ouroboros batch] {unit.goal[:60]}",
                "--body", f"Automated: {unit.goal}\nUnit: {unit.unit_id}",
                "--head", unit.branch_name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(self._root),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            return stdout.decode().strip() if proc.returncode == 0 else None
        except Exception:
            return None


# ═══════════════════════════════════════════════════════════════════════════
# 3. PATH-SCOPED SKILL ACTIVATION
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ScopedRule:
    name: str
    content: str
    path_patterns: List[str]
    priority: int = 0


class PathScopedSkillRegistry:
    """Rules that activate only when target files match glob patterns."""

    def __init__(self) -> None:
        self._rules: List[ScopedRule] = []

    def register(self, name: str, content: str, patterns: List[str], priority: int = 0) -> None:
        self._rules.append(ScopedRule(name, content, patterns, priority))

    @staticmethod
    def _glob_match(path: str, pattern: str) -> bool:
        """Match a path against a glob pattern. Supports ** for recursive."""
        import re as _re
        # Step-by-step conversion to avoid * clobbering **
        r = pattern
        r = r.replace(".", r"\.")
        # Temporarily encode ** patterns
        r = r.replace("**/", "<<DSTAR_SLASH>>")
        r = r.replace("**", "<<DSTAR>>")
        # Now convert single *
        r = r.replace("*", "[^/]*")
        # Restore ** patterns
        # Replace ? BEFORE restoring placeholders (so ? in (.+/)? isn't clobbered)
        r = r.replace("?", "[^/]")
        r = r.replace("<<DSTAR_SLASH>>", "(.+/)?")
        r = r.replace("<<DSTAR>>", ".*")
        return bool(_re.fullmatch(r, path))

    def get_matching(self, target_files: Tuple[str, ...]) -> List[ScopedRule]:
        matching = []
        for rule in self._rules:
            for target in target_files:
                if any(self._glob_match(target, p) for p in rule.path_patterns):
                    matching.append(rule)
                    break
        matching.sort(key=lambda r: -r.priority)
        return matching

    def format_for_prompt(self, target_files: Tuple[str, ...]) -> str:
        rules = self.get_matching(target_files)
        if not rules: return ""
        lines = ["## Path-Scoped Rules"]
        for r in rules:
            lines.append(f"\n### {r.name}\n{r.content}")
        return "\n".join(lines)

    def load_from_directory(self, rules_dir: Path) -> int:
        if not rules_dir.exists(): return 0
        count = 0
        for md in rules_dir.rglob("*.md"):
            try:
                raw = md.read_text()
                name = md.stem
                patterns: List[str] = []
                content = raw
                if raw.startswith("---"):
                    parts = raw.split("---", 2)
                    if len(parts) >= 3:
                        for line in parts[1].split("\n"):
                            line = line.strip()
                            if line.startswith("- ") and ("*" in line or "/" in line):
                                patterns.append(line[2:].strip().strip('"').strip("'"))
                        content = parts[2].strip()
                self.register(name, content, patterns)
                count += 1
            except Exception:
                pass
        return count
