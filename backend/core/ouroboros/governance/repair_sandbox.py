"""L2 Iterative Self-Repair Loop — Repair Sandbox

Provides an isolated sandbox environment for validating candidate patches
without touching the live repository.

The sandbox lifecycle:

1. ``__aenter__``: create a temporary directory and attempt to populate it via:
   a. ``git worktree add --detach <tmpdir> HEAD`` (preferred — preserves git context)
   b. ``rsync --archive --exclude=.git ...`` (fallback — plain copy)
   c. ``SandboxSetupError`` if both fail.

2. ``apply_patch(unified_diff, file_path)``: apply a unified diff to the
   sandboxed copy of a file using the system ``patch`` binary.

3. ``run_tests(test_targets, timeout_s)``: execute pytest inside the sandbox,
   capturing stdout/stderr and returning a :class:`SandboxValidationResult`.

4. ``__aexit__``: always tears down the sandbox, killing any live subprocess
   and removing the temporary directory.

Public API
----------
- ``SandboxSetupError``
- ``SandboxValidationResult``
- ``RepairSandbox``
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class SandboxSetupError(Exception):
    """Raised when the sandbox cannot be initialised.

    Both the git-worktree and rsync strategies have failed.
    """


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class SandboxValidationResult:
    """Result of a single test run inside the repair sandbox.

    Parameters
    ----------
    passed:
        ``True`` iff pytest exited with returncode 0.
    stdout:
        Combined standard output captured from the test process.
    stderr:
        Combined standard error captured from the test process.
    returncode:
        Exit code of the test process (``-1`` on timeout/kill).
    duration_s:
        Wall-clock seconds the test run took.
    """

    passed: bool
    stdout: str
    stderr: str
    returncode: int
    duration_s: float


# ---------------------------------------------------------------------------
# RepairSandbox
# ---------------------------------------------------------------------------


class RepairSandbox:
    """Isolated sandbox for validating candidate patches.

    Usage::

        async with RepairSandbox(repo_root=Path("."), test_timeout_s=60.0) as sb:
            await sb.apply_patch(diff_text, "backend/foo.py")
            result = await sb.run_tests(("tests/test_foo.py",), timeout_s=30.0)
            if result.passed:
                ...

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.  The sandbox will mirror this
        directory tree (excluding ``.git``, ``__pycache__``, ``*.pyc``).
    test_timeout_s:
        Default timeout for test runs (seconds).  Individual calls to
        :meth:`run_tests` may pass a different ``timeout_s``.
    """

    def __init__(self, repo_root: Path, test_timeout_s: float) -> None:
        self._repo_root: Path = repo_root
        self._test_timeout_s: float = test_timeout_s
        self._sandbox_dir: Optional[Path] = None
        self._worktree_mode: bool = False
        self._active_proc: Optional[asyncio.subprocess.Process] = None

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "RepairSandbox":
        await self._setup()
        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        await self._teardown()
        # Do not suppress exceptions.
        return None

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def sandbox_root(self) -> Optional[Path]:
        """Return the sandbox directory, or ``None`` if not yet set up."""
        return self._sandbox_dir

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    async def _setup(self) -> None:
        """Create sandbox directory and populate it."""
        tmpdir = Path(tempfile.mkdtemp(prefix="jarvis_repair_sandbox_"))
        _logger.debug("repair_sandbox: tmpdir=%s", tmpdir)

        # Strategy 1: git worktree
        try:
            await self._git_worktree_add(tmpdir)
            self._sandbox_dir = tmpdir
            self._worktree_mode = True
            _logger.debug("repair_sandbox: initialised via git worktree at %s", tmpdir)
            return
        except Exception as exc:
            _logger.debug("repair_sandbox: git worktree failed (%s), trying rsync", exc)

        # Strategy 2: rsync
        try:
            await self._rsync_copy(tmpdir)
            self._sandbox_dir = tmpdir
            self._worktree_mode = False
            _logger.debug("repair_sandbox: initialised via rsync at %s", tmpdir)
            return
        except Exception as exc:
            _logger.debug("repair_sandbox: rsync failed (%s)", exc)

        # Both failed — clean up and raise.
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise SandboxSetupError(
            f"Failed to create repair sandbox at {tmpdir}: "
            "both git-worktree and rsync strategies failed."
        )

    async def _git_worktree_add(self, tmpdir: Path) -> None:
        """Attempt git worktree add --detach <tmpdir> HEAD."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            "worktree",
            "add",
            "--detach",
            str(tmpdir),
            "HEAD",
            cwd=str(self._repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("git worktree add timed out")

        if proc.returncode:
            raise RuntimeError(
                f"git worktree add exited {proc.returncode}: "
                f"{stderr.decode(errors='replace').strip()}"
            )

    async def _rsync_copy(self, tmpdir: Path) -> None:
        """Attempt rsync copy, excluding .git, __pycache__, and *.pyc."""
        src = str(self._repo_root).rstrip("/") + "/"
        proc = await asyncio.create_subprocess_exec(
            "rsync",
            "--archive",
            "--exclude=.git",
            "--exclude=__pycache__",
            "--exclude=*.pyc",
            src,
            str(tmpdir) + "/",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("rsync timed out")

        if proc.returncode:
            raise RuntimeError(
                f"rsync exited {proc.returncode}: "
                f"{stderr.decode(errors='replace').strip()}"
            )

    # ------------------------------------------------------------------
    # Patch application
    # ------------------------------------------------------------------

    async def apply_patch(self, unified_diff: str, file_path: str) -> None:
        """Apply unified_diff to file_path inside the sandbox.

        Parameters
        ----------
        unified_diff:
            A unified diff string.  If it lacks --- / +++ file
            headers, they are prepended automatically.
        file_path:
            Repo-relative path to the file being patched (e.g.
            "backend/foo.py").

        Raises
        ------
        RuntimeError
            If the sandbox is not active or patch returns non-zero.
        """
        if self._sandbox_dir is None:
            raise RuntimeError("RepairSandbox is not active (call __aenter__ first)")

        target = self._sandbox_dir / file_path

        # Ensure the target file exists in the sandbox; copy from repo if missing.
        if not target.exists():
            src = self._repo_root / file_path
            if src.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(target))
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()

        # Prepend file headers if missing.
        patch_text = unified_diff
        if "--- " not in patch_text:
            patch_text = f"--- {file_path}\n+++ {file_path}\n{patch_text}"

        proc = await asyncio.create_subprocess_exec(
            "patch",
            "-p0",
            str(target),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=patch_text.encode()),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"patch timed out for {file_path}")

        if proc.returncode:
            # BSD patch (macOS) writes diagnostics like "I can't seem to find
            # a patch in there anywhere." to stdout, not stderr — capture
            # both so the failure is self-describing in every environment.
            _out = stdout.decode(errors="replace").strip()
            _err = stderr.decode(errors="replace").strip()
            if _err and _out:
                _details = f"{_err} | stdout: {_out}"
            else:
                _details = _err or _out
            raise RuntimeError(
                f"patch failed (exit {proc.returncode}) for {file_path}: {_details}"
            )

    async def apply_full_content(self, content: str, file_path: str) -> None:
        """Write ``content`` verbatim to ``file_path`` inside the sandbox.

        Mirrors the main-pipeline APPLY path for ``schema_version=2b.1``
        candidates (``full_content``), where the provider returns the whole
        file instead of a unified diff. With ``force_full_content=True`` set
        on every provider, this is the dominant candidate shape — so L2 must
        be able to materialize these candidates inside the sandbox without
        forging an empty unified diff and feeding it to ``patch``.

        This is sandbox-only test validation, the same role ``apply_patch``
        plays: the Iron Gate / ASCII / AST checks still run at real APPLY
        time (``ChangeEngine.execute``). Convergence in sandbox is not a
        guaranteed production apply.

        Parameters
        ----------
        content:
            The complete file content to write, verbatim.
        file_path:
            Repo-relative path to the file being materialized (e.g.
            ``"backend/foo.py"``).

        Raises
        ------
        RuntimeError
            If the sandbox is not active or the write fails.
        """
        if self._sandbox_dir is None:
            raise RuntimeError("RepairSandbox is not active (call __aenter__ first)")

        target = self._sandbox_dir / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                f"apply_full_content write failed for {file_path}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Test execution
    # ------------------------------------------------------------------

    async def run_tests(
        self,
        test_targets: Sequence[str],
        timeout_s: float,
    ) -> SandboxValidationResult:
        """Run pytest inside the sandbox.

        This method never raises.  All errors (including timeout) are captured
        into the returned SandboxValidationResult.

        Parameters
        ----------
        test_targets:
            Sequence of pytest target paths (relative to sandbox root).
            Pass an empty sequence to let pytest discover tests.
        timeout_s:
            Hard timeout for the test process.  The actual asyncio.wait_for
            deadline is timeout_s + 2.0 seconds to allow pytest teardown.

        Returns
        -------
        SandboxValidationResult
        """
        if self._sandbox_dir is None:
            return SandboxValidationResult(
                passed=False,
                stdout="",
                stderr="sandbox not initialised",
                returncode=-1,
                duration_s=0.0,
            )

        sandbox = self._sandbox_dir

        # Build environment for the subprocess.
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPYCACHEPREFIX"] = str(sandbox / ".pycache")
        env["TMPDIR"] = str(sandbox / ".tmp")
        env["PYTEST_CACHE_DIR"] = str(sandbox / ".pytest_cache")

        # Ensure scratch directories exist.
        (sandbox / ".tmp").mkdir(exist_ok=True)

        cmd = [
            "python3",
            "-m",
            "pytest",
            "--tb=short",
            "-q",
            "--no-header",
            f"--timeout={int(timeout_s)}",
            "--basetemp",
            str(sandbox / ".pytest_tmp"),
        ]
        cmd.extend(test_targets)

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(sandbox),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._active_proc = proc

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout_s + 2.0,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                finally:
                    self._active_proc = None

                duration = time.monotonic() - start
                return SandboxValidationResult(
                    passed=False,
                    stdout="",
                    stderr="timeout",
                    returncode=-1,
                    duration_s=duration,
                )

            self._active_proc = None
            duration = time.monotonic() - start

            stdout = stdout_b.decode(errors="replace")
            stderr = stderr_b.decode(errors="replace")
            returncode = proc.returncode if proc.returncode is not None else -1
            passed = returncode == 0

            return SandboxValidationResult(
                passed=passed,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                duration_s=duration,
            )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._active_proc = None
            duration = time.monotonic() - start
            _logger.warning("repair_sandbox: run_tests error: %s", exc)
            return SandboxValidationResult(
                passed=False,
                stdout="",
                stderr=str(exc),
                returncode=-1,
                duration_s=duration,
            )

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def _teardown(self) -> None:
        """Tear down the sandbox, killing any active subprocess first."""
        # Kill any active test process.
        if self._active_proc is not None:
            if self._active_proc.returncode is None:
                try:
                    self._active_proc.kill()
                    await self._active_proc.wait()
                except Exception:
                    pass
            self._active_proc = None

        sandbox = self._sandbox_dir
        if sandbox is None:
            return

        # Remove git worktree registration first.
        if self._worktree_mode:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "worktree",
                    "remove",
                    "--force",
                    str(sandbox),
                    cwd=str(self._repo_root),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=10.0
                )
                if proc.returncode:
                    # Aggregate both streams — git worktree, like BSD patch,
                    # sometimes emits diagnostics only on stdout.
                    _out = stdout_b.decode(errors="replace").strip()
                    _err = stderr_b.decode(errors="replace").strip()
                    if _err and _out:
                        _details = f"{_err} | stdout: {_out}"
                    else:
                        _details = (
                            _err
                            or _out
                            or f"exit {proc.returncode}, no diagnostic output"
                        )
                    _logger.debug(
                        "repair_sandbox: git worktree remove failed "
                        "(best-effort, exit %d): %s",
                        proc.returncode, _details,
                    )
            except Exception as exc:
                _logger.debug(
                    "repair_sandbox: git worktree remove failed (best-effort): %s",
                    exc or type(exc).__name__,
                )

        # Always remove the directory tree.
        shutil.rmtree(sandbox, ignore_errors=True)
        self._sandbox_dir = None
