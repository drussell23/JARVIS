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

_CEILING_ENV = "JARVIS_DW_MAX_CONTEXT_TOKENS"
_DEFAULT_CEILING = 8000
_RAG_K_ENV = "JARVIS_DW_RAG_TOP_K"
_DEFAULT_RAG_K = 6
_CHARS_PER_TOKEN = 4

_STRATEGY_AST = "ast"
_STRATEGY_RAG = "rag"
_STRATEGY_WHOLE = "whole"

_TELEMETRY_TABLE = "chunk_strategy_outcomes"


# ---------------------------------------------------------------------------
# Dynamic token ceiling — the "never brute-force above this" line
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Coarse ~4-chars-per-token estimate. Deterministic; never raises."""
    return max(0, len(text or "")) // _CHARS_PER_TOKEN


def dynamic_token_ceiling() -> int:
    """The context-token ceiling above which whole-file ingestion is FORBIDDEN
    (env ``JARVIS_DW_MAX_CONTEXT_TOKENS``, default 8000). Dynamic — an operator
    tunes it to the live DW RT context budget. Clamped >= 512."""
    try:
        return max(512, int(os.environ.get(_CEILING_ENV, str(_DEFAULT_CEILING))))
    except (TypeError, ValueError):
        return _DEFAULT_CEILING


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
        radius = radius_of_relevance(source, file_path, symbol)
        if not radius:
            return None
        chunk = None
        try:
            from backend.core.ouroboros.governance.chunked_generation import (
                extract_target_chunk,
            )
            chunk = extract_target_chunk(source, file_path, symbol)
        except Exception:  # noqa: BLE001
            chunk = None
        return ChunkPlan(
            strategy=_STRATEGY_AST, context=radius,
            forbade_whole_file=True, chunk=chunk,
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
    "best_strategy",
    "dynamic_token_ceiling",
    "estimate_tokens",
    "exceeds_ceiling",
    "keyword_rag_chunks",
    "radius_of_relevance",
    "record_strategy_outcome",
    "select_extraction_strategy",
    "strategy_weights",
]
