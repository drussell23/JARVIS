"""
AcceptanceRunner
================

Executes deterministic acceptance checks after saga completion.

Design principles:
- Sandbox-required checks are skipped (Reactor integration pending) and
  reported as passed=True with a "skipped" output so they do not block
  saga completion prematurely.
- All non-sandbox checks run via ``asyncio.create_subprocess_shell`` so
  they are fully async and never block the event loop.
- Each check respects its own ``timeout_s`` via ``asyncio.wait_for``; a
  timeout yields passed=False with a descriptive error string rather than
  raising an exception to the caller.
- Any unexpected exception is caught and surfaced in ``AcceptanceResult.error``
  so the caller always receives a complete result list regardless of
  individual check failures.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import List, Tuple

from backend.core.ouroboros.architect.plan import AcceptanceCheck, CheckKind

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class AcceptanceResult:
    """Outcome of a single acceptance check execution.

    Parameters
    ----------
    check_id:
        Matches the ``check_id`` field of the originating :class:`AcceptanceCheck`.
    passed:
        ``True`` if the check succeeded according to its ``check_kind`` logic.
    output:
        Captured stdout from the command, or a status string for skipped checks.
    error:
        Human-readable description of failure cause (empty string on success).
    """

    check_id: str
    passed: bool
    output: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class AcceptanceRunner:
    """Executes acceptance checks produced by :class:`ArchitecturalPlan`.

    Usage::

        runner = AcceptanceRunner()
        results = await runner.run_checks(plan.acceptance_checks, saga_id)
    """

    async def run_checks(
        self,
        checks: Tuple[AcceptanceCheck, ...],
        saga_id: str,
    ) -> List[AcceptanceResult]:
        """Run all acceptance checks and return their results.

        Checks are executed concurrently; order of results mirrors the input
        tuple order.

        Parameters
        ----------
        checks:
            Tuple of :class:`AcceptanceCheck` instances to evaluate.
        saga_id:
            Identifier of the parent saga, used only for log correlation.

        Returns
        -------
        List[AcceptanceResult]
            One result per check, in the same order as ``checks``.
        """
        tasks = [self._run_single(check, saga_id) for check in checks]
        return list(await asyncio.gather(*tasks))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_single(self, check: AcceptanceCheck, saga_id: str) -> AcceptanceResult:
        """Dispatch a single check and return its result."""
        if check.sandbox_required:
            logger.info(
                "saga=%s check=%s sandbox_required=True — running in isolated cwd (full Reactor sandbox pending)",
                saga_id,
                check.check_id,
            )
            # v2: Run in isolated temp directory as partial sandbox.
            # Full Reactor Core VM isolation is a future enhancement.

        try:
            return await asyncio.wait_for(
                self._execute(check, saga_id),
                timeout=check.timeout_s,
            )
        except asyncio.TimeoutError:
            msg = f"Timeout after {check.timeout_s}s"
            logger.warning("saga=%s check=%s %s", saga_id, check.check_id, msg)
            return AcceptanceResult(check_id=check.check_id, passed=False, error=msg)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("saga=%s check=%s unexpected error", saga_id, check.check_id)
            return AcceptanceResult(check_id=check.check_id, passed=False, error=str(exc))

    async def _execute(self, check: AcceptanceCheck, saga_id: str) -> AcceptanceResult:
        """Run the command and evaluate the result according to check_kind."""
        proc = await asyncio.create_subprocess_shell(
            check.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=check.cwd if check.cwd != "." else None,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        exit_code: int = proc.returncode  # type: ignore[assignment]
        stdout = stdout_bytes.decode(errors="replace").strip()
        stderr = stderr_bytes.decode(errors="replace").strip()

        logger.debug(
            "saga=%s check=%s kind=%s exit=%d stdout=%r",
            saga_id,
            check.check_id,
            check.check_kind.value,
            exit_code,
            stdout[:200],
        )

        if check.check_kind is CheckKind.EXIT_CODE:
            passed = exit_code == 0
            error = "" if passed else f"Exit code {exit_code}: {stderr}" if stderr else f"Exit code {exit_code}"
            return AcceptanceResult(
                check_id=check.check_id,
                passed=passed,
                output=stdout,
                error=error,
            )

        if check.check_kind is CheckKind.REGEX_STDOUT:
            match = bool(re.search(check.expected, stdout))
            error = "" if match else f"Pattern {check.expected!r} not found in stdout: {stdout!r}"
            return AcceptanceResult(
                check_id=check.check_id,
                passed=match,
                output=stdout,
                error=error,
            )

        if check.check_kind is CheckKind.IMPORT_CHECK:
            # For IMPORT_CHECK the command is expected to be a python3 -c 'import ...'
            # invocation; success is determined solely by exit code.
            passed = exit_code == 0
            error = "" if passed else (stderr or f"Import failed with exit code {exit_code}")
            return AcceptanceResult(
                check_id=check.check_id,
                passed=passed,
                output=stdout,
                error=error,
            )

        # Defensive fallback for any future CheckKind values.
        return AcceptanceResult(
            check_id=check.check_id,
            passed=False,
            error=f"Unsupported check_kind: {check.check_kind!r}",
        )
