"""epistemic_shedder.py — tiered pure-AST weight shedder.

Reduces a Python source string to fit within a character budget using
deterministic, model-free, pure-AST transformations applied in order
of increasing aggression.  Never raises; always returns a string <=
target_chars (or best-effort on Tier 3).

Tiers
-----
none   – source already fits; return unchanged.
tier1  – strip all docstrings (first Expr(Constant(str)) in each
          Module/ClassDef/FunctionDef/AsyncFunctionDef body).
          Comments are not in the AST so ast.unparse drops them for
          free, reducing size further.
tier2  – replace the body of every FunctionDef/AsyncFunctionDef/
          ClassDef with a single "[SOVEREIGN YIELD: Implementation
          Omitted]" Expr, heaviest-first, re-measuring after each
          stub until source fits.  Operates on the Tier-1 output.
tier3  – nuclear truncation: best_so_far[:target_chars] where
          best_so_far is the smallest intermediate result from Tier
          1/2 (or the original on parse error).

Parse error at any AST tier → fall straight to Tier-3 truncation of
the smallest available source (original on early parse failure).

ABSOLUTE CONSTRAINT: pure AST only.
  ast.parse / ast.unparse / ast.get_source_segment / ast.fix_missing_locations
  NEVER exec / eval / compile(..., mode="exec").
"""
from __future__ import annotations

import ast
import logging

log = logging.getLogger(__name__)

_YIELD_STUB = "[SOVEREIGN YIELD: Implementation Omitted]"

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def shed_to_fit(source: str, target_chars: int) -> tuple[str, str]:
    """Reduce *source* until ``len(result) <= target_chars``.

    Parameters
    ----------
    source:
        Raw Python source text.
    target_chars:
        Maximum character budget for the output.

    Returns
    -------
    (shed_source, tier_reached)
        *shed_source* is the (possibly reduced) source string.
        *tier_reached* is one of ``{"none", "tier1", "tier2", "tier3"}``.
    """
    # Fast path — already fits.
    if len(source) <= target_chars:
        return source, "none"

    # Track the best (smallest) intermediate result across tiers so that
    # Tier-3 nuclear truncation cuts from the most-reduced form, not the raw
    # original.  This preserves as much structure as possible even when
    # truncating.
    best: str = source

    try:
        # ----------------------------------------------------------------
        # Tier 1 — strip docstrings.
        # ----------------------------------------------------------------
        t1 = _strip_docstrings(source)
        if len(t1) < len(best):
            best = t1
        if len(t1) <= target_chars:
            return t1, "tier1"

        # ----------------------------------------------------------------
        # Tier 2 — stub out heaviest function defs, one by one (on the
        # Tier-1 output).  ClassDef bodies are intentionally excluded here
        # so that nested function SIGNATURES remain visible in the output.
        # The callback updates *best* with every intermediate result so
        # that Tier-3 truncation slices the most-reduced form that still
        # contains structural information (function signatures, stubs).
        # ----------------------------------------------------------------
        t2, t2_best = _stub_heavy_defs(t1, target_chars)
        # t2_best is the smallest intermediate seen during Tier-2 stubs.
        if len(t2_best) < len(best):
            best = t2_best
        if len(t2) < len(best):
            best = t2
        if len(t2) <= target_chars:
            return t2, "tier2"

    except Exception:  # noqa: BLE001  (fail-soft; any AST error → tier3)
        log.debug(
            "epistemic_shedder: AST processing failed; falling to tier3",
            exc_info=True,
        )

    # ----------------------------------------------------------------
    # Tier 3 — nuclear truncation of the best intermediate result.
    # Truncates from the smallest intermediate so that any structure
    # (function signatures, stub markers) that survived earlier tiers
    # is preserved as much as possible within the budget.
    # Falls here either because Tier 2 didn't fit OR on any parse error.
    # ----------------------------------------------------------------
    return best[:target_chars], "tier3"


# ---------------------------------------------------------------------------
# Tier 1 helpers
# ---------------------------------------------------------------------------


def _is_docstring_node(node: ast.stmt) -> bool:
    """Return True when *node* is an ``Expr(Constant(str))`` docstring stmt."""
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


class _DocstringStripper(ast.NodeTransformer):
    """Remove the leading docstring from each supported scope."""

    def _strip_body(self, body: list[ast.stmt]) -> list[ast.stmt]:
        if body and _is_docstring_node(body[0]):
            return body[1:] or [ast.Pass()]
        return body

    def visit_Module(self, node: ast.Module) -> ast.Module:
        node.body = self._strip_body(node.body)
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        node.body = self._strip_body(node.body)
        self.generic_visit(node)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        node.body = self._strip_body(node.body)
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(
        self, node: ast.AsyncFunctionDef
    ) -> ast.AsyncFunctionDef:
        node.body = self._strip_body(node.body)
        self.generic_visit(node)
        return node


def _strip_docstrings(source: str) -> str:
    """Return *source* with all docstrings removed via AST round-trip."""
    tree = ast.parse(source)
    stripped = _DocstringStripper().visit(tree)
    ast.fix_missing_locations(stripped)
    return ast.unparse(stripped)


# ---------------------------------------------------------------------------
# Tier 2 helpers
# ---------------------------------------------------------------------------

_FUNC_STUBBABLE = (ast.FunctionDef, ast.AsyncFunctionDef)
_CLASS_STUBBABLE = (ast.ClassDef,)
_STUBBABLE = _FUNC_STUBBABLE + _CLASS_STUBBABLE


def _yield_body() -> list[ast.stmt]:
    """Return the single-statement stub body for a stubbable def."""
    return [ast.Expr(value=ast.Constant(value=_YIELD_STUB))]


def _collect_stubbable_defs(
    tree: ast.AST,
    source: str,
    kinds: tuple[type, ...],
) -> list[tuple[int, ast.AST]]:
    """Collect stubbable nodes of *kinds* with source size, largest first."""
    results: list[tuple[int, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, kinds):
            seg = ast.get_source_segment(source, node)
            size = len(seg) if seg is not None else 0
            results.append((size, node))
    # Heaviest first (stable sort for determinism when sizes tie).
    results.sort(key=lambda t: t[0], reverse=True)
    return results


def _stub_heavy_defs(source: str, target_chars: int) -> tuple[str, str]:
    """Replace function/async-function def bodies one-by-one (heaviest first).

    Only ``FunctionDef``/``AsyncFunctionDef`` bodies are replaced; ``ClassDef``
    bodies are intentionally excluded so that nested function signatures remain
    visible in the output.

    Returns
    -------
    (final_result, best_intermediate)
        *final_result* is the unparsed tree after all applicable stubs.
        *best_intermediate* is the smallest string seen across all
        intermediate steps (may differ from *final_result* when later stubs
        grow the text, e.g. when a tiny function body is replaced by a stub
        string that is longer).
    """
    tree = ast.parse(source)
    current = ast.unparse(ast.fix_missing_locations(tree))
    best_so_far = current

    func_defs = _collect_stubbable_defs(tree, source, _FUNC_STUBBABLE)
    for _size, node in func_defs:
        if len(current) <= target_chars:
            return current, best_so_far
        node.body = _yield_body()  # type: ignore[attr-defined]
        ast.fix_missing_locations(tree)
        current = ast.unparse(tree)
        if len(current) < len(best_so_far):
            best_so_far = current

    return current, best_so_far
