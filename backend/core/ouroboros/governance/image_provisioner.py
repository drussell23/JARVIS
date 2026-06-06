"""Asynchronous Image Provisioning Daemon — Slice 107 (Phase 2).

The orchestrator must never fail because the VERIFY sandbox image is missing or
stale. At boot this daemon silently checks whether ``jarvis-verify-sandbox:latest``
exists AND whether its baked-in state hash matches the current
``requirements-sandbox.txt`` + ``Dockerfile.verify-sandbox`` — if not, it rebuilds
the image in the BACKGROUND (a fire-and-forget asyncio task) without blocking the
intake router. The state hash is stamped as an image label at build time and read
back via ``docker image inspect``, so "is the image current?" is a cheap label
comparison, not a rebuild.

Master ``JARVIS_IMAGE_PROVISIONER_ENABLED`` — §33.1 default-FALSE. NEVER raises;
every Docker interaction degrades to a structured result, never an exception into
the boot path.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger("ouroboros.image_provisioner")

_ENV_MASTER = "JARVIS_IMAGE_PROVISIONER_ENABLED"
_ENV_IMAGE = "JARVIS_RUNTIME_SANDBOX_VERIFY_IMAGE"
_TRUTHY = ("1", "true", "yes", "on")

_ENV_REQ = "JARVIS_SANDBOX_REQUIREMENTS_FILE"
_ENV_DOCKERFILE = "JARVIS_SANDBOX_DOCKERFILE"
_DEFAULT_IMAGE = "jarvis-verify-sandbox:latest"
_DEFAULT_REQ = "requirements-sandbox.txt"
_DEFAULT_DOCKERFILE = "Dockerfile.verify-sandbox"
_STATE_LABEL = "org.jarvis.state-hash"
_BUILD_TIMEOUT_S = 1800.0  # accommodates the heavier production (governance/ML) image


def provisioner_enabled() -> bool:
    """§33.1 master — default FALSE. Never raises."""
    try:
        raw = os.environ.get(_ENV_MASTER)
        return bool(raw) and raw.strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return False


def sandbox_profiles_dir() -> Path:
    return Path(__file__).resolve().parent / "sandbox_profiles"


def verify_image() -> str:
    return os.environ.get(_ENV_IMAGE, _DEFAULT_IMAGE).strip() or _DEFAULT_IMAGE


def requirements_file() -> str:
    """The requirements file this image is built from — light governance default,
    or the production governance / full-ML variant via env."""
    return os.environ.get(_ENV_REQ, _DEFAULT_REQ).strip() or _DEFAULT_REQ


def dockerfile_name() -> str:
    return os.environ.get(_ENV_DOCKERFILE, _DEFAULT_DOCKERFILE).strip() or _DEFAULT_DOCKERFILE


def image_state_hash() -> str:
    """Deterministic hash over the sandbox image's INPUTS (the CONFIGURED
    requirements file + Dockerfile). A change to either flips the hash → the
    daemon rebuilds; an unchanged requirements.txt → the heavy deps layer is
    re-used immutably and the pre-warmed container serves instantly. NEVER raises."""
    h = hashlib.sha256()
    try:
        d = sandbox_profiles_dir()
        for name in (requirements_file(), dockerfile_name()):
            p = d / name
            h.update(name.encode("utf-8"))
            h.update(p.read_bytes() if p.exists() else b"<missing>")
        return h.hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return "unknown"


@dataclass(frozen=True)
class ProvisionResult:
    master_enabled: bool
    image: str
    present: bool
    current: bool
    rebuilt: bool
    action: str          # "skipped" | "current" | "rebuilt" | "rebuild_failed" | "disabled"
    diagnostic: str


async def _default_docker_run(argv, timeout_s):
    from backend.core.ouroboros.governance.swe_bench_pro.container_engine import (
        _real_docker_run,
    )
    return await _real_docker_run(argv, timeout_s)


async def image_present_and_current(
    *, docker_run: Any = None, expected_hash: Optional[str] = None,
) -> Tuple[bool, bool]:
    """Returns (present, current). ``current`` is True only if the image exists AND
    its ``org.jarvis.state-hash`` label matches the current inputs. NEVER raises."""
    runner = docker_run or _default_docker_run
    img = verify_image()
    want = expected_hash if expected_hash is not None else image_state_hash()
    try:
        from backend.core.ouroboros.governance.swe_bench_pro.container_engine import (
            docker_bin,
        )
        dbin = docker_bin()
    except Exception:  # noqa: BLE001
        dbin = "docker"
    try:
        rc, out, _err = await runner(
            [dbin, "image", "inspect", img, "--format",
             f'{{{{index .Config.Labels "{_STATE_LABEL}"}}}}'],
            15.0,
        )
    except Exception:  # noqa: BLE001
        return (False, False)
    if rc != 0:
        return (False, False)
    present = True
    current = (out or "").strip() == want
    return (present, current)


async def provision_image(
    *, force: bool = False, docker_run: Any = None,
) -> ProvisionResult:
    """Ensure the verify-sandbox image exists + is current; rebuild if not. NEVER
    raises. Returns a DISABLED result when the master flag is off."""
    img = verify_image()
    want = image_state_hash()
    if not provisioner_enabled():
        return ProvisionResult(False, img, False, False, False, "disabled",
                               f"disabled via {_ENV_MASTER}=false")
    runner = docker_run or _default_docker_run
    present, current = await image_present_and_current(docker_run=runner, expected_hash=want)
    if present and current and not force:
        return ProvisionResult(True, img, True, True, False, "current",
                               f"image {img} present + hash-current ({want})")
    # Rebuild (the daemon runs this as a background task → non-blocking).
    try:
        from backend.core.ouroboros.governance.swe_bench_pro.container_engine import (
            docker_bin,
        )
        dbin = docker_bin()
    except Exception:  # noqa: BLE001
        dbin = "docker"
    ctx = str(sandbox_profiles_dir())
    build_argv = [
        dbin, "build", "-f", f"{ctx}/{dockerfile_name()}",
        "--build-arg", f"REQUIREMENTS={requirements_file()}",
        "-t", img, "--label", f"{_STATE_LABEL}={want}", ctx,
    ]
    try:
        rc, _out, err = await runner(build_argv, _BUILD_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        return ProvisionResult(present, img, present, False, False, "rebuild_failed",
                               f"docker build spawn failed: {exc}")
    if rc == 0:
        logger.info("[ImageProvisioner] rebuilt %s (hash=%s)", img, want)
        return ProvisionResult(True, img, True, True, True, "rebuilt",
                               f"rebuilt {img} → hash {want}")
    return ProvisionResult(present, img, present, False, False, "rebuild_failed",
                           f"docker build rc={rc}: {(err or '')[:200]}")


async def run_provisioner_daemon(*, docker_run: Any = None) -> ProvisionResult:
    """Boot entry: provision the image (rebuilding only if missing/stale). Intended
    to be launched as a fire-and-forget background task at GLS boot so it NEVER
    blocks the intake router. Inert when the master flag is off. NEVER raises."""
    try:
        if not provisioner_enabled():
            return ProvisionResult(False, verify_image(), False, False, False,
                                   "disabled", "provisioner disabled")
        return await provision_image(docker_run=docker_run)
    except Exception as exc:  # noqa: BLE001 — boot must never see us raise
        logger.debug("[ImageProvisioner] daemon swallowed: %s", exc)
        return ProvisionResult(False, verify_image(), False, False, False,
                               "rebuild_failed", f"daemon error: {exc}")
