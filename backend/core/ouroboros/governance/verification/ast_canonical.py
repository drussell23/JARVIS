"""Move 6 Slice 2 — AST-normalized signature canonicalizer.

Computes a canonical sha256 hash of source code's AST that:

  * **Ignores noise**: whitespace, comments (handled by ``ast.parse``)
    + optionally docstrings (env-tunable strip).
  * **Normalizes literal values to type tags**: ``42`` →
    ``<INT>``, ``"hello"`` → ``<STR>``, ``True`` → ``<BOOL>``, etc.
    Two candidates that differ only in literal values produce the
    same signature (e.g., both write a function returning a
    different magic number — semantically equivalent at the
    structural level for Quorum purposes).
  * **Preserves semantics**: symbol names (function/class/method
    defs, attribute access, imports), control flow (if/else/for/
    while/try), type annotations.
  * **Is stable across Python minor versions** via ``ast.dump
    (annotate_fields=True, include_attributes=False)``.

Direct-solve principles:

  * **Asynchronous-ready** — pure-sync compute; safe in any async
    context.
  * **Dynamic** — strip-docstrings + normalize-literals each
    optional via env knobs (Slice 2 default: keep docstrings,
    normalize literals).
  * **Adaptive** — ``compute_multi_file_signature`` for diff
    candidates spanning multiple files (Slice 3 + 4 use this for
    multi-file Quorum).
  * **Intelligent** — type-sentinel mapping covers every
    ``ast.Constant`` payload type (None, bool, int, float, str,
    bytes, complex, Ellipsis, set/frozenset). Defensive
    "<UNKNOWN>" fallback for future Python versions adding new
    Constant payloads.
  * **Robust** — ``compute_ast_signature`` is total: every input
    maps to either a 64-char sha256 hex digest or empty string.
    Syntax errors / empty source / non-string input → empty
    string (Slice 1 treats as no-signal in
    ``compute_consensus``).
  * **No hardcoding** — type tags are module-level constants;
    strip + normalize switches env-tunable.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib ONLY (``ast``, ``hashlib``, ``logging``,
    ``os``, ``typing``).
  * NEVER imports any governance module — pure-stdlib primitive
    consumed by Slice 3+ runners.
  * NEVER references mutation tool names in code.
  * No async functions (Slice 3 introduces async).
  * Read-only — never writes a file, never executes candidate
    code (only ``ast.parse``s it; never runs).

Quorum integration:

  * Slice 3's runner calls ``compute_ast_signature(roll.candidate
    _diff)`` for each ``CandidateRoll`` to populate the
    ``ast_signature`` field.
  * Slice 1's ``compute_consensus`` groups by signature to detect
    convergence/divergence.
  * For multi-file candidates (orchestrator's
    ``_apply_multi_file_candidate`` produces a ``files: [{file_
    path, full_content}, ...]`` shape), Slice 3 uses
    ``compute_multi_file_signature``.
"""
from __future__ import annotations

import ast
import hashlib
import logging
import os
from typing import (
    Any,
    Dict,
    Mapping,
    Tuple,
)

logger = logging.getLogger(__name__)


AST_CANONICAL_SCHEMA_VERSION: str = "ast_canonical.1"


# ---------------------------------------------------------------------------
# Type-sentinel constants — module-level for AST audit + env-knob
# parity. Each ``ast.Constant.value`` is replaced with the
# corresponding sentinel before ``ast.dump`` so candidates that
# differ only in literal values hash identically.
# ---------------------------------------------------------------------------


_TAG_NONE: str = "<NONE>"
_TAG_BOOL: str = "<BOOL>"
_TAG_INT: str = "<INT>"
_TAG_FLOAT: str = "<FLOAT>"
_TAG_STR: str = "<STR>"
_TAG_BYTES: str = "<BYTES>"
_TAG_COMPLEX: str = "<COMPLEX>"
_TAG_ELLIPSIS: str = "<ELLIPSIS>"
_TAG_TUPLE: str = "<TUPLE>"
_TAG_FROZENSET: str = "<FROZENSET>"
_TAG_UNKNOWN: str = "<UNKNOWN>"


# ---------------------------------------------------------------------------
# Env knobs — defaults overridable, never hardcoded behavior constants
# ---------------------------------------------------------------------------


def normalize_literals_default() -> bool:
    """``JARVIS_AST_CANONICAL_NORMALIZE_LITERALS`` (default
    ``true``).

    When true, ``ast.Constant`` values are replaced with type
    sentinels before hashing. Two candidates that differ only in
    literal values hash identically (e.g., ``return 42`` and
    ``return 99`` produce the same signature).

    When false, literal values are preserved verbatim — strict
    equality check. Useful when operators want to detect
    even-tiny-numeric-drift across rolls."""
    raw = os.environ.get(
        "JARVIS_AST_CANONICAL_NORMALIZE_LITERALS", "",
    ).strip().lower()
    if raw == "":
        return True  # default true
    return raw in ("1", "true", "yes", "on")


def strip_docstrings_default() -> bool:
    """``JARVIS_AST_CANONICAL_STRIP_DOCSTRINGS`` (default
    ``false``).

    When false, docstrings are preserved (default — conservative;
    docstring text might be semantically load-bearing). When
    true, the first string statement at the top of every Module/
    FunctionDef/AsyncFunctionDef/ClassDef body is removed before
    hashing. Useful when models produce different docstring
    phrasings for the same logic."""
    raw = os.environ.get(
        "JARVIS_AST_CANONICAL_STRIP_DOCSTRINGS", "",
    ).strip().lower()
    if raw == "":
        return False  # default false
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Internal: literal → type-sentinel mapping
# ---------------------------------------------------------------------------


def _coerce_constant_to_tag(value: Any) -> str:
    """Map an ``ast.Constant.value`` to its type sentinel. NEVER
    raises. Order matters: ``bool`` is a subclass of ``int`` in
    Python, so we check bool BEFORE int."""
    try:
        if value is None:
            return _TAG_NONE
        if value is Ellipsis:
            return _TAG_ELLIPSIS
        # bool is subclass of int — check first
        if isinstance(value, bool):
            return _TAG_BOOL
        if isinstance(value, int):
            return _TAG_INT
        if isinstance(value, float):
            return _TAG_FLOAT
        if isinstance(value, complex):
            return _TAG_COMPLEX
        if isinstance(value, bytes):
            return _TAG_BYTES
        if isinstance(value, str):
            return _TAG_STR
        # Tuple / frozenset can appear in ast.Constant for some
        # literal forms (Python parses certain constant tuples
        # eagerly into ast.Constant nodes in modern Python).
        if isinstance(value, tuple):
            return _TAG_TUPLE
        if isinstance(value, frozenset):
            return _TAG_FROZENSET
        return _TAG_UNKNOWN
    except Exception:  # noqa: BLE001 — defensive
        return _TAG_UNKNOWN


def _normalize_literals_in_place(tree: ast.Module) -> None:
    """Walk AST and replace every ``ast.Constant.value`` with its
    type sentinel. Mutates the tree in place. NEVER raises.

    This is structural canonicalization: two candidates that
    differ only in literal values produce identical AST dumps
    after this pass."""
    try:
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant):
                try:
                    node.value = _coerce_constant_to_tag(
                        node.value,
                    )
                except Exception:  # noqa: BLE001 — defensive
                    node.value = _TAG_UNKNOWN
    except Exception:  # noqa: BLE001 — defensive
        # Walk failure is unlikely but defended; leave tree as-is.
        pass


def _strip_docstrings_in_place(tree: ast.Module) -> None:
    """Remove the docstring (first ``ast.Expr`` wrapping an
    ``ast.Constant`` of type str) from Module / FunctionDef /
    AsyncFunctionDef / ClassDef bodies. Mutates the tree in place.
    NEVER raises.

    If removing the docstring leaves a body empty, pads with
    ``ast.Pass()`` to keep the AST valid."""
    docstring_holder_types = (
        ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
        ast.ClassDef,
    )
    try:
        for node in ast.walk(tree):
            if not isinstance(node, docstring_holder_types):
                continue
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                # Remove docstring; pad with Pass if empty
                node.body = body[1:] if len(body) > 1 else [
                    ast.Pass(),
                ]
    except Exception:  # noqa: BLE001 — defensive
        pass


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def compute_ast_signature(
    source_code: str,
    *,
    normalize_literals: bool = True,
    strip_docstrings: bool = False,
) -> str:
    """Compute canonical sha256 hash of source code's AST.
    Returns 64-char hex digest on success, empty string on any
    failure (caller treats empty as no-signal, mirrors Slice 1
    convention). NEVER raises.

    Decision tree:

      1. Non-string / empty / whitespace-only → ``""``
      2. ``ast.parse`` raises (syntax error) → ``""``
      3. Apply ``_normalize_literals_in_place`` if normalize_literals
      4. Apply ``_strip_docstrings_in_place`` if strip_docstrings
      5. ``ast.dump(tree, annotate_fields=True,
         include_attributes=False)`` for stable canonical form
      6. ``hashlib.sha256(canonical.encode("utf-8")).hexdigest()``
      7. Any unexpected exception → ``""``"""
    if not isinstance(source_code, str):
        return ""
    if not source_code.strip():
        return ""
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return ""
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ASTCanonical] ast.parse raised non-SyntaxError: %s",
            exc,
        )
        return ""
    try:
        if normalize_literals:
            _normalize_literals_in_place(tree)
        if strip_docstrings:
            _strip_docstrings_in_place(tree)
        canonical = ast.dump(
            tree,
            annotate_fields=True,
            include_attributes=False,
        )
        return hashlib.sha256(
            canonical.encode("utf-8"),
        ).hexdigest()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ASTCanonical] canonical hash raised: %s", exc,
        )
        return ""


def compute_multi_file_signature(
    files: Mapping[str, str],
    *,
    normalize_literals: bool = True,
    strip_docstrings: bool = False,
) -> str:
    """Compute combined canonical sha256 across multiple files.
    Each file is hashed independently via
    ``compute_ast_signature``; per-file hashes are joined in
    sorted-key order (deterministic regardless of dict iteration).
    The combined string ``"{path}:{hash}\\n..."`` is then sha256'd.

    Returns 64-char hex digest on success, empty string on:
      * Empty mapping → ``""``
      * Non-mapping input → ``""``
      * Any per-file syntax error → that file contributes empty
        hash, but the combined hash still computes (caller can
        detect partial failures by re-running with single-file
        signature). NEVER raises."""
    if not isinstance(files, Mapping):
        return ""
    if not files:
        return ""
    try:
        per_file_lines = []
        for path in sorted(str(p) for p in files.keys()):
            content = files.get(path, "")
            if not isinstance(content, str):
                content = ""
            per_file_hash = compute_ast_signature(
                content,
                normalize_literals=normalize_literals,
                strip_docstrings=strip_docstrings,
            )
            per_file_lines.append(f"{path}:{per_file_hash}")
        combined = "\n".join(per_file_lines)
        return hashlib.sha256(
            combined.encode("utf-8"),
        ).hexdigest()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[ASTCanonical] multi-file hash raised: %s", exc,
        )
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "AST_CANONICAL_SCHEMA_VERSION",
    "compute_ast_signature",
    "compute_multi_file_signature",
    "normalize_literals_default",
    "strip_docstrings_default",
]
