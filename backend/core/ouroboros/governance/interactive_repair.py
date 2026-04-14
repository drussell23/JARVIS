"""
InteractiveRepairLoop — REPL-style execute-observe-fix debugging.

Closes the "interactive debugging" gap. Instead of regenerating the entire
candidate on failure, this module:
1. Runs the candidate via subprocess (argv-based, no shell)
2. Captures the SPECIFIC runtime error (traceback, line, message)
3. Builds a focused micro-prompt targeting JUST that error
4. Gets a targeted patch (not a full regeneration)
5. Applies the micro-fix and re-runs

Boundary Principle:
  Deterministic: Subprocess execution (argv, no shell), error extraction
  via regex, micro-patch application via line replacement.
  Agentic: The micro-prompt sent to the provider for the fix.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_MICRO_ITERATIONS = int(os.environ.get("JARVIS_INTERACTIVE_REPAIR_MAX_ITERS", "3"))
_MICRO_TIMEOUT_S = float(os.environ.get("JARVIS_INTERACTIVE_REPAIR_TIMEOUT_S", "30"))
# Default OFF: this path writes to disk outside the Iron Gate / ChangeEngine
# immune system. Manifesto §6 keeps model-driven mutations behind the gates
# until this loop is re-homed through ChangeEngine/APPLY.
_ENABLED = os.environ.get("JARVIS_INTERACTIVE_REPAIR_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ExtractedError:
    """A specific runtime error extracted from subprocess output."""
    error_type: str
    message: str
    file_path: str
    line_number: int
    traceback_excerpt: str
    full_output: str


@dataclass
class MicroFix:
    """A targeted fix for a specific runtime error."""
    target_file: str
    line_range: Tuple[int, int]
    replacement: str
    reasoning: str


@dataclass
class InteractiveRepairResult:
    """Result of the interactive repair loop."""
    fixed: bool
    iterations_used: int
    errors_encountered: List[ExtractedError]
    fixes_applied: List[MicroFix]
    total_duration_s: float
    final_output: str


class InteractiveRepairLoop:
    """REPL-style debug loop — surgical error-by-error repair.

    Converges faster than full regeneration because:
    - Micro-prompts are 10x smaller (fewer tokens)
    - Fixes are surgical (one error at a time)
    - Each iteration provides the EXACT error context
    """

    def __init__(self, provider: Any, project_root: Path) -> None:
        self._provider = provider
        self._project_root = project_root

    async def repair(
        self, file_path: str, file_content: str,
        test_argv: List[str], op_id: str = "",
    ) -> InteractiveRepairResult:
        """Run the interactive repair loop."""
        t0 = time.monotonic()
        errors: List[ExtractedError] = []
        fixes: List[MicroFix] = []
        current = file_content

        if not _ENABLED:
            logger.info(
                "[InteractiveRepair] disabled via JARVIS_INTERACTIVE_REPAIR_ENABLED=false (op=%s) — falling through to VALIDATE_RETRY/L2",
                op_id,
            )
            return InteractiveRepairResult(
                fixed=False, iterations_used=0,
                errors_encountered=[], fixes_applied=[],
                total_duration_s=0.0,
                final_output="InteractiveRepair disabled (JARVIS_INTERACTIVE_REPAIR_ENABLED=false)",
            )

        for iteration in range(_MAX_MICRO_ITERATIONS):
            err = await self._run_and_capture(file_path, current, test_argv)
            if err is None:
                return InteractiveRepairResult(
                    fixed=True, iterations_used=iteration,
                    errors_encountered=errors, fixes_applied=fixes,
                    total_duration_s=time.monotonic() - t0, final_output="Tests passed",
                )
            errors.append(err)

            # Hard guard: refuse to call the model or touch disk when we have
            # no parseable traceback / no located line. Without this, pytest
            # assertion failures (which don't emit a stdlib traceback) fall
            # through to UnknownError at line 0, which _build_micro_prompt
            # translates to a blind "patch lines 1-5" request — the exact
            # pathway that corrupted tests/test_reflex_provocation/test_one.py
            # during bt-2026-04-14-234236.
            if err.error_type in {"UnknownError", "TimeoutError"} or err.line_number <= 0:
                logger.warning(
                    "[InteractiveRepair] Refusing to patch: error_type=%s line=%d file=%s (op=%s) — "
                    "traceback unparseable or location unknown, falling through to VALIDATE_RETRY/L2",
                    err.error_type, err.line_number, file_path, op_id,
                )
                break

            prompt = self._build_micro_prompt(file_path, current, err)
            try:
                from datetime import datetime, timedelta, timezone
                deadline = datetime.now(timezone.utc) + timedelta(seconds=_MICRO_TIMEOUT_S)
                raw = await self._provider.plan(prompt, deadline)
                fix = self._parse_micro_fix(raw, file_path)
            except Exception as exc:
                logger.warning("[InteractiveRepair] Provider failed iter %d: %s", iteration, exc)
                break
            if fix is None:
                break

            fixes.append(fix)
            current = self._apply_fix(current, fix)
            (self._project_root / file_path).write_text(current, encoding="utf-8")
            logger.info(
                "[InteractiveRepair] Iter %d: fixed %s at L%d-%d (op=%s)",
                iteration, err.error_type, fix.line_range[0], fix.line_range[1], op_id,
            )

        return InteractiveRepairResult(
            fixed=False, iterations_used=len(errors),
            errors_encountered=errors, fixes_applied=fixes,
            total_duration_s=time.monotonic() - t0,
            final_output=errors[-1].full_output if errors else "",
        )

    async def _run_and_capture(
        self, file_path: str, content: str, test_argv: List[str],
    ) -> Optional[ExtractedError]:
        """Run test argv and extract specific error. Argv-based, no shell."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *test_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._project_root),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_MICRO_TIMEOUT_S)
            if proc.returncode == 0:
                return None
            output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
            return self._extract_error(output, file_path)
        except asyncio.TimeoutError:
            return ExtractedError(
                error_type="TimeoutError", message=f"Timed out after {_MICRO_TIMEOUT_S}s",
                file_path=file_path, line_number=0, traceback_excerpt="", full_output="TIMEOUT",
            )

    @staticmethod
    def _extract_error(output: str, default_file: str) -> ExtractedError:
        """Extract structured error from subprocess output. Deterministic regex."""
        tb = re.search(
            r'Traceback \(most recent call last\):\n(.*?)(\w+Error.*?)$',
            output, re.DOTALL | re.MULTILINE,
        )
        if tb:
            tb_text, error_line = tb.group(1), tb.group(2).strip()
            parts = error_line.split(":", 1)
            etype = parts[0].strip()
            msg = parts[1].strip() if len(parts) > 1 else ""
            file_lines = re.findall(r'File "([^"]+)", line (\d+)', tb_text)
            if file_lines:
                last_file, last_line = file_lines[-1]
                return ExtractedError(
                    error_type=etype, message=msg, file_path=last_file,
                    line_number=int(last_line), traceback_excerpt=tb_text[-500:],
                    full_output=output[-2000:],
                )
        return ExtractedError(
            error_type="UnknownError", message="No parseable traceback",
            file_path=default_file, line_number=0,
            traceback_excerpt=output[-500:], full_output=output[-2000:],
        )

    @staticmethod
    def _build_micro_prompt(file_path: str, content: str, error: ExtractedError) -> str:
        """Build focused micro-prompt for one error."""
        lines = content.split("\n")
        start = max(0, error.line_number - 5)
        end = min(len(lines), error.line_number + 5)
        ctx = "\n".join(
            f"{'>>>' if i + start + 1 == error.line_number else '   '} "
            f"{i + start + 1:4d} | {line}"
            for i, line in enumerate(lines[start:end])
        )
        return (
            f"MICRO-FIX REQUEST\n=================\n\n"
            f"File: {file_path}\nError: {error.error_type}: {error.message}\n"
            f"Line: {error.line_number}\n\nCode context:\n```python\n{ctx}\n```\n\n"
            f"Traceback:\n```\n{error.traceback_excerpt}\n```\n\n"
            f"Fix ONLY this specific error. Return JSON:\n"
            f'{{"start_line": {start + 1}, "end_line": {end}, '
            f'"replacement": "fixed code for these lines", '
            f'"reasoning": "why this fixes the error"}}'
        )

    @staticmethod
    def _parse_micro_fix(raw: str, file_path: str) -> Optional[MicroFix]:
        stripped = raw.strip()
        if stripped.startswith("```"):
            stripped = "\n".join(l for l in stripped.split("\n") if not l.startswith("```")).strip()
        try:
            data = json.loads(stripped)
            return MicroFix(
                target_file=file_path, line_range=(data["start_line"], data["end_line"]),
                replacement=data["replacement"], reasoning=data.get("reasoning", ""),
            )
        except (json.JSONDecodeError, KeyError):
            return None

    @staticmethod
    def _apply_fix(content: str, fix: MicroFix) -> str:
        lines = content.split("\n")
        lines[fix.line_range[0] - 1:fix.line_range[1]] = fix.replacement.split("\n")
        return "\n".join(lines)
