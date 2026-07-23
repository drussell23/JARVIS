"""Agentic Super-Agent — goal-bounded autonomous ReAct worker per AST node.

RECON (project_sovereign_swarm.md, MERGED): ~70% of this chassis already
exists. This module is the ONE genuine net-new composition — it makes each
ephemeral swarm agent a SELF-CORRECTING multi-turn ReAct worker instead of a
single DW round, composing the existing pieces rather than rebuilding them:

  * ReAct loop body → ``ToolLoopCoordinator.run`` (tool_executor.py) — the
    Plan→act→observe tool loop (read_file / edit_file / run_tests). Injected as
    the per-agent ``agent_fn`` so the production worker is the real tool loop;
    tests inject a mock.
  * Write isolation → ``ScopedToolBackend`` (scoped_tool_backend.py) +
    ``WorkUnitSpec.owned_paths`` — each agent's WRITES are locked to its
    assigned AST-node file (max_mutations-bounded). The Fan-In relies on this +
    the swarm's descending-line atomic stitch so 5 concurrent agents can NEVER
    corrupt each other.
  * Scatter/gather + atomic stitch → ``chunk_swarm.swarm_repair`` (#70023).
  * Resilient network legs → the #70019 transient-absorb / watchdog decorators.

This module adds the AGENTIC BOUNDING the recon flagged as missing at the
per-target grain: a goal-bounded ``Plan → Modify → Verify (local AST) → Refine``
mini-loop with a strict ``max_turns`` guardrail that emits a graceful
``agent_unconverged`` signal instead of hanging or burning tokens.

Pure asyncio (no threads/processes); env-driven; never raises on the hot path.
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

from backend.core.ouroboros.governance.chunk_swarm import (
    ChunkTarget,
    SwarmResult,
    swarm_repair,
)

logger = logging.getLogger("Ouroboros.AgenticSuperAgent")

_MAX_TURNS_ENV = "JARVIS_AGENT_MAX_TURNS"
_DEFAULT_MAX_TURNS = 5

STATUS_CONVERGED = "converged"
STATUS_UNCONVERGED = "agent_unconverged"


def agent_max_turns() -> int:
    """Strict per-agent reasoning-cycle budget (env ``JARVIS_AGENT_MAX_TURNS``,
    default 5). Clamped [1, 12] — a runaway agent can never exceed this."""
    try:
        return max(1, min(12, int(os.environ.get(_MAX_TURNS_ENV, str(_DEFAULT_MAX_TURNS)))))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TURNS


@dataclass
class AgentOutcome:
    """The result of one Agentic Super-Agent's bounded ReAct run."""
    symbol: str
    status: str                      # STATUS_CONVERGED | STATUS_UNCONVERGED
    node: Optional[str] = None       # the verified replacement AST node
    turns: int = 0
    last_error: str = ""

    @property
    def converged(self) -> bool:
        return self.status == STATUS_CONVERGED


# agent_fn(target, feedback) -> Awaitable[str]: ONE ReAct turn's proposed node.
# In production this is a ScopedToolBackend-caged ToolLoopCoordinator.run that
# itself may take multiple tool rounds; here it is the injection seam.
AgentTurnFn = Callable[[ChunkTarget, str], Awaitable[str]]


def _verify_node_against_ast(node: str, target: ChunkTarget) -> tuple:
    """VERIFY step — the agent's output must be a single, syntactically-valid
    function/method definition with the target's name. Returns ``(ok, error)``.
    This is the local-AST gate the agent refines against. Never raises."""
    if not node or not node.strip():
        return False, "empty output — return the complete function definition"
    try:
        tree = ast.parse(node)
    except SyntaxError as exc:
        return False, f"SyntaxError: {exc.msg} (line {exc.lineno})"
    funcs = [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not funcs:
        return False, "no function definition found — return ONLY the function"
    want = target.symbol.split(".")[-1]
    if funcs[0].name != want:
        return False, (
            f"wrong function name '{funcs[0].name}' — must be '{want}' with the "
            f"same signature"
        )
    return True, ""


async def run_agentic_repair(
    target: ChunkTarget,
    agent_fn: AgentTurnFn,
    *,
    max_turns: Optional[int] = None,
    seam_validator: Optional[Callable[[str], Optional[str]]] = None,
) -> AgentOutcome:
    """One Agentic Super-Agent: a goal-bounded autonomous ReAct mini-loop —
    Plan → Modify → Verify (local AST + in-memory seam pre-compile) → Refine —
    over its OWN AST node.

    Each turn calls ``agent_fn`` with the accumulated feedback. The output is
    (1) trial-grafted + whole-file precompiled by ``seam_validator`` if provided
    — a ``StitchCollisionError`` observation is fed back on a seam fracture so the
    node can never corrupt the file — then (2) verified against the local AST for
    symbol identity. The loop is hard-bounded by ``max_turns`` and emits
    ``agent_unconverged`` rather than hanging. Never raises."""
    turns = max_turns if max_turns is not None else agent_max_turns()
    feedback = ""
    last_error = ""
    for turn in range(1, turns + 1):
        try:
            node = await agent_fn(target, feedback)
        except Exception as exc:  # noqa: BLE001 — an agent fault is isolated, not fatal
            last_error = f"agent_fault:{type(exc).__name__}"
            feedback = (
                f"REFINE (turn {turn}): the previous attempt errored "
                f"({last_error}). Return ONLY the corrected function."
            )
            continue
        # (1) In-memory Syntax Pre-Compiler at the seam: trial-graft + whole-file
        # validate BEFORE anything reaches disk. A fracture routes a
        # StitchCollisionError (or RebaseCollisionError under the Rolling VFS)
        # observation back for self-correction. The validator may be async (the
        # Rolling VFS acquires the Sequential Compilation Lock).
        if seam_validator is not None:
            seam_err = seam_validator(node)
            if inspect.isawaitable(seam_err):
                seam_err = await seam_err
            if seam_err:
                # Preserve a pre-labeled error class (Rebase/Stitch); else label it.
                last_error = (
                    seam_err if seam_err.startswith(
                        ("RebaseCollisionError", "StitchCollisionError")
                    ) else f"StitchCollisionError: {seam_err}"
                )
                feedback = (
                    f"REFINE (turn {turn}): {last_error}. Your node broke the file "
                    f"when grafted at the seam — return ONLY the corrected complete "
                    f"definition so the WHOLE file parses after insertion."
                )
                logger.warning(
                    "[AgenticSuperAgent] %s turn %d SEAM FRACTURE: %s — refining "
                    "(disk write blocked)", target.symbol, turn, last_error,
                )
                continue
        # Local AST symbol check — Python only. A polyglot node (json/yaml/tsx)
        # is not a Python function; its whole-file structural validate at the
        # seam is the gate (mirrors the ProductionAgentTurnFn polymorphic verify).
        _lang = getattr(getattr(target, "chunk", None), "language", "python")
        if _lang and _lang != "python":
            logger.info(
                "[AgenticSuperAgent] %s CONVERGED in %d turn(s) (%s)",
                target.symbol, turn, _lang,
            )
            return AgentOutcome(
                symbol=target.symbol, status=STATUS_CONVERGED, node=node, turns=turn,
            )
        ok, err = _verify_node_against_ast(node, target)
        if ok:
            logger.info(
                "[AgenticSuperAgent] %s CONVERGED in %d turn(s)",
                target.symbol, turn,
            )
            return AgentOutcome(
                symbol=target.symbol, status=STATUS_CONVERGED, node=node,
                turns=turn,
            )
        # Self-correction: feed the exact verify error back for the next turn.
        last_error = err
        feedback = (
            f"REFINE (turn {turn}): your previous node failed local AST "
            f"verification — {err}. Return ONLY the corrected complete function, "
            f"correctly indented."
        )
        logger.warning(
            "[AgenticSuperAgent] %s turn %d verify failed: %s — refining",
            target.symbol, turn, err,
        )
    # Budget exhausted → graceful unconverged signal (no hang, no token waste).
    logger.warning(
        "[AgenticSuperAgent] %s UNCONVERGED after %d turns — emitting "
        "agent_unconverged", target.symbol, turns,
    )
    return AgentOutcome(
        symbol=target.symbol, status=STATUS_UNCONVERGED, node=None,
        turns=turns, last_error=last_error,
    )


async def swarm_agentic_repair(
    full_source: str,
    file_path: str,
    targets: List[ChunkTarget],
    agent_fn: AgentTurnFn,
    *,
    max_concurrency: Optional[int] = None,
    max_turns: Optional[int] = None,
) -> SwarmResult:
    """Fan out one AGENTIC Super-Agent per AST node — each a goal-bounded
    self-correcting ReAct loop — concurrently, then stitch atomically.

    Composes ``run_agentic_repair`` (the per-agent ReAct bounding) into
    ``chunk_swarm.swarm_repair`` (#70023: semaphore-bounded scatter → gather →
    descending-line atomic fan-in). An agent that returns ``agent_unconverged``
    yields an empty node, so the swarm records it ``failed`` and leaves the file
    untouched at that node — the atomic invariant holds. Never raises."""
    from backend.core.ouroboros.governance.stitch_precompiler import RollingVFS

    # ONE Rolling VFS shared across all agents: the Sequential Compilation Lock
    # serializes the heavy in-memory parse (memory bounding under extreme
    # map-reduce) while each agent rebases its graft onto the prior agents'
    # committed state (cross-agent semantic collision → RebaseCollisionError).
    vfs = RollingVFS(full_source, file_path)

    async def _agentic_generate(target: ChunkTarget) -> str:
        outcome = await run_agentic_repair(
            target, agent_fn, max_turns=max_turns,
            seam_validator=vfs.seam_validator_for(target.symbol),
        )
        # Empty string → swarm marks this node failed (unconverged); a converged
        # node flows into the atomic descending-line stitch.
        return outcome.node or "" if outcome.converged else ""

    return await swarm_repair(
        full_source, file_path, targets, _agentic_generate,
        max_concurrency=max_concurrency,
    )


__all__ = [
    "STATUS_CONVERGED",
    "STATUS_UNCONVERGED",
    "AgentOutcome",
    "AgentTurnFn",
    "agent_max_turns",
    "run_agentic_repair",
    "swarm_agentic_repair",
]
