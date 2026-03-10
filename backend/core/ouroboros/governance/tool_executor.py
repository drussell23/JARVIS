"""Tool execution engine for J-Prime's tool-use interface.

Provides a sandboxed executor for the five read-only introspection tools
available to J-Prime during multi-turn code generation.

Tools
-----
- read_file(path, lines_from, lines_to)
- list_symbols(module_path)
- search_code(pattern, file_glob)
- run_tests(paths)
- get_callers(function_name, file_path)

Security
--------
All path / file_path arguments are validated against repo_root via
_safe_resolve. Traversal attempts raise BlockedPathError, which the
executor maps to ToolResult.error (never re-raised).
"""
from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.test_runner import BlockedPathError


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation request from J-Prime."""

    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """The result of executing a ToolCall."""

    tool_call: ToolCall
    output: str
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

_MAX_TOOL_OUTPUT_CHARS = 4_000  # truncate results exceeding this


class ToolExecutor:
    """Dispatch ToolCall objects to read-only introspection handlers.

    All handlers are synchronous and safe to call from any context.
    ``execute()`` never raises — all errors are captured in ``ToolResult.error``.
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root
        self._dispatch: Dict[str, Any] = {
            "read_file": self._read_file,
            "list_symbols": self._list_symbols,
            "search_code": self._search_code,
            "run_tests": self._run_tests,
            "get_callers": self._get_callers,
        }

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def execute(self, tool_call: ToolCall) -> ToolResult:
        """Dispatch a ToolCall and return a ToolResult. Never raises."""
        handler = self._dispatch.get(tool_call.name)
        if handler is None:
            return ToolResult(
                tool_call=tool_call,
                output="",
                error=f"unknown tool: '{tool_call.name}'",
            )
        try:
            output = handler(tool_call.arguments)
            # Truncate if needed
            if len(output) > _MAX_TOOL_OUTPUT_CHARS:
                output = output[:_MAX_TOOL_OUTPUT_CHARS] + f"\n... (truncated to {_MAX_TOOL_OUTPUT_CHARS} chars)"
            return ToolResult(tool_call=tool_call, output=output)
        except BlockedPathError as exc:
            return ToolResult(tool_call=tool_call, output="", error=f"blocked path: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call=tool_call, output="", error=str(exc))

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------

    def _safe_resolve(self, path_str: str) -> Path:
        """Resolve path_str relative to repo_root and verify containment.

        Raises BlockedPathError if the resolved path escapes repo_root or
        is a symbolic link.

        Both relative and absolute paths are accepted; absolute paths are
        validated against repo_root exactly like relative ones — the
        ``relative_to`` containment check below will block anything outside.
        """
        raw = Path(path_str)
        if raw.is_absolute():
            pre_resolve = raw
        else:
            pre_resolve = self._repo_root / raw
        # Check for symlink BEFORE resolving (resolve() follows symlinks)
        if pre_resolve.exists() and pre_resolve.is_symlink():
            raise BlockedPathError(f"blocked symlink: {path_str!r}")
        resolved = pre_resolve.resolve()
        try:
            resolved.relative_to(self._repo_root.resolve())
        except ValueError:
            raise BlockedPathError(
                f"blocked path traversal: {path_str!r} escapes repo_root"
            )
        return resolved

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _read_file(self, args: Dict[str, Any]) -> str:
        path_str: str = args["path"]
        lines_from: int = max(1, int(args.get("lines_from", 1)))
        lines_to: int = int(args.get("lines_to", 200))

        resolved = self._safe_resolve(path_str)

        if not resolved.exists():
            return f"(file not found: {path_str})"

        text = resolved.read_text(errors="replace")
        all_lines = text.splitlines(keepends=True)
        selected = all_lines[lines_from - 1 : lines_to]
        return "".join(f"{lines_from + i}: {line}" for i, line in enumerate(selected))

    def _list_symbols(self, args: Dict[str, Any]) -> str:
        path_str: str = args["module_path"]
        resolved = self._safe_resolve(path_str)

        if not resolved.exists():
            return f"(file not found: {path_str})"

        source = resolved.read_text(errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return f"(SyntaxError: {exc})"

        entries: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                entries.append(f"  class: {node.name} (line {node.lineno})")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                entries.append(f"  function: {node.name} (line {node.lineno})")

        return "\n".join(sorted(set(entries))) if entries else "(no symbols found)"

    def _search_code(self, args: Dict[str, Any]) -> str:
        pattern: str = args["pattern"]
        file_glob: str = args.get("file_glob", "*.py")

        try:
            proc = subprocess.run(
                ["grep", "-r", "--include", file_glob, "-n", "--", pattern, str(self._repo_root)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return "(search timed out after 10s)"

        raw_lines = (proc.stdout or "").splitlines()
        if not raw_lines:
            return "(no matches)"

        cap = 50
        if len(raw_lines) <= cap:
            return "\n".join(raw_lines)

        n_extra = len(raw_lines) - cap
        return "\n".join(raw_lines[:cap]) + f"\n... ({n_extra} more lines truncated)"

    def _run_tests(self, args: Dict[str, Any]) -> str:
        paths_arg = args.get("paths", [])
        if isinstance(paths_arg, str):
            paths_arg = [paths_arg]

        safe_paths: List[str] = []
        for p in paths_arg:
            try:
                resolved = self._safe_resolve(str(p))
                safe_paths.append(str(resolved))
            except BlockedPathError:
                return f"(blocked path: {p!r})"

        cmd = ["python3", "-m", "pytest", "--tb=short", "-q"] + safe_paths
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self._repo_root),
            )
        except subprocess.TimeoutExpired:
            return "(pytest timed out after 30s)"

        combined = (proc.stdout or "") + (proc.stderr or "")
        return combined[-_MAX_TOOL_OUTPUT_CHARS:] if len(combined) > _MAX_TOOL_OUTPUT_CHARS else combined

    def _get_callers(self, args: Dict[str, Any]) -> str:
        function_name: str = args["function_name"]
        file_path_str: Optional[str] = args.get("file_path")

        if file_path_str is not None:
            resolved_file = self._safe_resolve(file_path_str)
            search_root = str(resolved_file.parent)
        else:
            search_root = str(self._repo_root)

        pattern = rf"\b{function_name}\s*\("
        try:
            proc = subprocess.run(
                ["grep", "-r", "--include", "*.py", "-n", "-E", "--", pattern, search_root],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return "(search timed out)"

        raw_lines = (proc.stdout or "").splitlines()
        if not raw_lines:
            return "(no callers found)"

        cap = 30
        if len(raw_lines) <= cap:
            return "\n".join(raw_lines)

        n_extra = len(raw_lines) - cap
        return "\n".join(raw_lines[:cap]) + f"\n... ({n_extra} more)"
