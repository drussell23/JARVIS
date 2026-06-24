"""backend/core/ouroboros/governance/multi_repo/cross_repo_blast_context.py

G1 -- Dynamic AST Dependency Tracing (Blast Radius -> generation context).

Spec: docs/superpowers/specs/2026-06-23-sovereign-cross-repo-mutator.md S3, S8.

Before O+V generates a mutation in `reactor` (Nerves) or `prime` (Mind), this
provider forces the Oracle-traced CROSS-REPO dependents into the generation
context window -- so the model is forced to recognize every downstream symbol
in a DIFFERENT repo that depends on what it is about to change, and not break
their contract.

Reuse-first:
  * `oracle.compute_blast_radius(node_id, max_depth)` -> the cross-repo AST
    graph (returns `directly_affected` / `transitively_affected` sets of
    `NodeID`). We do NOT reimplement graph traversal.
  * `RepoRegistry.read_file(repo, path)` -> read the dependent's source
    (traversal-guarded). We read only the enclosing symbol region (dense).
  * `dw_egress_interceptor.estimate_body_chars` -> the token-budget estimate.

This module is a CONTEXT PROVIDER: pure async read + string rendering. It has
ZERO write / policy authority. Fail-soft: any Oracle / registry error yields an
EMPTY context -- the caller treats an empty blast context as a signal to
escalate the risk floor (fail-CLOSED).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- env knobs (NO hardcoding of depth / budget) ---------------------------
_DEPTH_ENV = "JARVIS_CROSS_REPO_BLAST_DEPTH"
_TOKEN_BUDGET_ENV = "JARVIS_CROSS_REPO_BLAST_TOKEN_BUDGET"
_ENABLED_ENV = "JARVIS_CROSS_REPO_BLAST_CONTEXT_ENABLED"

_DEFAULT_DEPTH = 3
_DEFAULT_TOKEN_BUDGET = 6000
_CHARS_PER_TOKEN = 4  # mirrors context_builder's rough estimate

# Repo -> Trinity role label for the prompt header.
_REPO_ROLE = {
    "prime": "Mind",
    "reactor": "Nerves",
    "jarvis": "Body",
}


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependentRef:
    """A single cross-repo downstream dependent of the target symbol.

    `relevance` carries the dense source excerpt of the dependent's enclosing
    symbol (not the whole file) so the prompt stays compact.
    """

    repo: str
    file: str
    symbol: str
    relevance: str


@dataclass(frozen=True)
class BlastRadiusContext:
    """Resolved cross-repo blast radius for a single mutation target."""

    target_repo: str
    target_symbol: str
    dependents: Tuple[DependentRef, ...]
    rendered_prompt_block: str
    truncated: bool
    total_dependents: int


_EMPTY = BlastRadiusContext(
    target_repo="",
    target_symbol="",
    dependents=(),
    rendered_prompt_block="",
    truncated=False,
    total_dependents=0,
)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """Master gate. Default TRUE. Explicit falsy -> OFF (no-op empty block)."""
    raw = os.environ.get(_ENABLED_ENV, "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def _blast_depth(explicit: Optional[int]) -> int:
    if explicit is not None:
        return explicit
    try:
        raw = os.environ.get(_DEPTH_ENV, "").strip()
        if not raw:
            return _DEFAULT_DEPTH
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_DEPTH


def _token_budget(explicit: Optional[int]) -> int:
    if explicit is not None:
        return explicit
    try:
        raw = os.environ.get(_TOKEN_BUDGET_ENV, "").strip()
        if not raw:
            return _DEFAULT_TOKEN_BUDGET
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_TOKEN_BUDGET


def _role(repo: str) -> str:
    return _REPO_ROLE.get(repo, repo)


def _estimate_tokens(text: str) -> int:
    """Token estimate via the reused egress estimator (fail-soft)."""
    try:
        from backend.core.ouroboros.governance.dw_egress_interceptor import (
            estimate_body_chars,
        )

        chars = estimate_body_chars({"messages": [{"content": text}]})
    except Exception:  # noqa: BLE001 -- never let the estimator break rendering
        chars = len(text)
    return max(1, chars // _CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Source excerpting -- read the dependent's ENCLOSING symbol region (dense)
# ---------------------------------------------------------------------------


def _excerpt_symbol(source: str, symbol: str) -> str:
    """Return the dense source region enclosing `symbol`.

    Prefers an AST-located def/class block; falls back to a line-window around
    the first textual mention. Pure read -- never executes the source.
    """
    # AST path -- locate the def/class whose name matches the (possibly
    # dotted, e.g. "Class.method") symbol's final component.
    leaf = symbol.split(".")[-1]
    try:
        import ast

        tree = ast.parse(source)
        lines = source.splitlines()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ) and node.name == leaf:
                start = max(0, node.lineno - 1)
                end = getattr(node, "end_lineno", None) or (start + 1)
                region = "\n".join(lines[start:end]).strip("\n")
                if region:
                    return region
    except Exception:  # noqa: BLE001 -- fall through to textual window
        pass

    # Textual fallback: a small window around the first mention of the leaf.
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if leaf in line:
            start = max(0, i)
            end = min(len(lines), i + 12)
            region = "\n".join(lines[start:end]).strip("\n")
            if region:
                return region
    # Last resort: a bounded head of the file.
    return "\n".join(lines[:12]).strip("\n")


# ---------------------------------------------------------------------------
# Core trace
# ---------------------------------------------------------------------------


def _node_attr(node: Any, *names: str) -> Any:
    for n in names:
        if hasattr(node, n):
            return getattr(node, n)
    return None


def _ordered_affected(report: Any) -> List[Tuple[Any, int]]:
    """Yield (NodeID, depth_rank) nearest-first.

    `directly_affected` (depth-1, most directly coupled) ranks before
    `transitively_affected` (deeper). Each set is sorted deterministically.
    """
    out: List[Tuple[Any, int]] = []
    direct = _node_attr(report, "directly_affected") or set()
    trans = _node_attr(report, "transitively_affected") or set()

    def _key(n: Any) -> str:
        return f"{_node_attr(n, 'repo')}:{_node_attr(n, 'file_path', 'file')}:{_node_attr(n, 'name', 'symbol')}"

    for n in sorted(direct, key=_key):
        out.append((n, 1))
    for n in sorted(trans, key=_key):
        out.append((n, 2))
    return out


async def trace_cross_repo_blast(
    *,
    target_node_id: Any,
    oracle: Any,
    registry: Any,
    max_depth: Optional[int] = None,
) -> BlastRadiusContext:
    """Trace the cross-repo blast radius of `target_node_id`.

    Calls `oracle.compute_blast_radius(target_node_id, max_depth=...)`, collects
    every affected `NodeID` in a DIFFERENT repo than the target (the cross-repo
    dependents), reads each one's enclosing source region via the registry, and
    returns a `BlastRadiusContext` with a rendered prompt block.

    Fail-soft: any error -> EMPTY context (caller fail-CLOSED escalates).
    OFF -> EMPTY context.
    """
    if not enabled():
        return _EMPTY

    target_repo = _node_attr(target_node_id, "repo") or ""
    target_symbol = _node_attr(target_node_id, "name", "symbol") or ""

    try:
        report = oracle.compute_blast_radius(
            target_node_id, max_depth=_blast_depth(max_depth)
        )
    except Exception:  # noqa: BLE001 -- fail-soft -> empty (caller escalates)
        logger.warning(
            "[CrossRepoBlast] oracle.compute_blast_radius failed for %s -- "
            "returning empty context (caller escalates risk floor)",
            target_symbol,
            exc_info=True,
        )
        return _EMPTY

    if report is None:
        return _EMPTY

    refs: List[DependentRef] = []
    for node, _depth in _ordered_affected(report):
        dep_repo = _node_attr(node, "repo") or ""
        # CROSS-repo only: same-repo dependents are not the cross-repo blast.
        if not dep_repo or dep_repo == target_repo:
            continue
        dep_file = _node_attr(node, "file_path", "file") or ""
        dep_symbol = _node_attr(node, "name", "symbol") or ""
        try:
            source = await registry.read_file(dep_repo, dep_file)
        except Exception:  # noqa: BLE001 -- one bad read must not abort the trace
            logger.warning(
                "[CrossRepoBlast] registry.read_file failed for %s:%s -- skipping",
                dep_repo,
                dep_file,
                exc_info=True,
            )
            continue
        if not source:
            # Missing file -> skip (cannot show a contract we can't read).
            continue
        excerpt = _excerpt_symbol(source, dep_symbol)
        refs.append(
            DependentRef(
                repo=dep_repo,
                file=dep_file,
                symbol=dep_symbol,
                relevance=excerpt,
            )
        )

    ctx_no_render = BlastRadiusContext(
        target_repo=target_repo,
        target_symbol=target_symbol,
        dependents=tuple(refs),
        rendered_prompt_block="",
        truncated=False,
        total_dependents=len(refs),
    )
    # Pre-render once at the default budget so callers that read
    # `rendered_prompt_block` directly get a populated block. Callers can
    # re-render with an explicit budget via render_blast_block().
    rendered = render_blast_block(ctx_no_render)
    return BlastRadiusContext(
        target_repo=target_repo,
        target_symbol=target_symbol,
        dependents=tuple(refs),
        rendered_prompt_block=rendered,
        truncated="further dependents elided" in rendered,
        total_dependents=len(refs),
    )


# ---------------------------------------------------------------------------
# Rendering -- token-budgeted, NEVER-silent truncation
# ---------------------------------------------------------------------------


def render_blast_block(
    ctx: BlastRadiusContext,
    *,
    token_budget: Optional[int] = None,
) -> str:
    """Render the CROSS-REPO BLAST RADIUS prompt block.

    Dependents are emitted nearest-first (the trace already ordered them).
    Token-budgeted via `estimate_body_chars`; if the listing would exceed the
    budget the tail is truncated and a `... N further dependents elided ...`
    marker is appended (NEVER silently dropped -- the count is logged).
    """
    if not ctx.dependents:
        return ""

    budget = _token_budget(token_budget)
    repos = sorted({d.repo for d in ctx.dependents})
    repos_label = "/".join(repos)
    role = _role(ctx.target_repo)

    header = (
        "## CROSS-REPO BLAST RADIUS\n"
        f"You are mutating `{ctx.target_symbol}` in the {ctx.target_repo} repo "
        f"({role}).\n"
        f"The following {ctx.total_dependents} downstream symbols in "
        f"{repos_label} DEPEND on it -- you MUST NOT break their contract:\n"
    )

    emitted: List[str] = []
    rendered = header
    truncated_count = 0

    for idx, dep in enumerate(ctx.dependents):
        entry = (
            f"- [{dep.repo}] {dep.file}::{dep.symbol}  "
            f"(depends on {ctx.target_symbol})\n"
            f"    {dep.relevance.strip()}\n"
        )
        candidate = rendered + "".join(emitted) + entry
        if _estimate_tokens(candidate) > budget and emitted:
            # Over budget AND we already have at least one entry -> stop here.
            truncated_count = len(ctx.dependents) - idx
            break
        emitted.append(entry)

    body = "".join(emitted)
    out = header + body

    if truncated_count > 0:
        marker = (
            f"... {truncated_count} further dependents elided "
            "(over token budget) ...\n"
        )
        out += marker
        logger.warning(
            "[CrossRepoBlast] token budget %d exceeded -- truncated %d of %d "
            "cross-repo dependents for target %s (NOT silent)",
            budget,
            truncated_count,
            ctx.total_dependents,
            ctx.target_symbol,
        )

    return out
