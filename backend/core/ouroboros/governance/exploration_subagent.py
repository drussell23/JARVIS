"""
ExplorationSubagent — Read-only autonomous agent that explores before acting.

Like Claude Code's Explore subagent: can search code, read files, analyze
imports, trace call chains, and report findings — all WITHOUT modifying
anything. Used before code generation to build comprehensive understanding.

The exploration agent has access to:
  - read_file (read any file in the repo)
  - search_code (regex search across files)
  - list_symbols (AST function/class listing)
  - get_callers (find call sites)
  - code_explore (run Python snippets for hypothesis testing)
  - web_search (DuckDuckGo for external context)

It does NOT have access to: Edit, Write, Bash, or any mutation tools.

Boundary Principle:
  Deterministic: Tool dispatch, file reading, regex search.
  Agentic: What to explore and how to synthesize findings (via model).
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ExplorationFinding:
    """One finding from exploration."""
    category: str              # "import_chain", "call_graph", "test_gap", "complexity", "pattern"
    description: str
    file_path: str = ""
    evidence: str = ""
    relevance: float = 0.0     # 0.0–1.0


@dataclass
class ExplorationReport:
    """Complete report from an exploration session."""
    goal: str
    findings: List[ExplorationFinding]
    files_read: List[str]
    search_queries: List[str]
    duration_s: float
    summary: str = ""


class ExplorationSubagent:
    """Read-only autonomous agent for codebase exploration.

    Given a goal (e.g., "understand the auth module"), the agent
    autonomously reads files, searches code, traces imports, and
    builds a comprehensive report — without modifying anything.

    This runs BEFORE code generation to give the model better context.
    Like Claude Code's Explore subagent but integrated into Ouroboros.

    Usage:
        agent = ExplorationSubagent(project_root)
        report = await agent.explore(
            goal="understand voice_unlock authentication flow",
            entry_files=("backend/voice_unlock/core/verify.py",),
        )
        # report.findings contains structured insights
        # report.summary is injectable into the generation prompt
    """

    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    async def explore(
        self,
        goal: str,
        entry_files: Tuple[str, ...] = (),
        max_files: int = 20,
        max_depth: int = 3,
    ) -> ExplorationReport:
        """Autonomously explore the codebase for a given goal.

        Starts from entry_files (or infers from goal), then follows
        imports, call chains, and test relationships to build a
        comprehensive understanding.

        All read-only — never modifies files.
        """
        import time
        t0 = time.monotonic()

        findings: List[ExplorationFinding] = []
        files_read: List[str] = []
        search_queries: List[str] = []
        visited: set[str] = set()

        # Phase 1: Identify starting files
        start_files = list(entry_files) or self._infer_entry_files(goal)

        # Phase 2: Read and analyze each starting file
        to_explore = list(start_files)
        depth = 0

        while to_explore and depth < max_depth and len(files_read) < max_files:
            next_round: List[str] = []

            for rel_path in to_explore:
                if rel_path in visited:
                    continue
                visited.add(rel_path)

                full = self._root / rel_path
                if not full.exists() or not rel_path.endswith(".py"):
                    continue

                # Read and analyze
                analysis = self._analyze_file(full, rel_path)
                if analysis:
                    files_read.append(rel_path)
                    findings.extend(analysis["findings"])

                    # Follow imports to discover related files
                    for imp in analysis.get("imports", []):
                        imp_path = self._resolve_import(imp)
                        if imp_path and imp_path not in visited:
                            next_round.append(imp_path)

                    # Follow test counterparts
                    test_path = self._find_test_file(rel_path)
                    if test_path and test_path not in visited:
                        next_round.append(test_path)

            to_explore = next_round[:10]  # Cap per-round expansion
            depth += 1

        # Phase 3: Search for patterns mentioned in the goal
        if goal:
            keywords = self._extract_keywords(goal)
            for keyword in keywords[:3]:
                search_queries.append(keyword)
                search_results = self._search_codebase(keyword)
                for sr in search_results[:5]:
                    findings.append(ExplorationFinding(
                        category="pattern",
                        description=f"Found '{keyword}' in {sr['file']}:{sr['line']}",
                        file_path=sr["file"],
                        evidence=sr["context"][:100],
                        relevance=0.5,
                    ))

        # Phase 4: Generate summary
        summary = self._synthesize_summary(goal, findings, files_read)

        elapsed = time.monotonic() - t0

        report = ExplorationReport(
            goal=goal,
            findings=findings,
            files_read=files_read,
            search_queries=search_queries,
            duration_s=elapsed,
            summary=summary,
        )

        logger.info(
            "[ExploreAgent] Explored %d files, %d findings in %.1fs for: %s",
            len(files_read), len(findings), elapsed, goal[:50],
        )
        return report

    def format_for_prompt(self, report: ExplorationReport) -> str:
        """Format exploration report for generation prompt injection."""
        if not report.findings:
            return ""

        lines = [
            f"## Exploration Report: {report.goal}",
            f"Explored {len(report.files_read)} files in {report.duration_s:.1f}s",
            "",
        ]

        # Group findings by category
        by_cat: Dict[str, List[ExplorationFinding]] = {}
        for f in report.findings:
            by_cat.setdefault(f.category, []).append(f)

        for cat, cat_findings in by_cat.items():
            lines.append(f"### {cat} ({len(cat_findings)} findings)")
            for f in cat_findings[:5]:
                lines.append(f"- {f.description}")
                if f.evidence:
                    lines.append(f"  Evidence: {f.evidence[:60]}")
            lines.append("")

        if report.summary:
            lines.append(f"**Summary:** {report.summary}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Read-only tools (no mutation)
    # ------------------------------------------------------------------

    def _analyze_file(self, full_path: Path, rel_path: str) -> Optional[Dict[str, Any]]:
        """Analyze a single file. Read-only — AST + content inspection."""
        try:
            source = full_path.read_text(errors="replace")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):
            return None

        findings: List[ExplorationFinding] = []
        imports: List[str] = []

        # Extract imports
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

        # Extract functions with their signatures
        functions = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node) or ""
                functions.append({
                    "name": node.name,
                    "line": node.lineno,
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                    "doc": doc.split("\n")[0][:80] if doc else "",
                })

        # Extract classes
        classes = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                bases = [getattr(b, "id", getattr(b, "attr", "?")) for b in node.bases]
                classes.append({
                    "name": node.name,
                    "bases": bases,
                    "line": node.lineno,
                })

        # Complexity
        branches = sum(
            1 for n in ast.walk(tree)
            if isinstance(n, (ast.If, ast.For, ast.While, ast.ExceptHandler))
        )

        lines = len(source.split("\n"))

        # Build findings
        if branches > 30:
            findings.append(ExplorationFinding(
                category="complexity",
                description=f"{rel_path} has high complexity ({branches} branches, {lines} lines)",
                file_path=rel_path,
                relevance=0.7,
            ))

        if classes:
            findings.append(ExplorationFinding(
                category="structure",
                description=f"{rel_path}: {len(classes)} classes ({', '.join(c['name'] for c in classes[:5])})",
                file_path=rel_path,
                relevance=0.5,
            ))

        if functions:
            public_fns = [f for f in functions if not f["name"].startswith("_")]
            findings.append(ExplorationFinding(
                category="api_surface",
                description=f"{rel_path}: {len(public_fns)} public functions",
                file_path=rel_path,
                evidence=", ".join(f["name"] for f in public_fns[:5]),
                relevance=0.4,
            ))

        # Check for import_chain relationships
        for imp in imports[:5]:
            findings.append(ExplorationFinding(
                category="import_chain",
                description=f"{rel_path} imports {imp}",
                file_path=rel_path,
                relevance=0.3,
            ))

        return {
            "findings": findings,
            "imports": imports,
            "functions": functions,
            "classes": classes,
            "lines": lines,
            "complexity": branches,
        }

    def _resolve_import(self, module_path: str) -> Optional[str]:
        """Resolve a module import to a file path. Deterministic."""
        rel = module_path.replace(".", "/") + ".py"
        if (self._root / rel).exists():
            return rel
        # Try as package
        pkg = module_path.replace(".", "/") + "/__init__.py"
        if (self._root / pkg).exists():
            return pkg
        return None

    def _find_test_file(self, rel_path: str) -> Optional[str]:
        """Find the test counterpart for a file."""
        stem = Path(rel_path).stem
        test = f"tests/test_{stem}.py"
        if (self._root / test).exists():
            return test
        return None

    def _search_codebase(self, keyword: str) -> List[Dict[str, Any]]:
        """Search for a keyword in the codebase. Read-only grep."""
        results = []
        try:
            pattern = re.compile(re.escape(keyword), re.IGNORECASE)
            for py_file in self._root.rglob("*.py"):
                if any(skip in py_file.parts for skip in ("venv", "__pycache__", "node_modules")):
                    continue
                try:
                    content = py_file.read_text(errors="replace")
                    for i, line in enumerate(content.split("\n"), 1):
                        if pattern.search(line):
                            results.append({
                                "file": str(py_file.relative_to(self._root)),
                                "line": i,
                                "context": line.strip()[:100],
                            })
                            if len(results) >= 20:
                                return results
                except Exception:
                    pass
        except Exception:
            pass
        return results

    def _extract_keywords(self, goal: str) -> List[str]:
        """Extract searchable keywords from a goal. Deterministic."""
        # Remove common words
        stop_words = frozenset({
            "the", "a", "an", "in", "on", "at", "to", "for", "of", "and",
            "or", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would",
            "could", "should", "may", "might", "can", "shall",
            "this", "that", "these", "those", "it", "its",
            "how", "what", "where", "when", "why", "which", "who",
            "fix", "update", "add", "remove", "change", "modify",
            "understand", "explain", "find", "check", "look",
        })
        words = re.findall(r"\b[a-z_]+\b", goal.lower())
        return [w for w in words if w not in stop_words and len(w) > 2]

    def _synthesize_summary(
        self, goal: str, findings: List[ExplorationFinding], files: List[str],
    ) -> str:
        """Generate a deterministic summary from findings."""
        if not findings:
            return "No relevant findings."

        parts = [f"Explored {len(files)} files for: {goal}"]

        complexities = [f for f in findings if f.category == "complexity"]
        if complexities:
            parts.append(f"{len(complexities)} complex files identified")

        imports = [f for f in findings if f.category == "import_chain"]
        if imports:
            parts.append(f"{len(imports)} import relationships traced")

        patterns = [f for f in findings if f.category == "pattern"]
        if patterns:
            parts.append(f"{len(patterns)} pattern matches found")

        return ". ".join(parts) + "."
