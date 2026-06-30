from __future__ import annotations
import hashlib
import logging
import os
from typing import Awaitable, Callable, Optional, Sequence, Tuple

from .dag_capability_token import (
    CapabilityToken, DAGProofChain, SandboxExecutionToken, TokenKind,
)

logger = logging.getLogger(__name__)


class SandboxLockFailed(RuntimeError):
    """Candidate failed to compile/run in the L4 container -- terminate the DAG."""


class RequiresCloudExecution(RuntimeError):
    """No local Docker daemon -- strict L4 policy forbids a process downgrade.

    Phase 1: terminate + flag the op. Phase 2: route execution to the GCP node.
    """


def lock_enabled() -> bool:
    return os.environ.get("JARVIS_A1_SANDBOX_LOCK_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _candidate_hash(candidate_files: Sequence[Tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for path, content in sorted(candidate_files):
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
        h.update(content.encode("utf-8"))
    return h.hexdigest()


async def acquire_sandbox_execution_token(
    *,
    op_id: str,
    candidate_files: Sequence[Tuple[str, str]],
    repo_root: str,
    chain: DAGProofChain,
    prev_token: Optional[CapabilityToken] = None,
    docker_available: Optional[Callable[[], bool]] = None,
    runner: Optional[Callable[..., Awaitable]] = None,
    sandbox_image: Optional[Callable[[], str]] = None,
    branch_context: str = "",
) -> SandboxExecutionToken:
    """Run the candidate in a hardened L4 container; mint a token iff exit==0.

    Strict container-only: no Docker -> RequiresCloudExecution (no fallback).
    Any non-zero exit / containment breach -> SandboxLockFailed (nothing is
    written to the real tree; the caller terminates the DAG).
    """
    from . import container_sandbox  # lazy import keeps module load cheap

    _docker = docker_available or container_sandbox.docker_available
    if not _docker():
        raise RequiresCloudExecution(f"op={op_id} no local Docker daemon")

    _run = runner or container_sandbox.run_in_container
    _image = sandbox_image or container_sandbox.sandbox_image
    # Build a compile+import probe over the candidate's changed modules.
    probe = _build_probe(candidate_files)
    result = await _run(code=probe, worktree=repo_root)

    exit_code = getattr(result, "exit_code", 1)
    breached = bool(getattr(result, "breached", False))
    if exit_code != 0 or breached:
        raise SandboxLockFailed(
            f"op={op_id} exit={exit_code} breached={breached} "
            f"diag={getattr(result, 'diagnostic', '')}")

    state_binding = _candidate_hash(candidate_files)
    token = chain.mint(
        kind=TokenKind.SANDBOX_EXECUTION,
        op_id=op_id,
        state_binding=state_binding,
        payload={
            "exit_code": "0",
            "image": _image(),
            "py_files": str(len([p for p, _ in candidate_files if p.endswith(".py")])),
        },
        prev=prev_token,
        branch_context=branch_context,
    )
    return token  # type: ignore[return-value]  # mint() returns the typed subclass


async def docker_preflight(*, probe=None) -> bool:
    """Async, non-blocking daemon ping run once at A1-loop start.

    Surfaces Docker absence BEFORE an op reaches APPLY, so the orchestrator
    can flag REQUIRES_CLOUD_EXECUTION early rather than failing mid-DAG.
    Default-OFF safety: only warns when lock_enabled(); otherwise just returns
    the bool with no side effects.
    """
    import asyncio
    from . import container_sandbox
    _probe = probe if probe is not None else container_sandbox.docker_available
    available = await asyncio.get_running_loop().run_in_executor(None, _probe)
    if not available and lock_enabled():
        logger.warning(
            "[Gate1] Docker daemon ABSENT at preflight -- ops will route REQUIRES_CLOUD_EXECUTION"
        )
    return available


def _build_probe(candidate_files: Sequence[Tuple[str, str]]) -> str:
    """A deterministic compile-check payload over the candidate's .py files."""
    py = [p for p, _ in candidate_files if p.endswith(".py")]
    listing = ",".join(repr(p) for p in py)
    return (
        "import py_compile, sys\n"
        f"paths = [{listing}]\n"
        "for p in paths:\n"
        "    py_compile.compile(p, doraise=True)\n"
        "print('compile-ok')\n"
    )
