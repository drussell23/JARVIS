"""Signature-level context: what a caller needs, not what a file contains.

Why
---

Measured across 4,639 local-lane generations: prompt p50 **10,510** tokens,
p90 **22,770**, max 32,756. The model is not starved of context -- it is
drowning in it. Meanwhile the dominant failure is grounding: of validation
failures carrying an identifiable exception, ~63% are ``AttributeError`` /
``ModuleNotFoundError`` / ``ImportError`` -- code written against APIs that
do not exist -- against only **6** ``SyntaxError`` in the same corpus.

That pairing is the whole diagnosis. Structural competence is near-perfect;
what fails is recalling an exact symbol out of twenty thousand tokens. The
served model is an A3B MoE: roughly 3B *active* parameters per token, and
active count is what governs long-context retrieval precision. Pasting whole
file bodies asks the weakest axis of this model to do needle-exact recall,
and pays a 10k-token tax for the privilege.

A caller needs the *surface*: what exists, what it is called, what it takes,
what it returns. Bodies are the part that is both largest and least
relevant, so they are what goes.

Degradation ladder
------------------

``FULL`` → ``SIGNATURES`` → ``NAMES``. Each rung is strictly smaller and
strictly still true; nothing is invented on the way down. A rung that fails
to parse degrades to the next-coarser one rather than emitting a slice that
cannot compile -- the same posture ``ast_symbol_scoper`` takes, and its
``slice_is_valid`` gate is reused rather than reimplemented.

Budget
------

No literal ceiling. The budget derives from the ACTIVE model's context
window, resolved by ``context_pruner.resolve_context_limit`` from the
canonical model cards, times a fraction. On a 32k card the default fraction
yields ~4k -- the number the operator asked for, arrived at rather than
stamped, so a model swap moves it automatically instead of silently
inheriting a ceiling chosen for different physics.
"""
from __future__ import annotations

import ast
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.SignaturePruner")

# Fraction of the resolved context window one *dependency block* may occupy.
# The rest of the window belongs to the task, the plan, the failure critique
# and the model's own output, so a dependency block that eats the window has
# starved the thing it exists to serve.
_DEFAULT_FRACTION = 0.125
_MIN_BUDGET_TOKENS = 256


class Detail(str, Enum):
    """How much of a module survives."""

    FULL = "full"
    SIGNATURES = "signatures"
    NAMES = "names"


@dataclass(frozen=True)
class PrunedModule:
    """One module reduced to the surface a caller can rely on."""

    path: str
    detail: Detail
    text: str
    symbols: Tuple[str, ...]

    def render(self) -> str:
        return f"{self.path} [{self.detail.value}] {len(self.symbols)} symbol(s)"


class _BodyStripper(ast.NodeTransformer):
    """Replace every function body with its docstring and ``...``.

    Signatures, decorators, annotations, defaults and class structure all
    survive untouched -- they are the contract. The body is the
    implementation, which a caller must not depend on and a generator does
    not need to see in order to call the thing correctly.
    """

    def _strip(self, node: ast.AST) -> ast.AST:
        self.generic_visit(node)
        body = list(getattr(node, "body", []))
        keep: List[ast.stmt] = []
        if body and isinstance(body[0], ast.Expr) and isinstance(
            getattr(body[0], "value", None), ast.Constant,
        ) and isinstance(body[0].value.value, str):
            keep.append(body[0])          # the docstring IS the contract
        keep.append(ast.Expr(value=ast.Constant(value=Ellipsis)))
        node.body = keep  # type: ignore[attr-defined]
        return node

    def visit_FunctionDef(self, node):  # noqa: N802
        return self._strip(node)

    def visit_AsyncFunctionDef(self, node):  # noqa: N802
        return self._strip(node)


def _is_public(name: str) -> bool:
    """Dunder methods are public API; single-underscore names are not."""
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


def symbols_of(source: str, *, public_only: bool = True) -> Tuple[str, ...]:
    """Top-level and class-level def/class names. NEVER raises."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ()
    out: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if public_only and not _is_public(node.name):
                continue
            if node.name not in out:
                out.append(node.name)
    return tuple(out)


def prune_to_signatures(source: str, *, public_only: bool = True) -> Optional[str]:
    """*source* with every function body replaced by docstring + ``...``.

    ``None`` when the source does not parse or the result would not --
    never a slice that cannot compile. Callers degrade to :func:`names_only`.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    if public_only:
        for parent in list(ast.walk(tree)):
            body = getattr(parent, "body", None)
            if not isinstance(body, list):
                continue
            parent.body = [  # type: ignore[attr-defined]
                n for n in body
                if not (
                    isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and not _is_public(n.name)
                )
            ] or [ast.Expr(value=ast.Constant(value=Ellipsis))]

    try:
        stripped = ast.fix_missing_locations(_BodyStripper().visit(tree))
        text = ast.unparse(stripped)
    except Exception:  # noqa: BLE001
        logger.debug("[SignaturePruner] unparse failed", exc_info=True)
        return None

    # Reuse the scoper's integrity gate rather than a second opinion on what
    # "valid" means; a slice that fails it degrades instead of shipping.
    try:
        from backend.core.ouroboros.governance.ast_symbol_scoper import (  # noqa: PLC0415
            slice_is_valid,
        )
        if not slice_is_valid(text):
            return None
    except Exception:  # noqa: BLE001
        try:
            ast.parse(text)
        except (SyntaxError, ValueError):
            return None
    return text


def names_only(source: str, path: str = "", *, public_only: bool = True) -> str:
    """The coarsest true statement about a module: what it defines."""
    names = symbols_of(source, public_only=public_only)
    header = f"# {path}" if path else "# module"
    if not names:
        return f"{header}\n# (no public symbols)"
    return f"{header}\n# defines: " + ", ".join(names)


def dependency_budget_tokens(*, model_id: str = "") -> int:
    """Tokens a dependency block may occupy, derived from the model card.

    ``JARVIS_DEPENDENCY_CONTEXT_TOKENS`` pins it outright;
    ``JARVIS_DEPENDENCY_CONTEXT_FRACTION`` tunes the share. Otherwise the
    ACTIVE model's window decides, so swapping the served model moves the
    budget with it instead of inheriting a ceiling chosen for other physics.
    NEVER raises.
    """
    try:
        pinned = int((os.environ.get("JARVIS_DEPENDENCY_CONTEXT_TOKENS", "") or "0").strip())
        if pinned > 0:
            return pinned
    except (ValueError, TypeError):
        pass

    try:
        fraction = float(
            (os.environ.get("JARVIS_DEPENDENCY_CONTEXT_FRACTION", "") or "").strip()
            or _DEFAULT_FRACTION
        )
    except (ValueError, TypeError):
        fraction = _DEFAULT_FRACTION
    if not (0.0 < fraction <= 1.0):
        fraction = _DEFAULT_FRACTION

    # The NEGOTIATED window, not the model card. These disagree by 4x on the
    # current lane: the transport negotiates num_ctx=32768 (observed 12,287
    # times) while ``resolve_context_limit`` answers 131,072, because
    # ``qwen3-coder-ov:30b`` has no model card and falls through to a
    # default. A pruner sized against a window four times larger than the one
    # the transport uses never prunes -- which is the mechanical root of the
    # 10.5k median prompt this module exists to cut.
    #
    # ``ingest_ceiling_tokens`` is the right source twice over: it is the
    # negotiated number, and it has already subtracted the output reserve.
    # The largest observed prompt was 32,756 against num_ctx=32,768 -- twelve
    # tokens for the model to answer in, which is how a 32k prompt produces a
    # sub-200-token completion.
    # ``ingest_ceiling_tokens`` answers even when nothing has been
    # negotiated -- it falls back to a small default, which in a fresh
    # process is not a statement about the served model at all. Ask whether a
    # negotiation HAPPENED before believing its number, or a helper process
    # inherits a ceiling meant for no model in particular.
    window = 0
    try:
        from backend.core.ouroboros.governance.context_budget import (  # noqa: PLC0415
            current_budget, ingest_ceiling_tokens,
        )
        if current_budget() is not None:
            window = int(ingest_ceiling_tokens() or 0)
        else:
            logger.debug(
                "[SignaturePruner] no negotiated budget in this process — "
                "the ceiling would be a default, not a measurement",
            )
    except Exception:  # noqa: BLE001
        logger.debug("[SignaturePruner] negotiated ceiling unresolved", exc_info=True)

    if window <= 0:
        try:
            from backend.core.ouroboros.governance.context_pruner import (  # noqa: PLC0415
                resolve_context_limit,
            )
            window = int(resolve_context_limit(model_id) or 0)
            logger.info(
                "[SignaturePruner] no negotiated ceiling; falling back to the "
                "model card (%d). This is the 4x-disagreement path -- the "
                "budget may be sized for a window the transport will not use.",
                window,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[SignaturePruner] context limit unresolved", exc_info=True)

    if window <= 0:
        try:
            from backend.core.ouroboros.governance.context_budget import (  # noqa: PLC0415
                fallback_window_tokens,
            )
            window = int(fallback_window_tokens() or 0)
        except Exception:  # noqa: BLE001
            window = 0
    if window <= 0:
        return _MIN_BUDGET_TOKENS
    return max(_MIN_BUDGET_TOKENS, int(window * fraction))


def _estimate(text: str) -> int:
    """Tokens, via the self-calibrating ledger that already tracks the
    active provider's real char/token density."""
    try:
        from backend.core.ouroboros.governance.context_pruner import (  # noqa: PLC0415
            get_default_ledger,
        )
        return get_default_ledger().estimate_tokens(text)
    except Exception:  # noqa: BLE001
        return max(0, len(text) // 4)


def fit_dependencies(
    modules: Sequence[Tuple[str, str]],
    *,
    budget_tokens: Optional[int] = None,
    model_id: str = "",
) -> Tuple[List[PrunedModule], int]:
    """Reduce ``(path, source)`` pairs until they fit the budget.

    Walks the whole set down one rung at a time rather than truncating the
    tail, because a caller that is shown three complete modules and nothing
    at all about the fourth will confidently invent the fourth. Every module
    keeps *some* true statement about itself at every rung -- which is the
    difference between a small prompt and a misleading one.

    Returns ``(pruned, estimated_tokens)``. NEVER raises.
    """
    budget = budget_tokens if budget_tokens is not None else dependency_budget_tokens(
        model_id=model_id,
    )
    pairs = [(str(p), str(s or "")) for p, s in (modules or ()) if str(p)]
    if not pairs:
        return [], 0

    for detail in (Detail.FULL, Detail.SIGNATURES, Detail.NAMES):
        rendered: List[PrunedModule] = []
        for path, source in pairs:
            if detail is Detail.FULL:
                text = source
            elif detail is Detail.SIGNATURES:
                text = prune_to_signatures(source) or names_only(source, path)
            else:
                text = names_only(source, path)
            rendered.append(PrunedModule(
                path=path, detail=detail, text=text,
                symbols=symbols_of(source),
            ))
        total = sum(_estimate(m.text) for m in rendered)
        if total <= budget:
            logger.info(
                "[SignaturePruner] %d module(s) at %s — %d/%d tokens",
                len(rendered), detail.value, total, budget,
            )
            return rendered, total

    logger.warning(
        "[SignaturePruner] %d module(s) exceed %d tokens even at NAMES — "
        "emitting names anyway; a caller told nothing about a module will "
        "invent it",
        len(pairs), budget,
    )
    return rendered, total


__all__ = [
    "Detail",
    "PrunedModule",
    "dependency_budget_tokens",
    "fit_dependencies",
    "names_only",
    "prune_to_signatures",
    "symbols_of",
]
