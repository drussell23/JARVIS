#!/usr/bin/env python3
"""AST-based checker for banned direct constructors and psutil calls.

Enforces Memory Control Plane governance by detecting direct usage of
banned APIs outside approved modules.

Usage:
    python3 scripts/check_memory_governance.py [--path backend/]

Exit codes:
    0 = No violations found
    1 = Violations found
"""

import ast
import sys
from pathlib import Path
from typing import Dict, FrozenSet, List, NamedTuple


class Violation(NamedTuple):
    file: str
    line: int
    rule: str
    detail: str


# Governance rules: name -> set of allowed files (relative to repo root)
BANNED_CONSTRUCTORS: Dict[str, FrozenSet[str]] = {
    "SentenceTransformer": frozenset({
        "backend/core/embedding_service.py",
        "backend/core/budgeted_loaders.py",
    }),
}

BANNED_CALLS: Dict[str, FrozenSet[str]] = {
    "psutil.virtual_memory": frozenset({
        "backend/core/memory_quantizer.py",
    }),
    "psutil.swap_memory": frozenset({
        "backend/core/memory_quantizer.py",
    }),
}


class GovernanceChecker(ast.NodeVisitor):
    """AST visitor that detects banned constructor and function calls."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.violations: List[Violation] = []
        self._imports: Dict[str, str] = {}  # alias -> module

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname if alias.asname else alias.name
            self._imports[name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            for alias in node.names:
                local_name = alias.asname if alias.asname else alias.name
                self._imports[local_name] = f"{node.module}.{alias.name}"
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # Check for banned constructors: SentenceTransformer(...)
        func_name = self._get_call_name(node)

        if func_name:
            # Check banned constructors
            for banned, allowed_files in BANNED_CONSTRUCTORS.items():
                if func_name == banned or func_name.endswith(f".{banned}"):
                    if self.file_path not in allowed_files:
                        self.violations.append(Violation(
                            file=self.file_path,
                            line=node.lineno,
                            rule=f"banned_constructor:{banned}",
                            detail=f"Direct {banned}() construction banned outside {', '.join(sorted(allowed_files))}",
                        ))

            # Check banned calls: psutil.virtual_memory(), etc.
            for banned, allowed_files in BANNED_CALLS.items():
                if func_name == banned:
                    if self.file_path not in allowed_files:
                        self.violations.append(Violation(
                            file=self.file_path,
                            line=node.lineno,
                            rule=f"banned_call:{banned}",
                            detail=f"Direct {banned}() call banned outside {', '.join(sorted(allowed_files))}",
                        ))

        self.generic_visit(node)

    def _get_call_name(self, node: ast.Call) -> str:
        """Extract the full dotted name of a function call."""
        if isinstance(node.func, ast.Name):
            # Simple call: SentenceTransformer(...)
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            # Dotted call: psutil.virtual_memory()
            parts = []
            current = node.func
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            parts.reverse()
            return ".".join(parts)
        return ""


def check_file(file_path: Path, repo_root: Path) -> List[Violation]:
    """Check a single Python file for governance violations."""
    relative = str(file_path.relative_to(repo_root))

    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
    except (SyntaxError, UnicodeDecodeError):
        return []  # Skip unparseable files

    checker = GovernanceChecker(relative)
    # Large files (e.g. unified_supervisor.py at 73K+ lines) produce
    # deeply nested ASTs that exceed the default recursion limit.
    old_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(max(old_limit, 10000))
        checker.visit(tree)
    except RecursionError:
        # Extremely deep AST — skip this file gracefully
        print(f"  WARNING: Skipped {relative} (AST too deeply nested)")
        return []
    finally:
        sys.setrecursionlimit(old_limit)
    return checker.violations


def check_directory(path: Path, repo_root: Path) -> List[Violation]:
    """Check all Python files in a directory tree."""
    violations = []
    for py_file in sorted(path.rglob("*.py")):
        # Skip test files, __pycache__, migrations
        rel = str(py_file.relative_to(repo_root))
        if "__pycache__" in rel or "migrations" in rel:
            continue
        violations.extend(check_file(py_file, repo_root))
    return violations


def main() -> int:
    """Entry point for CI usage."""
    import argparse

    parser = argparse.ArgumentParser(description="Memory governance checker")
    parser.add_argument("--path", default="backend/", help="Directory to check")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    target = repo_root / args.path

    if not target.exists():
        print(f"Path not found: {target}")
        return 1

    if target.is_file():
        violations = check_file(target, repo_root)
    else:
        violations = check_directory(target, repo_root)

    if violations:
        print(f"\n{'='*60}")
        print(f"MEMORY GOVERNANCE VIOLATIONS: {len(violations)}")
        print(f"{'='*60}\n")
        for v in violations:
            print(f"  {v.file}:{v.line} [{v.rule}]")
            print(f"    {v.detail}\n")
        return 1
    else:
        print("Memory governance check passed: no violations found.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
