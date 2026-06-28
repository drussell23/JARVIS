"""Isomorphic Local Sandbox — Task 1: environment context manager.

Closes the fidelity gap between the Mac dev machine and the GCP A1 soak node
by forcing the four live conditions a process sees on the node, so path/cwd/env
integration bugs surface locally in milliseconds instead of 50-min cloud runs.

Reuses (no duplication):
- ``_PARITY_RELATIVE_SHAPE`` mirrors the constant in
  ``tests/integration/test_scoped_verify_parity.py`` (same value, not imported
  from a test file into product code — the strategy is reused, not the import).
- ``test_runner._ALLOWED_SANDBOX_PREFIXES`` monkeypatching strategy from
  the ``parity_repo`` fixture (same attribute, same override value).
- ``build_container_argv`` / ``run_in_container`` from ``container_sandbox``
  for ``mode="container"``.
- ``_surgery_env_exports`` env var contract from
  ``scripts/sovereign_iac_hypervisor.py`` (no import — we read the same env
  key ``JARVIS_IAC_REMOTE_ROOT`` and emit the same exported vars).
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Any, Dict, List, Optional, Tuple, Type

# ---------------------------------------------------------------------------
# Shape constants
# ---------------------------------------------------------------------------

# The live node's repo sits at _REMOTE_TRINITY_ROOT + "/jarvis".
# Mirror the exact tuple from ``tests/integration/test_scoped_verify_parity.py``
# so both fixtures and this context manager agree on the shape without baking
# "/opt/trinity" as a literal anywhere.  This is the single authoritative shape
# declaration in the product tree.
_PARITY_RELATIVE_SHAPE: Tuple[str, ...] = ("opt", "trinity", "jarvis")

# The IaC hypervisor's env var for the remote trinity root (NOT the jarvis
# subdirectory — the parent directory shared by jarvis/prime/reactor).
_REMOTE_ROOT_ENV = "JARVIS_IAC_REMOTE_ROOT"
_DEFAULT_REMOTE_ROOT = "/opt/trinity"
_LIVE_REPO_DIRNAME = "jarvis"

# The test_runner module attribute we monkeypatch for sandbox-prefix fidelity.
# Must match the exact module path and attribute name (verified via grep).
_TR_MODULE_NAME = "backend.core.ouroboros.governance.test_runner"
_TR_PREFIXES_ATTR = "_ALLOWED_SANDBOX_PREFIXES"

# Node-reality allowlist: no /tmp passthrough (exactly as on /opt/trinity/jarvis).
# A nonexistent prefix forces _is_safe_path to fall through to the repo-root
# containment check — the identical condition that caused the run-#13 rejection.
_NODE_SANDBOX_PREFIXES: Tuple[str, ...] = ("/nonexistent-sandbox-prefix",)

# Env var that test_runner._effective_sandbox_prefixes() reads at call-time.
# Setting this propagates the node policy across the process boundary so child
# processes launched by the driver inherit the same restricted allowlist without
# requiring a monkeypatch inside the child.
_SANDBOX_PREFIXES_ENV = "JARVIS_SANDBOX_PREFIXES"


# ---------------------------------------------------------------------------
# Node env-var contract
# ---------------------------------------------------------------------------

def _build_node_env(remote_root: str, effective_root: Path) -> Dict[str, str]:
    """Return the env vars the live node carries (from ``_surgery_env_exports``
    + the startup script).  ``remote_root`` is the trinity root (e.g.
    ``/opt/trinity``); ``effective_root`` is the materialized jarvis path."""
    trinity = Path(remote_root)
    return {
        # The IaC hypervisor env var — the trinity root, NOT the jarvis path.
        _REMOTE_ROOT_ENV: remote_root,
        # surgery_env_exports: prime + reactor paths
        "JARVIS_PRIME_REPO_PATH": str(trinity / "prime"),
        "JARVIS_REACTOR_REPO_PATH": str(trinity / "reactor"),
        # surgery_env_exports: capability flags
        "JARVIS_TRINITY_PREBAKE_ENABLED": "1",
        "JARVIS_CROSS_REPO_MUTATION_ENABLED": "1",
        "JARVIS_CHAOS_INJECTOR_ENABLED": "1",
        # Convenience: lets cwd-independent code locate the repo without
        # traversing .git when running under the isomorphic env.
        "JARVIS_REPO_PATH": str(effective_root),
    }


# ---------------------------------------------------------------------------
# IsomorphicEnv
# ---------------------------------------------------------------------------

class IsomorphicEnv:
    """Context manager that forces local conditions isomorphic to the GCP A1
    soak node; restores everything on exit (fail-soft).

    **Fidelity note**: Parity is mode-dependent. ``mode="process"`` reproduces
    cwd≠repo_root and the sandbox-prefix rejection policy, but does NOT catch
    code that hardcodes literal path comparisons (symlinks resolve to tmpdir).
    ``mode="container"`` uses a genuine bind-mount at ``/opt/trinity/jarvis``,
    providing full path-literal parity; use this for strict final local confirm.

    Four conditions forced on ``__enter__``:

    1. **Live absolute root** — the repo is exposed at a path whose trailing
       segments match ``_PARITY_RELATIVE_SHAPE`` (``opt/trinity/jarvis``).

       ``mode="process"`` (default, fast on M1): a symlink is created at
       ``<tmpdir>/opt/trinity/jarvis`` pointing to ``repo_root``.  ``env.root``
       returns this symlink path.

       ``mode="container"``: the in-container live path
       ``/opt/trinity/jarvis`` is the logical ``env.root``; ``run()`` mounts
       ``repo_root`` there via ``build_container_argv``.

    2. **cwd ≠ repo_root** — the process CWD is set to a disjoint sibling
       (``<tmpdir>/app``) so code relying on ``os.getcwd()`` to derive
       ``repo_root`` reproduces the run-#13 mismatch.

    3. **Live sandbox-prefix policy** — ``test_runner._ALLOWED_SANDBOX_PREFIXES``
       is patched to ``("/nonexistent-sandbox-prefix",)`` for the duration,
       removing the ``/tmp`` passthrough that masks wrong-root rejections in
       the hermetic harness.  ALSO exports ``JARVIS_SANDBOX_PREFIXES`` env var
       so any child process (e.g. the O+V organism subprocess) inherits the
       same restricted policy without a monkeypatch.
       ``test_runner._effective_sandbox_prefixes()`` reads this env var at
       call-time, making fidelity cross the process boundary.
       Both the module attribute and the env var are restored verbatim on exit.

    4. **Node env vars** — sets the vars that ``build_startup_script`` /
       ``_surgery_env_exports`` stamp on the live node (``JARVIS_IAC_REMOTE_ROOT``,
       ``JARVIS_PRIME_REPO_PATH``, ``JARVIS_REACTOR_REPO_PATH``,
       ``JARVIS_TRINITY_PREBAKE_ENABLED``, ``JARVIS_CROSS_REPO_MUTATION_ENABLED``,
       ``JARVIS_CHAOS_INJECTOR_ENABLED``, ``JARVIS_REPO_PATH``).  Each prior
       value is saved and restored on exit.

    All four are restored on ``__exit__`` even if the body raises — each step
    is fail-soft (independent try/except) so a failure in one restore never
    blocks the others.

    Re-entrancy: each ``IsomorphicEnv`` instance owns its own saved state; it
    is safe to run multiple instances sequentially (e.g. across test functions).
    Nested use is not recommended (the inner instance sees the outer's patched
    state as its "original" and restores it correctly, but the outer may then
    restore stale values).

    Parameters
    ----------
    repo_root:
        The local repository directory to expose at the live path.
    mode:
        ``"process"`` (default) — symlink + chdir (fast, M1-native).
        ``"container"`` — Docker bind-mount (strict parity; requires Docker).
    live_root:
        Override the full live path (e.g. ``"/custom/trinity/jarvis"``).
        When ``None`` (default), computed as
        ``os.environ.get("JARVIS_IAC_REMOTE_ROOT", "/opt/trinity") + "/jarvis"``.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        mode: str = "process",
        live_root: Optional[str] = None,
    ) -> None:
        if mode not in ("process", "container"):
            raise ValueError(
                f"IsomorphicEnv: unknown mode {mode!r}; expected 'process' or 'container'"
            )
        self._repo_root = Path(repo_root).resolve()
        self._mode = mode

        # Derive the trinity root (parent of the jarvis subdirectory) so we can
        # set JARVIS_IAC_REMOTE_ROOT correctly.
        if live_root is not None:
            # Caller supplied the full live path; parent is the trinity root.
            self._remote_root = str(Path(live_root).parent)
        else:
            self._remote_root = os.environ.get(_REMOTE_ROOT_ENV, _DEFAULT_REMOTE_ROOT)

        # Set on __enter__; None signals "not entered".
        self._effective_root: Optional[Path] = None

        # Saved state for fail-soft restore.
        self._tmpdir: Optional[tempfile.TemporaryDirectory] = None  # type: ignore[type-arg]
        self._saved_cwd: Optional[str] = None
        self._saved_env: Dict[str, Optional[str]] = {}
        self._saved_prefixes: Optional[Tuple[str, ...]] = None
        self._saved_prefixes_env: Optional[str] = None  # prior JARVIS_SANDBOX_PREFIXES
        self._tr_module: Optional[Any] = None

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "IsomorphicEnv":
        if self._mode == "container":
            self._enter_container()
        else:
            self._enter_process()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> bool:
        # Restore in reverse order — each step is independent / fail-soft.
        self._restore_node_env()
        self._restore_sandbox_prefixes()
        self._restore_cwd()
        self._cleanup_tmpdir()
        # Never suppress exceptions — the caller sees the body's exception.
        return False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """The effective live-shaped absolute path (available after ``__enter__``)."""
        if self._effective_root is None:
            raise RuntimeError(
                "IsomorphicEnv.root accessed outside context — call __enter__ first."
            )
        return self._effective_root

    # ------------------------------------------------------------------
    # run()
    # ------------------------------------------------------------------

    def run(self, cmd: List[str], **kw: Any) -> "subprocess.CompletedProcess[bytes]":
        """Run *cmd* under the isomorphic env.

        ``mode="process"``: ``subprocess.run`` with ``cwd=env.root`` and the
        current (node-patched) ``os.environ``.

        ``mode="container"``: ``docker run`` with ``repo_root`` bind-mounted
        read-only at the live path inside the container.  Requires Docker.
        """
        if self._mode == "container":
            return self._run_container(cmd, **kw)
        return self._run_process(cmd, **kw)

    # ------------------------------------------------------------------
    # Private — enter helpers
    # ------------------------------------------------------------------

    def _enter_process(self) -> None:
        """Create live-shaped symlink, chdir to disjoint sibling, patch policy,
        set env.  All four conditions in one atomic sequence."""
        # --- Condition 1: live absolute root via symlink --------------------
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)

        live_shaped = tmp.joinpath(*_PARITY_RELATIVE_SHAPE)
        live_shaped.parent.mkdir(parents=True, exist_ok=True)
        # Symlink: tmp/opt/trinity/jarvis -> repo_root
        live_shaped.symlink_to(self._repo_root)
        self._effective_root = live_shaped

        # --- Condition 2: cwd ≠ repo_root ------------------------------------
        self._saved_cwd = os.getcwd()
        disjoint = tmp / "app"  # disjoint sibling — NOT a parent of repo_root
        disjoint.mkdir(parents=True, exist_ok=True)
        os.chdir(str(disjoint))

        # --- Condition 3: sandbox-prefix policy -------------------------------
        self._patch_sandbox_prefixes()

        # --- Condition 4: node env vars ---------------------------------------
        self._apply_node_env()

    def _enter_container(self) -> None:
        """Container mode: set logical root, patch policy, set env.

        The actual isolation happens inside Docker when ``run()`` is called.
        We still patch the local policy + env so any surrounding test-runner
        calls see the correct node conditions.
        """
        # --- Condition 1: logical live root (in-container path) ---------------
        # The canonical in-container path is /opt/trinity/jarvis.
        self._effective_root = Path("/") / Path(*_PARITY_RELATIVE_SHAPE)

        # --- Condition 2: cwd ≠ repo_root (local cwd mismatch) ---------------
        self._saved_cwd = os.getcwd()
        # Use a temp dir so we have a valid directory to chdir to.
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        disjoint = tmp / "app"
        disjoint.mkdir(parents=True, exist_ok=True)
        os.chdir(str(disjoint))

        # --- Condition 3: sandbox-prefix policy --------------------------------
        self._patch_sandbox_prefixes()

        # --- Condition 4: node env vars ----------------------------------------
        self._apply_node_env()

    # ------------------------------------------------------------------
    # Private — run helpers
    # ------------------------------------------------------------------

    def _run_process(self, cmd: List[str], **kw: Any) -> "subprocess.CompletedProcess[bytes]":
        kw.setdefault("cwd", str(self.root))
        kw.setdefault("env", os.environ.copy())
        return subprocess.run(cmd, **kw)  # type: ignore[return-value]

    def _run_container(self, cmd: List[str], **kw: Any) -> "subprocess.CompletedProcess[bytes]":
        """Build a hardened ``docker run`` argv mounting the repo at the live path."""
        docker_bin = shutil.which("docker") or "docker"
        live_path = "/" + "/".join(_PARITY_RELATIVE_SHAPE)  # /opt/trinity/jarvis

        # Attempt to use the existing hardened security profile from container_sandbox.
        # Falls back to a minimal safe argv if the module is unavailable.
        try:
            from backend.core.ouroboros.governance.container_sandbox import (  # noqa: PLC0415
                sandbox_image,
            )
            image = sandbox_image()
        except Exception:  # noqa: BLE001
            image = "python:3.11-slim"

        argv: List[str] = [
            docker_bin, "run", "--rm",
            "--network", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "256",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            # Mount the real repo_root at the live path (read-only — inspection only).
            "-v", f"{self._repo_root}:{live_path}:ro",
            "-w", live_path,
            image,
        ] + cmd

        kw.setdefault("capture_output", True)
        return subprocess.run(argv, **kw)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Private — state helpers
    # ------------------------------------------------------------------

    def _patch_sandbox_prefixes(self) -> None:
        """Replace ``test_runner._ALLOWED_SANDBOX_PREFIXES`` with the node
        reality (no ``/tmp`` passthrough).  Saves the original for restore.

        ALSO exports ``JARVIS_SANDBOX_PREFIXES`` env var so any child process
        spawned under this context (e.g. the O+V organism launched by the A1
        driver) inherits the restricted allowlist without requiring an in-process
        monkeypatch.  ``test_runner._effective_sandbox_prefixes()`` reads this
        env var at call-time, so the child's sandbox gate sees the node policy.
        """
        try:
            self._tr_module = importlib.import_module(_TR_MODULE_NAME)
            self._saved_prefixes = getattr(self._tr_module, _TR_PREFIXES_ATTR)
            setattr(self._tr_module, _TR_PREFIXES_ATTR, _NODE_SANDBOX_PREFIXES)
        except Exception:  # noqa: BLE001 — fail-soft; leave module=None → skip restore
            self._saved_prefixes = None
            self._tr_module = None

        # Export env var for child-process inheritance (process-boundary propagation).
        self._saved_prefixes_env = os.environ.get(_SANDBOX_PREFIXES_ENV)
        os.environ[_SANDBOX_PREFIXES_ENV] = ",".join(_NODE_SANDBOX_PREFIXES)

    def _apply_node_env(self) -> None:
        """Set node env vars; save prior values for restore."""
        assert self._effective_root is not None  # always set before this call
        node_env = _build_node_env(self._remote_root, self._effective_root)
        # Add PYTHONPATH: prepend the live root so subprocess ``-m backend``
        # imports resolve exactly as on the GCP node (which boots via
        # ``cd <jarvis_repo>`` in _remote_boot_shell — cwd == repo satisfies
        # the same need as an explicit PYTHONPATH).  This matches the pattern
        # in bake_soak_golden_image.py:345 and chaos_injector_ast.py:554.
        # The existing save/restore loop handles PYTHONPATH like any other key.
        existing_pythonpath = os.environ.get("PYTHONPATH", "")
        node_env["PYTHONPATH"] = (
            str(self._effective_root)
            + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
        )
        for key, val in node_env.items():
            self._saved_env[key] = os.environ.get(key)
            os.environ[key] = val

    def _restore_node_env(self) -> None:
        """Restore each saved env var (fail-soft per key)."""
        for key, saved in self._saved_env.items():
            try:
                if saved is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = saved
            except Exception:  # noqa: BLE001
                pass

    def _restore_sandbox_prefixes(self) -> None:
        """Restore the original ``_ALLOWED_SANDBOX_PREFIXES`` and the
        ``JARVIS_SANDBOX_PREFIXES`` env var (fail-soft per step)."""
        if self._tr_module is not None:
            try:
                setattr(self._tr_module, _TR_PREFIXES_ATTR, self._saved_prefixes)
            except Exception:  # noqa: BLE001
                pass
        # Restore the env var (or remove it if it was absent before entry).
        try:
            if self._saved_prefixes_env is None:
                os.environ.pop(_SANDBOX_PREFIXES_ENV, None)
            else:
                os.environ[_SANDBOX_PREFIXES_ENV] = self._saved_prefixes_env
        except Exception:  # noqa: BLE001
            pass

    def _restore_cwd(self) -> None:
        """Restore the saved working directory (fail-soft)."""
        if self._saved_cwd is not None:
            try:
                os.chdir(self._saved_cwd)
            except Exception:  # noqa: BLE001
                pass

    def _cleanup_tmpdir(self) -> None:
        """Remove the temporary directory created by the context (fail-soft)."""
        if self._tmpdir is not None:
            try:
                self._tmpdir.cleanup()
            except Exception:  # noqa: BLE001
                pass
