"""AstSymbolScoper — pure-AST symbol isolation + syntactic-integrity gate (B1a).

Task B1 of the Sovereign Resilience Chunking Matrix.

Isolates exact AST symbols a GOAL needs so a sub-goal can target
``file::Symbol`` instead of a whole 3247-line file, collapsing blast radius.

## Authority posture

- PURE AST ONLY — ``ast.parse`` / ``ast.get_source_segment`` / ``textwrap.dedent``.
- NEVER ``exec``, ``eval``, or ``compile(..., mode="exec")``.
- Fail-soft: any error → whole-file degrade, never crash.
- Stdlib only; no third-party deps.
- ASCII source.

## B1a syntactic-integrity gate

Every isolated slice MUST pass ``slice_is_valid`` before being returned.
A slice that fails the gate degrades to the next-coarser valid scope
(enclosing symbol → whole file). This prevents the decomposer from ever
emitting uncompilable garbage to downstream generators.
"""
from __future__ import annotations

import ast
import logging
import re
import textwrap
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopedTarget:
    """One symbol extracted from a source file.

    ``symbol == ""`` means whole-file fallback (parse failure, no match,
    or B1a gate failure at the outermost scope).
    """

    file_path: str
    symbol: str
    lineno: int
    end_lineno: int


# ---------------------------------------------------------------------------
# B1a syntactic-integrity gate
# ---------------------------------------------------------------------------


def slice_is_valid(source_segment: str) -> bool:
    """Return True iff ``source_segment`` is a structurally valid Python fragment.

    Uses ``ast.parse(textwrap.dedent(segment))`` for the round-trip check.
    NEVER calls ``exec``, ``eval``, or ``compile(..., mode="exec")``.

    Typical rejects:
    - ``"@deco\\n"``          — severed decorator with no following ``def``
    - ``"    return 1"``      — orphaned indented body (bare ``return`` at
      module level after dedent is syntactically parseable by the AST module
      but semantically invalid as a standalone slice)

    Args:
        source_segment: Raw source text to validate.

    Returns:
        ``True`` if the segment parses cleanly and contains no bare
        flow-control statements (``return``, ``yield``, ``continue``,
        ``break``) at module level, ``False`` otherwise.
    """
    if not source_segment or not source_segment.strip():
        return False
    try:
        tree = ast.parse(textwrap.dedent(source_segment))
    except SyntaxError:
        return False
    except Exception:  # noqa: BLE001 — defensive, fail-soft
        return False

    # A slice that begins with orphaned flow-control at module level is
    # semantically invalid even though ast.parse accepts it.  For example,
    # "    return 1" dedents to "return 1" which parses fine but would be a
    # SyntaxError at compile time inside any real module.
    _INVALID_MODULE_LEVEL = (ast.Return, ast.Continue, ast.Break)
    for stmt in tree.body:
        if isinstance(stmt, _INVALID_MODULE_LEVEL):
            return False
        # A bare Expr wrapping a Yield/YieldFrom is also orphaned.
        if isinstance(stmt, ast.Expr) and isinstance(
            stmt.value, (ast.Yield, ast.YieldFrom)
        ):
            return False

    return True


# ---------------------------------------------------------------------------
# Symbol isolation
# ---------------------------------------------------------------------------


def _name_appears_in(name: str, description: str, hints: Sequence[str]) -> bool:
    """Return True if ``name`` appears as a word in ``description`` or any hint."""
    # Build a combined search corpus.
    corpus = description + " " + " ".join(hints)
    # Whole-word match (allow `.` as a separator so "SemanticIndex.build" also
    # matches the bare method name "build" when searching for individual parts).
    pattern = r"(?<![.\w])" + re.escape(name) + r"(?![.\w])"
    return bool(re.search(pattern, corpus))


def _qualified_appears_in(
    class_name: str, method_name: str, description: str, hints: Sequence[str]
) -> bool:
    """Return True if ``Class.method`` appears literally in description/hints."""
    qualified = f"{class_name}.{method_name}"
    corpus = description + " " + " ".join(hints)
    return qualified in corpus


def _extract_segment(source: str, node: ast.AST) -> str | None:
    """Extract the source segment for ``node`` using ``ast.get_source_segment``."""
    try:
        seg = ast.get_source_segment(source, node)
        return seg
    except Exception:  # noqa: BLE001
        return None


def isolate_symbols(
    file_path: str,
    description: str,
    *,
    hints: tuple[str, ...] = (),
) -> tuple[ScopedTarget, ...]:
    """Parse ``file_path`` and return ``ScopedTarget``s for matching symbols.

    Selects top-level ``ClassDef`` / ``FunctionDef`` / ``AsyncFunctionDef``
    (and one level of methods, named ``Class.method``) whose name appears in
    ``description`` or ``hints``.

    Each candidate is run through the B1a integrity gate (``slice_is_valid``).
    A failing slice is discarded and the enclosing symbol (the whole class, or
    the whole file) is used as the degraded fallback.

    Args:
        file_path:   Absolute path to the Python source file.
        description: Natural-language description of the GOAL (e.g. a signal
                     body). Symbol names found here drive selection.
        hints:       Optional additional name hints (e.g. from a plan phase).

    Returns:
        Non-empty tuple of ``ScopedTarget``s. Whole-file degrade
        ``(ScopedTarget(file_path, "", 0, 0),)`` on parse failure, unreadable
        file, or no matching symbols.
    """
    _whole_file = (ScopedTarget(file_path, "", 0, 0),)

    # --- Read the file --------------------------------------------------
    try:
        with open(file_path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError as exc:
        logger.warning("[AstSymbolScoper] Cannot read %s: %s", file_path, exc)
        return _whole_file
    except Exception as exc:  # noqa: BLE001
        logger.warning("[AstSymbolScoper] Read failed for %s: %s", file_path, exc)
        return _whole_file

    # --- Parse the AST --------------------------------------------------
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as exc:
        logger.debug("[AstSymbolScoper] Syntax error in %s: %s", file_path, exc)
        return _whole_file
    except Exception as exc:  # noqa: BLE001
        logger.warning("[AstSymbolScoper] Parse failed for %s: %s", file_path, exc)
        return _whole_file

    # --- Walk top-level body --------------------------------------------
    targets: list[ScopedTarget] = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            class_name = node.name
            class_lineno = node.lineno
            class_end = node.end_lineno or node.lineno

            # Check for qualified ``Class.method`` matches first (one level deep).
            method_matched = False
            for item in node.body:
                if not isinstance(
                    item, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    continue
                method_name = item.name
                qualified = f"{class_name}.{method_name}"

                if _qualified_appears_in(
                    class_name, method_name, description, hints
                ) or _name_appears_in(method_name, description, hints):
                    # Try to emit the method slice.
                    seg = _extract_segment(source, item)
                    if seg and slice_is_valid(seg):
                        targets.append(
                            ScopedTarget(
                                file_path=file_path,
                                symbol=qualified,
                                lineno=item.lineno,
                                end_lineno=item.end_lineno or item.lineno,
                            )
                        )
                        method_matched = True
                    else:
                        # Method slice failed gate — degrade to enclosing class.
                        logger.debug(
                            "[AstSymbolScoper] Method slice failed gate for %s::%s"
                            " — degrading to class scope",
                            file_path,
                            qualified,
                        )
                        # Fall through to class-level check below.

            # Also check if the class name itself matches.
            if _name_appears_in(class_name, description, hints):
                class_seg = _extract_segment(source, node)
                if class_seg and slice_is_valid(class_seg):
                    targets.append(
                        ScopedTarget(
                            file_path=file_path,
                            symbol=class_name,
                            lineno=class_lineno,
                            end_lineno=class_end,
                        )
                    )
                else:
                    # Class slice invalid → degrade to whole-file (handled at end).
                    logger.debug(
                        "[AstSymbolScoper] Class slice failed gate for %s::%s"
                        " — will degrade to whole-file if no other targets",
                        file_path,
                        class_name,
                    )
            elif method_matched:
                # Method(s) matched but class name wasn't requested — that's fine,
                # method ScopedTargets already appended above.
                pass

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_name = node.name
            if _name_appears_in(func_name, description, hints):
                seg = _extract_segment(source, node)
                if seg and slice_is_valid(seg):
                    targets.append(
                        ScopedTarget(
                            file_path=file_path,
                            symbol=func_name,
                            lineno=node.lineno,
                            end_lineno=node.end_lineno or node.lineno,
                        )
                    )
                else:
                    logger.debug(
                        "[AstSymbolScoper] Function slice failed gate for %s::%s"
                        " — degrading to whole-file",
                        file_path,
                        func_name,
                    )

    # --- Degrade if nothing matched -------------------------------------
    if not targets:
        return _whole_file

    return tuple(targets)


__all__ = [
    "ScopedTarget",
    "isolate_symbols",
    "slice_is_valid",
]
