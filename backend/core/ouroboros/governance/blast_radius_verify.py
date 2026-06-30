from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable, Optional, Sequence, Set

from .dag_capability_token import (
    BlastRadiusClearedToken,
    CapabilityToken,
    DAGProofChain,
    TokenKind,
)

logger = logging.getLogger(__name__)


class BlastRadiusBreach(RuntimeError):
    """A test in the reverse-dep closure failed -- rolled back to pre-op SHA."""


class BlastRadiusGraphFailure(RuntimeError):
    """The reverse-dep graph could not be built -- fail-closed, no marker fallback."""


def blast_radius_enabled() -> bool:
    return (
        os.environ.get("JARVIS_A1_BLAST_RADIUS_ENABLED", "false").strip().lower()
        in ("1", "true", "yes")
    )


async def acquire_blast_radius_token(
    *,
    op_id: str,
    scope_files: Sequence[str],
    pre_op_tree_sha: str,
    chain: DAGProofChain,
    prev_token: CapabilityToken,
    graph_fn: Callable[[Sequence[str]], Awaitable[Set[str]]],
    test_fn: Callable[[Set[str]], Awaitable[dict]],
    current_tree_sha_fn: Callable[[], Awaitable[str]],
    rollback_fn: Optional[Callable[[str], Awaitable[None]]],
    dlq_fn: Optional[Callable[[str], None]],
) -> BlastRadiusClearedToken:
    """Run the full reverse-dependency closure of the modified AST.

    Phases:
      1. graph_fn(scope_files) -- ANY exception => fail-closed rollback + DLQ +
         raise BlastRadiusGraphFailure. test_fn is NEVER called on graph failure.
      2. test_fn(tests) -- any non-empty failed list (NO retry) => rollback to
         pre_op_tree_sha, assert SHA equality cryptographically, DLQ, raise
         BlastRadiusBreach.
      3. All pass => mint BlastRadiusClearedToken chained to prev_token.

    rollback_fn and current_tree_sha_fn are ASYNC callables so they integrate
    with WorkspaceCheckpointManager.restore_checkpoint (async) and the async
    git-subprocess tree-SHA reader without blocking the event loop.
    dlq_fn is sync (intake_dlq.append_dlq is sync); called fail-soft after
    rollback confirming the SHA before the exception propagates.
    """

    async def _rollback_and_assert(reason: str) -> None:
        if rollback_fn is not None:
            await rollback_fn(pre_op_tree_sha)
        restored = await current_tree_sha_fn()
        if dlq_fn is not None:
            try:
                dlq_fn(reason)
            except Exception as _dlq_exc:  # noqa: BLE001 — DLQ is best-effort; never mask the primary failure
                logger.warning("[Gate2] op=%s dlq_fn failed (best-effort): %s", op_id, _dlq_exc)
        if restored != pre_op_tree_sha:
            raise BlastRadiusBreach(
                f"op={op_id} ROLLBACK FAILED restored={restored!r} != pre={pre_op_tree_sha!r}"
            )

    # Phase 1 -- graph build; ANY exception is fail-closed
    try:
        tests: Set[str] = await graph_fn(scope_files)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Gate2] op=%s graph FAILURE: %s", op_id, exc)
        await _rollback_and_assert("blast_radius_graph_failure")
        # NOTE: if _rollback_and_assert raised BlastRadiusBreach (rollback left
        # the tree at a different SHA), that propagates instead — a broken
        # rollback is the more alarming signal and intentionally takes priority.
        raise BlastRadiusGraphFailure(f"op={op_id}: {exc}") from exc

    # Phase 2 + 3 -- wrapped so ANY unexpected crash forces rollback
    try:
        result: dict = await test_fn(set(tests))
        failed = list(result.get("failed", []))
        if failed:
            logger.warning(
                "[Gate2] op=%s blast-radius FAIL %d/%s",
                op_id,
                len(failed),
                result.get("total"),
            )
            await _rollback_and_assert("blast_radius_breach")
            raise BlastRadiusBreach(f"op={op_id} failed={failed}")

        # Phase 3 -- all pass; read post-op SHA for the token payload
        post_tree_sha = await current_tree_sha_fn()
        token = chain.mint(
            kind=TokenKind.BLAST_RADIUS_CLEARED,
            op_id=op_id,
            state_binding=pre_op_tree_sha,
            payload={
                "n_tests": str(result.get("total", len(tests))),
                "post_tree_sha": post_tree_sha,
            },
            prev=prev_token,
        )
        return token  # type: ignore[return-value]
    except (BlastRadiusBreach, BlastRadiusGraphFailure):
        raise  # rollback already performed; do not double-roll
    except Exception as exc:  # noqa: BLE001 -- any unexpected crash forces rollback
        await _rollback_and_assert("blast_radius_unexpected")
        raise BlastRadiusBreach(f"op={op_id} unexpected blast-radius error: {exc}") from exc
