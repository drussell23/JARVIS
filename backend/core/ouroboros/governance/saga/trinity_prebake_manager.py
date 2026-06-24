"""trinity_prebake_manager -- the Autonomous Pre-Flight Cache Manager.

The Trinity air-gapped sandbox (``trinity_compose_generator`` /
``trinity_docker_runner``) is REAL: the integration network is declared
``internal: true``, so Docker blackholes all WAN egress -- which means PyPI is
unreachable and a ``pip install`` AT BOOT cannot resolve. This manager closes
that operational gap by separating the build into TWO strictly-ordered phases:

  1. **BAKE phase (WAN-connected).** Before the air-gap is ever entered, build a
     per-repo, hash-tagged image from a JARVIS-side ``Dockerfile.<repo>.sandbox``
     template using the SIBLING repo as the build context
     (``docker build -f deploy/sandbox/Dockerfile.<repo>.sandbox <repo-root>``).
     The Dockerfile ``pip install``s the repo's deps HERE, where PyPI is
     reachable. (Templates live in JARVIS, never in the sibling repos -- putting
     a Dockerfile in jarvis-prime/reactor-core would be a cross-repo *write*.)

  2. **AIR-GAPPED RUN phase.** Only AFTER baking does the integration boot enter
     the ``internal: true`` network, running the CACHED images (deps already
     inside) -- no pip-at-boot, no WAN.

The cache key is a **dependency content hash** (``dep_hash``): a mutated
dependency file -> a new hash -> a new image tag -> a cache MISS -> a re-bake.
An unchanged dep set -> a cache HIT -> the fast path (no build).

Discipline:
  * **Default-OFF.** ``JARVIS_TRINITY_PREBAKE_ENABLED`` (default false) -> the
    manager is inert (``skipped=True``) and the compose generator falls back to
    its existing base-image + bind-mount path byte-identical.
  * **Fail-CLOSED.** A missing/uninspectable image -> treated as needs-bake (we
    fail toward RE-BAKING, never toward a stale/missing image). A bake FAILURE ->
    a result flagging the failure so the caller must NOT proceed to the
    air-gapped boot with a missing image (it must FRACTURE instead).
  * **WAN-vs-air-gap separation is structural.** The bake runs ``docker build``
    (WAN). The air-gapped boot is a SEPARATE step (the runner's ``up``). This
    manager NEVER enters the air-gapped network and NEVER pip-installs in it.
  * **Injectable runner.** All ``docker`` invocations live behind an injectable
    async callable so tests drive the full flow with NO real Docker / build.
  * **No hardcoding.** Image prefix, bake timeout, and Dockerfile dir are env /
    arg tunable.

Env knobs:
  * ``JARVIS_TRINITY_PREBAKE_ENABLED`` (default false) -- master switch.
  * ``JARVIS_TRINITY_IMAGE_PREFIX`` (default ``jarvis-trinity-sandbox``).
  * ``JARVIS_TRINITY_BAKE_TIMEOUT_S`` (default 1800) -- per-build bound.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.TrinityPrebake")

# --------------------------------------------------------------------------- #
# Env knobs (no hardcoding of the tunables)
# --------------------------------------------------------------------------- #
_ENV_PREBAKE_ENABLED = "JARVIS_TRINITY_PREBAKE_ENABLED"
_ENV_IMAGE_PREFIX = "JARVIS_TRINITY_IMAGE_PREFIX"
_ENV_BAKE_TIMEOUT_S = "JARVIS_TRINITY_BAKE_TIMEOUT_S"

_DEFAULT_IMAGE_PREFIX = "jarvis-trinity-sandbox"
_DEFAULT_BAKE_TIMEOUT_S = 1800.0

_TRUTHY = {"1", "true", "yes", "on"}

# The dep files that compose the cache key, in a stable order. A repo may have
# either, both, or neither; a missing file contributes an empty section.
_DEP_FILES: Tuple[str, ...] = ("requirements.txt", "pyproject.toml")


# --------------------------------------------------------------------------- #
# Injectable command boundary (reuses the runner shape from trinity_docker_runner)
# --------------------------------------------------------------------------- #
@dataclass
class CmdResult:
    """Result of a single shell command invocation (docker)."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# (argv) -> CmdResult. Injectable so tests touch no real Docker.
CmdRunner = Callable[[Sequence[str]], Awaitable[CmdResult]]


async def _default_cmd_runner(argv: Sequence[str]) -> CmdResult:  # pragma: no cover - real subprocess, operator-gated
    import subprocess  # noqa: PLC0415 - lazy, behind the boundary

    def _call() -> CmdResult:
        proc = subprocess.run(  # noqa: S603 - argv constructed, never shell
            list(argv), capture_output=True, text=True, timeout=_bake_timeout_s()
        )
        return CmdResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    return await asyncio.to_thread(_call)


# --------------------------------------------------------------------------- #
# Env helpers
# --------------------------------------------------------------------------- #
def prebake_enabled() -> bool:
    """``JARVIS_TRINITY_PREBAKE_ENABLED`` (default false)."""
    raw = os.environ.get(_ENV_PREBAKE_ENABLED, "false").strip().lower()
    return raw in _TRUTHY


def _image_prefix() -> str:
    return (os.environ.get(_ENV_IMAGE_PREFIX) or "").strip() or _DEFAULT_IMAGE_PREFIX


def _bake_timeout_s() -> float:
    try:
        return max(1.0, float(os.environ.get(_ENV_BAKE_TIMEOUT_S, _DEFAULT_BAKE_TIMEOUT_S)))
    except (TypeError, ValueError):
        return _DEFAULT_BAKE_TIMEOUT_S


# --------------------------------------------------------------------------- #
# The cache key: a deterministic dependency content hash
# --------------------------------------------------------------------------- #
def dep_hash(repo_root: str) -> str:
    """Deterministic sha256 over the CONTENT of ``requirements.txt`` +
    ``pyproject.toml`` in ``repo_root``.

    Pure + stable: same dep content -> same hash; a mutated dependency file ->
    a different hash -> a cache miss -> a re-bake. Missing file -> empty section
    (the hash still changes if a file is later added). Returns ASCII hex[:16].

    The hash is computed over a canonical, labelled concatenation so that two
    repos with the same combined bytes but different file split still differ.
    """
    h = hashlib.sha256()
    for name in _DEP_FILES:  # stable, sorted-by-definition order
        h.update(b"\x00")
        h.update(name.encode("ascii"))
        h.update(b"\x00")
        try:
            with open(os.path.join(repo_root, name), "rb") as fh:
                h.update(fh.read())
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
            # Missing / unreadable -> empty section (deterministic).
            pass
        except OSError:
            pass
    return h.hexdigest()[:16]


def sandbox_image_tag(repo: str, dhash: str) -> str:
    """``<prefix>-<repo>:<dhash>`` (prefix env-overridable).

    e.g. ``jarvis-trinity-sandbox-prime:1a2b3c4d5e6f7a8b``.
    """
    return "%s-%s:%s" % (_image_prefix(), repo, dhash)


# --------------------------------------------------------------------------- #
# Cache probe (fail-CLOSED toward re-baking)
# --------------------------------------------------------------------------- #
async def is_image_cached(repo: str, dhash: str, *, runner: CmdRunner) -> bool:
    """True iff ``docker image inspect <tag>`` reports the image present.

    Fail-soft -> False: any error (Docker absent, inspect raised, non-zero rc)
    is treated as NOT cached, i.e. needs-bake. This is fail-CLOSED in the safe
    direction: we never assume a stale/missing image is present (which would let
    the air-gapped boot proceed with no image).
    """
    tag = sandbox_image_tag(repo, dhash)
    try:
        res = await runner(["docker", "image", "inspect", tag])
        return bool(res.ok)
    except Exception:
        logger.debug(
            "[TrinityPrebake] image inspect raised for %s -> treat as needs-bake",
            tag,
            exc_info=True,
        )
        return False


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
@dataclass
class PrebakeResult:
    """Outcome of the Pre-Flight Cache Manager."""

    images: Dict[str, str] = field(default_factory=dict)
    baked: Tuple[str, ...] = ()
    cached: Tuple[str, ...] = ()
    skipped: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        """True iff the caller may proceed to the air-gapped boot.

        Either prebake was skipped (disabled -> generator uses its base-image
        path), or every required image is present (cached or freshly baked).
        A bake FAILURE -> ``ok=False`` -> the caller must FRACTURE, never boot
        air-gapped with a missing image.
        """
        return self.skipped or self.reason in ("all_cached", "baked")

    def to_dict(self) -> dict:
        return {
            "images": dict(self.images),
            "baked": list(self.baked),
            "cached": list(self.cached),
            "skipped": self.skipped,
            "reason": self.reason,
            "ok": self.ok,
        }


# The repo key -> (repo_root accessor key, dockerfile name) wiring. The repo
# roots themselves are caller-supplied (no hardcoded paths).
_REPO_KEYS: Tuple[str, ...] = ("jarvis", "prime", "reactor")


def _dockerfile_path(dockerfile_dir: str, repo: str) -> str:
    return os.path.join(dockerfile_dir, "Dockerfile.%s.sandbox" % repo)


# --------------------------------------------------------------------------- #
# The Pre-Flight Cache Manager
# --------------------------------------------------------------------------- #
async def prebake_if_needed(
    *,
    jarvis_root: str,
    prime_root: str,
    reactor_root: str,
    runner: CmdRunner,
    dockerfile_dir: str = "deploy/sandbox",
) -> PrebakeResult:
    """Ensure a hash-tagged sandbox image exists for each repo, baking the
    missing ones in a WAN-connected phase BEFORE any air-gapped boot.

    Flow:
      1. ``not prebake_enabled()`` -> ``PrebakeResult(skipped=True,
         reason="prebake_disabled")`` (inert; the generator falls back to its
         base-image + bind-mount path).
      2. For each repo: compute ``dep_hash`` -> image tag; probe ``is_image_cached``.
      3. **Fast path:** all cached -> return the image map, NO build.
         **Bake path:** for each MISSING image, run the WAN-connected
         ``docker build -f <dockerfile_dir>/Dockerfile.<repo>.sandbox -t <tag>
         <repo_root>`` (this phase is WAN -- NOT the air-gapped net). A build
         FAILURE -> fail-CLOSED: return a result flagging the failure; the
         caller must NOT proceed to the air-gapped boot.
      4. Return ``images={repo: tag}`` for ``generate_trinity_compose(images=...)``.

    NEVER raises (fail-CLOSED: any error -> a failure result, never a silent
    proceed-with-missing-image).
    """
    if not prebake_enabled():
        return PrebakeResult(skipped=True, reason="prebake_disabled")

    roots: Dict[str, str] = {
        "jarvis": jarvis_root,
        "prime": prime_root,
        "reactor": reactor_root,
    }

    # Resolve every repo's dep hash + tag, then probe the cache.
    images: Dict[str, str] = {}
    hashes: Dict[str, str] = {}
    cached: list = []
    missing: list = []
    for repo in _REPO_KEYS:
        dhash = dep_hash(roots[repo])
        hashes[repo] = dhash
        tag = sandbox_image_tag(repo, dhash)
        images[repo] = tag
        if await is_image_cached(repo, dhash, runner=runner):
            cached.append(repo)
        else:
            missing.append(repo)

    # Fast path: nothing to bake.
    if not missing:
        return PrebakeResult(
            images=images,
            baked=(),
            cached=tuple(cached),
            skipped=False,
            reason="all_cached",
        )

    # Bake path (WAN-connected). Build each missing image from the JARVIS-side
    # Dockerfile with the SIBLING repo as the build context. Fail-CLOSED on the
    # FIRST build failure -> the caller must FRACTURE.
    baked: list = []
    for repo in missing:
        tag = images[repo]
        dockerfile = _dockerfile_path(dockerfile_dir, repo)
        argv = [
            "docker",
            "build",
            "-f",
            dockerfile,
            "-t",
            tag,
            roots[repo],
        ]
        try:
            res = await runner(argv)
        except Exception as exc:  # build raised -> fail-CLOSED
            logger.warning(
                "[TrinityPrebake] bake raised for %s (%s) -> fail-CLOSED: %s",
                repo,
                tag,
                exc,
            )
            return PrebakeResult(
                images=images,
                baked=tuple(baked),
                cached=tuple(cached),
                skipped=False,
                reason="bake_failed:%s:exception:%s" % (repo, exc),
            )
        if not res.ok:
            logger.warning(
                "[TrinityPrebake] bake FAILED for %s (%s, rc=%s) -> fail-CLOSED: %s",
                repo,
                tag,
                res.returncode,
                (res.stderr or res.stdout or "")[:200],
            )
            return PrebakeResult(
                images=images,
                baked=tuple(baked),
                cached=tuple(cached),
                skipped=False,
                reason="bake_failed:%s:rc=%s" % (repo, res.returncode),
            )
        baked.append(repo)

    return PrebakeResult(
        images=images,
        baked=tuple(baked),
        cached=tuple(cached),
        skipped=False,
        reason="baked",
    )


__all__ = [
    "CmdResult",
    "CmdRunner",
    "PrebakeResult",
    "dep_hash",
    "sandbox_image_tag",
    "is_image_cached",
    "prebake_if_needed",
    "prebake_enabled",
]
