"""Slice 213 — Native Orchestration Kernel (HOST-side Python launcher).

Replaces the brittle bash launcher whose ``set -e`` trap silently aborted the
2026-06-10 relaunch BEFORE the build — leaving a stale dirty image running,
caught only because the Slice-212 attestation gate kept refusing it. The
kernel is typed, tested, and async (``asyncio.create_subprocess_exec`` — no
shell, no word-splitting, no ``set -e`` semantics), and it makes POST-LAUNCH
ATTESTATION VERIFICATION part of the launch contract: a launch is not DONE
until the running container's stamp MATCHes the pinned commit and the marker
code is grep-confirmed present. The phantom-deploy class (stale image
masquerading as a fresh deploy) can no longer return exit 0.

SCOPE REFUSALS (load-bearing — these lines must not move):

* **HOST-side only.** This module is the launcher's replacement and runs where
  the launcher always ran: on the host. It does NOT enable in-container
  self-cycling — that would require mounting the Docker control socket into an
  autonomous LLM-agent container, which is root-equivalent host escape (the
  same refusal class as the Slice-199 ``~/.ssh`` mount). The socket's literal
  path deliberately appears NOWHERE in this file, and the regression suite
  pins that absence.

* **No auto-reload on self-verified patches** — refused. The Slice-208
  deception detectors are friction, not proof; a kernel that hot-reloads the
  organism with code "verified clean" by the organism's own filters would
  collapse the operator boundary. The deploy boundary stays: O+V proposes →
  orange PR → the OPERATOR merges → the kernel relaunches attested.

Graceful shutdown: SIGINT/SIGTERM during a launch terminates the child
``docker compose`` process group cleanly (no orphaned builds). In-container
graceful shutdown is already owned by the harness signal handlers (Ticket B)
+ ``stop_grace_period`` — the kernel does not duplicate it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# dirt that can never enter the image (mirrors .dockerignore — .jarvis is
# runtime state mutated through the bind-mount; __pycache__ is build noise)
_DIRT_EXCLUDES = (":!.jarvis", ":!**/__pycache__")
_DEFAULT_MARKER = "STRATEGIC IGNITION MESH"
_DEFAULT_COMPOSE = "docker-compose.dw-cortex-soak.yml"

# sync runner: (args) -> (rc, stdout)
_SyncRun = Callable[[list], Tuple[int, str]]
# async runner: (args) -> awaitable (rc, stdout)
_AsyncRun = Callable[[list], Awaitable[Tuple[int, str]]]


class LaunchVerdict:
    """Post-launch verification outcomes (the launch contract)."""

    class _V:
        def __init__(self, name: str) -> None:
            self.name = name

        def __repr__(self) -> str:  # pragma: no cover
            return f"LaunchVerdict.{self.name}"

    ATTESTED_MATCH = _V("ATTESTED_MATCH")
    STAMP_MISMATCH = _V("STAMP_MISMATCH")
    MARKER_MISSING = _V("MARKER_MISSING")
    UNVERIFIED = _V("UNVERIFIED")


def _default_sync_run(args: list) -> Tuple[int, str]:
    import subprocess
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, timeout=60,
        )
        return p.returncode, p.stdout
    except Exception:  # noqa: BLE001
        return 127, ""


def compute_dirty(*, run: Optional[_SyncRun] = None) -> str:
    """'true'/'false'/'unknown' — dirt scoped to what actually ships
    (excludes .jarvis runtime state + __pycache__, both dockerignored).
    NEVER raises."""
    run = run or _default_sync_run
    try:
        rc, out = run(
            ["git", "status", "--porcelain", "--", *_DIRT_EXCLUDES],
        )
        if rc != 0:
            return "unknown"
        return "true" if out.strip() else "false"
    except Exception:  # noqa: BLE001
        return "unknown"


def resolve_commit(*, run: Optional[_SyncRun] = None) -> str:
    """Full HEAD hash, or 'unstamped' on any failure. NEVER raises."""
    run = run or _default_sync_run
    try:
        rc, out = run(["git", "rev-parse", "HEAD"])
        commit = out.strip().lower()
        if rc != 0 or not commit:
            return "unstamped"
        return commit
    except Exception:  # noqa: BLE001
        return "unstamped"


def build_launch_env(
    *, commit: str, dirty: str, base: Dict[str, str],
) -> Dict[str, str]:
    """Stamp + pin env for the compose build. REFUSES a dirty tree upfront —
    the strict attestation gate would refuse the image at boot anyway, so
    failing at launch time is the honest, earlier failure."""
    if dirty == "true":
        raise RuntimeError(
            "refusing to launch from a DIRTY tree (image-relevant dirt "
            "present): commit or stash first — the boot-time attestation "
            "gate would fail-close this image anyway",
        )
    env = dict(base)
    env["GIT_COMMIT"] = commit
    env["GIT_DIRTY"] = dirty
    env["JARVIS_ATTESTATION_EXPECTED_COMMIT"] = commit
    env.setdefault("SOAK_REQUIREMENTS", "requirements-soak-oracle.txt")
    return env


async def _default_async_run(args: list) -> Tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def verify_postlaunch(
    *,
    container: str,
    expected_commit: str,
    run: Optional[_AsyncRun] = None,
    marker: str = _DEFAULT_MARKER,
):
    """The launch contract: the running container must carry (1) a build
    stamp whose commit prefix-matches the pin and (2) the marker code.
    NEVER raises — returns a LaunchVerdict."""
    run = run or _default_async_run
    try:
        rc, out = await run([
            "docker", "exec", container,
            "cat", "/app/.build_attestation.json",
        ])
        if rc != 0:
            return LaunchVerdict.UNVERIFIED
        stamp = str(json.loads(out).get("commit", "")).strip().lower()
        pin = expected_commit.strip().lower()
        if not (stamp.startswith(pin) or pin.startswith(stamp)):
            logger.critical(
                "[LifecycleKernel] STAMP_MISMATCH: container carries %s, "
                "pinned %s — phantom deploy detected at launch time",
                stamp[:12], pin[:12],
            )
            return LaunchVerdict.STAMP_MISMATCH
        rc2, _ = await run([
            "docker", "exec", container, "grep", "-c", marker,
            "/app/backend/core/ouroboros/governance/governed_loop_service.py",
        ])
        if rc2 != 0:
            return LaunchVerdict.MARKER_MISSING
        return LaunchVerdict.ATTESTED_MATCH
    except Exception:  # noqa: BLE001
        return LaunchVerdict.UNVERIFIED


async def launch(
    *,
    compose_file: str = _DEFAULT_COMPOSE,
    container: str = "jarvis-dw-cortex-soak",
    skip_build: bool = False,
    settle_s: float = 25.0,
) -> int:
    """Full attested launch: stamp → pin → build → up → VERIFY. Returns a
    process exit code (0 only on ATTESTED_MATCH)."""
    commit = resolve_commit()
    dirty = compute_dirty()
    logger.info(
        "[LifecycleKernel] stamping %s (dirty=%s) + pinning as boot expectation",
        commit[:12], dirty,
    )
    try:
        env = build_launch_env(commit=commit, dirty=dirty, base=dict(os.environ))
    except RuntimeError as exc:
        logger.critical("[LifecycleKernel] %s", exc)
        return 3

    async def _compose(*cargs: str) -> int:
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", compose_file, *cargs, env=env,
        )
        # graceful SIGINT/SIGTERM: forward terminate to the child so a
        # half-finished build is never orphaned
        loop = asyncio.get_running_loop()

        def _fwd() -> None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _fwd)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            return await proc.wait()
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass

    if not skip_build:
        rc = await _compose("build")
        if rc != 0:
            logger.critical("[LifecycleKernel] build failed rc=%d", rc)
            return rc
    rc = await _compose("up", "-d", "--force-recreate")
    if rc != 0:
        logger.critical("[LifecycleKernel] up failed rc=%d", rc)
        return rc
    await asyncio.sleep(settle_s)
    verdict = await verify_postlaunch(
        container=container, expected_commit=commit,
    )
    if verdict is LaunchVerdict.ATTESTED_MATCH:
        logger.info(
            "[LifecycleKernel] LAUNCH ATTESTED: %s running, stamp==pin, "
            "marker present", commit[:12],
        )
        return 0
    logger.critical(
        "[LifecycleKernel] LAUNCH NOT ATTESTED: %s — refusing to report "
        "success on an unverified deploy", verdict,
    )
    return 4


def main(argv: Optional[list] = None) -> int:  # pragma: no cover
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--compose", default=_DEFAULT_COMPOSE)
    ap.add_argument("--container", default="jarvis-dw-cortex-soak")
    ap.add_argument("--skip-build", action="store_true")
    args = ap.parse_args(argv)
    return asyncio.run(launch(
        compose_file=args.compose,
        container=args.container,
        skip_build=args.skip_build,
    ))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
