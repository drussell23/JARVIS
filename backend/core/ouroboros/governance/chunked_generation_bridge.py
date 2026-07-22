"""Generation-Prompt Bridge — plugs intelligent chunk-routing into GENERATE.

An INTERCEPTION LAYER between the GENERATE context-builder and the DW
response-parser — it never touches the 8.7K-line provider's core execution
flow. Two seams + one async telemetry hook:

  * **Context-builder seam** (:func:`frame_for_generation`): route the target
    file through ``select_extraction_strategy`` (#70021); on the AST / RAG
    paths, REPLACE the whole-file context with the pruned Radius of Relevance /
    top-k snippets AND prepend a strict **System Instruction Modifier** that
    frames the LLM's constrained Map-Reduce view, so it never hallucinates the
    pruned lines.

  * **Response-parser seam** (:func:`stitch_with_l2_recovery`): stitch the
    DW-returned AST node back into the full file; if the graft fails to parse,
    DON'T fail terminally — run an ``L2_CONVERGED``-style bounded retry loop,
    feeding the specific parse error back to the LLM for immediate
    self-correction (reuses the L2 iterate-with-error-feedback architecture).

  * **Async ML logging** (:class:`StrategyOutcomeLogger`): on the ``op.terminal``
    TrinityEventBus event, update the SQLite reinforcement weights (#70021)
    OFF the main ASGI thread (``run_in_executor``).

Composes PR #70020/#70021 primitives entirely (DRY). Env-driven; never raises
on the hot path.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional, Tuple

from backend.core.ouroboros.governance.chunked_generation import (
    stitch_replacement,
)
from backend.core.ouroboros.governance.intelligent_chunking import (
    ChunkPlan,
    record_strategy_outcome,
    select_extraction_strategy,
)

logger = logging.getLogger("Ouroboros.ChunkBridge")

_L2_ITERS_ENV = "JARVIS_STITCH_L2_MAX_ITERS"
_DEFAULT_L2_ITERS = 3


# ---------------------------------------------------------------------------
# Dynamic Context Framing — the System Instruction Modifier
# ---------------------------------------------------------------------------

MAP_REDUCE_FRAMING = (
    "SYSTEM CONSTRAINT — MAP-REDUCE MODE:\n"
    "You are operating in a constrained Map-Reduce environment. The code below "
    "is a LOCALIZED RADIUS OF RELEVANCE extracted from a much larger file — you "
    "are NOT viewing the whole file. Do NOT hallucinate, reconstruct, or "
    "reference the missing surrounding lines. Return ONLY the modified AST node "
    "(the complete function/method definition, same name and signature, "
    "correctly indented) and NOTHING else — no prose, no imports, no "
    "surrounding class body. The system stitches your node back into the full "
    "file locally.\n"
)

_RAG_FRAMING = (
    "SYSTEM CONSTRAINT — MAP-REDUCE MODE (RAG):\n"
    "The snippets below were semantically retrieved from a large file (the "
    "target symbol could not be AST-resolved). They are NOT the whole file. Do "
    "NOT hallucinate the missing code. Identify the correct edit and return "
    "ONLY the minimal modified definition.\n"
)


@dataclass
class FramedContext:
    """The context-builder interception result."""
    plan: ChunkPlan
    prompt: str
    chunked: bool
    instruction_injected: bool = False


def frame_for_generation(
    source: str,
    file_path: str,
    symbol: Optional[str],
    instruction: str = "",
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> FramedContext:
    """Context-builder seam: pick the strategy and build the DW prompt. For a
    small file → whole-file, unframed (legacy). For a massive file → the pruned
    Radius of Relevance (AST) or top-k snippets (RAG), PREPENDED with the strict
    System Instruction Modifier so the LLM never tries to rebuild the pruned
    lines. Never raises."""
    try:
        plan = select_extraction_strategy(
            source, file_path, symbol, instruction, conn=conn,
        )
    except Exception:  # noqa: BLE001 — degrade to whole-file only for SMALL files
        return FramedContext(
            plan=ChunkPlan(strategy="whole", context=source or ""),
            prompt=source or "", chunked=False,
        )
    if plan.strategy == "whole":
        return FramedContext(plan=plan, prompt=source or "", chunked=False)
    framing = MAP_REDUCE_FRAMING if plan.strategy == "ast" else _RAG_FRAMING
    task = f"\nINSTRUCTION: {instruction}\n" if instruction else ""
    prompt = f"{framing}{task}\n{plan.context}"
    return FramedContext(
        plan=plan, prompt=prompt, chunked=True, instruction_injected=True,
    )


# ---------------------------------------------------------------------------
# L2 Stitch Recovery — bounded self-correcting graft (response-parser seam)
# ---------------------------------------------------------------------------


def _l2_max_iters() -> int:
    try:
        return max(1, int(os.environ.get(_L2_ITERS_ENV, str(_DEFAULT_L2_ITERS))))
    except (TypeError, ValueError):
        return _DEFAULT_L2_ITERS


@dataclass
class StitchResult:
    ok: bool
    stitched: Optional[str]
    attempts: int
    last_error: str = ""


# generate_fn(prompt: str) -> Awaitable[str]  — returns the model's node source.
GenerateFn = Callable[[str], Awaitable[str]]


async def stitch_with_l2_recovery(
    plan: ChunkPlan,
    full_source: str,
    generate_fn: GenerateFn,
    *,
    base_prompt: str = "",
    max_iters: Optional[int] = None,
) -> StitchResult:
    """Response-parser seam: graft the model's node back into *full_source*; on
    a parse/graft failure, run the ``L2_CONVERGED``-style retry — feed the exact
    error back and re-generate — up to ``max_iters`` before giving up. Only the
    AST strategy stitches by line-range (RAG has no single node to graft).
    Never raises."""
    if plan.chunk is None:
        return StitchResult(ok=False, stitched=None, attempts=0,
                            last_error="no_ast_chunk_to_stitch")
    iters = max_iters if max_iters is not None else _l2_max_iters()
    error_feedback = ""
    last_error = ""
    for attempt in range(1, iters + 2):
        prompt = base_prompt + error_feedback
        try:
            node_src = await generate_fn(prompt)
        except Exception as exc:  # noqa: BLE001 — a generate failure is not a stitch failure
            return StitchResult(ok=False, stitched=None, attempts=attempt,
                                last_error=f"generate_failed:{type(exc).__name__}")
        stitched = stitch_replacement(full_source, plan.chunk, node_src or "")
        if stitched is None:
            last_error = "graft out-of-range / empty node"
            error_feedback = (
                "\n\nL2 CORRECTION: your previous output could not be grafted. "
                "Return ONLY the complete function definition, correctly indented."
            )
            continue
        # Validate the WHOLE stitched file parses (the graft is syntactically
        # sound in context).
        try:
            ast.parse(stitched)
            logger.info(
                "[ChunkBridge] L2 stitch converged in %d attempt(s) "
                "(strategy=%s)", attempt, plan.strategy,
            )
            return StitchResult(ok=True, stitched=stitched, attempts=attempt)
        except SyntaxError as exc:
            last_error = f"{exc.msg} (line {exc.lineno})"
            error_feedback = (
                f"\n\nL2 CORRECTION: your previous node produced a SyntaxError "
                f"when grafted into the file: {last_error}. Fix ONLY the "
                f"function and return it complete and correctly indented."
            )
            logger.warning(
                "[ChunkBridge] L2 stitch attempt %d failed to parse: %s — "
                "feeding error back for self-correction", attempt, last_error,
            )
    return StitchResult(ok=False, stitched=None, attempts=iters + 1,
                        last_error=last_error)


# ---------------------------------------------------------------------------
# Async ML Logging — reinforcement update on op.terminal (off the ASGI thread)
# ---------------------------------------------------------------------------

# op_id -> (strategy, file_lines, ext), set at frame time; read at terminal.
_pending_strategy: Dict[str, Tuple[str, int, str]] = {}
_pending_lock = threading.Lock()


def record_pending_strategy(
    op_id: str, *, strategy: str, file_lines: int, ext: str,
) -> None:
    """Remember which extraction strategy an op used, so the terminal observer
    can attribute the outcome. Called at frame time. Never raises."""
    if not op_id:
        return
    with _pending_lock:
        _pending_strategy[op_id] = (strategy, int(file_lines), ext or "")


def _pop_pending(op_id: str) -> Optional[Tuple[str, int, str]]:
    with _pending_lock:
        return _pending_strategy.pop(op_id, None)


class StrategyOutcomeLogger:
    """Subscribes to ``op.terminal.#``; on each terminal event it updates the
    SQLite reinforcement weights for the op's extraction strategy — OFF the main
    ASGI thread. Reuses the terminal-observer pattern (#70018). Never raises."""

    def __init__(self, conn: Optional[sqlite3.Connection]) -> None:
        self._conn = conn
        self._sub_id: Optional[str] = None

    async def on_terminal(self, event) -> None:
        """Bus handler — attribute + log the outcome off-loop."""
        try:
            payload = getattr(event, "payload", None) or {}
            op_id = payload.get("op_id")
            outcome = payload.get("state") or payload.get("outcome") or ""
            if not op_id:
                return
            pending = _pop_pending(op_id)
            if pending is None:
                return  # op didn't go through chunked generation
            strategy, file_lines, ext = pending
            if self._conn is None:
                return
            loop = asyncio.get_running_loop()
            # SQLite write is blocking → run it in the default executor so the
            # ASGI event loop is never stalled by disk I/O.
            await loop.run_in_executor(
                None,
                lambda: record_strategy_outcome(
                    self._conn, strategy=strategy, file_lines=file_lines,
                    ext=ext, outcome=str(outcome),
                ),
            )
            logger.info(
                "[ChunkBridge] reinforcement updated op=%s strategy=%s "
                "outcome=%s (off-loop)", op_id, strategy, outcome,
            )
        except Exception:  # noqa: BLE001 — telemetry never perturbs the bus
            logger.debug("[ChunkBridge] outcome logger error", exc_info=True)

    async def attach_to_bus(self, bus, *, pattern: str = "op.terminal.#"):
        if bus is None or self._sub_id is not None:
            return self._sub_id
        try:
            self._sub_id = await bus.subscribe(pattern, self.on_terminal)
            return self._sub_id
        except Exception:  # noqa: BLE001
            return None


__all__ = [
    "FramedContext",
    "GenerateFn",
    "MAP_REDUCE_FRAMING",
    "StitchResult",
    "StrategyOutcomeLogger",
    "frame_for_generation",
    "record_pending_strategy",
    "stitch_with_l2_recovery",
]
