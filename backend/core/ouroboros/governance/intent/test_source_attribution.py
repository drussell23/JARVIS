"""Slice 6 — deterministic AST test→source attribution bridge.

THE GAP (battle-test Run #16): a TestFailure signal's ``target_files``
was definitionally the failing TEST file (``test_id.split("::")[0]``),
so APPLY scope never contained the module under test, the
``file_scope_mismatch`` guard REJECTED correct source repairs, and
VERIFY died deterministically at pass_rate<1.0 while the source bug
survived. This module resolves the source loci a test exercises by
parsing the test module's AST and tracing its ACTUAL imports — never
path heuristics (mandate 1), never a new parser (mandate 3: composes
``reverse_dep_resolver``'s sanctioned extractor + the new inverse
module→path map), alias/relative/indirection-aware with typed fail-fast
(mandate 4). Traceback frames are a ranking TIE-BREAKER only: for the
Run-16 class (assertion failures) the deepest in-repo frame is the test
line itself, so imports must be primary.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence, Set, Tuple

from backend.core.ouroboros.governance.reverse_dep_resolver import (
    _is_test_module,
    _module_from_relpath,
    _relpath_under_root,
    build_module_to_path,
    extract_module_imports,
)

logger = logging.getLogger(__name__)

ATTRIBUTION_SCHEMA_VERSION = 1

# Evidence kinds, ranked: direct imports are the primary deterministic
# signal; patch-target strings recover mock-indirection (~17% of suite).
_KIND_DIRECT = "direct_import"
_KIND_PATCH = "patch_target"


class AttributionUnresolved(Exception):
    """Typed fail-fast (mandate 4): the source under test cannot be
    deterministically resolved. Carries a machine-readable ``reason`` so
    the signal evidence (and the scope gate) can act on it."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(
            f"test->source attribution unresolved: {reason}"
            + (f" ({detail})" if detail else "")
        )


@dataclass(frozen=True)
class Attribution:
    """Resolved loci. All paths repo-relative POSIX. ``source_loci`` is
    never empty (emptiness raises ``AttributionUnresolved`` instead)."""

    test_locus: str
    source_loci: Tuple[str, ...]
    method: str
    evidence_kinds: Tuple[str, ...]


def attribution_enabled() -> bool:
    return os.environ.get(
        "JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")


def _max_source_files() -> int:
    try:
        val = int(os.environ.get("JARVIS_ATTRIBUTION_MAX_SOURCE_FILES", "8"))
        return max(1, val)
    except (TypeError, ValueError):
        return 8


def _module_map_ttl_s() -> float:
    try:
        return max(0.0, float(os.environ.get(
            "JARVIS_ATTRIBUTION_MODULE_MAP_TTL_S", "300",
        )))
    except (TypeError, ValueError):
        return 300.0


def _test_dir_names() -> frozenset:
    """Config-driven test-tree classification — reuses TestRunner's
    existing ``JARVIS_TEST_DIR_NAMES`` knob (mandate 1: no hardcoded
    directory assumptions; the default matches TestRunner's)."""
    raw = os.environ.get("JARVIS_TEST_DIR_NAMES", "tests").strip()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


# Bounded TTL cache for the module→path map (one rglob per repo per TTL,
# not per failing test). Keyed by repo_root; thread-safe.
_MAP_CACHE: Dict[str, Tuple[float, Dict[str, str]]] = {}
_MAP_CACHE_LOCK = threading.Lock()


def _get_module_map(repo_root: str) -> Dict[str, str]:
    now = time.monotonic()
    with _MAP_CACHE_LOCK:
        hit = _MAP_CACHE.get(repo_root)
        if hit is not None and now - hit[0] < _module_map_ttl_s():
            return hit[1]
    mapping = build_module_to_path(repo_root)
    with _MAP_CACHE_LOCK:
        _MAP_CACHE[repo_root] = (now, mapping)
    return mapping


def _resolve_dotted_to_path(
    dotted: str, module_map: Dict[str, str],
) -> Optional[str]:
    """Longest-prefix resolution: ``x.y`` tries the submodule ``x.y``
    first, then the module ``x`` (``y`` was a symbol) — the exact-match-
    first discipline ``test_runner._find_tests_by_ast_import`` documents
    to avoid parent-package over-matching."""
    parts = dotted.split(".")
    while parts:
        hit = module_map.get(".".join(parts))
        if hit:
            return hit
        parts.pop()
    return None


_PATCH_CALL_NAMES = frozenset({"patch", "setattr", "delattr"})


def _extract_patch_targets(tree: ast.Module) -> Set[str]:
    """Dotted-string first arguments of ``mock.patch("x.y.z")`` /
    ``monkeypatch.setattr("x.y.z", ...)`` calls — deterministic AST
    literal extraction (string constants only; f-strings/variables are
    not resolvable and are correctly ignored)."""
    targets: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = ""
        if isinstance(fn, ast.Attribute):
            name = fn.attr
        elif isinstance(fn, ast.Name):
            name = fn.id
        if name not in _PATCH_CALL_NAMES:
            continue
        if not node.args:
            continue
        arg0 = node.args[0]
        if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
            val = arg0.value.strip()
            if "." in val:
                targets.add(val)
    return targets


def _is_test_infra(rel_path: str, dir_names: frozenset) -> bool:
    """True when *rel_path* lives in the configured test tree — it is a
    test-locus (the test itself, a helper, a conftest), never a
    source-locus. Config-driven via JARVIS_TEST_DIR_NAMES."""
    module = _module_from_relpath(rel_path)
    if not module:
        return True
    parts = module.split(".")
    if parts[0] in dir_names:
        return True
    return _is_test_module(module, dir_names)


def attribute_test_to_sources(
    test_file: str,
    *,
    repo_root: str,
    traceback_frames: Sequence[str] = (),
) -> Attribution:
    """Resolve the source file(s) *test_file* exercises. Deterministic:
    identical inputs yield identical output. Raises
    :class:`AttributionUnresolved` (typed reason) when no first-party
    source module is deterministically reachable — the caller must then
    fail-fast, never silently fall back to test-file mutation scope."""
    rel_test = _relpath_under_root(test_file, repo_root)
    if not rel_test:
        raise AttributionUnresolved("test_outside_root", test_file)
    abs_test = os.path.join(repo_root, rel_test)
    try:
        source = Path(abs_test).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise AttributionUnresolved("test_file_missing", rel_test) from exc
    try:
        tree = ast.parse(source, filename=abs_test)
    except (SyntaxError, ValueError) as exc:
        raise AttributionUnresolved("parse_error", f"{rel_test}: {exc}") from exc

    module = _module_from_relpath(rel_test)
    is_init = rel_test == "__init__.py" or rel_test.endswith("/__init__.py")
    dir_names = _test_dir_names()
    module_map = _get_module_map(repo_root)

    # candidates: rel_path -> evidence kind (direct import wins over patch)
    candidates: Dict[str, str] = {}
    for dotted in sorted(extract_module_imports(tree, module, is_init)):
        rel = _resolve_dotted_to_path(dotted, module_map)
        if not rel or rel == rel_test or _is_test_infra(rel, dir_names):
            continue
        candidates.setdefault(rel, _KIND_DIRECT)
    for dotted in sorted(_extract_patch_targets(tree)):
        rel = _resolve_dotted_to_path(dotted, module_map)
        if not rel or rel == rel_test or _is_test_infra(rel, dir_names):
            continue
        candidates.setdefault(rel, _KIND_PATCH)

    if not candidates:
        raise AttributionUnresolved("no_first_party_source_imports", rel_test)

    tb_hits = {
        _relpath_under_root(f, repo_root) or f.replace("\\", "/")
        for f in traceback_frames
    }
    ranked = sorted(
        candidates.items(),
        key=lambda kv: (
            kv[0] not in tb_hits,          # traceback-implicated first
            kv[1] != _KIND_DIRECT,          # direct imports before patch targets
            kv[0],                          # lexical — total deterministic order
        ),
    )[: _max_source_files()]

    kinds = tuple(kind for _, kind in ranked)
    method = _KIND_DIRECT if set(kinds) == {_KIND_DIRECT} else (
        f"{_KIND_DIRECT}+{_KIND_PATCH}" if _KIND_PATCH in kinds else kinds[0]
    )
    return Attribution(
        test_locus=rel_test,
        source_loci=tuple(path for path, _ in ranked),
        method=method,
        evidence_kinds=kinds,
    )


# ---------------------------------------------------------------------------
# Scope-gate predicate (Task 5 wires this at the orchestrator)
# ---------------------------------------------------------------------------


def scope_gate_enabled() -> bool:
    return os.environ.get(
        "JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")


def unattributed_test_scope_violation(
    intake_evidence_json: str,
    candidate_files: Sequence[str],
) -> Optional[str]:
    """Mandate 4's enforcement predicate: when the op's attribution is
    ``unresolved`` and EVERY candidate file is a test-locus, mutating is
    exactly the Run-16 blind class — return a violation message (the
    orchestrator escalates to APPROVAL_REQUIRED). ``None`` = no
    violation. Strictly fail-soft on malformed evidence (absent /
    non-JSON / missing keys → None): this gate must never break ops that
    predate the schema."""
    if not scope_gate_enabled() or not candidate_files:
        return None
    try:
        evidence = json.loads(intake_evidence_json or "{}")
        attribution = evidence.get("attribution") or {}
        status = str(attribution.get("status", ""))
    except (ValueError, TypeError, AttributeError):
        return None
    if status != "unresolved":
        return None
    dir_names = _test_dir_names()
    test_locus = str(attribution.get("test_locus", ""))
    normalized = [str(f).replace("\\", "/").lstrip("./") for f in candidate_files]
    if all(
        f == test_locus or _is_test_infra(f, dir_names) for f in normalized
    ):
        return (
            "attribution_unresolved_test_scope: op attribution is "
            f"unresolved ({attribution.get('reason', 'unknown')}) and the "
            f"candidate mutates only test loci {normalized} — blind "
            "test-file mutation is forbidden; requires human approval "
            "or source-locus exploration"
        )
    return None
