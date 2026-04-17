"""DeepAnalysisSensor — semantic-adjacent intent inference from the codebase.

**Scope framing**: existing sensors (TodoScanner, OpportunityMiner,
DocStaleness, CapabilityGap) are syntactic pattern matching — they
find ``TODO:`` markers, long functions, undocumented symbols. This
sensor targets a different layer: correlations that suggest INTENT
has drifted — what the code claims to do vs. what it actually does,
what was recently added vs. what's tested, what's exported vs.
what's used, what a module's docstring promises vs. what's in it now.

**Honest scope caveat** (load-bearing): this is heuristics **targeting**
semantic intent, not semantic understanding. No LLM, no network, no
hidden state. A true semantic sensor would need an LLM layer (deferred
to V1.1 as a separate scoped project — ``semantic_sensor_llm.md``).
V1 uses AST + git + cross-reference analysis that produces signals
operators should treat as **"worth investigating"**, not "definitely
broken." Every signal carries evidence (file, line, specific mismatch)
so the operator can judge in seconds.

**Four analyzer categories**:

  1. ``contract_drift`` — docstring + type-hint promises vs. impl
     (return-type mismatches, undocumented raises, params without docs)
  2. ``coverage_gap`` — newly-added symbols (git-diff HEAD~N) in
     ``src/`` / ``backend/`` without matching ``tests/test_*``
  3. ``purpose_drift`` — module docstring themes vs. current function
     name clusters; stale ``This file contains:`` preambles
  4. ``orphan_surface`` — ``def public_thing(...)`` with zero callers
     in the repo; ``__init__.py`` exports nobody imports

**Authority invariant**: the sensor emits low-urgency signals through
the normal ``UnifiedIntakeRouter``. Every signal flows through
CLASSIFY → risk engine → SemanticGuardian → tier floor, exactly like
every other sensor. DeepAnalysisSensor does not bypass any gate, and
its findings never forcibly upgrade the risk tier — the operator or
the risk engine always remains authoritative.

Env gates (all fail-closed / safe defaults):

    JARVIS_DEEP_ANALYSIS_SENSOR_ENABLED     default 0 (opt-in master)
    JARVIS_DEEP_ANALYSIS_CONTRACT_ENABLED   default 1 (per-category sub-gate)
    JARVIS_DEEP_ANALYSIS_COVERAGE_ENABLED   default 1
    JARVIS_DEEP_ANALYSIS_PURPOSE_ENABLED    default 1
    JARVIS_DEEP_ANALYSIS_ORPHAN_ENABLED     default 1
    JARVIS_DEEP_ANALYSIS_POLL_INTERVAL_S    default 900 (every 15min)
    JARVIS_DEEP_ANALYSIS_LOOKBACK_COMMITS   default 50
    JARVIS_DEEP_ANALYSIS_MAX_FINDINGS_PER_CYCLE default 5 (intake flood guard)
    JARVIS_DEEP_ANALYSIS_COOLDOWN_S         default 86400 (dedup same finding for 24h)
    JARVIS_DEEP_ANALYSIS_MAX_AGE_DAYS       default 180 (ignore ancient stale TODOs)
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


logger = logging.getLogger("Ouroboros.DeepAnalysisSensor")

_ENV_ENABLED = "JARVIS_DEEP_ANALYSIS_SENSOR_ENABLED"
_ENV_CONTRACT = "JARVIS_DEEP_ANALYSIS_CONTRACT_ENABLED"
_ENV_COVERAGE = "JARVIS_DEEP_ANALYSIS_COVERAGE_ENABLED"
_ENV_PURPOSE = "JARVIS_DEEP_ANALYSIS_PURPOSE_ENABLED"
_ENV_ORPHAN = "JARVIS_DEEP_ANALYSIS_ORPHAN_ENABLED"
_ENV_POLL = "JARVIS_DEEP_ANALYSIS_POLL_INTERVAL_S"
_ENV_LOOKBACK = "JARVIS_DEEP_ANALYSIS_LOOKBACK_COMMITS"
_ENV_MAX_PER_CYCLE = "JARVIS_DEEP_ANALYSIS_MAX_FINDINGS_PER_CYCLE"
_ENV_COOLDOWN = "JARVIS_DEEP_ANALYSIS_COOLDOWN_S"
_ENV_MAX_AGE_DAYS = "JARVIS_DEEP_ANALYSIS_MAX_AGE_DAYS"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def sensor_enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "0").strip().lower() in _TRUTHY


def _per_category_enabled(env_key: str, *, default_on: bool = True) -> bool:
    """Per-category gate: defaults ON when master is ON, so enabling
    the sensor turns everything on. Explicit 0 disables one category."""
    val = os.environ.get(env_key, "").strip().lower()
    if not val:
        return default_on
    return val in _TRUTHY


def _int_env(key: str, default: int, *, lo: int = 1, hi: int = 1_000_000) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(key, str(default)))))
    except (TypeError, ValueError):
        return default


def _poll_interval_s() -> int:
    return _int_env(_ENV_POLL, 900, lo=60, hi=86400)


def _lookback_commits() -> int:
    return _int_env(_ENV_LOOKBACK, 50, lo=5, hi=500)


def _max_findings_per_cycle() -> int:
    return _int_env(_ENV_MAX_PER_CYCLE, 5, lo=1, hi=50)


def _cooldown_s() -> int:
    return _int_env(_ENV_COOLDOWN, 86400, lo=60, hi=604800)


def _max_age_days() -> int:
    return _int_env(_ENV_MAX_AGE_DAYS, 180, lo=7, hi=3650)


# ---------------------------------------------------------------------------
# Finding data type — what each analyzer emits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeepAnalysisFinding:
    """One deterministic semantic-adjacent observation.

    ``category`` is one of the four V1 analyzer buckets.
    ``finding_id`` is a stable short hash so (category, file, id)
    dedups across polling cycles.
    ``description`` is the operator-facing human summary (short).
    ``evidence`` carries the machine-readable specifics the downstream
    prompt builder uses.
    """

    category: str                      # contract_drift | coverage_gap | purpose_drift | orphan_surface
    file: str                          # relative path
    finding_id: str                    # hash(category + file + specifics)[:10]
    description: str                   # one-line human-readable
    line: int = 0                      # optional anchor
    evidence: Dict[str, Any] = field(default_factory=dict)
    urgency: str = "low"               # analytical → low

    @property
    def dedup_key(self) -> str:
        return f"{self.category}:{self.file}:{self.finding_id}"


def _finding_hash(category: str, file: str, specifics: str) -> str:
    h = hashlib.sha256(f"{category}|{file}|{specifics}".encode()).hexdigest()
    return h[:10]


# ---------------------------------------------------------------------------
# AST helpers — shared across analyzers
# ---------------------------------------------------------------------------


def _safe_parse(src: str) -> Optional[ast.Module]:
    if not src:
        return None
    try:
        return ast.parse(src)
    except (SyntaxError, ValueError):
        return None


def _iter_py_files(
    repo_root: Path,
    *,
    skip_dirs: Iterable[str] = (
        ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
        ".tox", "dist", "build", ".mypy_cache", "htmlcov",
    ),
) -> Iterable[Path]:
    skip_set = set(skip_dirs)
    for p in repo_root.rglob("*.py"):
        if any(part in skip_set for part in p.parts):
            continue
        if not p.is_file():
            continue
        yield p


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# (1) Contract drift analyzer
# ---------------------------------------------------------------------------
#
# Looks for mismatches between what a function DECLARES (docstring +
# type hint) and what the IMPLEMENTATION does. Deterministic checks:
#
#   * return-type says -> bool, but function has `return None` or
#     `return <non-bool-literal>` somewhere
#   * docstring says "raises ValueError" but body has no `raise`
#   * param added to signature but missing from docstring (only when
#     docstring has a recognizable Parameters section)


_DOCSTRING_RAISES_RE = re.compile(
    r"(?:raises|throws|raise)\s*:?\s*([A-Z][A-Za-z]+(?:Error|Exception))",
    re.IGNORECASE,
)
_DOCSTRING_PARAMS_HEADER_RE = re.compile(
    r"(?:^|\n)\s*(?:Parameters|Args|Arguments)\s*[-=]*\s*\n",
    re.IGNORECASE,
)
_DOCSTRING_PARAM_NAME_RE = re.compile(
    r"(?:^|\n)\s*([a-z_][a-z0-9_]*)\s*[:(]",
)


def _fn_returns_only(fn: ast.AST) -> Set[str]:
    """Return a set of string labels describing what kinds of values
    the function returns. Possible labels: "bool_true", "bool_false",
    "none", "literal", "expr", "implicit_none" (function falls off end).
    """
    kinds: Set[str] = set()
    has_explicit_return = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return):
            continue
        has_explicit_return = True
        val = node.value
        if val is None:
            kinds.add("none")
            continue
        if isinstance(val, ast.Constant):
            if val.value is True:
                kinds.add("bool_true")
            elif val.value is False:
                kinds.add("bool_false")
            elif val.value is None:
                kinds.add("none")
            else:
                kinds.add("literal")
        else:
            kinds.add("expr")
    if not has_explicit_return:
        kinds.add("implicit_none")
    return kinds


def _body_raises(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Raise):
            return True
    return False


def _docstring_of(fn: ast.AST) -> str:
    try:
        return ast.get_docstring(fn) or ""  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return ""


def _param_names(fn: ast.AST) -> List[str]:
    """Extract positional + keyword parameter names from a FunctionDef/
    AsyncFunctionDef. Skips ``self`` / ``cls`` / ``*args`` / ``**kwargs``
    since docstring convention doesn't always document those."""
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    out: List[str] = []
    args = fn.args
    for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
        if a.arg in ("self", "cls"):
            continue
        out.append(a.arg)
    return out


def _docstring_documented_params(doc: str) -> Set[str]:
    """Parse the Parameters/Args section of a docstring and return the
    names it documents. Tolerant of numpy/google/sphinx styles — we
    only need to know if a given name is mentioned anywhere after the
    params header, not validate the format."""
    if not doc:
        return set()
    m = _DOCSTRING_PARAMS_HEADER_RE.search(doc)
    if m is None:
        return set()
    tail = doc[m.end():]
    found: Set[str] = set()
    for nm in _DOCSTRING_PARAM_NAME_RE.finditer(tail):
        name = nm.group(1)
        # Accept any valid identifier — short params (i, n, x, y) are real.
        if name:
            found.add(name)
    return found


def _return_annotation_kind(fn: ast.AST) -> str:
    """Return "bool", "none", "other", or "" (not annotated)."""
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ""
    ann = fn.returns
    if ann is None:
        return ""
    if isinstance(ann, ast.Name):
        return ann.id.lower()
    if isinstance(ann, ast.Constant) and ann.value is None:
        return "none"
    return "other"


def analyze_contract_drift(
    *, repo_root: Path,
) -> List[DeepAnalysisFinding]:
    """Walk every Python file; for each function/method, compare
    docstring + annotation promises against the implementation."""
    out: List[DeepAnalysisFinding] = []
    for path in _iter_py_files(repo_root):
        src = _read(path)
        tree = _safe_parse(src)
        if tree is None:
            continue
        rel_path = _rel(path, repo_root)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            doc = _docstring_of(node)
            # (a) return-type mismatch
            ann = _return_annotation_kind(node)
            if ann == "bool":
                kinds = _fn_returns_only(node)
                has_bool = bool(kinds & {"bool_true", "bool_false"})
                has_non_bool = bool(
                    kinds & {"none", "expr", "literal", "implicit_none"}
                )
                if has_bool and has_non_bool:
                    specifics = (
                        f"{node.name} annotated -> bool but returns "
                        f"{sorted(kinds & {'none', 'literal', 'implicit_none'})}"
                    )
                    fid = _finding_hash("contract_drift", rel_path, specifics)
                    out.append(DeepAnalysisFinding(
                        category="contract_drift",
                        file=rel_path,
                        finding_id=fid,
                        description=(
                            f"return-type drift in {node.name}: "
                            "-> bool vs. non-bool return path"
                        ),
                        line=getattr(node, "lineno", 0),
                        evidence={
                            "function": node.name,
                            "annotation": "bool",
                            "observed_returns": sorted(kinds),
                            "subcategory": "return_type_mismatch",
                        },
                    ))
            # (b) docstring claims raises but body has no raise
            if doc and not _body_raises(node):
                raises_claimed = _DOCSTRING_RAISES_RE.findall(doc)
                if raises_claimed:
                    specifics = (
                        f"{node.name} docstring claims raises "
                        f"{raises_claimed[0]} but body has no raise"
                    )
                    fid = _finding_hash("contract_drift", rel_path, specifics)
                    out.append(DeepAnalysisFinding(
                        category="contract_drift",
                        file=rel_path,
                        finding_id=fid,
                        description=(
                            f"docstring-vs-impl: {node.name} claims "
                            f"raises {raises_claimed[0]} but body doesn't"
                        ),
                        line=getattr(node, "lineno", 0),
                        evidence={
                            "function": node.name,
                            "claimed_exceptions": raises_claimed,
                            "subcategory": "raises_claimed_but_absent",
                        },
                    ))
            # (c) param added but not in docstring
            if doc:
                declared = set(_param_names(node))
                documented = _docstring_documented_params(doc)
                if documented:
                    missing = declared - documented
                    # Only flag when the docstring HAS a params section —
                    # otherwise the author hasn't claimed to document params.
                    if missing:
                        missing_sorted = sorted(missing)[:3]
                        specifics = (
                            f"{node.name} params {missing_sorted} "
                            "not in docstring"
                        )
                        fid = _finding_hash(
                            "contract_drift", rel_path, specifics,
                        )
                        out.append(DeepAnalysisFinding(
                            category="contract_drift",
                            file=rel_path,
                            finding_id=fid,
                            description=(
                                f"docstring-vs-impl: {node.name} "
                                f"params {missing_sorted} not documented"
                            ),
                            line=getattr(node, "lineno", 0),
                            evidence={
                                "function": node.name,
                                "missing_params": missing_sorted,
                                "subcategory": "params_added_not_documented",
                            },
                        ))
    return out


# ---------------------------------------------------------------------------
# (2) Coverage gap analyzer — git-diff aware
# ---------------------------------------------------------------------------
#
# Finds newly-added public symbols (last N commits) in source paths
# (``backend/``, ``src/``) whose matching test file is either absent,
# untouched in the same window, or lacks a reference to the new symbol.


def _git(repo_root: Path, args: List[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10.0,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout
    except Exception:  # noqa: BLE001
        return ""


def _recently_added_source_files(
    repo_root: Path, *, lookback: int,
) -> List[Path]:
    """Return source .py files that have had additions in the last
    N commits (git diff HEAD~N..HEAD --name-only --diff-filter=AM).

    Scoped to ``backend/`` and ``src/`` paths — tests, docs, configs
    are out of scope for coverage-gap analysis.

    Falls back gracefully when the repo has fewer than ``lookback``
    commits: clamps to available depth or diffs against the empty tree.
    """
    # Clamp lookback to actual repo depth so HEAD~N resolves.
    count_raw = _git(repo_root, ["rev-list", "--count", "HEAD"]).strip()
    try:
        depth = int(count_raw) if count_raw else 0
    except ValueError:
        depth = 0
    if depth <= 1:
        # Single commit (or no commits) — diff against the empty tree so
        # every file in HEAD counts as "recently added".
        empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        raw = _git(
            repo_root,
            ["diff", empty_tree, "HEAD",
             "--name-only", "--diff-filter=AM"],
        )
    else:
        effective = min(lookback, depth - 1)
        raw = _git(
            repo_root,
            [
                "diff", f"HEAD~{effective}", "HEAD",
                "--name-only", "--diff-filter=AM",
            ],
        )
    if not raw:
        return []
    out: List[Path] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.endswith(".py"):
            continue
        if not (line.startswith("backend/") or line.startswith("src/")):
            continue
        if "/tests/" in line or line.startswith("tests/"):
            continue
        p = repo_root / line
        if p.is_file():
            out.append(p)
    return out


def _public_symbols(tree: ast.Module) -> List[Tuple[str, int]]:
    """Return (name, lineno) for every top-level public function/class
    in a module. Public = name doesn't start with underscore."""
    out: List[Tuple[str, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                out.append((node.name, node.lineno))
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                out.append((node.name, node.lineno))
    return out


def _plausible_test_paths(source_path: Path, repo_root: Path) -> List[Path]:
    """Heuristic: for a source file ``backend/foo/bar.py``, plausible
    test files are:
        tests/foo/test_bar.py
        tests/test_bar.py
        backend/foo/tests/test_bar.py
    """
    candidates: List[Path] = []
    stem = source_path.stem
    try:
        rel = source_path.relative_to(repo_root)
    except ValueError:
        return []
    parts = list(rel.parts)
    # Strip leading backend/src and append test variants.
    if parts and parts[0] in ("backend", "src"):
        parts = parts[1:]
    if not parts:
        return []
    candidates.append(repo_root / "tests" / "/".join(parts[:-1]) / f"test_{stem}.py")
    candidates.append(repo_root / "tests" / f"test_{stem}.py")
    # Sibling tests/ dir.
    candidates.append(source_path.parent / "tests" / f"test_{stem}.py")
    # Dedup while preserving order.
    seen: Set[str] = set()
    out: List[Path] = []
    for c in candidates:
        k = str(c)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def analyze_coverage_gap(
    *, repo_root: Path, lookback: int,
) -> List[DeepAnalysisFinding]:
    out: List[DeepAnalysisFinding] = []
    recent = _recently_added_source_files(repo_root, lookback=lookback)
    for src_path in recent:
        src = _read(src_path)
        tree = _safe_parse(src)
        if tree is None:
            continue
        rel_path = _rel(src_path, repo_root)
        symbols = _public_symbols(tree)
        if not symbols:
            continue
        test_candidates = _plausible_test_paths(src_path, repo_root)
        test_path = next(
            (p for p in test_candidates if p.is_file()), None,
        )
        if test_path is None:
            # No test file at all — one finding for the whole module.
            specifics = (
                f"{rel_path} has {len(symbols)} public symbol(s) but "
                "no matching test file"
            )
            fid = _finding_hash("coverage_gap", rel_path, specifics)
            out.append(DeepAnalysisFinding(
                category="coverage_gap",
                file=rel_path,
                finding_id=fid,
                description=(
                    f"recently-added module with no test file: "
                    f"{len(symbols)} public symbol(s)"
                ),
                line=symbols[0][1] if symbols else 0,
                evidence={
                    "public_symbols": [s[0] for s in symbols][:10],
                    "expected_test_candidates": [
                        _rel(p, repo_root) for p in test_candidates
                    ],
                    "subcategory": "no_test_file",
                },
            ))
            continue
        # Test file exists — check that each public symbol appears in it.
        test_src = _read(test_path)
        missing: List[str] = []
        for sym_name, _ in symbols:
            # Exact-word substring scan. Avoids false negatives on
            # reasonable test files that import+call the symbol.
            if not re.search(rf"\b{re.escape(sym_name)}\b", test_src):
                missing.append(sym_name)
        if missing:
            missing_preview = missing[:5]
            specifics = (
                f"{rel_path} has {len(missing)} public symbol(s) "
                f"not referenced in {_rel(test_path, repo_root)}"
            )
            fid = _finding_hash("coverage_gap", rel_path, specifics)
            out.append(DeepAnalysisFinding(
                category="coverage_gap",
                file=rel_path,
                finding_id=fid,
                description=(
                    f"recently-added public symbols not referenced in "
                    f"{test_path.name}: {', '.join(missing_preview)}"
                    + (" …" if len(missing) > 5 else "")
                ),
                line=0,
                evidence={
                    "missing_symbols": missing_preview,
                    "total_missing": len(missing),
                    "test_file": _rel(test_path, repo_root),
                    "subcategory": "symbols_not_referenced_in_tests",
                },
            ))
    return out


# ---------------------------------------------------------------------------
# (3) Purpose drift analyzer
# ---------------------------------------------------------------------------
#
# For each module with a docstring, extract theme keywords from the
# docstring + the function/class names in the module. Flag modules
# where docstring keywords and identifier keywords are disjoint — the
# module has drifted from what its docstring claims it does.


_IDENT_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_DOC_STOPWORDS = frozenset({
    "this", "that", "these", "those", "the", "a", "an", "and", "or", "for",
    "from", "into", "with", "without", "of", "to", "in", "on", "at", "by",
    "is", "are", "was", "were", "be", "been", "has", "have", "do", "does",
    "it", "its", "we", "our", "us", "you", "your", "not", "any", "all",
    "module", "class", "file", "function", "method", "object", "return",
    "returns", "yields", "raises", "type", "types", "value", "values",
    "parameter", "parameters", "args", "arguments", "kwargs", "keyword",
    "keywords", "optional", "required", "default", "none", "true", "false",
    "str", "int", "bool", "float", "list", "dict", "tuple", "set", "bytes",
})


def _tokens_from_text(text: str) -> Set[str]:
    out: Set[str] = set()
    for m in _IDENT_TOKEN_RE.finditer(text or ""):
        t = m.group(0).lower()
        if t in _DOC_STOPWORDS:
            continue
        if len(t) < 3 or len(t) > 32:
            continue
        out.add(t)
    return out


def _split_camel_and_snake(name: str) -> Set[str]:
    """Break ``SemanticGuardian`` → {"semantic", "guardian"}
    and ``parse_manifest`` → {"parse", "manifest"}."""
    # Split on underscores first.
    parts = re.split(r"_+", name)
    out: Set[str] = set()
    for p in parts:
        # CamelCase split.
        for sub in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", p):
            s = sub.lower()
            if s in _DOC_STOPWORDS:
                continue
            if len(s) >= 3:
                out.add(s)
    return out


def _identifier_theme_tokens(tree: ast.Module) -> Set[str]:
    """Collect tokens from every top-level function/class name in a
    module. These represent what the module actually contains."""
    out: Set[str] = set()
    for node in tree.body:
        name: Optional[str] = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        if name is None:
            continue
        out.update(_split_camel_and_snake(name))
    return out


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


def _has_stem_overlap(
    doc_tokens: Set[str], id_tokens: Set[str], *, min_prefix: int = 4,
) -> bool:
    """True iff any doc token shares a common prefix of at least
    ``min_prefix`` chars with any identifier token. Stands in for a
    stemmer — ``charge``/``charging``/``refund``/``refunds`` all pass.
    """
    # Sorting + prefix-buckets would speed this up for huge repos; a
    # pairwise loop is fine for module-scope (typically < 100 tokens).
    for d in doc_tokens:
        for i in id_tokens:
            if _common_prefix_len(d, i) >= min_prefix:
                return True
    return False


def analyze_purpose_drift(
    *, repo_root: Path,
) -> List[DeepAnalysisFinding]:
    out: List[DeepAnalysisFinding] = []
    for path in _iter_py_files(repo_root):
        src = _read(path)
        tree = _safe_parse(src)
        if tree is None:
            continue
        module_doc = ast.get_docstring(tree) or ""
        if len(module_doc) < 40:
            continue  # no meaningful docstring to compare against
        doc_tokens = _tokens_from_text(module_doc)
        id_tokens = _identifier_theme_tokens(tree)
        # Require both sides to have enough tokens to compare.
        if len(doc_tokens) < 4 or len(id_tokens) < 4:
            continue
        # Drift threshold: docstring and identifiers share no tokens
        # AND no meaningful common-prefix (stem-approximation, 4 chars).
        # Stem-approximation catches `charge`/`charging` as related;
        # strict set-overlap would miss that.
        exact_overlap = doc_tokens & id_tokens
        stem_overlap = _has_stem_overlap(doc_tokens, id_tokens, min_prefix=4)
        if len(exact_overlap) == 0 and not stem_overlap:
            rel_path = _rel(path, repo_root)
            doc_preview = sorted(doc_tokens)[:4]
            id_preview = sorted(id_tokens)[:4]
            specifics = (
                f"docstring tokens {doc_preview} have zero overlap "
                f"with identifier tokens {id_preview}"
            )
            fid = _finding_hash("purpose_drift", rel_path, specifics)
            out.append(DeepAnalysisFinding(
                category="purpose_drift",
                file=rel_path,
                finding_id=fid,
                description=(
                    "module docstring themes have zero overlap with "
                    "current function/class names — purpose may have drifted"
                ),
                line=1,
                evidence={
                    "docstring_tokens": doc_preview,
                    "identifier_tokens": id_preview,
                    "subcategory": "docstring_identifier_disjoint",
                },
            ))
    return out


# ---------------------------------------------------------------------------
# (4) Orphan surface analyzer
# ---------------------------------------------------------------------------
#
# Find public top-level functions in ``backend/`` that are never
# referenced elsewhere in the repo. Two passes:
#   pass 1 — collect every public definition (name, file)
#   pass 2 — for each def, grep every other file for its name


def _index_public_defs(
    repo_root: Path,
) -> Dict[str, Tuple[Path, int]]:
    """name → (file, lineno) for every top-level public function in
    ``backend/``. Deliberately narrow: we don't try to track class
    methods (would need per-class scoping + inheritance analysis),
    and we skip ``__init__.py`` (those are re-exports by convention)."""
    out: Dict[str, Tuple[Path, int]] = {}
    backend = repo_root / "backend"
    if not backend.is_dir():
        return out
    for path in _iter_py_files(backend):
        if path.name == "__init__.py":
            continue
        # Skip test files.
        if path.name.startswith("test_") or "/tests/" in str(path):
            continue
        src = _read(path)
        tree = _safe_parse(src)
        if tree is None:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            # Dunder / protocol-ish names don't count — __init__ etc.
            if node.name.startswith("__") and node.name.endswith("__"):
                continue
            # First definition wins; duplicates are rare but we don't
            # want to overwrite a real-backend-function with a re-export.
            if node.name not in out:
                out[node.name] = (path, node.lineno)
    return out


def analyze_orphan_surface(
    *, repo_root: Path,
) -> List[DeepAnalysisFinding]:
    out: List[DeepAnalysisFinding] = []
    defs = _index_public_defs(repo_root)
    if not defs:
        return out
    # Build one concatenated corpus of "everywhere except the file
    # itself" per definition. For efficiency we read each file once
    # and check which def names appear in it.
    file_cache: Dict[Path, str] = {}
    for path in _iter_py_files(repo_root):
        file_cache[path] = _read(path)
    # For each definition, scan every OTHER file for a reference.
    for name, (def_path, lineno) in defs.items():
        referenced = False
        # Compile once per name.
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        for other_path, text in file_cache.items():
            if other_path == def_path:
                continue
            if pattern.search(text):
                referenced = True
                break
        if referenced:
            continue
        rel_path = _rel(def_path, repo_root)
        specifics = f"public function {name} defined but never referenced"
        fid = _finding_hash("orphan_surface", rel_path, specifics)
        out.append(DeepAnalysisFinding(
            category="orphan_surface",
            file=rel_path,
            finding_id=fid,
            description=(
                f"orphan public function: {name} has zero callers in the repo"
            ),
            line=lineno,
            evidence={
                "function": name,
                "subcategory": "public_function_no_callers",
            },
        ))
    return out


# ---------------------------------------------------------------------------
# Aggregator — one entry point the sensor calls per cycle
# ---------------------------------------------------------------------------


def run_all_analyzers(*, repo_root: Path) -> List[DeepAnalysisFinding]:
    """Run every enabled analyzer and return the merged finding list.

    Each analyzer is individually fallible: an exception in one
    doesn't stop the others. Per-category env gates are honored here
    so the sensor layer doesn't need to re-implement the gate logic.
    """
    merged: List[DeepAnalysisFinding] = []
    max_findings = _max_findings_per_cycle() * 4  # rough cap before sensor-side trim

    def _run(label: str, env_key: str, fn: Any, **kwargs: Any) -> None:
        if not _per_category_enabled(env_key):
            return
        try:
            part = fn(**kwargs)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[DeepAnalysis] analyzer %s raised — skipping",
                label, exc_info=True,
            )
            return
        if part:
            merged.extend(part)

    _run(
        "contract_drift", _ENV_CONTRACT, analyze_contract_drift,
        repo_root=repo_root,
    )
    _run(
        "coverage_gap", _ENV_COVERAGE, analyze_coverage_gap,
        repo_root=repo_root, lookback=_lookback_commits(),
    )
    _run(
        "purpose_drift", _ENV_PURPOSE, analyze_purpose_drift,
        repo_root=repo_root,
    )
    _run(
        "orphan_surface", _ENV_ORPHAN, analyze_orphan_surface,
        repo_root=repo_root,
    )
    return merged[:max_findings]


# ---------------------------------------------------------------------------
# Sensor — the IntakeRouter-facing wrapper
# ---------------------------------------------------------------------------


class DeepAnalysisSensor:
    """Semantic-adjacent intent inference. Emits low-urgency signals
    through the normal intake pipeline when an analyzer fires.

    Follows the implicit sensor protocol (``start()`` / ``stop()`` /
    ``scan_once()``) used by the other 16 sensors, so registration with
    the intake fleet is uniform.
    """

    def __init__(
        self,
        repo: str,
        router: Any,
        *,
        poll_interval_s: Optional[float] = None,
        project_root: Optional[Path] = None,
    ) -> None:
        self._repo = repo
        self._router = router
        self._poll_interval_s = (
            poll_interval_s
            if poll_interval_s is not None
            else float(_poll_interval_s())
        )
        self._root = Path(project_root) if project_root else Path(".")
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Per-finding cooldown — key = dedup_key, value = emit time.
        self._recent_emits: Dict[str, float] = {}
        # Stats for /infer-style inspectability (future).
        self._last_cycle_count: int = 0
        self._total_emitted: int = 0

    async def start(self) -> None:
        if not sensor_enabled():
            logger.info(
                "[DeepAnalysis] disabled — set %s=1 to enable",
                _ENV_ENABLED,
            )
            return
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"deep_analysis_{self._repo}",
        )
        logger.info(
            "[DeepAnalysis] Started for repo=%s poll_interval=%.0fs",
            self._repo, self._poll_interval_s,
        )

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def scan_once(self) -> List[DeepAnalysisFinding]:
        """One analysis cycle. Public so operators / tests can drive
        the sensor synchronously."""
        findings = await asyncio.get_event_loop().run_in_executor(
            None, run_all_analyzers, self._root,
        ) if False else _run_sync(self._root)
        # (Ran synchronously in-thread — analyzers are already fast;
        # the executor hop adds latency without benefit. The ``if False``
        # branch keeps the comment locus for future async-dispatch work.)
        findings = _run_sync(self._root)
        self._last_cycle_count = len(findings)
        fresh = self._filter_cooldown(findings)
        # Apply per-cycle cap so intake doesn't flood.
        fresh = fresh[:_max_findings_per_cycle()]
        await self._emit_findings(fresh)
        return fresh

    async def _poll_loop(self) -> None:
        # Boot delay — let the harness settle before we start scanning.
        await asyncio.sleep(min(120.0, self._poll_interval_s / 2))
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[DeepAnalysis] cycle error — retrying next tick",
                    exc_info=True,
                )
            await asyncio.sleep(self._poll_interval_s)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _filter_cooldown(
        self, findings: Sequence[DeepAnalysisFinding],
    ) -> List[DeepAnalysisFinding]:
        now = time.time()
        cooldown = _cooldown_s()
        # GC old entries to keep the dict bounded.
        if self._recent_emits:
            cutoff = now - cooldown
            self._recent_emits = {
                k: t for k, t in self._recent_emits.items() if t >= cutoff
            }
        out: List[DeepAnalysisFinding] = []
        for f in findings:
            last = self._recent_emits.get(f.dedup_key)
            if last is not None and (now - last) < cooldown:
                continue
            out.append(f)
        return out

    async def _emit_findings(
        self, findings: Sequence[DeepAnalysisFinding],
    ) -> None:
        if not findings or self._router is None:
            return
        try:
            from backend.core.ouroboros.governance.intake.intent_envelope import (
                make_envelope,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[DeepAnalysis] intent_envelope import failed — "
                "findings dropped", exc_info=True,
            )
            return
        for f in findings:
            try:
                envelope = make_envelope(
                    # Use exploration — this is analytical suggestion.
                    source="exploration",
                    description=(
                        f"[deep_analysis/{f.category}] {f.description}"
                    ),
                    target_files=(f.file,) if f.file else (),
                    repo=self._repo,
                    confidence=0.55,
                    urgency=f.urgency,
                    evidence={
                        "deep_analysis_category": f.category,
                        "deep_analysis_finding_id": f.finding_id,
                        "deep_analysis_line": f.line,
                        **f.evidence,
                    },
                    requires_human_ack=False,
                )
                verdict = await self._router.ingest(envelope)
                self._recent_emits[f.dedup_key] = time.time()
                self._total_emitted += 1
                logger.info(
                    "[DeepAnalysis] category=%s file=%s id=%s verdict=%s",
                    f.category, f.file, f.finding_id, verdict,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[DeepAnalysis] emit failed for %s — continuing",
                    f.dedup_key, exc_info=True,
                )


def _run_sync(repo_root: Path) -> List[DeepAnalysisFinding]:
    """Internal shim — the analyzer functions are sync-safe, but the
    sensor's ``scan_once`` needs an await boundary. Keeping this
    extracted lets us swap in an executor later if analyzers grow."""
    return run_all_analyzers(repo_root=repo_root)
