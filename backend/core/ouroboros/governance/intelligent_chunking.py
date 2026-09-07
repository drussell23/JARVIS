"""Intelligent Chunk Routing — hierarchical pruning, RAG degradation, learning.

At ENTERPRISE scale (10k-100k-line files) whole-file ingestion is an instant
DoubleWord token-exhaustion — a catastrophic failure, not a graceful fallback.
This layer FORBIDS whole-file above a dynamic token ceiling and routes through
intelligence, composing PR #70020's extract/stitch primitives (DRY):

  * **Hierarchical AST Pruning (Radius of Relevance)** — for a massive file the
    router returns ONLY the module imports + the target's enclosing hierarchy
    (its class shell) + the target itself. Every sibling class, distant node,
    and irrelevant global is dropped, so DW receives a tiny, dense context.
  * **RAG Degradation (zero whole-file fallback)** — if the symbol can't be
    AST-resolved, the router NEVER falls back to the whole file; it degrades to
    a lightweight keyword-density chunker returning the top-k relevant snippets.
  * **Heuristic Reinforcement Loop (continuous learning)** — each strategy's
    outcome (PROMOTED / TIMEOUT / ABORTED) is logged by file-size bucket +
    extension to the SQLite telemetry layer (and the TrinityEventBus, DRY), and
    the router queries the accumulated success weights to predict the optimal
    strategy (AST vs RAG) over time.

Env-driven (no hardcoding); never raises on the hot path.
"""

from __future__ import annotations

import ast
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("Ouroboros.IntelligentChunking")

_CEILING_ENV = "JARVIS_DW_MAX_CONTEXT_TOKENS"   # legacy name: honoured ONLY as an explicit override
_RAG_K_ENV = "JARVIS_DW_RAG_TOP_K"
_DEFAULT_RAG_K = 6

_STRATEGY_AST = "ast"
_STRATEGY_RAG = "rag"
_STRATEGY_WHOLE = "whole"

_TELEMETRY_TABLE = "chunk_strategy_outcomes"


# ---------------------------------------------------------------------------
# Dynamic token ceiling — the "never brute-force above this" line
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """The package's one coarse token estimate (``context_budget``). Never raises."""
    from backend.core.ouroboros.governance.context_budget import estimate_tokens as _est
    return _est(text)


def dynamic_token_ceiling() -> int:
    """The context-token ceiling above which whole-file ingestion is FORBIDDEN.

    DERIVED, not declared: the served model's negotiated window minus its output
    reserve and the prompt's fixed overhead (``context_budget``), primed by the
    lane that negotiates it. The legacy ``JARVIS_DW_MAX_CONTEXT_TOKENS`` is
    honoured only when an operator sets it explicitly — it no longer carries a
    default of its own (the flat 8000 described a cloud lane, not the model
    answering). Never raises."""
    raw = (os.environ.get(_CEILING_ENV, "") or "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    from backend.core.ouroboros.governance.context_budget import ingest_ceiling_tokens
    return ingest_ceiling_tokens()


def exceeds_ceiling(source: str) -> bool:
    """True when *source* is too large to hand DW whole — the hard gate that
    forbids brute-force loading."""
    return estimate_tokens(source) > dynamic_token_ceiling()


# ---------------------------------------------------------------------------
# Hierarchical AST Pruning — the Radius of Relevance
# ---------------------------------------------------------------------------


def _leading_indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def radius_of_relevance(
    source: str, file_path: str, symbol: str,
) -> Optional[str]:
    """Prune *source* to the minimal context around *symbol*: the module
    imports + the target's ENCLOSING hierarchy (its class header + docstring) +
    the target function itself. Sibling classes, sibling methods, distant nodes,
    and unrelated globals are all DROPPED.

    Returns the reconstructed minimal source, or ``None`` if the symbol isn't
    found / the file won't parse. Never raises. This is what a 100k-line file
    collapses to before it ever reaches DoubleWord."""
    want = symbol.split(".")[-1].strip()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    src_lines = source.splitlines()

    def _segment(node: ast.AST) -> str:
        seg = ast.get_source_segment(source, node)
        if seg is not None:
            return seg
        lo = getattr(node, "lineno", None)
        hi = getattr(node, "end_lineno", None)
        if lo and hi:
            return "\n".join(src_lines[lo - 1: hi])
        return ""

    # Module-level imports — always kept (the target needs them).
    imports: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            seg = _segment(node)
            if seg:
                imports.append(seg)

    # Find the target function/method and its enclosing class (if any).
    target_node: Optional[ast.AST] = None
    enclosing_class: Optional[ast.ClassDef] = None

    def _is_target(n: ast.AST) -> bool:
        return (
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == want
        )

    for node in tree.body:
        if _is_target(node):
            target_node = node
            break
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if _is_target(child):
                    target_node = child
                    enclosing_class = node
                    break
            if target_node is not None:
                break

    if target_node is None:
        return None  # symbol not resolvable → caller degrades to RAG (never whole-file)

    parts: List[str] = []
    if imports:
        parts.append("\n".join(imports))

    if enclosing_class is not None:
        # Reconstruct a MINIMAL class shell: the ``class X(...):`` header + its
        # docstring, then ONLY the target method (drop every sibling member).
        header_lo = enclosing_class.lineno
        body_first = enclosing_class.body[0].lineno if enclosing_class.body else header_lo + 1
        class_header = "\n".join(src_lines[header_lo - 1: body_first - 1]).rstrip()
        shell = [class_header] if class_header else [f"class {enclosing_class.name}:"]
        doc = ast.get_docstring(enclosing_class, clean=False)
        if doc:
            indent = _leading_indent(src_lines[body_first - 1]) if body_first - 1 < len(src_lines) else "    "
            shell.append(f'{indent}"""{doc}"""')
        shell.append(_segment(target_node))
        parts.append("\n".join(shell))
    else:
        parts.append(_segment(target_node))

    return "\n\n\n".join(p for p in parts if p.strip())


# ---------------------------------------------------------------------------
# Fail-closed recursive shrinker — a radius that does not fit is narrowed, never sent
# ---------------------------------------------------------------------------

#: Shrink levels, widest first. Each drops the least task-relevant context.
SHRINK_LEVELS = ("radius", "radius_no_imports", "node", "node_compressed")


@dataclass(frozen=True)
class ShrunkRadius:
    """A Radius of Relevance narrowed until it fits a token budget."""
    context: str
    level: str
    tokens: int
    budget_tokens: int
    chunk: object = None


def shrink_radius_to_budget(
    source: str, file_path: str, symbol: str, budget_tokens: int,
) -> Optional[ShrunkRadius]:
    """Narrow the Radius of Relevance for *symbol* until it fits *budget_tokens*:
    full radius → radius without imports → the node alone → the node compressed
    (head + tail, the lane's own ``fit_prompt_to_window``). FAIL-CLOSED: when
    even the compressed node exceeds the budget, return ``None`` — the caller
    must decline rather than overflow the window. Never raises."""
    try:
        budget = max(1, int(budget_tokens))
        from backend.core.ouroboros.governance.chunked_generation import extract_target_chunk
        chunk = extract_target_chunk(source, file_path, symbol)
        node_src = (getattr(chunk, "source_code", "") or "") if chunk is not None else ""
        radius = radius_of_relevance(source, file_path, symbol) or ""
        candidates = []
        if radius:
            candidates.append(("radius", radius))
            no_imports = _drop_module_imports(radius)
            if no_imports and no_imports != radius:
                candidates.append(("radius_no_imports", no_imports))
        if node_src:
            candidates.append(("node", node_src))
        for level, text in candidates:
            t = estimate_tokens(text)
            if t <= budget:
                return ShrunkRadius(text, level, t, budget, chunk)
        if node_src:
            from backend.core.ouroboros.governance.local_inference_director import fit_prompt_to_window
            # fit_prompt_to_window is best-effort (its compression marker costs
            # tokens of its own), so the target is tightened until the MEASURED
            # size fits; a budget the marker alone would dominate is declined.
            target = budget
            for _ in range(6):
                _sys, compressed, _c = fit_prompt_to_window("", node_src, max_tokens=max(1, target))
                t = estimate_tokens(compressed)
                if compressed.strip() and t <= budget and not compressed.startswith("[context omitted") and "def " in compressed:
                    return ShrunkRadius(compressed, "node_compressed", t, budget, chunk)
                if target <= 1:
                    break
                target = int(target * 0.7)
        logger.warning(
            "[IntelligentChunking] %s::%s does not fit %d tokens at ANY shrink level — declining (fail-closed)",
            file_path, symbol, budget,
        )
        return None
    except Exception:  # noqa: BLE001
        logger.debug("[IntelligentChunking] shrink degraded", exc_info=True)
        return None


def _drop_module_imports(radius: str) -> str:
    """The radius without its module-level import block (the first shrink)."""
    try:
        tree = ast.parse(radius)
    except (SyntaxError, ValueError):
        return radius
    keep = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    if len(keep) == len(tree.body):
        return radius
    lines = radius.splitlines()
    out = []
    for n in keep:
        lo, hi = getattr(n, "lineno", None), getattr(n, "end_lineno", None)
        if lo and hi:
            out.append("\n".join(lines[lo - 1: hi]))
    return "\n\n\n".join(out)


# ---------------------------------------------------------------------------
# RAG Degradation — keyword-density chunker (NEVER whole-file)
# ---------------------------------------------------------------------------


def rag_top_k() -> int:
    try:
        return max(1, int(os.environ.get(_RAG_K_ENV, str(_DEFAULT_RAG_K))))
    except (TypeError, ValueError):
        return _DEFAULT_RAG_K


def _query_terms(query: str) -> List[str]:
    toks = []
    cur = []
    for ch in query or "":
        if ch.isalnum() or ch == "_":
            cur.append(ch.lower())
        else:
            if cur:
                toks.append("".join(cur))
            cur = []
    if cur:
        toks.append("".join(cur))
    return [t for t in toks if len(t) >= 3]


def keyword_rag_chunks(
    source: str, query: str, *, k: Optional[int] = None, window: int = 40,
) -> List[str]:
    """Split *source* into line-windows and return the top-k by keyword-density
    overlap with *query* — the RAG degradation path. NEVER returns the whole
    file (bounded to k windows). Deterministic; never raises.

    A lightweight local retriever (keyword density) — an embedding backend can
    later replace the scorer behind the same interface."""
    top = k if k is not None else rag_top_k()
    terms = set(_query_terms(query))
    lines = source.splitlines()
    if not lines:
        return []
    windows: List[str] = []
    for i in range(0, len(lines), window):
        windows.append("\n".join(lines[i: i + window]))
    if not terms:
        return windows[:top]
    scored = []
    for w in windows:
        low = w.lower()
        score = sum(low.count(t) for t in terms)
        scored.append((score, w))
    scored.sort(key=lambda sw: sw[0], reverse=True)
    return [w for score, w in scored[:top] if score > 0] or windows[:1]


# ---------------------------------------------------------------------------
# Heuristic Reinforcement Loop — SQLite-backed strategy weights
# ---------------------------------------------------------------------------


def _size_bucket(file_lines: int) -> str:
    if file_lines <= 300:
        return "small"
    if file_lines <= 3000:
        return "medium"
    if file_lines <= 30000:
        return "large"
    return "massive"


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_TELEMETRY_TABLE} ("
        "strategy TEXT, size_bucket TEXT, ext TEXT, outcome TEXT, "
        "ts REAL DEFAULT 0)"
    )


def record_strategy_outcome(
    conn: Optional[sqlite3.Connection],
    *,
    strategy: str,
    file_lines: int,
    ext: str,
    outcome: str,
    ts: float = 0.0,
) -> bool:
    """Log an extraction-strategy outcome to the SQLite telemetry layer (DRY —
    the same store the Context Distillation GC compacts). Returns True on a
    successful insert. Never raises."""
    if conn is None:
        return False
    try:
        _ensure_table(conn)
        conn.execute(
            f"INSERT INTO {_TELEMETRY_TABLE} "
            "(strategy, size_bucket, ext, outcome, ts) VALUES (?,?,?,?,?)",
            (strategy, _size_bucket(file_lines), (ext or "").lower(),
             str(outcome).lower(), ts),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        logger.debug("[IntelligentChunking] outcome log failed", exc_info=True)
        return False


_SUCCESS_OUTCOMES = frozenset({"promoted", "applied", "completed", "complete"})


def strategy_weights(
    conn: Optional[sqlite3.Connection], *, file_lines: int, ext: str,
) -> Dict[str, float]:
    """Historical success rate per strategy for this size-bucket + extension.
    Returns ``{strategy: success_rate in [0,1]}``. Empty when no history / no
    conn. Never raises — this is the learned prior the router consults."""
    if conn is None:
        return {}
    try:
        _ensure_table(conn)
        cur = conn.execute(
            f"SELECT strategy, outcome FROM {_TELEMETRY_TABLE} "
            "WHERE size_bucket=? AND ext=?",
            (_size_bucket(file_lines), (ext or "").lower()),
        )
        rows = cur.fetchall()
    except sqlite3.Error:
        return {}
    tally: Dict[str, List[int]] = {}
    for strat, outcome in rows:
        ok = 1 if str(outcome).lower() in _SUCCESS_OUTCOMES else 0
        tally.setdefault(strat, [0, 0])
        tally[strat][0] += ok
        tally[strat][1] += 1
    return {
        s: (succ / total if total else 0.0) for s, (succ, total) in tally.items()
    }


def best_strategy(
    conn: Optional[sqlite3.Connection], *, file_lines: int, ext: str,
) -> Optional[str]:
    """The historically-strongest strategy for this size/ext, or ``None`` when
    there's no signal (router then uses the static preference)."""
    weights = strategy_weights(conn, file_lines=file_lines, ext=ext)
    if not weights:
        return None
    return max(weights.items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# The router — forbids whole-file above the ceiling, learns over time
# ---------------------------------------------------------------------------


@dataclass
class ChunkPlan:
    """The routing decision for a candidate file."""
    strategy: str                       # "ast" | "rag" | "whole"
    context: str                        # the pruned/retrieved DW context
    forbade_whole_file: bool = False    # True iff whole-file was blocked
    chunk: object = None                # AST CodeChunk for stitch-back (ast only)
    rag_snippets: List[str] = field(default_factory=list)


def select_extraction_strategy(
    source: str,
    file_path: str,
    symbol: Optional[str],
    query: str = "",
    *,
    conn: Optional[sqlite3.Connection] = None,
    node_budget_tokens: Optional[int] = None,
) -> ChunkPlan:
    """Choose how to feed *source* to DoubleWord. Under the ceiling → whole-file
    is fine (small files). OVER the ceiling → whole-file is FORBIDDEN: try
    Hierarchical AST Pruning first (or per the learned prior), and on any
    symbol-miss degrade to the RAG keyword chunker — NEVER the whole file.
    Never raises."""
    ext = os.path.splitext(file_path or "")[1] or ""
    file_lines = (source.count("\n") + 1) if source else 0

    if not exceeds_ceiling(source):
        return ChunkPlan(strategy=_STRATEGY_WHOLE, context=source or "")

    # ── massive file: whole-file INGESTION IS FORBIDDEN from here on ──
    pref = best_strategy(conn, file_lines=file_lines, ext=ext)

    def _try_ast() -> Optional[ChunkPlan]:
        if not symbol:
            return None
        budget = node_budget_tokens if node_budget_tokens is not None else dynamic_token_ceiling()
        shrunk = shrink_radius_to_budget(source, file_path, symbol, budget)
        if shrunk is None:
            return None   # fail-closed: nothing that fits → RAG, never whole-file
        if shrunk.level != "radius":
            logger.info(
                "[IntelligentChunking] %s::%s radius narrowed to level=%s (%d/%d tokens)",
                file_path, symbol, shrunk.level, shrunk.tokens, shrunk.budget_tokens,
            )
        return ChunkPlan(
            strategy=_STRATEGY_AST, context=shrunk.context,
            forbade_whole_file=True, chunk=shrunk.chunk,
        )

    def _rag() -> ChunkPlan:
        snippets = keyword_rag_chunks(source, query or (symbol or ""))
        return ChunkPlan(
            strategy=_STRATEGY_RAG,
            context="\n\n# ---- retrieved snippet ----\n\n".join(snippets),
            forbade_whole_file=True, rag_snippets=snippets,
        )

    # Learned prior nudges the order; correctness is identical either way
    # (AST when the symbol resolves, else RAG — whole-file is never an option).
    if pref == _STRATEGY_RAG:
        return _rag()
    plan = _try_ast()
    if plan is not None:
        return plan
    logger.info(
        "[IntelligentChunking] symbol %r unresolved in %s (%d lines) — "
        "BLOCKING whole-file, degrading to RAG retrieval",
        symbol, file_path, file_lines,
    )
    return _rag()


__all__ = [
    "ChunkPlan",
    "SHRINK_LEVELS",
    "ShrunkRadius",
    "best_strategy",
    "dynamic_token_ceiling",
    "estimate_tokens",
    "exceeds_ceiling",
    "keyword_rag_chunks",
    "radius_of_relevance",
    "record_strategy_outcome",
    "select_extraction_strategy",
    "shrink_radius_to_budget",
    "strategy_weights",
]
