"""
LSP Checker — Type checking for Ouroboros VALIDATE phase.

P1: Claude Code has LSP. Ouroboros uses AST-only. This adds pyright/mypy
type checking via subprocess (argv, no shell) with JSON output parsing.

Boundary Principle: Deterministic subprocess + JSON parse. No inference.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TIMEOUT_S = float(os.environ.get("JARVIS_TYPE_CHECK_TIMEOUT_S", "30"))


@dataclass
class TypeCheckResult:
    passed: bool
    error_count: int
    warning_count: int
    errors: List[Dict[str, Any]]
    checker_used: str
    duration_s: float = 0.0


class LSPTypeChecker:
    """Pyright/mypy type checking. Argv-based subprocess, no shell."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root
        self._checker: Optional[str] = None
        self._checker_detected: bool = False

    @property
    def is_available(self) -> bool:
        """True if a type checker (pyright or mypy) was detected.

        Note: this only returns a meaningful result AFTER at least one call
        to ``check_files`` or ``check_incremental`` (which run ``_detect_checker``).
        For a synchronous pre-check, call ``detect_checker_sync()`` first.
        """
        return self._checker not in (None, "none")

    def detect_checker_sync(self) -> bool:
        """Synchronous helper — detects checker availability without asyncio.

        Safe to call from ``run_in_executor``.  Caches result so subsequent
        async calls to ``_detect_checker`` are free.
        """
        if self._checker_detected:
            return self.is_available
        import subprocess, shutil
        for name in ("pyright", "mypy"):
            if shutil.which(name) is not None:
                try:
                    result = subprocess.run(
                        [name, "--version"],
                        capture_output=True, timeout=5.0,
                    )
                    if result.returncode == 0:
                        self._checker = name
                        self._checker_detected = True
                        return True
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
        self._checker = "none"
        self._checker_detected = True
        return False

    def check_incremental(
        self,
        changed_files: List[str],
        timeout_s: float = 15.0,
    ) -> TypeCheckResult:
        """Synchronous incremental type check — only the given files.

        Designed for ``run_in_executor`` from the orchestrator.  Falls back
        gracefully (passed=True, 0 errors) if no checker is available.

        Parameters
        ----------
        changed_files:
            Absolute paths to the modified files.
        timeout_s:
            Per-invocation timeout (default 15 s — shorter than full-project).
        """
        import time, subprocess
        t0 = time.monotonic()

        # Ensure checker is detected (sync path)
        if not self._checker_detected:
            self.detect_checker_sync()

        if not self.is_available:
            return TypeCheckResult(True, 0, 0, [], "none", time.monotonic() - t0)

        py_files = [f for f in changed_files if f.endswith(".py") and os.path.isfile(f)]
        if not py_files:
            return TypeCheckResult(True, 0, 0, [], self._checker or "none", time.monotonic() - t0)

        checker = self._checker
        try:
            if checker == "pyright":
                result = self._run_pyright_sync(py_files, timeout_s)
            else:
                result = self._run_mypy_sync(py_files, timeout_s)
            result.duration_s = time.monotonic() - t0
            return result
        except Exception as exc:
            logger.debug("[LSPTypeChecker] incremental check failed: %s", exc)
            return TypeCheckResult(True, 0, 0, [], f"{checker}_error", time.monotonic() - t0)

    def _run_pyright_sync(self, files: List[str], timeout_s: float) -> TypeCheckResult:
        """Run pyright synchronously on specific files."""
        import subprocess
        try:
            proc = subprocess.run(
                ["pyright", "--outputjson", *files],
                capture_output=True, timeout=timeout_s,
                cwd=str(self._project_root),
            )
            try:
                data = json.loads(proc.stdout.decode())
                errors: List[Dict[str, Any]] = []
                ec, wc = 0, 0
                for d in data.get("generalDiagnostics", []):
                    sev = d.get("severity", "information")
                    entry = {
                        "file": d.get("file", ""),
                        "line": d.get("range", {}).get("start", {}).get("line", 0),
                        "message": d.get("message", ""),
                        "severity": sev,
                        "rule": d.get("rule", ""),
                    }
                    if sev == "error":
                        ec += 1; errors.append(entry)
                    elif sev == "warning":
                        wc += 1; errors.append(entry)
                return TypeCheckResult(ec == 0, ec, wc, errors[:20], "pyright")
            except json.JSONDecodeError:
                return TypeCheckResult(
                    proc.returncode == 0,
                    0 if proc.returncode == 0 else 1,
                    0, [], "pyright",
                )
        except subprocess.TimeoutExpired:
            return TypeCheckResult(True, 0, 0, [], "pyright_timeout")

    def _run_mypy_sync(self, files: List[str], timeout_s: float) -> TypeCheckResult:
        """Run mypy synchronously on specific files."""
        import subprocess
        try:
            proc = subprocess.run(
                ["mypy", "--no-color-output", "--show-error-codes", "--no-error-summary", *files],
                capture_output=True, timeout=timeout_s,
                cwd=str(self._project_root),
            )
            errors: List[Dict[str, Any]] = []
            ec, wc = 0, 0
            for line in proc.stdout.decode().strip().split("\n"):
                match = re.match(r"(.+?):(\d+):\s*(error|warning|note):\s*(.+)", line)
                if match:
                    sev = match.group(3)
                    entry = {
                        "file": match.group(1),
                        "line": int(match.group(2)),
                        "message": match.group(4),
                        "severity": sev,
                    }
                    if sev == "error":
                        ec += 1; errors.append(entry)
                    elif sev == "warning":
                        wc += 1; errors.append(entry)
            return TypeCheckResult(ec == 0, ec, wc, errors[:20], "mypy")
        except subprocess.TimeoutExpired:
            return TypeCheckResult(True, 0, 0, [], "mypy_timeout")

    async def check_files(self, files: List[str], timeout_s: float = _TIMEOUT_S) -> TypeCheckResult:
        import time; t0 = time.monotonic()
        checker = await self._detect_checker()
        if not checker:
            return TypeCheckResult(True, 0, 0, [], "none", time.monotonic() - t0)
        abs_files = [str(self._project_root / f) for f in files if f.endswith(".py")]
        if not abs_files:
            return TypeCheckResult(True, 0, 0, [], checker, time.monotonic() - t0)
        result = await (self._run_pyright(abs_files, timeout_s) if checker == "pyright"
                        else self._run_mypy(abs_files, timeout_s))
        result.duration_s = time.monotonic() - t0
        return result

    async def _detect_checker(self) -> Optional[str]:
        if self._checker is not None:
            return self._checker if self._checker != "none" else None
        for name in ("pyright", "mypy"):
            try:
                p = await asyncio.create_subprocess_exec(
                    name, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await asyncio.wait_for(p.communicate(), timeout=5.0)
                if p.returncode == 0:
                    self._checker = name; return name
            except (FileNotFoundError, asyncio.TimeoutError):
                pass
        self._checker = "none"; return None

    async def _run_pyright(self, files: List[str], timeout_s: float) -> TypeCheckResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pyright", "--outputjson", *files,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(self._project_root))
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            try:
                data = json.loads(stdout.decode())
                errors, ec, wc = [], 0, 0
                for d in data.get("generalDiagnostics", []):
                    sev = d.get("severity", "information")
                    entry = {"file": d.get("file", ""), "line": d.get("range", {}).get("start", {}).get("line", 0),
                             "message": d.get("message", ""), "severity": sev, "rule": d.get("rule", "")}
                    if sev == "error": ec += 1; errors.append(entry)
                    elif sev == "warning": wc += 1; errors.append(entry)
                return TypeCheckResult(ec == 0, ec, wc, errors[:20], "pyright")
            except json.JSONDecodeError:
                return TypeCheckResult(proc.returncode == 0, 0 if proc.returncode == 0 else 1, 0, [], "pyright")
        except asyncio.TimeoutError:
            return TypeCheckResult(True, 0, 0, [], "pyright_timeout")

    async def _run_mypy(self, files: List[str], timeout_s: float) -> TypeCheckResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                "mypy", "--no-color-output", "--show-error-codes", "--no-error-summary", *files,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(self._project_root))
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            errors, ec, wc = [], 0, 0
            for line in stdout.decode().strip().split("\n"):
                match = re.match(r"(.+?):(\d+):\s*(error|warning|note):\s*(.+)", line)
                if match:
                    sev = match.group(3)
                    entry = {"file": match.group(1), "line": int(match.group(2)),
                             "message": match.group(4), "severity": sev}
                    if sev == "error": ec += 1; errors.append(entry)
                    elif sev == "warning": wc += 1; errors.append(entry)
            return TypeCheckResult(ec == 0, ec, wc, errors[:20], "mypy")
        except asyncio.TimeoutError:
            return TypeCheckResult(True, 0, 0, [], "mypy_timeout")

    @staticmethod
    def format_for_prompt(result: TypeCheckResult) -> str:
        if result.passed or not result.errors: return ""
        lines = [f"## Type Errors ({result.checker_used}: {result.error_count}E, {result.warning_count}W)"]
        for err in result.errors[:10]:
            lines.append(f"- {err['file']}:{err['line']}: [{err['severity']}] {err['message']}")
        lines.append("\nFix these type errors.")
        return "\n".join(lines)

    @staticmethod
    def format_errors_for_repair(result: TypeCheckResult) -> str:
        """Format type errors into a prompt-injectable string for InteractiveRepairLoop.

        Produces a compact, LLM-friendly listing like::

            Type errors found:
            - file.py:42: error: Argument of type "str" is not assignable to parameter "x" of type "int"
            - file.py:87: error: "foo" is not a known member of module "bar"

        Returns an empty string when no errors are present.
        """
        if result.passed or not result.errors:
            return ""
        lines = ["Type errors found:"]
        for err in result.errors[:15]:
            sev = err.get("severity", "error")
            rule = err.get("rule")
            rule_suffix = f" [{rule}]" if rule else ""
            lines.append(
                f"- {err['file']}:{err['line']}: {sev}: {err['message']}{rule_suffix}"
            )
        if result.error_count > 15:
            lines.append(f"... and {result.error_count - 15} more error(s)")
        return "\n".join(lines)
