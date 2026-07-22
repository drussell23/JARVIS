"""Production ``AgentTurnFn`` adapter — the Swarm drone's real production brain.

The interceptor / swarm stack (#70020-#70025) is contract-driven around a
lightweight injection seam::

    AgentTurnFn = Callable[[ChunkTarget, feedback], Awaitable[str]]

Every test injects a mock; there was NO production binding. This module is that
binding — it bridges the *stateless* per-node interceptor contract to the
*stateful* Fleet Commander tool loop WITHOUT duplicating a single dispatcher,
parser, or ReAct engine:

  * **Real brain (dependency-injected).** ``_generate_raw`` in ``providers.py``
    is just a thin closure over ``self._client.generate(...)``. This adapter
    takes that SAME provider client and drives it per node. In production the
    live-seam wire (Step 2) injects the real DoubleWord client; tests inject a
    fake. That is dependency injection, not an inert island — the adapter's
    whole reason to exist is to be handed the real client.

  * **Strict Line-Range Jailing (``LineRangeJail``).** ``ScopedToolBackend``
    already jails a sub-agent to a *tool allowlist* + a *mutation budget*, and
    surfaces a denial as a ``POLICY_DENIED`` ``ToolResult`` the ReAct loop feeds
    back as a self-correctable observation. It does NOT jail to a *line range*.
    ``LineRangeJail`` is the one net-new mechanism: an OUTER ``ToolBackend`` that
    wraps the real ``ScopedToolBackend`` and, on any mutation tool-call whose
    target file or line span escapes the assigned ``ChunkTarget`` bounds, denies
    it with ``PermissionError(Out of Bounds: Stick to assigned AST node)`` — the
    agent then self-corrects inside its own loop. Reads pass straight through.

  * **Rolling context compaction.** Deep ReAct loops (repetitive test-failure
    stack traces) starve DW's window. The adapter reuses the existing
    ``ContextCompactor`` (Gap #8) — when the running ReAct transcript crosses the
    threshold, older turns are compacted into a deterministic summary BEFORE the
    next DW prompt. No new summariser.

  * **Contract bridge.** The tool loop returns a file-level answer; the swarm
    wants ONE verified function node. The adapter extracts the target node from
    the loop's output and verifies it against the local AST (reusing
    ``agentic_super_agent._verify_node_against_ast``), returning the node source
    or ``""`` (→ the swarm records the node failed → the interceptor's RAG
    fallback). Never raises on the hot path.

Composes ``ScopedToolBackend`` / ``ScopedToolGate`` / ``ToolScope`` /
``ContextCompactor`` / ``extract_target_chunk`` / ``_verify_node_against_ast``.
Env-driven; pure asyncio.
"""

from __future__ import annotations

import ast
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.agentic_super_agent import (
    _verify_node_against_ast,
    agent_max_turns,
)
from backend.core.ouroboros.governance.chunk_swarm import ChunkTarget
from backend.core.ouroboros.governance.chunked_generation import (
    extract_target_chunk,
)
from backend.core.ouroboros.governance.scoped_tool_access import (
    _MUTATION_TOOLS,
    ScopedToolGate,
    ToolScope,
)
from backend.core.ouroboros.governance.scoped_tool_backend import ScopedToolBackend

logger = logging.getLogger("Ouroboros.AgentTurnAdapter")

_TURN_BUDGET_ENV = "JARVIS_AGENT_TURN_BUDGET_S"
_DEFAULT_TURN_BUDGET_S = 60.0
_MAX_MUTATIONS_ENV = "JARVIS_AGENT_TURN_MAX_MUTATIONS"
_DEFAULT_MAX_MUTATIONS = 8

# The out-of-bounds observation injected verbatim into the ReAct loop. The exact
# phrase the agent reads and self-corrects against.
_OUT_OF_BOUNDS = "PermissionError(Out of Bounds: Stick to assigned AST node)"


def _turn_budget_s() -> float:
    try:
        return max(1.0, float(os.environ.get(_TURN_BUDGET_ENV, str(_DEFAULT_TURN_BUDGET_S))))
    except (TypeError, ValueError):
        return _DEFAULT_TURN_BUDGET_S


def _max_mutations() -> int:
    try:
        return max(1, int(os.environ.get(_MAX_MUTATIONS_ENV, str(_DEFAULT_MAX_MUTATIONS))))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_MUTATIONS


# ---------------------------------------------------------------------------
# Strict Line-Range Jailing
# ---------------------------------------------------------------------------

_PATH_ARG_KEYS = ("file_path", "path", "target", "filename", "file")
_START_ARG_KEYS = ("start_line", "line_start", "from_line", "begin_line")
_END_ARG_KEYS = ("end_line", "line_end", "to_line", "finish_line")
_SINGLE_LINE_KEYS = ("line", "lineno", "line_number", "at_line")


def _same_file(a: str, b: str) -> bool:
    """Path equality tolerant of relative/absolute + trailing separators."""
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        pa, pb = Path(a), Path(b)
    except (TypeError, ValueError):
        return False
    if pa.name != pb.name:
        return False
    # Same basename AND one path is a suffix of the other (repo-relative vs abs).
    sa, sb = str(pa), str(pb)
    return sa.endswith(sb) or sb.endswith(sa)


def _first_int(args: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[int]:
    for k in keys:
        if k in args and args[k] is not None:
            try:
                return int(args[k])
            except (TypeError, ValueError):
                continue
    return None


class LineRangeJail:
    """Outer ``ToolBackend`` that jails MUTATION tool-calls to one AST node's
    file + line range, composing an inner ``ScopedToolBackend``.

    Implements the ``ToolBackend`` protocol (``execute_async``) so it is a
    drop-in wherever the scoped backend was used. Enforcement is
    first-rejection-wins and ONLY applies to mutation tools — read tools
    (read_file / search_code / get_callers / ...) always pass to the inner
    backend so exploration (Iron Gate) is never impeded.

    A denied call returns a ``ToolResult(status=PERMISSION_DENIED)`` whose
    ``error`` carries the ``PermissionError(Out of Bounds: ...)`` observation the
    ReAct loop surfaces to the model for self-correction — the SAME
    denial-as-observation contract ``ScopedToolBackend`` already relies on.
    """

    def __init__(
        self,
        inner: Any,
        *,
        file_path: str,
        start_line: int,
        end_line: int,
    ) -> None:
        self._inner = inner
        self._file_path = file_path or ""
        self._start_line = int(start_line or 0)
        self._end_line = int(end_line or 0)
        # Every out-of-bounds attempt, for the audit trail: (tool, path, span).
        self._denials: List[Tuple[str, str, Optional[Tuple[int, int]]]] = []

    @property
    def denials(self) -> Tuple[Tuple[str, str, Optional[Tuple[int, int]]], ...]:
        return tuple(self._denials)

    def _call_path(self, call: Any) -> Optional[str]:
        args = getattr(call, "arguments", {}) or {}
        for k in _PATH_ARG_KEYS:
            v = args.get(k)
            if isinstance(v, str) and v:
                return v
        return None

    def _call_line_span(self, call: Any) -> Optional[Tuple[int, int]]:
        """The absolute line span a mutation touches, or ``None`` if the call
        carries no resolvable line info (then only the path gate applies)."""
        args = getattr(call, "arguments", {}) or {}
        lo = _first_int(args, _START_ARG_KEYS)
        hi = _first_int(args, _END_ARG_KEYS)
        if lo is not None or hi is not None:
            a: int = lo if lo is not None else hi  # type: ignore[assignment]
            b: int = hi if hi is not None else lo  # type: ignore[assignment]
            return (min(a, b), max(a, b))
        single = _first_int(args, _SINGLE_LINE_KEYS)
        if single is not None:
            return (single, single)
        return None

    def _within(self, span: Tuple[int, int]) -> bool:
        if self._start_line <= 0 or self._end_line <= 0:
            return True  # no bounds declared → path gate only
        lo, hi = span
        return lo >= self._start_line and hi <= self._end_line

    def _deny(self, call: Any, detail: str) -> Any:
        from backend.core.ouroboros.governance.tool_executor import (
            ToolExecStatus,
            ToolResult,
        )
        span_key = self._call_line_span(call)
        self._denials.append((getattr(call, "name", "?"), self._call_path(call) or "", span_key))
        logger.info(
            "[LineRangeJail] DENIED tool=%s %s (assigned=%s:%d-%d)",
            getattr(call, "name", "?"), detail,
            self._file_path, self._start_line, self._end_line,
        )
        return ToolResult(
            tool_call=call,
            output="",
            error=(
                f"{_OUT_OF_BOUNDS} — {detail}. Your write scope is EXACTLY "
                f"{self._file_path} lines {self._start_line}-{self._end_line}. "
                "Re-issue the edit targeting ONLY that range."
            ),
            status=ToolExecStatus.PERMISSION_DENIED,
        )

    async def execute_async(self, call: Any, policy_ctx: Any, deadline: float) -> Any:
        name = getattr(call, "name", "")
        if name in _MUTATION_TOOLS:
            path = self._call_path(call)
            if path is not None and not _same_file(path, self._file_path):
                return self._deny(call, f"wrong file {path!r}")
            span = self._call_line_span(call)
            if span is not None and not self._within(span):
                return self._deny(call, f"lines {span[0]}-{span[1]} escape the node")
        return await self._inner.execute_async(call, policy_ctx, deadline)

    def __getattr__(self, name: str) -> Any:
        # Transparent passthrough for optional backend methods (release_op etc.)
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# Production AgentTurnFn adapter
# ---------------------------------------------------------------------------

# coordinator_run(prompt, generate_fn, parse_fn, backend, op_id, deadline,
#                 repo, repo_root, target) -> Awaitable[str]
# Production injects a thin lambda that constructs the real ToolLoopCoordinator
# with `backend` (the jail) and returns coordinator.run(...)'s raw answer. When
# omitted, the adapter uses `_default_react_runner` — an adapter-local micro
# orchestrator that REUSES `parse_fn` (no ReAct parsing duplicated) so the
# adapter is functional standalone (lightweight nodes / tests).
CoordinatorRun = Callable[..., Awaitable[str]]


class ProductionAgentTurnFn:
    """A callable satisfying ``AgentTurnFn = (ChunkTarget, feedback) -> str``.

    Construct once per op with the live provider client + real tool backend,
    then hand ``self`` to ``swarm_agentic_repair`` / ``intercept_full_content``
    as ``agent_fn``. Each call runs ONE goal-bounded ReAct repair for the node,
    caged by a per-node ``LineRangeJail``, with rolling context compaction.
    """

    def __init__(
        self,
        *,
        client: Any,
        tool_backend: Any,
        repo_root: Any = ".",
        op_id: str = "",
        model_name: str = "",
        system_prompt: str = "",
        repo: str = "jarvis",
        parse_fn: Optional[Callable[[str], Optional[List[Any]]]] = None,
        coordinator_run: Optional[CoordinatorRun] = None,
        allowed_tools: Optional[Tuple[str, ...]] = None,
        max_mutations: Optional[int] = None,
        max_turns: Optional[int] = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        task_profile: str = "code_repair",
        compactor: Optional[Any] = None,
        compaction_config: Optional[Any] = None,
    ) -> None:
        self._client = client
        self._tool_backend = tool_backend
        self._repo_root = repo_root
        self._op_id = op_id
        self._model_name = model_name
        self._system_prompt = system_prompt
        self._repo = repo
        self._parse_fn = parse_fn
        self._coordinator_run = coordinator_run or self._default_react_runner
        # allowed_tools: read tools always; edits are jailed by LineRangeJail.
        self._allowed_tools = allowed_tools or (
            "read_file", "search_code", "get_callers", "list_symbols",
            "glob_files", "list_dir", "edit_file",
        )
        self._max_mutations = max_mutations if max_mutations is not None else _max_mutations()
        self._max_turns = max_turns if max_turns is not None else agent_max_turns()
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._task_profile = task_profile
        self._compactor = compactor
        self._compaction_config = compaction_config

    # -- jail construction ------------------------------------------------

    def _build_jail(self, target: ChunkTarget) -> LineRangeJail:
        scope = ToolScope(allowed_tools=frozenset(self._allowed_tools), read_only=False)
        gate = ScopedToolGate(scope)
        inner = ScopedToolBackend(
            self._tool_backend, gate, max_mutations=self._max_mutations,
        )
        chunk = target.chunk
        return LineRangeJail(
            inner,
            file_path=(
                getattr(chunk, "file_path", "")
                or getattr(chunk, "path", "")
                or ""
            ),
            start_line=int(getattr(chunk, "start_line", 0) or 0),
            end_line=int(getattr(chunk, "end_line", 0) or 0),
        )

    # -- node prompt ------------------------------------------------------

    def _node_prompt(self, target: ChunkTarget, feedback: str) -> str:
        chunk = target.chunk
        src = getattr(chunk, "source_code", "") or ""
        start = int(getattr(chunk, "start_line", 0) or 0)
        end = int(getattr(chunk, "end_line", 0) or 0)
        fp = getattr(chunk, "file_path", "") or getattr(chunk, "path", "") or ""
        base = (
            f"You are repairing exactly ONE function: `{target.symbol}` in "
            f"{fp} (lines {start}-{end}). Do NOT touch any other function or "
            f"line. Your entire write scope is those lines.\n\n"
            f"Task: {target.instruction or ('repair ' + target.symbol)}\n\n"
            f"Current source of the node:\n```python\n{src}\n```\n\n"
            f"Return ONLY the complete, corrected `{target.symbol}` function "
            f"definition — correctly indented, no surrounding prose."
        )
        if feedback:
            base += f"\n\nREFINE: {feedback}"
        return base

    # -- node extraction --------------------------------------------------

    def _extract_node(self, raw: str, target: ChunkTarget) -> str:
        """Pull the target function node out of the loop's final answer.

        Accepts three shapes, most→least direct: bare function source, a fenced
        ```python block, or a candidate carrying ``full_content`` (slice the
        node via the existing ``extract_target_chunk``)."""
        if not raw or not raw.strip():
            return ""
        want = target.symbol.split(".")[-1]

        def _has_target(text: str) -> bool:
            try:
                tree = ast.parse(text)
            except SyntaxError:
                return False
            return any(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == want
                for n in tree.body
            )

        # 1) bare function source
        if _has_target(raw):
            return raw.strip()
        # 2) fenced code block(s)
        for block in _iter_code_fences(raw):
            if _has_target(block):
                return block.strip()
        # 3) full_content candidate → slice the node out
        fc = _extract_full_content(raw)
        if fc:
            chunk = extract_target_chunk(fc, target.symbol.replace(".", "/") + ".py", target.symbol)
            if chunk is None:
                fp = (
                    getattr(target.chunk, "file_path", "")
                    or getattr(target.chunk, "path", "")
                    or "node.py"
                )
                chunk = extract_target_chunk(fc, fp, target.symbol)
            if chunk is not None:
                node = getattr(chunk, "source_code", "") or ""
                if _has_target(node):
                    return node.strip()
        return ""

    # -- the AgentTurnFn contract ----------------------------------------

    async def __call__(self, target: ChunkTarget, feedback: str = "") -> str:
        """One caged, self-correcting ReAct repair of the node. Never raises —
        an empty return routes the node to the swarm-failed / RAG-fallback path."""
        try:
            jail = self._build_jail(target)
            prompt = self._node_prompt(target, feedback)
            deadline = time.monotonic() + _turn_budget_s()
            raw = await self._coordinator_run(
                prompt=prompt,
                generate_fn=self._make_generate_fn(),
                parse_fn=self._parse_fn,
                backend=jail,
                op_id=self._op_id,
                deadline=deadline,
                repo=self._repo,
                repo_root=self._repo_root,
                target=target,
            )
            node = self._extract_node(raw or "", target)
            if not node:
                return ""
            ok, _err = _verify_node_against_ast(node, target)
            return node if ok else ""
        except Exception:  # noqa: BLE001 — the drone's fault is isolated, not fatal
            logger.exception(
                "[AgentTurnAdapter] %s repair errored — isolating node",
                target.symbol,
            )
            return ""

    # -- real brain + rolling compaction ---------------------------------

    def _make_generate_fn(self) -> Callable[[str], Awaitable[str]]:
        """Wrap the injected provider client's ``generate`` as the tool loop's
        ``generate_fn`` — the real DoubleWord brain call."""
        async def _generate(p: str) -> str:
            resp = await self._client.generate(
                prompt=p,
                system_prompt=self._system_prompt,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                model_name=self._model_name,
                task_profile=self._task_profile,
            )
            content = getattr(resp, "content", None)
            if content is None and isinstance(resp, str):
                content = resp
            return content or ""
        return _generate

    async def _maybe_compact(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Rolling ReAct-transcript compaction via the existing ContextCompactor.
        Deep loops (repeated stack traces) are folded into a summary before the
        next DW prompt so the node agent never starves the window."""
        if self._compactor is None:
            return entries
        try:
            if not self._compactor.should_compact(entries, self._compaction_config):
                return entries
            result = await self._compactor.compact(
                entries, self._compaction_config, op_id=self._op_id,
            )
        except Exception:  # noqa: BLE001 — compaction is advisory, never fatal
            return entries
        preserve = getattr(self._compaction_config, "preserve_count", None) or 6
        kept = entries[-int(preserve):] if preserve else entries
        summary_entry = {
            "role": "system",
            "type": "compaction_summary",
            "content": getattr(result, "summary", "") or "",
        }
        logger.info(
            "[AgentTurnAdapter] compacted ReAct transcript %d→%d (op=%s)",
            getattr(result, "entries_before", len(entries)),
            1 + len(kept), self._op_id,
        )
        return [summary_entry, *kept]

    # -- standalone ReAct micro-runner (used when no ToolLoopCoordinator) --

    async def _default_react_runner(
        self,
        *,
        prompt: str,
        generate_fn: Callable[[str], Awaitable[str]],
        parse_fn: Optional[Callable[[str], Optional[List[Any]]]],
        backend: LineRangeJail,
        op_id: str,
        deadline: float,
        repo: str,
        repo_root: Any,
        target: ChunkTarget,
        **_: Any,
    ) -> str:
        """Adapter-local ReAct orchestration for when the caller does not inject
        a full ``ToolLoopCoordinator``. It REUSES ``parse_fn`` for tool parsing
        (no parsing logic duplicated) and drives the SAME ``LineRangeJail`` +
        ``ScopedToolBackend`` cage, so the jail's self-correction contract is
        exercised identically. Production wiring may instead inject the real
        coordinator for its richer budget/parallelism machinery."""
        from backend.core.ouroboros.governance.tool_executor import (
            PolicyContext,
        )

        entries: List[Dict[str, Any]] = []
        last_raw = ""
        for turn in range(1, self._max_turns + 1):
            if time.monotonic() >= deadline:
                break
            entries = await self._maybe_compact(entries)
            transcript = _render_entries(entries)
            full_prompt = prompt if not transcript else f"{prompt}\n\n{transcript}"
            last_raw = await generate_fn(full_prompt)
            entries.append({"role": "assistant", "content": last_raw})

            calls = parse_fn(last_raw) if parse_fn else None
            if not calls:
                # Final answer — no more tool calls requested.
                return last_raw

            for idx, call in enumerate(calls):
                pctx = PolicyContext(
                    repo=repo,
                    repo_root=(repo_root if isinstance(repo_root, Path) else Path(str(repo_root or "."))),
                    op_id=op_id,
                    call_id=f"{op_id}:r{turn}:{getattr(call, 'name', 'tool')}:{idx}",
                    round_index=turn,
                    is_read_only=False,
                )
                result = await backend.execute_async(call, pctx, deadline)
                obs = result.error if result.error else result.output
                entries.append({
                    "role": "tool",
                    "tool": getattr(call, "name", "?"),
                    "status": getattr(result.status, "value", str(result.status)),
                    "content": obs or "",
                })
        return last_raw


# ---------------------------------------------------------------------------
# Free helpers
# ---------------------------------------------------------------------------


def _iter_code_fences(text: str) -> List[str]:
    """Yield the bodies of ```...``` fenced blocks (language tag stripped)."""
    blocks: List[str] = []
    parts = text.split("```")
    # Odd indices are inside fences.
    for i in range(1, len(parts), 2):
        body = parts[i]
        nl = body.find("\n")
        if nl != -1 and body[:nl].strip().isalpha():
            body = body[nl + 1:]  # drop the language tag line
        blocks.append(body)
    return blocks


def _extract_full_content(raw: str) -> str:
    """Best-effort pull of a ``full_content`` string from a candidate JSON blob.
    Returns ``""`` when the answer is not a candidate. Never raises."""
    import json

    txt = raw.strip()
    start = txt.find("{")
    end = txt.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ""
    try:
        data = json.loads(txt[start:end + 1])
    except (ValueError, TypeError):
        return ""

    def _walk(obj: Any) -> str:
        if isinstance(obj, dict):
            fc = obj.get("full_content")
            if isinstance(fc, str) and fc.strip():
                return fc
            for v in obj.values():
                found = _walk(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = _walk(v)
                if found:
                    return found
        return ""

    return _walk(data)


def _render_entries(entries: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for e in entries:
        role = e.get("role", "?")
        if e.get("type") == "compaction_summary":
            lines.append(f"[earlier turns, compacted]\n{e.get('content', '')}")
        elif role == "tool":
            lines.append(
                f"[tool {e.get('tool', '?')} → {e.get('status', '?')}]\n{e.get('content', '')}"
            )
        else:
            lines.append(f"[{role}]\n{e.get('content', '')}")
    return "\n\n".join(lines)


__all__ = [
    "LineRangeJail",
    "ProductionAgentTurnFn",
]
