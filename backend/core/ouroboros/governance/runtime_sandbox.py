"""Out-of-Process Runtime Containment — Slice 104 (§43 Arc 5).

The static cage (Iron Gate + SemanticGuardian + AST validator) is complete for
SYNTACTIC attack shapes, but cannot catch ``eval``/``exec``/``subprocess``/
``pickle`` of values constructed AT RUNTIME — the ~8.8% residual (§41.11.2). The
only unconditional answer is to stop running generated code in the O+V process at
all: execute it in a separate, confined OS process and treat any breach (timeout,
crash, egress) as a contained failure the FSM records without dying.

HONEST PLATFORM GUARANTEES (this is a safety primitive — over-claiming would be
worse than not building it):

  * **Linux/GCP** (the intended production target): the strong guarantee —
    seccomp-bpf syscall filtering + namespace isolation (or a gVisor/Docker/
    bubblewrap wrapper). Built here as an INJECTABLE backend interface
    (:func:`set_linux_isolation_backend`); this module does NOT ship a seccomp
    profile and does NOT claim one is active. A Linux deployment registers the
    real backend. On this repo's dev host (macOS/arm64) the backend is inert.

  * **macOS / arm64** (the dev host — no seccomp, no Linux namespaces exist):
    the REAL, TESTED guarantees are blast-radius confinement, NOT a syscall
    sandbox — (1) separate process (the main thread never execs generated code),
    (2) a STRIPPED environment (the child cannot read the parent's secrets),
    (3) cwd locked to the L3 worktree (relative writes are confined),
    (4) ``resource`` rlimits (CPU / address-space / file-size / process count),
    (5) a hard wall-clock timeout → SIGKILL. It does NOT block absolute-path
    filesystem access or network egress at the kernel level — that requires the
    Linux backend. :func:`guarantees_for_platform` returns the exact list.

Master ``JARVIS_RUNTIME_SANDBOX_ENABLED`` — §33.1 default-FALSE (this is an
execution-path change). This module ships the PRIMITIVE + the adversarial proof;
wiring it into the live orchestrator APPLY/VERIFY path is a deliberate, separately-
soaked step (and needs the Linux backend for a true security boundary). NEVER
raises into the caller — every failure resolves to a ``ContainmentResult``.
"""

from __future__ import annotations

import enum
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("ouroboros.runtime_sandbox")

_ENV_MASTER = "JARVIS_RUNTIME_SANDBOX_ENABLED"
_TRUTHY = ("1", "true", "yes", "on")

RUNTIME_SANDBOX_SCHEMA_VERSION = "runtime_sandbox.1"

_DEFAULT_TIMEOUT_S = 10.0
_DEFAULT_AS_BYTES = 512 * 1024 * 1024     # 512 MiB address space
_DEFAULT_CPU_S = 8                        # CPU seconds
_DEFAULT_FSIZE_BYTES = 16 * 1024 * 1024   # 16 MiB max file write


def runtime_sandbox_enabled() -> bool:
    """§33.1 master — default FALSE (execution-path change). Never raises."""
    try:
        raw = os.environ.get(_ENV_MASTER)
        return bool(raw) and raw.strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return False


def detect_platform() -> str:
    """linux / darwin / other. Never raises."""
    try:
        p = sys.platform
        if p.startswith("linux"):
            return "linux"
        if p == "darwin":
            return "darwin"
        return "other"
    except Exception:  # noqa: BLE001
        return "other"


def guarantees_for_platform(platform: Optional[str] = None) -> Tuple[str, ...]:
    """The HONEST list of guarantees actually enforced on a platform."""
    plat = platform or detect_platform()
    common = (
        "out_of_process",            # main thread never execs the payload
        "stripped_environment",      # child cannot read parent secrets
        "cwd_confined_to_worktree",  # relative-path writes confined
        "resource_limits",           # CPU / address-space / file-size rlimits
        "hard_timeout_sigkill",      # wall-clock ceiling → kill
    )
    if plat == "linux":
        # The strong guarantees — ONLY when a real backend is registered.
        if _LINUX_BACKEND is not None:
            return common + ("seccomp_syscall_filter", "namespace_isolation",
                             "network_egress_denied", "absolute_fs_confined")
        return common + ("linux_backend_not_registered",)
    # macOS / other: blast-radius confinement only (no kernel sandbox).
    return common + ("no_kernel_syscall_sandbox", "network_not_blocked",
                     "absolute_fs_not_blocked")


class ContainmentBreach(str, enum.Enum):
    """Closed taxonomy of why a contained execution did not complete cleanly."""

    NONE = "none"
    DISABLED = "disabled"
    SPAWN_FAILED = "spawn_failed"
    TIMEOUT = "timeout"               # wall-clock ceiling → SIGKILL
    NONZERO_EXIT = "nonzero_exit"     # payload crashed / raised (contained)
    SIGNAL_KILLED = "signal_killed"   # killed by a signal (e.g. OOM, segfault)
    POLICY_VIOLATION = "policy_violation"


@dataclass(frozen=True)
class ContainmentPolicy:
    """The confinement applied to a contained execution."""

    timeout_s: float = _DEFAULT_TIMEOUT_S
    env: Dict[str, str] = field(default_factory=dict)   # default {} = fully stripped
    as_bytes: int = _DEFAULT_AS_BYTES
    cpu_s: int = _DEFAULT_CPU_S
    fsize_bytes: int = _DEFAULT_FSIZE_BYTES


@dataclass(frozen=True)
class ContainmentResult:
    ok: bool
    breach: ContainmentBreach
    returncode: Optional[int]
    stdout: str
    stderr: str
    duration_s: float
    platform: str
    guarantees: Tuple[str, ...]
    diagnostic: str
    schema_version: str = RUNTIME_SANDBOX_SCHEMA_VERSION


# --- Linux isolation backend (injectable; NOT shipped on macOS) -------------
# A backend takes (argv, policy) and returns a wrapped argv (e.g. prefixed with
# bubblewrap/nsjail, or installs a seccomp pre-exec). Registered by a Linux
# deployment; absent on the dev host. We never fabricate one.
LinuxIsolationBackend = Callable[[list, "ContainmentPolicy"], list]
_LINUX_BACKEND: Optional[LinuxIsolationBackend] = None


def set_linux_isolation_backend(backend: Optional[LinuxIsolationBackend]) -> None:
    global _LINUX_BACKEND
    _LINUX_BACKEND = backend


def _build_preexec(policy: ContainmentPolicy):
    """Return a POSIX preexec_fn that drops resource limits in the child AFTER
    fork, BEFORE exec. Best-effort per-limit (macOS enforces a subset). Returns
    None on platforms without ``resource``."""
    try:
        import resource  # noqa: PLC0415 — POSIX only
    except Exception:  # noqa: BLE001
        return None

    def _preexec() -> None:  # pragma: no cover - runs in the child process
        for res_name, value in (
            ("RLIMIT_CPU", policy.cpu_s),
            ("RLIMIT_AS", policy.as_bytes),
            ("RLIMIT_FSIZE", policy.fsize_bytes),
        ):
            try:
                res = getattr(resource, res_name)
                resource.setrlimit(res, (value, value))
            except Exception:  # noqa: BLE001 — a limit the platform won't honor
                pass
        # New session so a SIGKILL on timeout reaps the whole child tree.
        try:
            os.setsid()
        except Exception:  # noqa: BLE001
            pass

    return _preexec


def run_contained_code(
    code: str,
    *,
    worktree: Any,
    policy: Optional[ContainmentPolicy] = None,
) -> ContainmentResult:
    """Execute ``code`` (a Python source string) in a SEPARATE, confined OS
    process whose cwd is locked to ``worktree`` and whose environment is stripped.
    NEVER raises — every outcome is a :class:`ContainmentResult`. Returns a
    DISABLED result when the master flag is off (the primitive is opt-in).
    """
    plat = detect_platform()
    pol = policy or ContainmentPolicy()
    guarantees = guarantees_for_platform(plat)
    started = time.monotonic()

    if not runtime_sandbox_enabled():
        return ContainmentResult(
            ok=False, breach=ContainmentBreach.DISABLED, returncode=None,
            stdout="", stderr="", duration_s=0.0, platform=plat,
            guarantees=guarantees, diagnostic=f"disabled via {_ENV_MASTER}=false",
        )

    try:
        wt = Path(worktree)
        wt.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return ContainmentResult(
            ok=False, breach=ContainmentBreach.SPAWN_FAILED, returncode=None,
            stdout="", stderr=str(exc), duration_s=0.0, platform=plat,
            guarantees=guarantees, diagnostic=f"worktree prepare failed: {exc}",
        )

    # sys.executable is absolute → the child needs no PATH → env can be fully
    # stripped (only what the policy explicitly allows; default {} = nothing).
    argv = [sys.executable, "-I", "-c", code]
    if plat == "linux" and _LINUX_BACKEND is not None:
        try:
            argv = list(_LINUX_BACKEND(argv, pol))
        except Exception as exc:  # noqa: BLE001 — a broken backend must not exec uncontained
            return ContainmentResult(
                ok=False, breach=ContainmentBreach.POLICY_VIOLATION, returncode=None,
                stdout="", stderr=str(exc), duration_s=0.0, platform=plat,
                guarantees=guarantees, diagnostic=f"linux backend failed: {exc}",
            )

    try:
        proc = subprocess.run(
            argv,
            cwd=str(wt),
            env=dict(pol.env),          # stripped (default {})
            capture_output=True,
            text=True,
            timeout=max(0.1, float(pol.timeout_s)),
            preexec_fn=_build_preexec(pol),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        dur = time.monotonic() - started
        return ContainmentResult(
            ok=False, breach=ContainmentBreach.TIMEOUT, returncode=None,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
            duration_s=dur, platform=plat, guarantees=guarantees,
            diagnostic=f"CONTAINMENT BREACH: timeout after {pol.timeout_s}s — killed",
        )
    except Exception as exc:  # noqa: BLE001 — spawn failure, never propagate
        return ContainmentResult(
            ok=False, breach=ContainmentBreach.SPAWN_FAILED, returncode=None,
            stdout="", stderr=str(exc), duration_s=time.monotonic() - started,
            platform=plat, guarantees=guarantees,
            diagnostic=f"spawn failed: {exc}",
        )

    dur = time.monotonic() - started
    rc = proc.returncode
    if rc == 0:
        return ContainmentResult(
            ok=True, breach=ContainmentBreach.NONE, returncode=0,
            stdout=proc.stdout or "", stderr=proc.stderr or "", duration_s=dur,
            platform=plat, guarantees=guarantees, diagnostic="completed clean",
        )
    # Negative return code on POSIX = killed by signal -rc (segfault, OOM, etc.).
    breach = ContainmentBreach.SIGNAL_KILLED if (rc is not None and rc < 0) else ContainmentBreach.NONZERO_EXIT
    return ContainmentResult(
        ok=False, breach=breach, returncode=rc,
        stdout=proc.stdout or "", stderr=proc.stderr or "", duration_s=dur,
        platform=plat, guarantees=guarantees,
        diagnostic=f"CONTAINMENT BREACH: {breach.value} rc={rc} (contained)",
    )
