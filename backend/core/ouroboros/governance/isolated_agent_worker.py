"""
Isolated Agent Worker — Subprocess entry point for mutation-capable work units.
================================================================================

Spawned by :class:`_SubprocessRunner` in ``hybrid_teammate_executor.py``.
Runs as a completely isolated Python process with its own imports, no shared
state with the parent, and no access to unified_supervisor or other heavy
singleton modules.

Communication Protocol (JSON lines over stdin/stdout)::

    ┌────────────────┐     stdin (1 JSON line)      ┌─────────────────────┐
    │   Parent       │ ──────────────────────────▸  │  isolated_agent_    │
    │   (_Subprocess │                              │  worker.py          │
    │    Runner)     │  ◂────────────────────────── │                     │
    │                │     stdout (N JSON lines)    │  - File reading     │
    └────────────────┘                              │  - AST analysis     │
                          {"type": "finding", ...}  │  - Regex search     │
                          {"type": "progress", ...} │  - Test execution   │
                          {"type": "result", ...}   │    (pytest)         │
                                                    └─────────────────────┘

stdin payload (two formats accepted for backward compat)::

    Legacy (from SubprocessRunner v1):
    {
      "op_id": "...",
      "goal": "...",
      "target_files": [...],
      "work_unit": {"operation_type": "..."}
    }

    New (from HybridTeammateExecutor):
    {
      "work_unit": {
        "role": "worker",
        "phase": "apply",
        "goal": "Fix the auth bug in login.py",
        "target_files": ["backend/auth/login.py"],
        "operation_type": "code_generation",
        "search_terms": [...],
        "patches": [...]
      },
      "goal": "Fix authentication bypass",
      "project_root": "/path/to/repo"
    }

stdout JSON line types::

    {"type": "finding",  "data": {"category": "...", "description": "...", ...}}
    {"type": "progress", "pct": 50, "message": "Analyzing imports..."}
    {"type": "result",   "success": true, "findings": [...], "patches": [...]}

Exit codes:
    0 -- success (result message already emitted)
    1 -- failure (result message with error already emitted)

IMPORTANT: This module deliberately avoids importing any heavy modules
(unified_supervisor, torch, sounddevice, etc.).  It uses only stdlib +
pathlib for maximum isolation and fast startup.

Boundary Principle:
  Deterministic: JSON protocol parsing, file I/O, AST walking, regex search,
  subprocess pytest execution, exit code mapping.
  Agentic: What files to explore, what patterns to search, how to synthesize
  findings into patches (driven by work unit content, not model calls).
"""
from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("JARVIS_WORKER_DEBUG") else logging.WARNING,
    format="%(asctime)s [worker] %(message)s",
    stream=sys.stderr,  # logs to stderr, protocol on stdout
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration (all via env vars with sane defaults)
# ---------------------------------------------------------------------------

_MAX_FILE_SIZE_BYTES = int(
    os.environ.get("JARVIS_WORKER_MAX_FILE_SIZE", str(2 * 1024 * 1024))  # 2 MB
)
_MAX_SEARCH_RESULTS = int(os.environ.get("JARVIS_WORKER_MAX_SEARCH_RESULTS", "100"))
_TEST_TIMEOUT_S = int(os.environ.get("JARVIS_WORKER_TEST_TIMEOUT_S", "60"))
_PYTHON_BIN = os.environ.get("JARVIS_PYTHON_BIN", "python3")

# Directories to skip during search/walk
_SKIP_DIRS: frozenset[str] = frozenset({
    "venv", "__pycache__", "node_modules", ".git", ".worktrees",
    "site-packages", "dist", "build", ".tox", ".mypy_cache",
    ".pytest_cache", "htmlcov", ".eggs",
})


# ═══════════════════════════════════════════════════════════════════════════
# Output Helpers (JSON lines to stdout)
# ═══════════════════════════════════════════════════════════════════════════


def _emit(msg: Dict[str, Any]) -> None:
    """Write a single JSON line to stdout (parent reads this)."""
    try:
        line = json.dumps(msg, default=str)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass  # Cannot fail — stdout is our only channel


def _emit_finding(category: str, description: str, file_path: str = "",
                  relevance: float = 0.5, **extra: Any) -> None:
    """Emit a finding to the parent process."""
    data: Dict[str, Any] = {
        "category": category,
        "description": description,
        "file_path": file_path,
        "relevance": relevance,
    }
    data.update(extra)
    _emit({"type": "finding", "data": data})


def _emit_progress(pct: int, message: str) -> None:
    """Emit a progress update to the parent process."""
    _emit({"type": "progress", "pct": pct, "message": message})


def _emit_result(
    success: bool,
    findings: Optional[List[Dict[str, Any]]] = None,
    patches: Optional[List[Dict[str, Any]]] = None,
    error: Optional[str] = None,
) -> None:
    """Emit the final result to the parent process."""
    msg: Dict[str, Any] = {
        "type": "result",
        "success": success,
        "findings": findings or [],
        "patches": patches or [],
    }
    if error:
        msg["error"] = error
    _emit(msg)


# ═══════════════════════════════════════════════════════════════════════════
# Read-only Tools
# ═══════════════════════════════════════════════════════════════════════════


def read_file(root: Path, rel_path: str) -> Optional[str]:
    """Read a file relative to the project root.  Returns None on error."""
    full = (root / rel_path).resolve()

    # Security: ensure the resolved path is under the project root
    try:
        full.relative_to(root)
    except ValueError:
        return None

    if not full.is_file():
        return None

    try:
        size = full.stat().st_size
        if size > _MAX_FILE_SIZE_BYTES:
            return None
        return full.read_text("utf-8", errors="replace")
    except Exception:
        return None


def list_symbols(source: str, filename: str = "<unknown>") -> List[Dict[str, Any]]:
    """Extract function/class definitions via AST."""
    symbols: List[Dict[str, Any]] = []
    try:
        tree = ast.parse(source, filename=filename)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                symbols.append({
                    "type": kind,
                    "name": node.name,
                    "line": node.lineno,
                    "args": [a.arg for a in node.args.args],
                })
            elif isinstance(node, ast.ClassDef):
                symbols.append({
                    "type": "class",
                    "name": node.name,
                    "line": node.lineno,
                    "bases": [_ast_name(b) for b in node.bases],
                })
    except SyntaxError:
        pass
    return symbols


def _ast_name(node: ast.expr) -> str:
    """Extract a name string from an AST node (best-effort)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_ast_name(node.value)}.{node.attr}"
    return "?"


def search_code(
    root: Path,
    pattern: str,
    max_results: int = _MAX_SEARCH_RESULTS,
    file_glob: str = "*.py",
) -> List[Dict[str, Any]]:
    """Regex search across files matching the glob pattern."""
    matches: List[Dict[str, Any]] = []
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return matches

    for filepath in root.rglob(file_glob):
        if len(matches) >= max_results:
            break
        if any(skip in filepath.parts for skip in _SKIP_DIRS):
            continue
        if not filepath.is_file():
            continue
        try:
            size = filepath.stat().st_size
            if size > _MAX_FILE_SIZE_BYTES:
                continue
            text = filepath.read_text("utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if compiled.search(line):
                    matches.append({
                        "file": str(filepath.relative_to(root)),
                        "line": lineno,
                        "text": line.strip()[:200],
                    })
                    if len(matches) >= max_results:
                        break
        except Exception:
            continue
    return matches


# ═══════════════════════════════════════════════════════════════════════════
# Execution Tools (mutation-capable)
# ═══════════════════════════════════════════════════════════════════════════


def run_pytest(
    root: Path,
    test_paths: List[str],
    timeout_s: int = _TEST_TIMEOUT_S,
) -> Dict[str, Any]:
    """Run pytest on specific test files/directories.

    Returns a dict with success, stdout, stderr, and return code.

    IMPORTANT: Uses subprocess.run (NOT shell=True) for isolation.
    """
    cmd = [_PYTHON_BIN, "-m", "pytest", "-x", "-q", "--tb=short"]
    cmd.extend(test_paths)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(root),
        )
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout[-5000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"pytest timed out after {timeout_s}s",
        }
    except Exception as exc:
        return {
            "success": False,
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
        }


# ═══════════════════════════════════════════════════════════════════════════
# Work Dispatch
# ═══════════════════════════════════════════════════════════════════════════


def dispatch_work(
    root: Path,
    work_unit: Dict[str, Any],
    goal: str,
) -> None:
    """Dispatch work based on the work unit's role/phase/operation_type.

    This is the main work routing function.  It reads the work unit,
    decides what to do, and emits findings/patches/result.
    """
    operation_type = str(work_unit.get("operation_type", "")).lower()
    phase = str(work_unit.get("phase", "")).lower()
    target_files: List[str] = work_unit.get("target_files", []) or []
    search_terms: List[str] = work_unit.get("search_terms", []) or []
    task_goal: str = work_unit.get("goal", goal)

    all_findings: List[Dict[str, Any]] = []
    all_patches: List[Dict[str, Any]] = []

    # --- Phase 1: Explore target files ---
    _emit_progress(10, "Analyzing target files")
    for rel_path in target_files:
        content = read_file(root, rel_path)
        if content is None:
            _emit_finding(
                "file_missing",
                f"Could not read target file: {rel_path}",
                file_path=rel_path,
                relevance=0.3,
            )
            continue

        symbols = list_symbols(content, rel_path)
        finding: Dict[str, Any] = {
            "category": "file_analysis",
            "file": rel_path,
            "line_count": content.count("\n") + 1,
            "symbol_count": len(symbols),
            "symbols": symbols[:50],
        }
        _emit_finding(
            "file_analysis",
            f"{rel_path}: {finding['line_count']} lines, {finding['symbol_count']} symbols",
            file_path=rel_path,
        )
        all_findings.append(finding)

    # --- Phase 2: Code search ---
    _emit_progress(30, "Searching codebase for relevant patterns")
    for term in search_terms:
        matches = search_code(root, term)
        if matches:
            finding = {
                "category": "search_result",
                "query": term,
                "match_count": len(matches),
                "matches": matches[:20],
            }
            _emit_finding(
                "search_result",
                f"Found {len(matches)} matches for {term!r}",
            )
            all_findings.append(finding)

    # --- Phase 3: Operation-specific work ---
    _emit_progress(50, f"Executing {operation_type or phase} work")

    if operation_type == "test_execution":
        _handle_test_execution(root, target_files, all_findings)
    elif operation_type in ("code_generation", "refactor", "patch_apply"):
        _handle_code_analysis(root, target_files, task_goal, all_findings, all_patches)
    elif phase in ("generate", "generate_retry"):
        _handle_code_analysis(root, target_files, task_goal, all_findings, all_patches)
    elif phase == "apply":
        _handle_apply_phase(root, work_unit, all_findings, all_patches)
    else:
        # Generic exploration for unknown operation types
        _handle_generic_exploration(root, task_goal, target_files, all_findings)

    # --- Emit final result ---
    _emit_progress(100, "Work complete")
    _emit_result(
        success=True,
        findings=all_findings,
        patches=all_patches,
    )


def _handle_test_execution(
    root: Path,
    target_files: List[str],
    findings: List[Dict[str, Any]],
) -> None:
    """Run tests and report results."""
    if not target_files:
        _emit_finding("test_skip", "No test files specified")
        return

    # Filter to only test files
    test_files = [
        f for f in target_files
        if "test" in f.lower() or f.startswith("tests/")
    ]
    if not test_files:
        # If no explicit test files, try to discover tests for target files
        test_files = _discover_tests(root, target_files)

    if not test_files:
        _emit_finding("test_skip", "No test files found for targets")
        return

    _emit_progress(60, f"Running {len(test_files)} test file(s)")
    result = run_pytest(root, test_files)

    finding = {
        "category": "test_result",
        "success": result["success"],
        "returncode": result["returncode"],
        "output_snippet": result["stdout"][:2000],
    }
    findings.append(finding)
    _emit_finding(
        "test_result",
        f"Tests {'passed' if result['success'] else 'failed'} (exit {result['returncode']})",
        relevance=0.9 if not result["success"] else 0.5,
    )


def _discover_tests(root: Path, source_files: List[str]) -> List[str]:
    """Try to find test files corresponding to source files."""
    tests: List[str] = []
    for src in source_files:
        src_path = Path(src)
        stem = src_path.stem

        # Common test file patterns
        candidates = [
            src_path.parent / f"test_{stem}.py",
            src_path.parent / "tests" / f"test_{stem}.py",
            Path("tests") / src_path.parent / f"test_{stem}.py",
            src_path.parent / f"{stem}_test.py",
        ]
        for candidate in candidates:
            full = root / candidate
            if full.is_file():
                tests.append(str(candidate))
                break
    return tests


def _handle_code_analysis(
    root: Path,
    target_files: List[str],
    goal: str,
    findings: List[Dict[str, Any]],
    patches: List[Dict[str, Any]],
) -> None:
    """Analyze code files for the generation/refactor goal.

    Collects deep structural information that downstream code generation
    can use: imports, dependencies, function signatures, complexity hints.
    """
    for rel_path in target_files:
        content = read_file(root, rel_path)
        if content is None:
            continue

        # Collect imports
        imports = _extract_imports(content)
        if imports:
            findings.append({
                "category": "imports",
                "file": rel_path,
                "imports": imports,
            })

        # Collect function signatures with docstrings
        signatures = _extract_signatures(content, rel_path)
        if signatures:
            findings.append({
                "category": "signatures",
                "file": rel_path,
                "signatures": signatures,
            })

        # Check for TODO/FIXME/HACK markers
        markers = _find_markers(content, rel_path)
        if markers:
            findings.append({
                "category": "markers",
                "file": rel_path,
                "markers": markers,
            })

        # Complexity analysis
        complexity = _analyze_complexity(content, rel_path)
        if complexity:
            findings.append(complexity)


def _handle_apply_phase(
    root: Path,
    work_unit: Dict[str, Any],
    findings: List[Dict[str, Any]],
    patches: List[Dict[str, Any]],
) -> None:
    """Handle the APPLY phase -- validate patches before application.

    The actual file mutation is handled by the governance pipeline's
    ChangeEngine, not here.  This worker validates that patches are
    structurally sound and target files exist.
    """
    incoming_patches: List[Dict[str, Any]] = work_unit.get("patches", []) or []
    for patch in incoming_patches:
        file_path = patch.get("file", "")
        if not file_path:
            findings.append({
                "category": "patch_validation",
                "status": "invalid",
                "reason": "Patch missing 'file' field",
            })
            continue

        full = root / file_path
        exists = full.is_file()
        findings.append({
            "category": "patch_validation",
            "file": file_path,
            "status": "valid" if exists else "target_missing",
            "exists": exists,
        })
        if exists:
            patches.append(patch)


def _handle_generic_exploration(
    root: Path,
    goal: str,
    target_files: List[str],
    findings: List[Dict[str, Any]],
) -> None:
    """Generic exploration when no specific operation type is given."""
    # Search for goal-related terms
    goal_terms = _extract_goal_terms(goal)
    for term in goal_terms[:5]:
        matches = search_code(root, term, max_results=20)
        if matches:
            findings.append({
                "category": "goal_search",
                "query": term,
                "match_count": len(matches),
                "top_files": list({m["file"] for m in matches[:10]}),
            })

    # If target files provided, do structural analysis as fallback
    for rel_path in target_files:
        content = read_file(root, rel_path)
        if content is None:
            continue
        if not rel_path.endswith(".py"):
            continue
        try:
            tree = ast.parse(content)
        except SyntaxError:
            findings.append({
                "category": "parse_error",
                "description": f"Cannot parse {rel_path}",
                "file_path": rel_path,
                "relevance": 0.8,
            })
            continue

        classes = [n.name for n in ast.iter_child_nodes(tree)
                   if isinstance(n, ast.ClassDef)]
        functions = [n.name for n in ast.iter_child_nodes(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        lines = len(content.splitlines())

        findings.append({
            "category": "structure",
            "description": (
                f"{rel_path}: {lines} lines, {len(classes)} classes, "
                f"{len(functions)} functions"
            ),
            "file_path": rel_path,
            "relevance": 0.5,
        })


# ═══════════════════════════════════════════════════════════════════════════
# Analysis Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _extract_imports(source: str) -> List[str]:
    """Extract import statements from Python source."""
    imports: List[str] = []
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    imports.append(f"{module}.{alias.name}")
    except SyntaxError:
        # Fallback: regex-based extraction
        for match in re.finditer(r'^(?:from\s+(\S+)\s+)?import\s+(.+)$', source, re.MULTILINE):
            from_mod = match.group(1) or ""
            names = match.group(2)
            for name in names.split(","):
                name = name.strip().split(" as ")[0].strip()
                if from_mod:
                    imports.append(f"{from_mod}.{name}")
                else:
                    imports.append(name)
    return imports


def _extract_signatures(source: str, filename: str) -> List[Dict[str, Any]]:
    """Extract function signatures with docstrings."""
    signatures: List[Dict[str, Any]] = []
    try:
        tree = ast.parse(source, filename=filename)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sig: Dict[str, Any] = {
                    "name": node.name,
                    "line": node.lineno,
                    "async": isinstance(node, ast.AsyncFunctionDef),
                    "args": [a.arg for a in node.args.args],
                }
                # Extract docstring
                if (node.body
                        and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    sig["docstring"] = node.body[0].value.value[:300]
                signatures.append(sig)
    except SyntaxError:
        pass
    return signatures


def _find_markers(source: str, filename: str) -> List[Dict[str, Any]]:
    """Find TODO, FIXME, HACK, XXX markers in source."""
    markers: List[Dict[str, Any]] = []
    pattern = re.compile(r'#\s*(TODO|FIXME|HACK|XXX|BUG|WARN)\b[:\s]*(.*)', re.IGNORECASE)
    for lineno, line in enumerate(source.splitlines(), 1):
        match = pattern.search(line)
        if match:
            markers.append({
                "type": match.group(1).upper(),
                "line": lineno,
                "text": match.group(2).strip()[:200],
                "file": filename,
            })
    return markers


def _analyze_complexity(source: str, filename: str) -> Optional[Dict[str, Any]]:
    """Analyze code complexity (branch count, nesting depth)."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return None

    branches = sum(
        1 for n in ast.walk(tree)
        if isinstance(n, (ast.If, ast.For, ast.While, ast.ExceptHandler,
                          ast.With, ast.AsyncWith, ast.AsyncFor))
    )
    if branches > 30:
        return {
            "category": "complexity",
            "description": f"{filename}: high complexity ({branches} branches)",
            "file": filename,
            "branch_count": branches,
            "relevance": 0.7,
        }
    return None


def _extract_goal_terms(goal: str) -> List[str]:
    """Extract search terms from a natural-language goal string."""
    terms: List[str] = []
    # Quoted strings
    for match in re.finditer(r'"([^"]+)"|\'([^\']+)\'', goal):
        terms.append(match.group(1) or match.group(2))
    # CamelCase identifiers
    for match in re.finditer(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b', goal):
        terms.append(match.group())
    # snake_case identifiers
    for match in re.finditer(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b', goal):
        terms.append(match.group())
    # Deduplicate while preserving order
    seen: set[str] = set()
    result: List[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Payload Normalization (backward compatibility)
# ═══════════════════════════════════════════════════════════════════════════


def _normalize_payload(raw_payload: Dict[str, Any]) -> tuple[Path, Dict[str, Any], str]:
    """Normalize both legacy and new payload formats into (root, work_unit, goal).

    Legacy format (SubprocessRunner v1)::

        {"op_id": "...", "goal": "...", "target_files": [...],
         "work_unit": {"operation_type": "..."}}

    New format (HybridTeammateExecutor)::

        {"work_unit": {...}, "goal": "...", "project_root": "/path"}
    """
    # Detect format: new format has "project_root" key
    if "project_root" in raw_payload:
        # New format
        root = Path(raw_payload["project_root"]).resolve()
        work_unit = raw_payload.get("work_unit", {})
        goal = raw_payload.get("goal", work_unit.get("goal", ""))
        return root, work_unit, goal

    # Legacy format
    root = Path(os.environ.get("JARVIS_PROJECT_ROOT", ".")).resolve()
    goal = raw_payload.get("goal", "")
    target_files = raw_payload.get("target_files", [])
    inner_wu = raw_payload.get("work_unit", {})

    # Merge top-level fields into work_unit for uniform handling
    work_unit: Dict[str, Any] = {
        "goal": goal,
        "target_files": target_files,
        "operation_type": inner_wu.get("operation_type", "explore"),
        **{k: v for k, v in inner_wu.items() if k != "operation_type"},
    }
    return root, work_unit, goal


# ═══════════════════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════════════════


def main() -> int:
    """Entry point when spawned as ``python3 -m ...isolated_agent_worker``."""
    t0 = time.monotonic()

    try:
        # Read exactly one JSON line from stdin
        raw = sys.stdin.readline()
        if not raw.strip():
            _emit_result(success=False, error="No input received on stdin")
            return 1

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _emit_result(success=False, error=f"Invalid JSON on stdin: {exc}")
            return 1

        root, work_unit, goal = _normalize_payload(payload)

        if not root.is_dir():
            _emit_result(
                success=False,
                error=f"Project root does not exist: {root}",
            )
            return 1

        logger.info(
            "Worker started: goal=%s, files=%d, root=%s",
            goal[:80], len(work_unit.get("target_files", [])), root,
        )

        # Dispatch the work
        dispatch_work(root, work_unit, goal)

        elapsed = time.monotonic() - t0
        logger.info("Worker finished successfully in %.1fs", elapsed)
        return 0

    except KeyboardInterrupt:
        _emit_result(success=False, error="Worker interrupted (SIGINT)")
        return 1
    except Exception as exc:
        _emit_result(
            success=False,
            error=f"Unhandled worker error: {exc}\n{traceback.format_exc()}",
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
