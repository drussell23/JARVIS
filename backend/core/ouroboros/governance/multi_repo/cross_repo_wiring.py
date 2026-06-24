"""cross_repo_wiring -- thin orchestrator-facing wiring for G1 + G2.

Keeps the orchestrator diff MINIMAL: all the resolve/trace/render/gate logic
lives here, the orchestrator just calls two coroutines.

  * ``build_blast_context_block(ctx, ...)`` -- GENERATE-time. Resolve the
    mutation target's Oracle ``NodeID``, trace the cross-repo blast radius (G1),
    emit the operator-visible ASCII tree visualizer, and return the rendered
    prompt block to prepend into the generation context. Fail-soft -> ``""``
    (safety unaffected: the promoter + G3 immutable-Orange floor already elevate
    the risk tier, so an empty blast block degrades context, not safety).

  * ``run_apply_sandbox_gate(ctx, ...)`` -- APPLY-time. Run the air-gapped
    Trinity integration sandbox gate (G2) and return its ``SandboxVerdict``. The
    caller routes ``verdict.fracture`` into the existing saga abort + compensating
    rollback (the FRACTURE yield is emitted INSIDE the gate).

BOTH are no-ops (return a disabled sentinel) when ``cross_repo_mutation_enabled()``
is False -- so when the master arming switch is OFF the path is BYTE-IDENTICAL to
today.

Design invariants:
  * Gated behind the master arming switch (``cross_repo_mutation_enabled()``).
  * Fail-soft on the GENERATE side (empty block), fail-CLOSED on the APPLY side
    (the gate itself returns a FRACTURE verdict on any uncertainty).
  * Reuse-only: Oracle, RepoRegistry, G1 (cross_repo_blast_context), G2
    (trinity_integration_gate), the visualizer. No reimplementation.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("Ouroboros.CrossRepoWiring")


# --------------------------------------------------------------------------- #
# Disabled sentinel verdict for the APPLY gate (master OFF -> no-op pass).
# --------------------------------------------------------------------------- #
def _disabled_sandbox_verdict() -> Any:
    """A no-op PASS verdict used when the master switch is OFF.

    Mirrors ``SandboxVerdict`` fields so the caller can treat it uniformly. When
    the master switch is OFF the surrounding cross-repo path is itself gated off
    (a real cross-repo op is never created), so a no-op pass here cannot silently
    pass a real cross-repo mutation.
    """
    from backend.core.ouroboros.governance.saga.trinity_integration_gate import (
        SandboxVerdict,
    )

    return SandboxVerdict(
        passed=True,
        fracture=False,
        reason="master_disabled",
        air_gapped=False,
        handshake_ok=False,
        containers=(),
    )


# --------------------------------------------------------------------------- #
# G1 -- blast-radius context block (GENERATE)
# --------------------------------------------------------------------------- #
def _resolve_target_node(ctx: Any, oracle: Any) -> Optional[Any]:
    """Resolve the mutation target's Oracle ``NodeID`` from ctx (best-effort).

    Strategy (first hit wins, deterministic):
      1. nodes in the primary target file that match the target symbol name;
      2. any node in the primary target file (the enclosing symbol);
      3. a global name lookup of the target symbol.
    Returns ``None`` when nothing resolves (caller fail-soft -> empty block).
    """
    try:
        target_files = tuple(getattr(ctx, "target_files", ()) or ())
        target_symbol = (getattr(ctx, "target_symbol", "") or "").strip()
        primary_file = target_files[0] if target_files else ""

        # (1)/(2) file-scoped lookup.
        if primary_file and hasattr(oracle, "find_nodes_in_file"):
            nodes = oracle.find_nodes_in_file(primary_file) or []
            if target_symbol:
                leaf = target_symbol.split(".")[-1]
                for n in nodes:
                    nm = getattr(n, "name", "") or ""
                    if nm == target_symbol or nm.split(".")[-1] == leaf:
                        return n
            if nodes:
                return nodes[0]

        # (3) global name lookup.
        if target_symbol and hasattr(oracle, "find_nodes_by_name"):
            by_name = oracle.find_nodes_by_name(target_symbol) or []
            if by_name:
                return by_name[0]
    except Exception:  # noqa: BLE001 -- resolution is best-effort
        logger.debug("[CrossRepoWiring] target node resolution failed", exc_info=True)
    return None


async def build_blast_context_block(
    ctx: Any,
    *,
    oracle: Any = None,
    registry: Any = None,
) -> str:
    """Build the G1 cross-repo blast-radius prompt block for a cross-repo op.

    Resolves the target ``NodeID``, traces the cross-repo blast radius, emits the
    operator-visible ASCII tree (logger.info), and returns the rendered prompt
    block to prepend into the generation context.

    Returns ``""`` (fail-soft) when the master switch is OFF, the op is not
    cross-repo, the oracle/registry are unavailable, or anything errors. An empty
    block does NOT relax safety -- the promoter + immutable-Orange floor already
    elevate the risk tier (fail-CLOSED at the floor, not here).
    """
    from backend.core.ouroboros.governance.cross_repo_master_flag import (
        cross_repo_mutation_enabled,
    )

    if not cross_repo_mutation_enabled():
        return ""

    if not getattr(ctx, "cross_repo", False):
        return ""

    try:
        # The oracle is injected by the orchestrator (``self._stack.oracle``);
        # there is no module-level singleton to fall back to. The registry can
        # be resolved from env when not injected.
        if registry is None:
            try:
                from backend.core.ouroboros.governance.multi_repo.registry import (
                    RepoRegistry,
                )

                registry = RepoRegistry.from_env()
            except Exception:  # noqa: BLE001
                registry = None

        if oracle is None or registry is None:
            logger.debug(
                "[CrossRepoWiring] oracle/registry unavailable -- empty blast block"
            )
            return ""

        from backend.core.ouroboros.governance.multi_repo.cross_repo_blast_context import (
            trace_cross_repo_blast,
        )

        node = _resolve_target_node(ctx, oracle)
        if node is None:
            logger.debug("[CrossRepoWiring] no target node resolved -- empty block")
            return ""

        blast = await trace_cross_repo_blast(
            target_node_id=node,
            oracle=oracle,
            registry=registry,
        )

        # Emit the operator-visible ASCII tree BEFORE approval (fail-soft).
        try:
            from backend.core.ouroboros.governance.multi_repo.blast_radius_visualizer import (
                render_blast_tree,
            )

            tree = render_blast_tree(blast)
            if tree:
                logger.info("\n%s", tree)
        except Exception:  # noqa: BLE001 -- visualizer is operator convenience
            logger.debug("[CrossRepoWiring] visualizer failed", exc_info=True)

        return blast.rendered_prompt_block or ""
    except Exception:  # noqa: BLE001 -- fail-soft -> empty (floor still elevates)
        logger.warning(
            "[CrossRepoWiring] build_blast_context_block failed -- empty block "
            "(risk floor still elevated by promoter)",
            exc_info=True,
        )
        return ""


# --------------------------------------------------------------------------- #
# G2 -- air-gapped Trinity sandbox gate (APPLY)
# --------------------------------------------------------------------------- #
async def run_apply_sandbox_gate(
    ctx: Any,
    *,
    candidate_root: str,
    op_id: str,
    runner: Any = None,
) -> Any:
    """Run the G2 air-gapped Trinity integration sandbox gate at APPLY time.

    Returns a ``SandboxVerdict``. The caller routes ``verdict.fracture`` into the
    existing saga abort + compensating rollback. When the master switch is OFF
    this returns a no-op PASS sentinel (the cross-repo path is itself gated off).

    Fail-CLOSED: the gate itself never raises and returns a FRACTURE verdict on
    any uncertainty (Docker absent, air-gap unverifiable, handshake failure,
    timeout). We surface that verdict unchanged.
    """
    from backend.core.ouroboros.governance.cross_repo_master_flag import (
        cross_repo_mutation_enabled,
    )

    if not cross_repo_mutation_enabled():
        return _disabled_sandbox_verdict()

    from backend.core.ouroboros.governance.saga.trinity_integration_gate import (
        run_trinity_sandbox_gate,
    )

    return await run_trinity_sandbox_gate(
        candidate_root=candidate_root,
        op_id=op_id,
        runner=runner,
    )


__all__ = ["build_blast_context_block", "run_apply_sandbox_gate"]
