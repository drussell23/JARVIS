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
        """Run test argv and extract specific error. Argv-based, no shell.

        Slice 4C-A (2026-05-25) — PYTHONPATH injection. The subprocess
        inherits the JARVIS Python environment by default, which does
        NOT have the target project's package installed (no ``pip
        install -e .`` is run during SWE-Bench-Pro prepare_problem).
        For Ansible-shape projects (src-layout: package code under
        ``lib/<pkg>/``), the test file's ``from ansible.cli.doc
        import ...`` fails with ``ModuleNotFoundError: No module
        named 'ansible'`` — proven by raw pytest replication from
        soak bt-2026-05-25-094217.

        Surgical fix: build a per-subprocess env dict that inherits
        ``os.environ`` and PREPENDS the worktree's canonical Python
        source roots to ``PYTHONPATH``:

          * ``<repo_root>/lib`` — Ansible / Django / Flask / Pandas
            convention (separate ``lib/`` source dir)
          * ``<repo_root>/src`` — modern Python packaging convention
          * ``<repo_root>`` itself — flat-layout projects (single
            top-level package dir at repo root)

        All three are prepended (PATHSEP-joined) so pytest finds the
        package regardless of layout convention. Existing PYTHONPATH
        (if any) is preserved AFTER the new prepends — operator
        overrides take precedence on conflicting names but the
        worktree paths win on absent ones.

        The injection is per-subprocess via ``env=`` kwarg — the
        parent process's ``os.environ`` is NEVER mutated. Stateless,
        bleeds zero into other code paths.
        """
        # Slice 4C-A — build per-subprocess env with PYTHONPATH override
        _proj_root = str(self._project_root)
        _existing_pp = os.environ.get("PYTHONPATH", "")
        _candidates = [
            os.path.join(_proj_root, "lib"),
            os.path.join(_proj_root, "src"),
            _proj_root,
        ]
        # Only include candidate paths that actually exist on disk —
        # avoids polluting PYTHONPATH with non-existent dirs that would
        # otherwise become noise in import-error tracebacks.
        _real = [p for p in _candidates if os.path.isdir(p)]
        _pythonpath_parts = _real + (
            [_existing_pp] if _existing_pp else []
        )
        _subprocess_env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(_pythonpath_parts),
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                *test_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._project_root),
                env=_subprocess_env,
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
        """Extract structured error from subprocess output. Deterministic
        regex cascade.

        Slice 3H.3 (2026-05-25) — parser cascade for pytest diagnostic
        shapes. The pre-3H.3 implementation matched ONLY stdlib
        ``Traceback (most recent call last)`` followed by
        ``ErrorClass: msg``. That covered Python script crashes but
        completely missed pytest's own diagnostic formats — every
        SWE-Bench-Pro op in the bt-2026-05-25-085310 soak bailed at
        the hard-guard with ``error_type=UnknownError`` even though
        pytest had captured rich, well-located failure info in its
        own format.

        Pattern cascade (first match wins, most-specific first):

          1. Standard Python ``Traceback (most recent call last):`` —
             stdlib script crashes; preserved verbatim from pre-3H.3.

          2. Pytest collection errors —
             ``ERROR collecting <path>`` + ``E   <ErrorClass>: <msg>``.
             Fires when pytest can't import a test module (e.g.,
             SyntaxError in the patched source).

          3. Pytest import-during-conftest —
             ``ImportError while loading conftest`` with
             ``<path>:<line>: in ...``.

          4. Pytest assertion failures — the ``E`` prefix lines
             pytest emits during failed test output:
             ``<path>:<line>: in <fn>`` + ``E   <ErrorClass>``.

          5. Pytest short summary — ``FAILED <path>::<test>`` or
             ``ERROR <path>::<test>`` for last-resort identification.

        Every pattern extracts ``error_type``, ``message``,
        ``file_path``, ``line_number`` so the downstream micro-prompt
        builder always sees a usable target. ``UnknownError`` with
        ``line_number=0`` is the final fallback — only reached when
        the output is genuinely unparseable.
        """
        # ── Cascade #1 — stdlib Traceback (pre-3H.3 path, preserved) ──
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

        # ── Cascade #2 — pytest collection errors ──
        # Pytest collection failures look like:
        #   ___________ ERROR collecting tests/foo.py ___________
        #   tests/foo.py:42: in <module>
        #       from bar import baz
        #   E   ImportError: cannot import name 'baz' from 'bar'
        coll = re.search(
            r'ERROR collecting (\S+).*?'
            r'^(\S+):(\d+):.*?'
            r'^E\s+(\w+(?:Error|Exception)):\s*(.*?)$',
            output, re.DOTALL | re.MULTILINE,
        )
        if coll:
            _coll_path, file_path, line_num, etype, msg = coll.groups()
            return ExtractedError(
                error_type=etype.strip(),
                message=msg.strip(),
                file_path=file_path,
                line_number=int(line_num),
                traceback_excerpt=output[
                    max(0, coll.start()):coll.end()
                ][-500:],
                full_output=output[-2000:],
            )

        # ── Cascade #3 — import-time error during conftest/loading ──
        # Pytest emits e.g.:
        #   ImportError while loading conftest '/path/conftest.py'.
        #   /path/conftest.py:7: in <module>
        #       from foo import bar
        #   E   ImportError: ...
        import_err = re.search(
            r'(ImportError|ModuleNotFoundError) while loading conftest.*?'
            r'^(\S+):(\d+):\s*in.*?'
            r'^E\s+(\w+(?:Error|Exception)):\s*(.*?)$',
            output, re.DOTALL | re.MULTILINE,
        )
        if import_err:
            _outer, conftest_path, conftest_line, etype, msg = (
                import_err.groups()
            )
            return ExtractedError(
                error_type=etype.strip(),
                message=msg.strip(),
                file_path=conftest_path,
                line_number=int(conftest_line),
                traceback_excerpt=output[
                    max(0, import_err.start() - 200):import_err.end()
                ][-500:],
                full_output=output[-2000:],
            )

        # ── Cascade #4 — pytest assertion failure (E   ErrorClass) ──
        # Pytest test failures look like:
        #   tests/foo.py:42: in test_bar
        #       assert x == 1
        #   E   AssertionError: assert 2 == 1
        # Match the LAST occurrence in case multiple tests failed.
        assertion_blocks = list(re.finditer(
            r'^(\S+):(\d+):\s*in\s+\S+\s*$(.*?)'
            r'^E\s+(\w+(?:Error|Exception)?):\s*(.*?)$',
            output, re.DOTALL | re.MULTILINE,
        ))
        if assertion_blocks:
            last = assertion_blocks[-1]
            a_path, a_line, a_block, a_etype, a_msg = last.groups()
            return ExtractedError(
                error_type=a_etype.strip() or "AssertionError",
                message=a_msg.strip(),
                file_path=a_path,
                line_number=int(a_line),
                traceback_excerpt=a_block[-500:],
                full_output=output[-2000:],
            )

        # ── Cascade #5 — pytest short summary (last-resort) ──
        # ``FAILED tests/foo.py::test_bar - AssertionError: ...``
        # Strict shape: requires ``::<test>`` AND ``- <ErrorClass>:`` to
        # avoid non-greedy ambiguity. The non-strict variant runs as a
        # second-tier fallback when the strict shape doesn't match.
        summary_strict = re.search(
            r'^(?:FAILED|ERROR)\s+(\S+)::\S+\s*-\s*'
            r'(\w+(?:Error|Exception))\s*:\s*(.*?)$',
            output, re.MULTILINE,
        )
        if summary_strict:
            s_path, s_etype, s_msg = summary_strict.groups()
            return ExtractedError(
                error_type=s_etype.strip(),
                message=s_msg.strip(),
                file_path=s_path,
                line_number=1,
                traceback_excerpt=output[-500:],
                full_output=output[-2000:],
            )
        # Non-strict fallback — just need ``FAILED <path>`` to attribute
        # which file failed; line/error are unknown but file is enough
        # for the model to inspect.
        summary_loose = re.search(
            r'^(?:FAILED|ERROR)\s+(\S+?)(?:::\S+)?\s*(?:-\s*(.*?))?$',
            output, re.MULTILINE,
        )
        if summary_loose:
            s_path = summary_loose.group(1)
            s_msg = summary_loose.group(2) or ""
            return ExtractedError(
                error_type="PytestFailure",
                message=s_msg.strip(),
                file_path=s_path,
                line_number=1,
                traceback_excerpt=output[-500:],
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
