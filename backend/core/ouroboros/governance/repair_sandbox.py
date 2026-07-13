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
- ``OverlayCapExceeded``
- ``OverlayCopyFault``
- ``SandboxValidationResult``
- ``RepairSandbox`` (incl. ``baseline_fidelity``)
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
from typing import List, Optional, Sequence, Tuple

_logger = logging.getLogger(__name__)


def _working_tree_mirror_enabled() -> bool:
    """Slice 9 (default ON): the sandbox baseline mirrors the WORKING
    TREE (what TestWatcher actually observed), not HEAD. OFF restores
    the legacy HEAD-baseline worktree strategy byte-identically."""
    return os.environ.get(
        "JARVIS_SANDBOX_WORKING_TREE_MIRROR_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")


def _overlay_max_files() -> int:
    """Slice 9 final review (Important): cap the working-tree overlay's
    O(dirty-delta) materialization cost — on a repo with thousands of
    untracked/modified files (e.g. no ``.gitignore``), copying every one
    of them into the sandbox dominates VALIDATE setup time. Env:
    ``JARVIS_SANDBOX_OVERLAY_MAX_FILES``, default ``2000``. Read at call
    time; never raises."""
    try:
        v = int(os.environ.get(
            "JARVIS_SANDBOX_OVERLAY_MAX_FILES", "2000",
        ).strip())
        return v if v > 0 else 2000
    except (ValueError, TypeError):
        return 2000


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class SandboxSetupError(Exception):
    """Raised when the sandbox cannot be initialised.

    Both the git-worktree and rsync strategies have failed.
    """


class OverlayCapExceeded(RuntimeError):
    """Raised when the working-tree dirty delta exceeds
    ``JARVIS_SANDBOX_OVERLAY_MAX_FILES`` — the overlay is refused BEFORE
    any copy, so the sandbox baseline degrades cleanly to HEAD."""


class OverlayCopyFault(RuntimeError):
    """Raised when the overlay copy loop faults mid-way.

    ``files_applied`` counts the overlay mutations (copies/removals)
    that landed BEFORE the fault — ``> 0`` means the sandbox baseline
    is a HEAD+partial-delta chimera, not clean HEAD."""

    def __init__(self, message: str, files_applied: int) -> None:
        super().__init__(message)
        self.files_applied = files_applied


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
# Working-tree overlay copy worker (sync, runs off the event loop)
# ---------------------------------------------------------------------------


def _apply_delta_entries(
    repo_root: str, tmpdir: str, entries: List[str],
) -> Tuple[int, Optional[str]]:
    """Parse ``git status --porcelain=v1 -z`` entries and apply the dirty
    delta onto the HEAD worktree at ``tmpdir``.

    Sync on purpose: up to ``JARVIS_SANDBOX_OVERLAY_MAX_FILES`` stat/
    mkdir/copy2/unlink syscalls must not run ON the event loop — the
    caller dispatches this via ``cooperative_fs_io.offload``.

    Never raises. Returns ``(applied, fault)`` where ``applied`` counts
    the overlay mutations (copies + removals) that landed, and ``fault``
    is ``None`` on success or a fault description — so the caller can
    tell a clean HEAD baseline (``applied == 0``) from a HEAD+partial-
    delta chimera (``applied > 0``)."""
    root = Path(repo_root)
    box = Path(tmpdir)
    applied = 0
    try:
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            code, rel = entry[:2], entry[3:]
            if code[0] == "R":  # rename: next entry is the ORIGINAL path
                orig = entries[i] if i < len(entries) else ""
                i += 1
                if orig and (box / orig).exists():
                    (box / orig).unlink()
                    applied += 1
            src = root / rel
            dst = box / rel
            if src.exists() and src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                applied += 1
            elif not src.exists() and dst.exists():
                dst.unlink()
                applied += 1
    except Exception as exc:  # noqa: BLE001 — fault reported with state
        return applied, f"{type(exc).__name__}: {exc}"
    return applied, None


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

    def __init__(
        self,
        repo_root: Path,
        test_timeout_s: float,
        mirror_working_tree: Optional[bool] = None,
    ) -> None:
        self._repo_root: Path = repo_root
        self._test_timeout_s: float = test_timeout_s
        self._sandbox_dir: Optional[Path] = None
        self._worktree_mode: bool = False
        self._active_proc: Optional[asyncio.subprocess.Process] = None
        self._mirror_working_tree: bool = (
            _working_tree_mirror_enabled()
            if mirror_working_tree is None
            else mirror_working_tree
        )
        # Slice 9 review Finding 2: honest baseline provenance, set by
        # _setup. "working_tree" = overlay fully applied (or rsync copy,
        # which IS the working tree); "head" = mirror disabled OR overlay
        # refused before any copy (cap-trip / git-status failure);
        # "partial" = copy loop faulted mid-way (HEAD+delta chimera).
        # Meaningful after __aenter__; consumers: repair_engine/orchestrator.
        self.baseline_fidelity: str = "head"

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
            if self._mirror_working_tree:
                try:
                    await self._overlay_working_tree_delta(tmpdir)
                    self.baseline_fidelity = "working_tree"
                except Exception as exc:  # noqa: BLE001 — fail-soft: the
                    # baseline is degraded-but-functional; rsync strategy
                    # is the heavyweight fallback, not worth forcing here.
                    # Slice 9 final review (Important): tag the reason so
                    # a delta-size cap-trip is distinguishable in logs
                    # from a genuine git fault (both raise Exception here).
                    _reason = (
                        "overlay_cap_exceeded"
                        if isinstance(exc, OverlayCapExceeded)
                        else "git_fault"
                    )
                    # Slice 9 review Finding 2: honest degradation report.
                    # "baseline is HEAD" is only true when NOTHING was
                    # copied; a mid-loop fault leaves a chimera.
                    if isinstance(exc, OverlayCopyFault) and exc.files_applied > 0:
                        self.baseline_fidelity = "partial"
                        _logger.warning(
                            "repair_sandbox: working-tree overlay faulted "
                            "mid-copy reason=%s (%s) — sandbox baseline is "
                            "PARTIAL (chimera: HEAD + %d overlay "
                            "mutation(s), no rollback)",
                            _reason, exc, exc.files_applied,
                        )
                    else:
                        self.baseline_fidelity = "head"
                        _logger.warning(
                            "repair_sandbox: working-tree overlay failed "
                            "reason=%s (%s) — sandbox baseline is HEAD, not "
                            "the working tree", _reason, exc,
                        )
            else:
                self.baseline_fidelity = "head"
            _logger.debug("repair_sandbox: initialised via git worktree at %s", tmpdir)
            return
        except Exception as exc:
            _logger.debug("repair_sandbox: git worktree failed (%s), trying rsync", exc)

        # Strategy 2: rsync
        try:
            await self._rsync_copy(tmpdir)
            self._sandbox_dir = tmpdir
            self._worktree_mode = False
            # rsync copies the live tree directly — the baseline IS the
            # working tree, no overlay needed.
            self.baseline_fidelity = "working_tree"
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

    async def _overlay_working_tree_delta(self, tmpdir: Path) -> None:
        """Copy the working tree's dirty delta onto the HEAD worktree so
        the sandbox baseline matches what TestWatcher observed. Uses
        ``git status --porcelain=v1 -z`` (NUL-safe); modified/added/
        untracked files are copied in, deleted files removed. Renames
        (``R``) arrive as two NUL-separated paths — both handled."""
        proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain=v1", "-z",
            "--untracked-files=all",
            cwd=str(self._repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        if proc.returncode:
            raise RuntimeError("git status failed for working-tree overlay")
        entries = stdout_b.decode(errors="replace").split("\0")

        # Slice 9 final review (Important): delta-size guard — count the
        # parsed status entries FIRST, before copying a single byte. An
        # unbounded dirty delta (e.g. thousands of untracked files on a
        # repo with no .gitignore) makes this overlay dominate VALIDATE
        # setup time. Over the cap, refuse the overlay outright so the
        # caller's fail-soft degrades to a HEAD-baseline sandbox instead
        # of silently eating the pipeline budget.
        _n_entries = sum(1 for _e in entries if len(_e) >= 4)
        _cap = _overlay_max_files()
        if _n_entries > _cap:
            raise OverlayCapExceeded(
                f"working-tree delta too large for overlay: {_n_entries} > {_cap}"
            )

        # Slice 9 review Finding 3: the parse+copy loop (stat/mkdir/copy2/
        # unlink × up to _cap files) must not run ON the event loop —
        # dispatch it through the shared offload substrate (thread pool;
        # IO-bound). The worker never raises: it returns (applied, fault)
        # so mid-loop-fault state survives the offload boundary.
        from backend.core.ouroboros.governance.cooperative_fs_io import (
            offload,
            is_offload_error,
        )
        result = await offload(
            _apply_delta_entries,
            str(self._repo_root), str(tmpdir), entries,
            cpu_bound=False,
        )
        if is_offload_error(result):
            # Pool-level fault before the worker ran — nothing copied.
            raise RuntimeError(
                f"working-tree overlay offload failed: {result.message}"
            )
        applied, fault = result
        if fault is not None:
            raise OverlayCopyFault(
                f"working-tree overlay copy faulted after "
                f"{applied} mutation(s): {fault}",
                files_applied=applied,
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

    def _contained_target(self, file_path: str) -> Path:
        """Clamp a MODEL-CHOSEN candidate path to the sandbox root.

        pathlib ``/`` with an absolute right operand REPLACES the left
        side entirely, and ``..`` segments walk out of the sandbox — the
        L2 repair lane feeds candidate paths straight into apply_patch /
        apply_full_content, so containment must be proven HERE, before
        any byte lands. Two proofs: (1) lexical — the normalized join
        stays under the normalized sandbox root; (2) physical — the
        nearest existing ancestor resolves (symlinks followed) inside
        the resolved root, defeating symlink hops.

        Raises ``BlockedPathError`` (the type the orchestrator's
        ``_map_tree_run_exception`` classifies fc='security') on any
        violation."""
        # Lazy import: keeps repair_sandbox free of module-level
        # governance imports (test_runner is heavyweight).
        from backend.core.ouroboros.governance.test_runner import BlockedPathError

        assert self._sandbox_dir is not None  # callers check sandbox is active
        root_norm = os.path.normpath(str(self._sandbox_dir))

        if os.path.isabs(file_path):
            raise BlockedPathError(
                f"sandbox write blocked: absolute path {file_path!r}"
            )
        joined = os.path.normpath(os.path.join(root_norm, file_path))
        if not joined.startswith(root_norm + os.sep):
            raise BlockedPathError(
                f"sandbox write blocked: {file_path!r} escapes sandbox root"
            )
        # Symlink hop defense: resolve the nearest existing (or dangling-
        # symlink) ancestor and require it to land inside the real root.
        target = Path(joined)
        anc = target
        while not (anc.exists() or anc.is_symlink()) and anc != anc.parent:
            anc = anc.parent
        root_resolved = Path(root_norm).resolve()
        anc_resolved = anc.resolve()
        if anc_resolved != root_resolved and root_resolved not in anc_resolved.parents:
            raise BlockedPathError(
                f"sandbox write blocked: {file_path!r} resolves outside "
                f"sandbox root (symlink hop)"
            )
        return target

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

        target = self._contained_target(file_path)

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

        target = self._contained_target(file_path)
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
        _ini = sandbox / "pytest.ini"
        if _ini.exists():
            # Pin the config: without this, targets under backend/ make
            # pytest adopt backend/pytest.ini whose --cov/-n addopts are
            # unrecognized in the runtime → usage error rc=4 (Run 18/19
            # 'FAILED (unknown)' contributor).
            cmd.extend(["-c", str(_ini)])
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
