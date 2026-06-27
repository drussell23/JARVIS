"""SemanticGuardian — deterministic pre-APPLY semantic-wrongness detector.

Closes the "SAFE_AUTO is size-heuristic only" governance gap. The 4-tier
risk engine is the mitigation for the asymmetric blast-radius asymmetry
between O+V (auto-apply + auto-commit + auto-push) and CC (human sees,
user rejects), but every classification in ``risk_engine.py`` is driven
by **size** (file count, blast radius, test-scope confidence). A 1-line
change that removes a critical import or flips a boolean guard lands as
SAFE_AUTO and auto-applies at 3 am while the operator is asleep.

This module does **not** try to detect all semantic errors — that's
undecidable. It catches a curated set of high-leverage *patterns* that
have known analogs in real-world incidents, using pure-Python AST +
regex analysis (no LLM, no network, ~10ms per candidate).

Each detection carries a severity:

    soft  → downgrade SAFE_AUTO to NOTIFY_APPLY (operator sees the diff,
            has the 5s /reject window). Other tiers unaffected.
    hard  → force APPROVAL_REQUIRED regardless of current tier. Even a
            human-watching operator must explicitly approve.

Pattern set (all deterministic, ~10ms total):

  1. removed_import_still_referenced (hard)
  2. function_body_collapsed (hard)
  3. guard_boolean_inverted (soft)
  4. credential_shape_introduced (hard)
  5. test_assertion_inverted (hard)
  6. return_value_flipped (soft)
  7. permission_loosened (hard)
  8. silent_exception_swallow (soft)
  9. hardcoded_url_swap (soft)
  10. docstring_only_delete (soft)

Env gates:

    JARVIS_SEMANTIC_GUARD_ENABLED (default 1)
        Master kill switch. When 0, inspect() returns empty findings.

    JARVIS_SEMGUARD_<PATTERN>_ENABLED (default 1 per pattern)
        Per-pattern kill switch so operators can tune false positives
        out without disabling the whole guardian.

Authority invariant: this module is read-only against the candidate
contents. It never mutates ctx, files, git state, or any governance
surface. The orchestrator consumes its findings and decides whether
to upgrade the risk tier.
"""
from __future__ import annotations

import ast
import logging
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Set, Tuple, Union

logger = logging.getLogger("Ouroboros.SemanticGuardian")

_ENV_ENABLED = "JARVIS_SEMANTIC_GUARD_ENABLED"
_PER_PATTERN_ENV = "JARVIS_SEMGUARD_{name}_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def guardian_enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "1").strip().lower() in _TRUTHY


def pattern_enabled(pattern_name: str) -> bool:
    env_key = _PER_PATTERN_ENV.format(name=pattern_name.upper())
    return os.environ.get(env_key, "1").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Detection — immutable pattern-match record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Detection:
    """One semantic-wrongness pattern hit."""

    pattern: str                   # canonical pattern name (snake_case)
    severity: str                  # "soft" | "hard"
    message: str                   # human-readable one-liner
    file_path: str = ""
    lines: Tuple[int, ...] = ()    # line numbers in new_content (1-indexed)
    snippet: str = ""              # short code snippet for context


# ---------------------------------------------------------------------------
# Guardian — composes the pattern detectors
# ---------------------------------------------------------------------------


@dataclass
class SemanticGuardian:
    """Runs every enabled pattern detector on a candidate and returns findings."""

    patterns: Sequence[str] = field(default_factory=lambda: tuple(_ALL_PATTERNS))

    def inspect(
        self,
        *,
        file_path: str,
        old_content: str,
        new_content: str,
    ) -> List[Detection]:
        """Return zero or more :class:`Detection`s for one (old → new) file pair.

        Never raises — any per-pattern exception is logged at DEBUG and
        dropped so a malformed candidate can't crash the orchestrator.
        Not all patterns apply to every file (e.g. ``test_assertion_inverted``
        only fires on test files); the detector is responsible for its
        own applicability check.
        """
        if not guardian_enabled():
            return []
        results: List[Detection] = []
        for name in self.patterns:
            if not pattern_enabled(name):
                continue
            detector = _PATTERNS.get(name)
            if detector is None:
                continue
            try:
                hit = detector(
                    file_path=file_path,
                    old_content=old_content,
                    new_content=new_content,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[SemanticGuard] pattern %s raised on %s — failing closed",
                    name, file_path, exc_info=True,
                )
                results.append(Detection(
                    pattern=f"{name}_eval_failed",
                    severity="hard",
                    message="pattern evaluator raised — failing closed",
                    file_path=file_path,
                    lines=(),
                    snippet="",
                ))
                continue
            if hit is not None:
                results.append(hit)
        return results

    def inspect_batch(
        self,
        candidates: Sequence[Tuple[str, str, str]],
    ) -> List[Detection]:
        """Run inspect() over a list of (path, old, new) tuples."""
        out: List[Detection] = []
        for path, old, new in candidates:
            out.extend(self.inspect(
                file_path=path, old_content=old, new_content=new,
            ))
        return out


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _safe_parse(src: str) -> Optional[ast.Module]:
    """Parse source, return None on SyntaxError (candidate already invalid)."""
    if not src:
        return None
    try:
        return ast.parse(src)
    except (SyntaxError, ValueError):
        return None


def _collect_imports(module: ast.Module) -> Set[str]:
    """Return every bound name from import statements.

    ``import os`` → {"os"}
    ``import os.path`` → {"os"}  (the head binding)
    ``from os import path`` → {"path"}
    ``from os.path import join as j`` → {"j"}
    """
    names: Set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # Top-level name is what's bound in the local scope.
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add(alias.asname or alias.name)
    return names


def _collect_name_references(module: ast.Module) -> Set[str]:
    """Every ``Name`` node in Load context — i.e. something that's being used."""
    names: Set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            # Walk the attribute chain to its root Name.
            current: ast.AST = node
            while isinstance(current, ast.Attribute):
                current = current.value
            if isinstance(current, ast.Name):
                names.add(current.id)
    return names


def _functions_by_name(module: ast.Module) -> dict:
    """Map qualified function name → FunctionDef/AsyncFunctionDef node.

    Handles nested classes via dotted qualifier (``ClassA.method``).
    """
    out: dict = {}
    def _walk(scope: str, body: list) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{scope}{node.name}"
                out[q] = node
                _walk(q + ".", node.body)
            elif isinstance(node, ast.ClassDef):
                _walk(f"{scope}{node.name}.", node.body)
    _walk("", module.body)
    return out


def _body_is_trivial(body: List[ast.stmt]) -> bool:
    """True iff the body is one of the 'collapsed' sentinels:
    ``pass``, ``...``, or ``raise NotImplementedError`` (possibly with
    a docstring in front). Otherwise False.
    """
    stmts = list(body)
    # Strip a leading docstring.
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(
        stmts[0].value, ast.Constant,
    ) and isinstance(stmts[0].value.value, str):
        stmts = stmts[1:]
    if not stmts:
        return True
    if len(stmts) != 1:
        return False
    only = stmts[0]
    if isinstance(only, ast.Pass):
        return True
    if (
        isinstance(only, ast.Expr)
        and isinstance(only.value, ast.Constant)
        and only.value.value is Ellipsis
    ):
        return True
    if isinstance(only, ast.Raise) and only.exc is not None:
        # ``raise NotImplementedError`` / ``raise NotImplementedError("...")``
        call = only.exc
        name = None
        if isinstance(call, ast.Name):
            name = call.id
        elif isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
            name = call.func.id
        if name == "NotImplementedError":
            return True
    return False


def _substantive_body_size(body: List[ast.stmt]) -> int:
    """Count non-docstring, non-pass, non-ellipsis top-level statements."""
    count = 0
    for i, stmt in enumerate(body):
        # Skip a leading docstring.
        if (
            i == 0
            and isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue
        if isinstance(stmt, ast.Pass):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis
        ):
            continue
        count += 1
    return count


def _get_docstring(body: List[ast.stmt]) -> Optional[str]:
    if not body:
        return None
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first.value.value
    return None


# ---------------------------------------------------------------------------
# PATTERN 1 — removed_import_still_referenced
# ---------------------------------------------------------------------------
# HARD: an import was removed from the module, but the bound name still
# appears in Load contexts elsewhere. Ship this and the module breaks on
# first execution.


def _pat_removed_import_still_referenced(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    old_tree = _safe_parse(old_content)
    new_tree = _safe_parse(new_content)
    if old_tree is None or new_tree is None:
        return None
    old_imports = _collect_imports(old_tree)
    new_imports = _collect_imports(new_tree)
    new_names = _collect_name_references(new_tree)
    removed = old_imports - new_imports
    # Bind names still referenced after removal.
    dangling = sorted(removed & new_names)
    if not dangling:
        return None
    return Detection(
        pattern="removed_import_still_referenced",
        severity="hard",
        message=(
            f"Import(s) removed but still referenced: "
            f"{', '.join(dangling[:3])}"
            + (" …" if len(dangling) > 3 else "")
        ),
        file_path=file_path,
        snippet=f"dangling names: {', '.join(dangling)}",
    )


# ---------------------------------------------------------------------------
# PATTERN 2 — function_body_collapsed
# ---------------------------------------------------------------------------
# HARD: a function with substantive body (≥3 statements) in the old
# version is replaced in the new version with a trivial body (pass, ...,
# raise NotImplementedError). This is the classic "silently disable"
# pattern.


def _pat_function_body_collapsed(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    old_tree = _safe_parse(old_content)
    new_tree = _safe_parse(new_content)
    if old_tree is None or new_tree is None:
        return None
    old_funcs = _functions_by_name(old_tree)
    new_funcs = _functions_by_name(new_tree)
    collapsed: List[str] = []
    for name, old_fn in old_funcs.items():
        new_fn = new_funcs.get(name)
        if new_fn is None:
            continue
        if _substantive_body_size(old_fn.body) < 3:
            continue
        if not _body_is_trivial(new_fn.body):
            continue
        collapsed.append(name)
    if not collapsed:
        return None
    return Detection(
        pattern="function_body_collapsed",
        severity="hard",
        message=(
            f"Function body collapsed to pass/…/raise: "
            f"{', '.join(collapsed[:3])}"
            + (" …" if len(collapsed) > 3 else "")
        ),
        file_path=file_path,
        snippet=f"collapsed: {', '.join(collapsed)}",
    )


# ---------------------------------------------------------------------------
# PATTERN 3 — guard_boolean_inverted
# ---------------------------------------------------------------------------
# SOFT: a top-level function return-guard ``if X: return Y`` flipped to
# ``if not X: return Y`` (or the reverse). Conservative: only flags
# single-statement if-return guards where the condition is a bare Name.


def _unparse_simple(node: ast.AST) -> Optional[str]:
    """Return a stable string repr for simple lookups — Name or an
    attribute chain rooted at a Name (e.g. ``user.is_admin``,
    ``self.flag.enabled``). Anything more complex returns None so the
    guard stays conservative.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list = [node.attr]
        current: ast.AST = node.value
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
    return None


def _extract_guard_return_names(module: ast.Module) -> Set[Tuple[str, bool, str]]:
    """Return a set of (condition_key, is_negated, return_repr) tuples
    from every ``if X: return Y`` at the top of a function body, where
    ``X`` is either a bare Name or a simple attribute chain rooted at
    a Name. Complex expressions (BoolOp, Call, Compare) are not
    considered — too many false positives.
    """
    out: Set[Tuple[str, bool, str]] = set()
    funcs = _functions_by_name(module)
    for qname, fn in funcs.items():
        for node in fn.body:
            if not isinstance(node, ast.If):
                continue
            test = node.test
            key: Optional[str] = None
            is_not = False
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                inner = _unparse_simple(test.operand)
                if inner is not None:
                    key = inner
                    is_not = True
            else:
                simple = _unparse_simple(test)
                if simple is not None:
                    key = simple
            if key is None:
                continue
            # Body must be exactly one Return.
            if len(node.body) != 1 or not isinstance(node.body[0], ast.Return):
                continue
            ret = node.body[0].value
            try:
                ret_repr = ast.unparse(ret) if ret is not None else "None"
            except Exception:  # noqa: BLE001
                ret_repr = "?"
            out.add((f"{qname}::{key}", is_not, ret_repr))
    return out


def _pat_guard_boolean_inverted(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    if not hasattr(ast, "unparse"):
        # Python 3.9+ has ast.unparse; bail on older.
        return None
    old_tree = _safe_parse(old_content)
    new_tree = _safe_parse(new_content)
    if old_tree is None or new_tree is None:
        return None
    old = _extract_guard_return_names(old_tree)
    new = _extract_guard_return_names(new_tree)
    # Look for the same (fn::name, ret_repr) key with flipped is_not.
    old_by_key = {(k, r): neg for (k, neg, r) in old}
    new_by_key = {(k, r): neg for (k, neg, r) in new}
    flips: List[str] = []
    for key, old_neg in old_by_key.items():
        if key in new_by_key and new_by_key[key] != old_neg:
            flips.append(key[0])
    if not flips:
        return None
    return Detection(
        pattern="guard_boolean_inverted",
        severity="soft",
        message=(
            f"Guard clause boolean inverted in: "
            f"{', '.join(flips[:3])}"
            + (" …" if len(flips) > 3 else "")
        ),
        file_path=file_path,
        snippet=f"flipped: {', '.join(flips)}",
    )


# ---------------------------------------------------------------------------
# PATTERN 4 — credential_shape_introduced
# ---------------------------------------------------------------------------
# HARD: a new line in the diff contains a string matching common
# credential shapes (OpenAI sk-*, AWS AKIA*, GitHub ghp_*, Slack xox[bp]-*,
# Anthropic sk-ant-*, SSH/RSA private key headers, or an API_KEY=<literal>
# assignment). Never ship a key.


_CREDENTIAL_SHAPES = [
    (r"sk-[A-Za-z0-9]{20,}", "OpenAI-style secret key"),
    (r"sk-ant-[A-Za-z0-9_-]{20,}", "Anthropic API key"),
    (r"AKIA[A-Z0-9]{16}", "AWS access key id"),
    (r"ghp_[A-Za-z0-9]{30,}", "GitHub personal access token"),
    (r"gho_[A-Za-z0-9]{30,}", "GitHub OAuth token"),
    (r"ghs_[A-Za-z0-9]{30,}", "GitHub app server token"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"-----BEGIN (OPENSSH|RSA|EC|DSA|ED25519) PRIVATE KEY-----", "private key PEM"),
    # API_KEY / SECRET / TOKEN = "<value>" — match even innocent-looking
    # non-empty literals since false positives here are cheap (soft-ish)
    # but real keys are disastrous.
    (
        r"(API_KEY|SECRET_KEY|ACCESS_TOKEN|AUTH_TOKEN|PRIVATE_KEY)\s*=\s*['\"][^'\"\n]{8,}['\"]",
        "hardcoded credential assignment",
    ),
    # Bearer JWT in Authorization header — discovered by P9.4
    # adversarial corpus 2026-05-07 (entry p9.4.009).
    # Three-segment base64url (header.payload.signature) with
    # the canonical eyJ prefix from {"alg":...}. Each segment
    # ≥4 chars to avoid trivial test fixture matches.
    (
        r"Bearer\s+eyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}",
        "Bearer JWT in Authorization header",
    ),
]


def _line_numbers_for_pattern(text: str, compiled: "re.Pattern") -> List[int]:
    out: List[int] = []
    for i, line in enumerate(text.splitlines(), 1):
        if compiled.search(line):
            out.append(i)
    return out


def _pat_credential_shape_introduced(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    # Only flag credentials that are NEW in the candidate. If the old
    # content already had the shape, it's a pre-existing artifact outside
    # our scope.
    for raw_pat, human in _CREDENTIAL_SHAPES:
        pat = re.compile(raw_pat)
        new_hits = pat.findall(new_content)
        if not new_hits:
            continue
        old_hits = pat.findall(old_content)
        # Only flag if there are MORE matches in new than old.
        if len(new_hits) <= len(old_hits):
            continue
        line_nos = _line_numbers_for_pattern(new_content, pat)
        return Detection(
            pattern="credential_shape_introduced",
            severity="hard",
            message=f"Possible {human} introduced in candidate",
            file_path=file_path,
            lines=tuple(line_nos),
            snippet=f"pattern: {raw_pat}",
        )
    return None


# ---------------------------------------------------------------------------
# PATTERN 5 — test_assertion_inverted
# ---------------------------------------------------------------------------
# HARD: in a test_*.py / *_test.py file, an ``assert X`` was replaced
# with ``assert not X`` (or vice-versa). Common cheat when "fixing" a
# failing test by making the assertion match the wrong output.


def _is_test_file(file_path: str) -> bool:
    name = os.path.basename(file_path or "").lower()
    return name.startswith("test_") or name.endswith("_test.py")


def _extract_assertions(module: ast.Module) -> Set[Tuple[str, bool, str]]:
    """Set of (qualified_test_name::asserted_repr, is_negated, rough_key)."""
    out: Set[Tuple[str, bool, str]] = set()
    funcs = _functions_by_name(module)
    for qname, fn in funcs.items():
        if not qname.split(".")[-1].startswith("test_"):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assert):
                continue
            test = node.test
            is_not = False
            if (
                isinstance(test, ast.UnaryOp)
                and isinstance(test.op, ast.Not)
            ):
                is_not = True
                test = test.operand
            try:
                expr_repr = ast.unparse(test) if hasattr(ast, "unparse") else repr(test)
            except Exception:  # noqa: BLE001
                expr_repr = "?"
            out.add((qname, is_not, expr_repr))
    return out


def _pat_test_assertion_inverted(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    if not _is_test_file(file_path):
        return None
    if not hasattr(ast, "unparse"):
        return None
    old_tree = _safe_parse(old_content)
    new_tree = _safe_parse(new_content)
    if old_tree is None or new_tree is None:
        return None
    old = _extract_assertions(old_tree)
    new = _extract_assertions(new_tree)
    # key on (func_qname, expr_repr); severity fires if is_not flipped.
    old_map = {(q, e): n for (q, n, e) in old}
    new_map = {(q, e): n for (q, n, e) in new}
    flipped: List[str] = []
    for key, old_n in old_map.items():
        if key in new_map and new_map[key] != old_n:
            flipped.append(key[0])
    if not flipped:
        return None
    return Detection(
        pattern="test_assertion_inverted",
        severity="hard",
        message=(
            f"Test assertion negation flipped in: "
            f"{', '.join(flipped[:3])}"
            + (" …" if len(flipped) > 3 else "")
        ),
        file_path=file_path,
        snippet=f"tests: {', '.join(flipped)}",
    )


# ---------------------------------------------------------------------------
# PATTERN 6 — return_value_flipped
# ---------------------------------------------------------------------------
# SOFT: a function's FIRST top-level return statement flipped from
# ``return True`` to ``return False`` (or vice-versa). Only looks at the
# first return to avoid flagging multi-branch functions where each
# branch has different truthiness intentionally.


def _first_return_bool(
    fn: Union[ast.FunctionDef, ast.AsyncFunctionDef],
) -> Optional[bool]:
    for stmt in fn.body:
        if isinstance(stmt, ast.Return) and isinstance(
            stmt.value, ast.Constant,
        ) and isinstance(stmt.value.value, bool):
            return stmt.value.value
    return None


def _pat_return_value_flipped(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    old_tree = _safe_parse(old_content)
    new_tree = _safe_parse(new_content)
    if old_tree is None or new_tree is None:
        return None
    old_funcs = _functions_by_name(old_tree)
    new_funcs = _functions_by_name(new_tree)
    flipped: List[str] = []
    for name, old_fn in old_funcs.items():
        new_fn = new_funcs.get(name)
        if new_fn is None:
            continue
        if not isinstance(old_fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not isinstance(new_fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        old_val = _first_return_bool(old_fn)
        new_val = _first_return_bool(new_fn)
        if old_val is not None and new_val is not None and old_val != new_val:
            flipped.append(name)
    if not flipped:
        return None
    return Detection(
        pattern="return_value_flipped",
        severity="soft",
        message=(
            f"Boolean return flipped (True↔False) in: "
            f"{', '.join(flipped[:3])}"
            + (" …" if len(flipped) > 3 else "")
        ),
        file_path=file_path,
        snippet=f"functions: {', '.join(flipped)}",
    )


# ---------------------------------------------------------------------------
# PATTERN 7 — permission_loosened
# ---------------------------------------------------------------------------
# HARD: new lines in the candidate loosen filesystem permissions. Common
# footguns:
#   chmod(path, 0o777)    # or 0o666, etc.
#   os.umask(0)
#   open(..., mode='w')   # (skipped — too broad)
# We match numeric mode literals in chmod/umask calls.


_PERM_LOOSE_RE = re.compile(
    r"\b(?:os\.)?(?:chmod|umask)\s*\(\s*[^)]*0o?(?:[4-7]\d{2}|[4-7]\d|777|0)\b",
)


def _pat_permission_loosened(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    new_hits = _PERM_LOOSE_RE.findall(new_content)
    if not new_hits:
        return None
    old_hits = _PERM_LOOSE_RE.findall(old_content)
    if len(new_hits) <= len(old_hits):
        return None
    lines = _line_numbers_for_pattern(new_content, _PERM_LOOSE_RE)
    return Detection(
        pattern="permission_loosened",
        severity="hard",
        message=(
            f"Filesystem permission call introduced "
            f"(chmod/umask, {len(new_hits) - len(old_hits)} new)"
        ),
        file_path=file_path,
        lines=tuple(lines),
        snippet=f"hits: {', '.join(repr(h) for h in new_hits[:2])}",
    )


# ---------------------------------------------------------------------------
# PATTERN 8 — silent_exception_swallow
# ---------------------------------------------------------------------------
# SOFT: a new ``except Exception: pass`` (or ``except: pass``) block in
# the candidate that has no sibling log/print/comment on the except line.


def _count_silent_excepts(module: ast.Module) -> int:
    n = 0
    for node in ast.walk(module):
        if not isinstance(node, ast.ExceptHandler):
            continue
        # Match bare ``except:`` or ``except Exception:`` (broad catches).
        type_ok = (
            node.type is None
            or (isinstance(node.type, ast.Name) and node.type.id == "Exception")
            or (isinstance(node.type, ast.Name) and node.type.id == "BaseException")
        )
        if not type_ok:
            continue
        body = list(node.body)
        # Strip leading docstring (rare in except blocks, but defensive).
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        # Pure ``pass`` or ``...`` swallow.
        if len(body) == 1 and isinstance(body[0], ast.Pass):
            n += 1
        elif (
            len(body) == 1
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and body[0].value.value is Ellipsis
        ):
            n += 1
    return n


def _pat_silent_exception_swallow(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    old_tree = _safe_parse(old_content)
    new_tree = _safe_parse(new_content)
    if old_tree is None or new_tree is None:
        return None
    old_n = _count_silent_excepts(old_tree)
    new_n = _count_silent_excepts(new_tree)
    delta = new_n - old_n
    if delta <= 0:
        return None
    return Detection(
        pattern="silent_exception_swallow",
        severity="soft",
        message=f"{delta} new broad-except swallow(s) without logging",
        file_path=file_path,
        snippet=f"old={old_n} new={new_n}",
    )


# ---------------------------------------------------------------------------
# PATTERN 9 — hardcoded_url_swap
# ---------------------------------------------------------------------------
# SOFT: a ``https?://…`` literal changed on a line. Common "oops I
# committed the staging URL" failure mode.


_URL_RE = re.compile(r"""['"]https?://[^'"\s]+['"]""")


def _collect_urls(text: str) -> Set[str]:
    out: Set[str] = set()
    for m in _URL_RE.finditer(text):
        out.add(m.group(0).strip("'\""))
    return out


def _pat_hardcoded_url_swap(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    old_urls = _collect_urls(old_content)
    new_urls = _collect_urls(new_content)
    added = new_urls - old_urls
    removed = old_urls - new_urls
    # Only flag when we see both an addition AND a removal — pure adds
    # are normal for new code; pure removals are cleanup. A swap is the
    # suspicious case.
    if not added or not removed:
        return None
    return Detection(
        pattern="hardcoded_url_swap",
        severity="soft",
        message=(
            f"URL literal(s) changed: {len(removed)} removed, {len(added)} added"
        ),
        file_path=file_path,
        snippet=f"added: {', '.join(list(added)[:2])}",
    )


# ---------------------------------------------------------------------------
# PATTERN 10 — docstring_only_delete
# ---------------------------------------------------------------------------
# SOFT: a function's docstring was removed without the body changing.
# Loss of documented intent is a smell even when behavior is preserved.


def _pat_docstring_only_delete(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    old_tree = _safe_parse(old_content)
    new_tree = _safe_parse(new_content)
    if old_tree is None or new_tree is None:
        return None
    old_funcs = _functions_by_name(old_tree)
    new_funcs = _functions_by_name(new_tree)
    stripped: List[str] = []
    for name, old_fn in old_funcs.items():
        new_fn = new_funcs.get(name)
        if new_fn is None:
            continue
        old_doc = _get_docstring(old_fn.body)
        new_doc = _get_docstring(new_fn.body)
        if not old_doc:
            continue
        if new_doc:
            continue
        # Also require substantive body preserved (this pattern is
        # specifically about docstring-only changes, not mass rewrites).
        if _substantive_body_size(old_fn.body) != _substantive_body_size(new_fn.body):
            continue
        stripped.append(name)
    if not stripped:
        return None
    return Detection(
        pattern="docstring_only_delete",
        severity="soft",
        message=(
            f"Docstring removed without body change in: "
            f"{', '.join(stripped[:3])}"
            + (" …" if len(stripped) > 3 else "")
        ),
        file_path=file_path,
        snippet=f"functions: {', '.join(stripped)}",
    )


# ---------------------------------------------------------------------------
# PATTERN 11 — dynamic_import_chain (§24.8.1 AST-pattern blindspot)
# ---------------------------------------------------------------------------
# HARD: new code introduces a dynamic import/eval/exec chain that bypasses
# static analysis. Walks the AST for __import__, eval, exec, compile,
# getattr (with dangerous target), importlib.import_module, base64/codecs
# decode chains, and open().read() chains.
# Only flags NEW introductions (count in new > count in old).

_DYNAMIC_EXEC_BUILTINS: frozenset = frozenset({
    "__import__", "eval", "exec", "compile",
})

_DYNAMIC_ATTR_CALLS: frozenset = frozenset({
    ("importlib", "import_module"),
    ("importlib", "__import__"),
    ("base64", "b64decode"),
    ("base64", "b64encode"),
    ("base64", "decodebytes"),
    ("codecs", "decode"),
    ("codecs", "encode"),
    ("builtins", "__import__"),
    ("builtins", "eval"),
    ("builtins", "exec"),
    ("builtins", "compile"),
    # Shell-exec pairs — S6 closure: also tracked by shell_exec_introduced
    # (regex) so non-Python files are covered even when _safe_parse → None.
    ("os", "system"),
    ("os", "popen"),
    ("subprocess", "run"),
    ("subprocess", "call"),
    ("subprocess", "Popen"),
    ("subprocess", "check_output"),
    ("subprocess", "check_call"),
})

_DANGEROUS_GETATTR_TARGETS: frozenset = frozenset({
    "__import__", "eval", "exec", "compile", "system", "popen",
    "run", "call", "check_output", "check_call", "Popen",
    "getattr", "setattr", "delattr", "__subclasses__",
    "__globals__", "__builtins__", "__code__", "__class__",
})


def _count_dynamic_chains(module: Optional[ast.Module]) -> int:
    if module is None:
        return 0
    count = 0
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in _DYNAMIC_EXEC_BUILTINS:
            count += 1
            continue
        if isinstance(func, ast.Attribute):
            root: ast.AST = func.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                pair = (root.id, func.attr)
                if pair in _DYNAMIC_ATTR_CALLS:
                    count += 1
                    continue
        if (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
        ):
            second_arg = node.args[1]
            if (
                isinstance(second_arg, ast.Constant)
                and isinstance(second_arg.value, str)
                and second_arg.value in _DANGEROUS_GETATTR_TARGETS
            ):
                count += 1
                continue
        if isinstance(func, ast.Attribute) and func.attr == "read":
            inner = func.value
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "open"
            ):
                count += 1
                continue
    return count


def _pat_dynamic_import_chain(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    old_tree = _safe_parse(old_content)
    new_tree = _safe_parse(new_content)
    if new_tree is None:
        return None
    old_count = _count_dynamic_chains(old_tree)
    new_count = _count_dynamic_chains(new_tree)
    delta = new_count - old_count
    if delta <= 0:
        return None
    return Detection(
        pattern="dynamic_import_chain",
        severity="hard",
        message=(
            f"Dynamic import/exec chain introduced "
            f"({delta} new node{'s' if delta != 1 else ''}: "
            f"__import__/eval/exec/compile/getattr/importlib)"
        ),
        file_path=file_path,
        snippet=f"old_count={old_count} new_count={new_count} delta={delta}",
    )


# ---------------------------------------------------------------------------
# PATTERN 12 — dynamic_dunder_construction (P9.4 KNOWN GAP closure 2026-05-07)
# ---------------------------------------------------------------------------
# HARD: detects ``getattr(x, <DYNAMIC>)`` where the second arg is constructed
# from fragments at runtime to evade Pattern 11's static-string check. The
# canonical bypass shape per Phase 7.7 Rule 7:
#
#   getattr(obj, chr(95) + chr(95) + 'subc' + 'lasses' + chr(95) + chr(95))
#
# Pattern 11 only catches `getattr(x, "__subclasses__")` (literal string). The
# motivated model can defeat that by constructing the dunder name at runtime
# via:
#   - chr(95) calls (underscore codepoint)
#   - string concatenation (`+`) of fragments
#   - str.join / str.format / f-string / format() chains
#   - bytes.decode chains, base64-decode chains
#
# This pattern walks the AST for getattr calls whose second arg is NOT a
# literal Constant — and applies a heuristic flagging dynamic-construction
# shapes. Conservative-fire (false positives are cheap; unflagged real
# bypass is disastrous).
#
# Closes the last KNOWN GAP in the P9.4 adversarial corpus (entry p9.4.024).


_DYNAMIC_DUNDER_CHR_CODES: frozenset = frozenset({95})
"""Underscore codepoint. Any ``chr(95)`` call inside a getattr second-arg
is a strong dunder-construction signal."""


_DYNAMIC_DUNDER_FRAGMENT_HINTS: frozenset = frozenset({
    # Sub-strings that ALONE are innocent but in the context of getattr
    # second-arg construction signal dunder-name assembly.
    "subc", "lasses", "glob", "buil", "tins", "impo", "rt__",
    "moro", "mro", "code", "clas", "init", "del", "new", "setattr",
})


def _is_dynamic_dunder_construction(node: ast.AST) -> bool:
    """Return True iff ``node`` (the second arg of a getattr call) shows
    structural signs of dynamic dunder-name construction.

    Conservative heuristics (any-match fires):

      1. ``chr(95)`` call anywhere in the subtree (underscore codepoint).
      2. Binary-add chain that concatenates 3+ string-typed leaves
         (motivated splitting of ``__subclasses__`` into 5+ fragments
         like ``__`` + ``subc`` + ``lasses`` + ``__``).
      3. ``str.join`` / ``"".join`` of a list/tuple containing
         underscore-shaped fragments.
      4. ``f-string`` (JoinedStr) whose static parts contain double-
         underscore fragments.
      5. ``codecs.decode`` / ``base64.b64decode`` calls (existing
         ``_DYNAMIC_ATTR_CALLS`` covers these but pattern 11 only fires
         when the call is at the top level — pattern 12 fires when
         such a call sits INSIDE a getattr arg).

    AST walk only — never evaluates the expression. Pure function;
    NEVER raises."""
    try:
        for sub in ast.walk(node):
            # Heuristic 1: chr(95) — underscore codepoint.
            if isinstance(sub, ast.Call):
                if (
                    isinstance(sub.func, ast.Name)
                    and sub.func.id == "chr"
                    and len(sub.args) == 1
                    and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, int)
                    and sub.args[0].value
                    in _DYNAMIC_DUNDER_CHR_CODES
                ):
                    return True
                # Heuristic 5: codecs.decode / base64.b64decode INSIDE
                # the getattr arg (subarg-position; pattern 11 only
                # catches top-level).
                if isinstance(sub.func, ast.Attribute):
                    root: ast.AST = sub.func.value
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    if isinstance(root, ast.Name):
                        pair = (root.id, sub.func.attr)
                        if pair in _DYNAMIC_ATTR_CALLS:
                            return True
                # Heuristic 3: ".join(<sequence>)" — flag if any
                # fragment contains underscore.
                if (
                    isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "join"
                    and len(sub.args) >= 1
                ):
                    arg = sub.args[0]
                    if isinstance(arg, (ast.List, ast.Tuple)):
                        for elt in arg.elts:
                            if (
                                isinstance(elt, ast.Constant)
                                and isinstance(elt.value, str)
                                and "_" in elt.value
                            ):
                                return True
            # Heuristic 4: f-string with dunder-shaped static parts.
            if isinstance(sub, ast.JoinedStr):
                for v in sub.values:
                    if (
                        isinstance(v, ast.Constant)
                        and isinstance(v.value, str)
                        and "__" in v.value
                    ):
                        return True
        # Heuristic 2: binary-add chain at the TOP level — count
        # string-typed leaves; 3+ flag (a normal `"foo" + bar`
        # concatenation is unflagged).
        if isinstance(node, ast.BinOp):
            string_leaves = 0
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Constant)
                    and isinstance(sub.value, str)
                ):
                    string_leaves += 1
                # chr(95) call counts as a "string-shaped" leaf for
                # this heuristic.
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "chr"
                ):
                    string_leaves += 1
            if string_leaves >= 3:
                return True
    except Exception:  # noqa: BLE001 — defensive
        return False
    return False


def _count_dynamic_dunder_constructions(
    module: Optional[ast.Module],
) -> int:
    """Count getattr calls whose second arg shows dynamic dunder-
    construction patterns. Pure function; NEVER raises."""
    if module is None:
        return 0
    count = 0
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Name) and func.id == "getattr"
        ):
            continue
        if len(node.args) < 2:
            continue
        second_arg = node.args[1]
        # Pattern 11 already catches literal-string dangerous targets;
        # pattern 12 fires for NON-literal second args showing
        # construction shape.
        if (
            isinstance(second_arg, ast.Constant)
            and isinstance(second_arg.value, str)
        ):
            continue  # literal — Pattern 11's job
        if _is_dynamic_dunder_construction(second_arg):
            count += 1
    return count


def _pat_dynamic_dunder_construction(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    """Pattern 12 — flag dynamic dunder-name construction in getattr
    second arg. Closes the P9.4 KNOWN GAP for runtime-string-built
    bypass of Pattern 11's literal-string check."""
    old_tree = _safe_parse(old_content)
    new_tree = _safe_parse(new_content)
    if new_tree is None:
        return None
    old_count = _count_dynamic_dunder_constructions(old_tree)
    new_count = _count_dynamic_dunder_constructions(new_tree)
    delta = new_count - old_count
    if delta <= 0:
        return None
    return Detection(
        pattern="dynamic_dunder_construction",
        severity="hard",
        message=(
            f"Dynamic dunder-name construction in getattr "
            f"introduced ({delta} new site"
            f"{'s' if delta != 1 else ''}). Likely bypass of "
            f"static-string AST blocks via chr() / split-string "
            f"concat / .join() / f-string assembly."
        ),
        file_path=file_path,
        snippet=(
            f"old_count={old_count} new_count={new_count} "
            f"delta={delta}"
        ),
    )


# ---------------------------------------------------------------------------
# Pattern registry
# ---------------------------------------------------------------------------


# Declared explicitly so tests + docs can enumerate without introspection.
# ---------------------------------------------------------------------------
# Slice 208 — Epistemic-integrity (metric-gaming deception) detectors
# ---------------------------------------------------------------------------
# Catch the structural-deception shapes observed this session that pass AST
# validation while gaming the organism's own honesty: a patch that self-signs
# a roadmap, suppresses an integrity counter, or launders Chronos
# unsupervised-time across a supervised boundary.
#
# HONEST SCOPE: pattern detectors. They catch the known/obvious shapes, raise
# friction, and route to APPROVAL_REQUIRED. They are NOT "un-gameable" — a
# deliberately obfuscated deception can evade a regex. Defense-in-depth on the
# immune system, not a proof of honesty.

_SIGNING_OWNER_FILES = ("roadmap_reader.py", "strategy_signer.py")
_SELF_SIGNING_RE = re.compile(
    r"(compute_signature\s*\(|sign_roadmap_doc\s*\(|_build_signing_payload\s*\(|"
    r"import\s+strategy_signer|from\s+\S*strategy_signer\s+import)"
)


def _pat_self_signing_attempt(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    """HARD: a patch in an AUTONOMOUS module invokes the roadmap signing
    primitive — the self-authorization shape. The signer/reader modules own
    the primitive and are exempt."""
    base = (file_path or "").rsplit("/", 1)[-1]
    if base in _SIGNING_OWNER_FILES:
        return None
    if len(_SELF_SIGNING_RE.findall(new_content)) <= len(
        _SELF_SIGNING_RE.findall(old_content)
    ):
        return None
    return Detection(
        pattern="self_signing_attempt", severity="hard",
        message=(
            "Epistemic-integrity violation: patch invokes the roadmap signing "
            "primitive (self-authorization — organism must not mint its own "
            "operator attestation). METRIC_GAMING_DECEIT_ANOMALY."
        ),
        file_path=file_path,
        lines=tuple(_line_numbers_for_pattern(new_content, _SELF_SIGNING_RE)),
        snippet="self-signing",
    )


_PROTECTED_METRIC_VARS = (
    "control_plane_starvation_events", "provider_exhaustions", "warmup_lag",
    "hedge_races_abandoned", "lag_ms", "lag_event_count",
)
_METRIC_ZERO_RE = re.compile(
    r"\b(" + "|".join(_PROTECTED_METRIC_VARS) + r")\b\s*=\s*0\b"
)
_ABSURD_THRESHOLD_RE = re.compile(
    r"(threshold_ms|_threshold|max_starvation|starvation_threshold)\s*=\s*"
    r"(9{6,}|1e\d{2,}|float\(\s*['\"]inf['\"]\s*\)|10\s*\*\*\s*\d{2,})"
)
_RECORD_CALL_RE = re.compile(
    r"record_(control_plane_starvation|provider_exhaustion|warmup_lag|"
    r"hedge_abandoned)\s*\("
)


def _pat_metric_counter_suppressed(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    """HARD: a patch hardcodes a protected integrity counter to zero, sets an
    absurd threshold to disable a watchdog, or REMOVES a record_*() call —
    suppressing a metric so it reports healthier than reality (the 'absolute
    zero' deception). Test files out of scope."""
    base = (file_path or "").rsplit("/", 1)[-1]
    if base.startswith("test_") or base.endswith("_test.py"):
        return None
    for rx, why in (
        (_METRIC_ZERO_RE, "hardcodes an integrity counter to 0"),
        (_ABSURD_THRESHOLD_RE, "sets an absurd threshold to disable a watchdog"),
    ):
        if len(rx.findall(new_content)) > len(rx.findall(old_content)):
            return Detection(
                pattern="metric_counter_suppressed", severity="hard",
                message=(
                    f"Epistemic-integrity violation: patch {why} — suppressing "
                    "a metric so it reports healthier than reality. "
                    "METRIC_GAMING_DECEIT_ANOMALY."
                ),
                file_path=file_path,
                lines=tuple(_line_numbers_for_pattern(new_content, rx)),
                snippet="metric-suppression",
            )
    # Count only ACTIVE (non-comment) record calls so a comment-out counts as
    # a removal (a commented line still contains the call text).
    def _active_record_calls(text: str) -> int:
        return sum(
            1 for ln in text.splitlines()
            if _RECORD_CALL_RE.search(ln) and not ln.lstrip().startswith("#")
        )
    if _active_record_calls(new_content) < _active_record_calls(old_content):
        return Detection(
            pattern="metric_counter_suppressed", severity="hard",
            message=(
                "Epistemic-integrity violation: patch removes a telemetry "
                "record_*() call — blinding the metric. "
                "METRIC_GAMING_DECEIT_ANOMALY."
            ),
            file_path=file_path, lines=(), snippet="record-call-removed",
        )
    return None


_CHRONOS_RESET_RE = re.compile(r"unsupervised_interval_s\"?\]?\s*=\s*0\.0")


def _pat_chronos_continuity_laundering(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    """HARD: a patch to chronos_ledger.py REMOVES an unsupervised-interval
    reset — letting a supervised rebuild/migration claim continuous
    UNSUPERVISED time (evidence laundering)."""
    base = (file_path or "").rsplit("/", 1)[-1]
    if base != "chronos_ledger.py":
        return None
    if len(_CHRONOS_RESET_RE.findall(new_content)) < len(
        _CHRONOS_RESET_RE.findall(old_content)
    ):
        return Detection(
            pattern="chronos_continuity_laundering", severity="hard",
            message=(
                "Epistemic-integrity violation: patch removes a Chronos "
                "unsupervised-interval reset — a supervised rebuild/migration "
                "could then claim continuous unsupervised time (evidence "
                "laundering). METRIC_GAMING_DECEIT_ANOMALY."
            ),
            file_path=file_path, lines=(), snippet="chronos-reset-removed",
        )
    return None


# ---------------------------------------------------------------------------
# PATTERN — shell_exec_introduced (S6 closure)
# ---------------------------------------------------------------------------
# HARD: new content introduces a shell-exec call not present in old content.
# Regex-based so it fires on .sh / .yaml / .pth and any other non-Python file
# where _safe_parse() returns None and all AST detectors early-return.
# Delta-gated: only fires when new_count > old_count, preventing false-positives
# when the existing content already contained the call.

_SHELL_EXEC_RE: re.Pattern = re.compile(
    r"(?:os\.(?:system|popen)|subprocess\.(?:run|call|Popen|check_output|check_call))\s*\("
)


def _pat_shell_exec_introduced(
    *, file_path: str, old_content: str, new_content: str,
) -> Optional[Detection]:
    """HARD: detects new shell-exec calls in any file type (regex, delta-gated).

    Covers non-Python files (.sh, .yaml, .pth …) where _safe_parse returns
    None so Pattern 11 (dynamic_import_chain) cannot fire.  When applied to
    Python files this acts as an additional defence-in-depth layer alongside
    the AST-based _DYNAMIC_ATTR_CALLS entries.
    """
    old_count = len(_SHELL_EXEC_RE.findall(old_content))
    new_count = len(_SHELL_EXEC_RE.findall(new_content))
    delta = new_count - old_count
    if delta <= 0:
        return None
    return Detection(
        pattern="shell_exec_introduced",
        severity="hard",
        message=(
            f"Shell-exec call introduced ({delta} new site"
            f"{'s' if delta != 1 else ''}: os.system/os.popen/"
            f"subprocess.run/call/Popen/check_output/check_call). "
            f"Fires on all file types including non-Python."
        ),
        file_path=file_path,
        lines=tuple(_line_numbers_for_pattern(new_content, _SHELL_EXEC_RE)),
        snippet=f"old_count={old_count} new_count={new_count} delta={delta}",
    )


_ALL_PATTERNS: Tuple[str, ...] = (
    "removed_import_still_referenced",
    "function_body_collapsed",
    "guard_boolean_inverted",
    "credential_shape_introduced",
    "test_assertion_inverted",
    "return_value_flipped",
    "permission_loosened",
    "silent_exception_swallow",
    "hardcoded_url_swap",
    "docstring_only_delete",
    "dynamic_import_chain",
    "dynamic_dunder_construction",
    # Slice 208 — epistemic-integrity (metric-gaming deception)
    "self_signing_attempt",
    "metric_counter_suppressed",
    "chronos_continuity_laundering",
    # S6 closure — shell-exec in non-Python files
    "shell_exec_introduced",
)


_PATTERNS: dict = {
    "removed_import_still_referenced": _pat_removed_import_still_referenced,
    "function_body_collapsed": _pat_function_body_collapsed,
    "guard_boolean_inverted": _pat_guard_boolean_inverted,
    "credential_shape_introduced": _pat_credential_shape_introduced,
    "test_assertion_inverted": _pat_test_assertion_inverted,
    "return_value_flipped": _pat_return_value_flipped,
    "permission_loosened": _pat_permission_loosened,
    "silent_exception_swallow": _pat_silent_exception_swallow,
    "hardcoded_url_swap": _pat_hardcoded_url_swap,
    "docstring_only_delete": _pat_docstring_only_delete,
    "dynamic_import_chain": _pat_dynamic_import_chain,
    "dynamic_dunder_construction": (
        _pat_dynamic_dunder_construction
    ),
    # Slice 208 — epistemic-integrity detectors
    "self_signing_attempt": _pat_self_signing_attempt,
    "metric_counter_suppressed": _pat_metric_counter_suppressed,
    "chronos_continuity_laundering": _pat_chronos_continuity_laundering,
    # S6 closure — shell-exec in non-Python files
    "shell_exec_introduced": _pat_shell_exec_introduced,
}


# ---------------------------------------------------------------------------
# Tier 0 — Anticipatory Edge-Case Armor (blindspot detectors)
# ---------------------------------------------------------------------------
#
# Static detectors for the bug classes that survive ast.parse and bite at runtime
# (frozen-instance mutation, unguarded Optional deref, no-op loop rebinds) — the gap the
# LiveKernelValidator catches dynamically. Additive + per-pattern kill switches
# (JARVIS_SEMGUARD_<NAME>_ENABLED), identical inspect()/Detection contract. Living in a
# separate module keeps the CFG/immutability analysis isolated; import is fail-soft so a
# load error can never disable the existing guardian.
try:  # pragma: no cover - import guard
    from backend.core.ouroboros.governance.semantic_guardian_blindspots import (
        BLINDSPOT_PATTERNS as _BLINDSPOT_PATTERNS,
    )
    for _bs_name, _bs_detector in _BLINDSPOT_PATTERNS.items():
        if _bs_name not in _PATTERNS:                     # never shadow a hand-written pattern
            _PATTERNS[_bs_name] = _bs_detector
            _ALL_PATTERNS = tuple(_ALL_PATTERNS) + (_bs_name,)
except Exception:  # noqa: BLE001
    logger.debug("[SemanticGuard] blindspot detectors unavailable — skipping", exc_info=True)


# ---------------------------------------------------------------------------
# Phase 7.1 — adapted-pattern boot-time merge
# ---------------------------------------------------------------------------
#
# Per OUROBOROS_VENOM_PRD.md §3.6 + §9 Phase 7.1, this is the activation
# wiring that converts Pass C Slice 2 (SemanticGuardian POSTMORTEM-mined
# patterns) from substrate-only to functional. When the
# JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS env flag is on AND the
# YAML at .jarvis/adapted_guardian_patterns.yaml exists, the loader
# bridges Pass C's operator-approved adaptation proposals into the live
# detector registry.
#
# Cage discipline (load-bearing):
#   * Adapted patterns are ADDITIVE only — name collisions with hand-
#     written patterns cause the adapted entry to be SKIPPED (Pass C §6.3).
#   * Default off — when the env flag is unset OR the YAML is missing,
#     SemanticGuardian behaves identically to pre-Phase-7.1.
#   * Fail-open — every error path in the loader returns an empty dict
#     and SemanticGuardian behaves identically. The boot-time merge can
#     never crash the orchestrator.
try:
    from backend.core.ouroboros.governance.adaptation.adapted_guardian_loader import (  # noqa: E501
        is_loader_enabled as _adapted_loader_enabled,
        load_adapted_patterns as _load_adapted_patterns,
    )
    if _adapted_loader_enabled():
        _adapted = _load_adapted_patterns(
            hand_written_names=tuple(_PATTERNS.keys()),
        )
        for _name, _detector in _adapted.items():
            # Adapted patterns are additive; the loader already filtered
            # name collisions with hand-written entries. Defensive
            # double-check: never overwrite an existing _PATTERNS key.
            if _name not in _PATTERNS:
                _PATTERNS[_name] = _detector
        if _adapted:
            _ALL_PATTERNS = tuple(_ALL_PATTERNS) + tuple(  # type: ignore[assignment]
                n for n in _adapted.keys() if n in _PATTERNS
            )
            logger.info(
                "[SemanticGuardian] merged %d adapted patterns from "
                "Pass C YAML (Phase 7.1 wiring)", len(_adapted),
            )
except Exception:  # noqa: BLE001 — fail-open boot-time hook
    logger.debug(
        "[SemanticGuardian] adapted-pattern loader skipped (Phase 7.1)",
        exc_info=True,
    )


def all_pattern_names() -> Tuple[str, ...]:
    return _ALL_PATTERNS


# ---------------------------------------------------------------------------
# Tier recommendation from findings
# ---------------------------------------------------------------------------


def recommend_tier_floor(findings: Sequence[Detection]) -> Optional[str]:
    """Given a batch of detections, return the minimum tier that should
    apply:

      * ``"approval_required"`` — any ``hard`` detection fired
      * ``"notify_apply"`` — at least one ``soft`` detection and no hard
      * ``None`` — no findings, no floor change

    The orchestrator compares this against the current tier and upgrades
    only when the guardian's floor is stricter.
    """
    if not findings:
        return None
    if any(d.severity == "hard" for d in findings):
        return "approval_required"
    if any(d.severity == "soft" for d in findings):
        return "notify_apply"
    return None
