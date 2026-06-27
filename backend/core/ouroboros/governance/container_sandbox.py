"""Containerized Sandbox Backend — Slice 105 (§43 Arc 5, the Linux bridge).

Closes the macOS gap from Slice 104. Docker Desktop on macOS/arm64 runs a REAL
Linux kernel in a lightweight VM, so a hardened Docker container delivers genuine
KERNEL containment — ``--cap-drop ALL`` + ``--network none`` + ``--read-only`` +
seccomp — runnable and verifiable locally, with the identical profile production
runs on GCP/Linux. Docker IS the bridge from the dev Mac to the Tier-D production
containment model.

This module is the GENERAL-PURPOSE bridge between two things that already exist:
  * Slice 92's ``container_engine.build_hardened_security_argv()`` — the zero-trust
    Docker profile, written precisely to neutralize the 5 residual ``run_body_*``
    runtime sinks (§50.12 / the ~8.8% §41.11.2 residual) — but wired ONLY into the
    SWE-bench scoring harness;
  * Slice 104's ``runtime_sandbox`` ``ContainmentResult`` / ``ContainmentBreach``
    types + the out-of-process contract.

We compose them so ARBITRARY generated APPLY/VERIFY code can be injected into the
locked-down container, executed asynchronously, and have stdout/stderr pulled back
— a contained sink may still execute, but it cannot exfiltrate (no network route),
reach the host FS (read-only rootfs, no host bind beyond the worktree), escalate
privilege (all caps dropped + no-new-privileges + seccomp), or fork-bomb
(pids-limit). NEVER raises into the caller — every outcome is a ContainmentResult.

Master ``JARVIS_RUNTIME_SANDBOX_ENABLED`` (default-FALSE, shared with Slice 104) +
backend selector ``JARVIS_RUNTIME_SANDBOX_BACKEND`` (``local`` | ``container``,
default ``container`` when Docker is present). Image ``JARVIS_RUNTIME_SANDBOX_IMAGE``
(default ``python:3.11-slim``). Optional strict profile
``JARVIS_RUNTIME_SANDBOX_SECCOMP_PROFILE``.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from backend.core.ouroboros.governance.runtime_sandbox import (
    ContainmentBreach,
    ContainmentPolicy,
    ContainmentResult,
    runtime_sandbox_enabled,
)

logger = logging.getLogger("ouroboros.container_sandbox")

_ENV_BACKEND = "JARVIS_RUNTIME_SANDBOX_BACKEND"
_ENV_IMAGE = "JARVIS_RUNTIME_SANDBOX_IMAGE"
_ENV_SECCOMP = "JARVIS_RUNTIME_SANDBOX_SECCOMP_PROFILE"
_DEFAULT_IMAGE = "python:3.11-slim"

# The honest, kernel-enforced guarantees of a hardened Docker container (real
# Linux kernel, even when the Docker VM runs on macOS).
_CONTAINER_GUARANTEES: Tuple[str, ...] = (
    "out_of_process",
    "stripped_environment",
    "network_egress_denied",       # --network none
    "all_capabilities_dropped",    # --cap-drop ALL
    "no_new_privileges",           # --security-opt no-new-privileges
    "readonly_rootfs",             # --read-only (writes only to the worktree mount + tmpfs)
    "pids_limited",                # --pids-limit (fork-bomb ceiling)
    "seccomp_filtered",            # Docker default seccomp (+ optional strict profile)
    "hard_timeout_kill",
)


def sandbox_image() -> str:
    return os.environ.get(_ENV_IMAGE, _DEFAULT_IMAGE).strip() or _DEFAULT_IMAGE


def _backend_selected() -> str:
    raw = os.environ.get(_ENV_BACKEND, "").strip().lower()
    return raw if raw in ("local", "container") else ""


def docker_available() -> bool:
    """Best-effort: is a docker binary on PATH? (We do NOT shell out here — that
    would be I/O on a hot import path; the actual run surfaces a SPAWN_FAILED
    breach if the daemon is down.) NEVER raises."""
    try:
        from backend.core.ouroboros.governance.swe_bench_pro.container_engine import (
            docker_bin,
        )
        return shutil.which(docker_bin()) is not None
    except Exception:  # noqa: BLE001
        return shutil.which("docker") is not None


def containerized_sandbox_enabled() -> bool:
    """The container backend is active iff the runtime-sandbox master is on AND
    the backend is container (explicitly, or by default when Docker is present).
    NEVER raises."""
    if not runtime_sandbox_enabled():
        return False
    sel = _backend_selected()
    if sel == "local":
        return False
    if sel == "container":
        return True
    # Unset → default to container when Docker is available (the strong path).
    return docker_available()


def container_guarantees() -> Tuple[str, ...]:
    return _CONTAINER_GUARANTEES


def build_container_argv(
    code: str,
    *,
    worktree: str,
    image: Optional[str] = None,
    policy: Optional[ContainmentPolicy] = None,
    seccomp_profile: Optional[str] = None,
    read_only: bool = False,
) -> List[str]:
    """PURE: the hardened ``docker run`` argv that injects ``code`` into a locked-
    down container. Composes Slice 92's zero-trust profile verbatim. The worktree
    is the ONLY writable host mount (the "designated output directory"); the
    rootfs is read-only. NEVER raises (returns a best-effort argv)."""
    pol = policy or ContainmentPolicy()
    img = (image or sandbox_image())
    try:
        from backend.core.ouroboros.governance.swe_bench_pro.container_engine import (
            build_hardened_security_argv,
            docker_bin,
            host_platform_flag,
        )
        dbin = docker_bin()
        plat = host_platform_flag()
        hardening = build_hardened_security_argv(allow_tmp=True)
    except Exception:  # noqa: BLE001 — fall back to literal flags if compose fails
        dbin = "docker"
        plat = None
        hardening = [
            "--network", "none", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--read-only",
            "--pids-limit", "128",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        ]

    argv: List[str] = [dbin, "run", "--rm"]
    if plat:
        argv += ["--platform", plat]
    argv += hardening
    # Resource ceilings (kernel-enforced inside the container).
    argv += ["--memory", str(int(pol.as_bytes)), "--cpus", "1"]
    prof = seccomp_profile if seccomp_profile is not None else os.environ.get(_ENV_SECCOMP, "").strip()
    if prof:
        argv += ["--security-opt", f"seccomp={prof}"]
    # Mount mode: read-only for inspection-only callers (bash); writable for L3
    # code-exec callers that legitimately write to a DISPOSABLE worktree.
    mount_mode = "ro" if read_only else "rw"
    argv += ["-v", f"{worktree}:/work:{mount_mode}", "-w", "/work"]
    # Inject the payload: isolated python, no env inheritance (-I + docker's
    # default empty env), code passed inline.
    argv += ["--entrypoint", "python3", img, "-I", "-c", code]
    return argv


async def run_in_container(
    code: str,
    *,
    worktree: Any,
    image: Optional[str] = None,
    policy: Optional[ContainmentPolicy] = None,
    seccomp_profile: Optional[str] = None,
    docker_run: Any = None,
    read_only: bool = False,
) -> ContainmentResult:
    """Execute ``code`` in a hardened, ephemeral Docker container; pull back
    stdout/stderr across the (async subprocess) IPC bridge. NEVER raises — every
    outcome is a :class:`ContainmentResult`. ``docker_run`` is the injectable
    async runner (default = container_engine._real_docker_run) — the test seam.
    Returns a DISABLED result when the master flag is off.
    """
    pol = policy or ContainmentPolicy()
    started = time.monotonic()
    if not runtime_sandbox_enabled():
        return ContainmentResult(
            ok=False, breach=ContainmentBreach.DISABLED, returncode=None,
            stdout="", stderr="", duration_s=0.0, platform="linux-container",
            guarantees=_CONTAINER_GUARANTEES,
            diagnostic="disabled via JARVIS_RUNTIME_SANDBOX_ENABLED=false",
        )
    try:
        wt = Path(worktree)
        wt.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return ContainmentResult(
            ok=False, breach=ContainmentBreach.SPAWN_FAILED, returncode=None,
            stdout="", stderr=str(exc), duration_s=0.0, platform="linux-container",
            guarantees=_CONTAINER_GUARANTEES, diagnostic=f"worktree prepare failed: {exc}",
        )

    if docker_run is None:
        try:
            from backend.core.ouroboros.governance.swe_bench_pro.container_engine import (
                _real_docker_run,
            )
            docker_run = _real_docker_run
        except Exception as exc:  # noqa: BLE001
            return ContainmentResult(
                ok=False, breach=ContainmentBreach.SPAWN_FAILED, returncode=None,
                stdout="", stderr=str(exc), duration_s=0.0, platform="linux-container",
                guarantees=_CONTAINER_GUARANTEES, diagnostic=f"docker runner unavailable: {exc}",
            )

    argv = build_container_argv(
        code, worktree=str(wt), image=image, policy=pol,
        seccomp_profile=seccomp_profile, read_only=read_only,
    )
    try:
        rc, out, err = await docker_run(argv, float(pol.timeout_s))
    except Exception as exc:  # noqa: BLE001 — docker spawn failure, never propagate
        return ContainmentResult(
            ok=False, breach=ContainmentBreach.SPAWN_FAILED, returncode=None,
            stdout="", stderr=str(exc), duration_s=time.monotonic() - started,
            platform="linux-container", guarantees=_CONTAINER_GUARANTEES,
            diagnostic=f"docker spawn failed: {exc}",
        )

    dur = time.monotonic() - started
    if rc == 0:
        return ContainmentResult(
            ok=True, breach=ContainmentBreach.NONE, returncode=0,
            stdout=out or "", stderr=err or "", duration_s=dur,
            platform="linux-container", guarantees=_CONTAINER_GUARANTEES,
            diagnostic="container completed clean",
        )
    # container_engine's runner returns 124 on its own timeout kill.
    if rc == 124:
        return ContainmentResult(
            ok=False, breach=ContainmentBreach.TIMEOUT, returncode=124,
            stdout=out or "", stderr=err or "", duration_s=dur,
            platform="linux-container", guarantees=_CONTAINER_GUARANTEES,
            diagnostic=f"CONTAINMENT BREACH: container timeout after {pol.timeout_s}s — killed",
        )
    breach = ContainmentBreach.SIGNAL_KILLED if (rc is not None and rc < 0) else ContainmentBreach.NONZERO_EXIT
    return ContainmentResult(
        ok=False, breach=breach, returncode=rc, stdout=out or "", stderr=err or "",
        duration_s=dur, platform="linux-container", guarantees=_CONTAINER_GUARANTEES,
        diagnostic=f"CONTAINMENT BREACH: container {breach.value} rc={rc} (contained)",
    )


async def run_payload_contained(
    code: str,
    *,
    worktree: Any,
    policy: Optional[ContainmentPolicy] = None,
    docker_run: Any = None,
) -> Optional[ContainmentResult]:
    """Live APPLY/VERIFY integration surface (Phase 3 wire). Gated, fail-safe:

      * containment DISABLED → returns ``None`` → the caller runs the payload its
        normal way (byte-identical legacy — the master flag is default-FALSE);
      * containment ENABLED → returns a :class:`ContainmentResult`; the caller
        inspects ``.ok`` and, on ``not ok`` (a ``ContainmentBreach``), records the
        formal breach and falls back gracefully WITHOUT crashing the FSM.

    The orchestrator adopts this as a one-line gated call at the VERIFY seam:

        res = await run_payload_contained(code, worktree=wt)
        if res is None:            # containment off → legacy path
            ...
        elif not res.ok:           # ContainmentBreach → record + graceful fallback
            ...
        else:                      # contained clean → use res.stdout

    NEVER raises.
    """
    try:
        if not containerized_sandbox_enabled():
            return None
        return await run_in_container(
            code, worktree=worktree, policy=policy, docker_run=docker_run,
        )
    except Exception as exc:  # noqa: BLE001 — the wire must never crash the FSM
        logger.debug("[ContainerSandbox] run_payload_contained swallowed: %s", exc)
        return ContainmentResult(
            ok=False, breach=ContainmentBreach.SPAWN_FAILED, returncode=None,
            stdout="", stderr=str(exc), duration_s=0.0, platform="linux-container",
            guarantees=_CONTAINER_GUARANTEES, diagnostic=f"wire error (fell back): {exc}",
        )


def _parse_pytest_summary(stdout: str) -> Tuple[int, int, int, int, Tuple[str, ...]]:
    """General pytest-summary parser → (passed, failed, errors, total, failed_names).
    Pure, NEVER raises. Reads the canonical ``N passed, M failed, K errors`` summary
    line + the ``FAILED <id>`` lines (NOT the SWE-bench expected-id parser, which is
    keyed to held-out test IDs)."""
    import re as _re
    try:
        text = stdout or ""
        def _count(word: str) -> int:
            m = _re.findall(r"(\d+)\s+" + word, text)
            return int(m[-1]) if m else 0
        passed = _count("passed")
        failed = _count("failed")
        errors = _count("error(?:s)?")
        skipped = _count("skipped")
        failed_names = tuple(_re.findall(r"^FAILED\s+(\S+)", text, _re.MULTILINE))[:64]
        total = passed + failed + errors + skipped
        return passed, failed, errors, total, failed_names
    except Exception:  # noqa: BLE001
        return 0, 0, 0, 0, ()


@dataclass(frozen=True)
class PytestContainerResult:
    """Structured pass/fail telemetry from a containerized pytest run."""
    ok: bool
    breach: ContainmentBreach
    passed: int
    failed: int
    total: int
    failed_names: Tuple[str, ...]
    returncode: Optional[int]
    duration_s: float
    diagnostic: str


def build_pytest_container_argv(
    test_targets: List[str],
    *,
    worktree: str,
    image: Optional[str] = None,
    policy: Optional[ContainmentPolicy] = None,
    seccomp_profile: Optional[str] = None,
) -> List[str]:
    """PURE: the hardened ``docker run`` argv that mounts the worktree READ-ONLY
    and runs the project's pytest suite inside the locked-down image (whose
    ENTRYPOINT is ``python -m pytest``). Cache is disabled + bytecode suppressed
    because the rootfs + /work are read-only. NEVER raises."""
    pol = policy or ContainmentPolicy()
    img = image or os.environ.get("JARVIS_RUNTIME_SANDBOX_VERIFY_IMAGE", "jarvis-verify-sandbox:latest")
    try:
        from backend.core.ouroboros.governance.swe_bench_pro.container_engine import (
            build_hardened_security_argv,
            docker_bin,
        )
        dbin = docker_bin()
        hardening = build_hardened_security_argv(allow_tmp=True)
    except Exception:  # noqa: BLE001
        dbin = "docker"
        hardening = ["--network", "none", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges", "--read-only",
                     "--pids-limit", "128", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"]
    # NOTE: no forced --platform. The verify-sandbox image is NATIVE arch
    # (arm64 on the M1 dev host, amd64 on GCP Spot VMs) or multi-arch — Docker
    # selects the matching arch. (container_engine.host_platform_flag() is amd64-
    # biased for the SWE-bench images and would mismatch a locally-built arm64 image.)
    argv: List[str] = [dbin, "run", "--rm"]
    argv += hardening
    argv += ["--memory", str(int(pol.as_bytes)), "--cpus", "1"]
    prof = seccomp_profile if seccomp_profile is not None else os.environ.get(_ENV_SECCOMP, "").strip()
    if prof:
        argv += ["--security-opt", f"seccomp={prof}"]
    # Worktree mounted READ-ONLY (the code is immutable during VERIFY); cwd /work.
    argv += ["-v", f"{worktree}:/work:ro", "-w", "/work", "-e", "PYTHONDONTWRITEBYTECODE=1"]
    argv += [img]
    # pytest args (the image entrypoint is `python -m pytest`); cache off, clean config.
    argv += list(test_targets) + ["-p", "no:cacheprovider", "-o", "addopts=", "-q"]
    return argv


async def run_pytest_in_container(
    test_targets: List[str],
    *,
    worktree: Any,
    image: Optional[str] = None,
    policy: Optional[ContainmentPolicy] = None,
    docker_run: Any = None,
) -> PytestContainerResult:
    """Run the project's pytest suite against the read-only worktree mount inside
    the hardened container; parse structured pass/fail telemetry from stdout
    (composes ``container_engine.parse_pytest_text``). NEVER raises. Returns a
    DISABLED breach result when the runtime-sandbox master is off."""
    import time as _t
    pol = policy or ContainmentPolicy()
    started = _t.monotonic()
    if not runtime_sandbox_enabled():
        return PytestContainerResult(
            ok=False, breach=ContainmentBreach.DISABLED, passed=0, failed=0, total=0,
            failed_names=(), returncode=None, duration_s=0.0,
            diagnostic="disabled via JARVIS_RUNTIME_SANDBOX_ENABLED=false",
        )
    if docker_run is None:
        try:
            from backend.core.ouroboros.governance.swe_bench_pro.container_engine import (
                _real_docker_run,
            )
            docker_run = _real_docker_run
        except Exception as exc:  # noqa: BLE001
            return PytestContainerResult(
                ok=False, breach=ContainmentBreach.SPAWN_FAILED, passed=0, failed=0,
                total=0, failed_names=(), returncode=None, duration_s=0.0,
                diagnostic=f"docker runner unavailable: {exc}",
            )
    argv = build_pytest_container_argv(test_targets, worktree=str(worktree), image=image, policy=pol)
    try:
        rc, out, err = await docker_run(argv, float(pol.timeout_s))
    except Exception as exc:  # noqa: BLE001
        return PytestContainerResult(
            ok=False, breach=ContainmentBreach.SPAWN_FAILED, passed=0, failed=0, total=0,
            failed_names=(), returncode=None, duration_s=_t.monotonic() - started,
            diagnostic=f"docker spawn failed: {exc}",
        )
    dur = _t.monotonic() - started
    if rc == 124:
        return PytestContainerResult(
            ok=False, breach=ContainmentBreach.TIMEOUT, passed=0, failed=0, total=0,
            failed_names=(), returncode=124, duration_s=dur,
            diagnostic=f"CONTAINMENT BREACH: pytest container timeout after {pol.timeout_s}s",
        )
    # Parse structured pass/fail telemetry from the pytest summary line.
    passed, failed, errors, total, failed_names = _parse_pytest_summary(out or "")
    ok = (rc == 0)
    return PytestContainerResult(
        ok=ok, breach=ContainmentBreach.NONE if ok else ContainmentBreach.NONZERO_EXIT,
        passed=passed, failed=failed, total=total, failed_names=failed_names,
        returncode=rc, duration_s=dur,
        diagnostic=f"pytest container rc={rc} passed={passed} failed={failed} total={total}",
    )


def record_containment_breach_belief(op_id: str, result: ContainmentResult, target_files: Any) -> None:
    """Slice 106 quarantine signal: when a candidate breaches containment at
    runtime, record a FALSIFYING belief about its target files — so the Phase-3
    learning loop + the GENERATE avoidance digest steer away from this paradigm,
    and the Phase-6 sleep consolidation absorbs it. Composes belief_revision_
    ledger (no new memory logic). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.belief_revision_ledger import (
            EvidenceKind,
            master_enabled,
            record_claim,
            record_evidence,
        )
        if not master_enabled():
            return
        files = list(target_files or [])
        sig = ", ".join(sorted({str(f) for f in files if f})[:6]) or "(no target files)"
        claim = record_claim(
            f"generation in [{sig}] is runtime-safe", "containment/breach",
            target_files=files, confidence=0.7,
        )
        if claim is not None:
            breach_val = str(getattr(result.breach, "value", result.breach))
            record_evidence(
                claim.claim_id, EvidenceKind.FALSIFYING,
                source_op_id=str(op_id or ""),
                note=f"containment_breach:{breach_val}",
            )
    except Exception as exc:  # noqa: BLE001 — quarantine bookkeeping must never raise
        logger.debug("[ContainerSandbox] breach-belief record swallowed: %s", exc)
