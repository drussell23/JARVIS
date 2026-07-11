# Slice 6 — Deterministic Test→Source Attribution Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Run #16 test→source attribution gap: a `TestFailure` signal must carry BOTH the failing test's locus AND the deterministically-resolved source file(s) it exercises, so APPLY scope includes the module under test and the `file_scope_mismatch` double-bind (correct source patches rejected as scope violations) dies.

**Architecture:** A new stdlib-only attributor (`test_source_attribution.py`) parses the failing test module's AST and traces its actual import statements to repo source files, composing the sanctioned import-graph layer in `reverse_dep_resolver.py` (which already handles `import x`, `from x import y`, `import x as y`, and relative imports). The `TestFailure` evidence schema gains a versioned `attribution` block (mirroring the `VisionSignalEvidence` discipline); `TestWatcher.process_failures` composes `target_files = (*source_loci, test_locus)`. When attribution cannot be deterministically resolved, the signal is marked `unresolved` and a deterministic orchestrator gate escalates any test-file-only mutation to `APPROVAL_REQUIRED` — fail-fast to a human, never blind test mutation.

**Tech Stack:** Python 3.9+ stdlib `ast` only on the hot path (no networkx, no Oracle warm-index dependency); pytest for TDD.

## Global Constraints

The four user-locked architectural mandates (2026-07-11), verbatim intent:

1. **Root-Cause Only** — no naive string-matching heuristics, no regex file-path mapping (e.g. swapping `tests/test_foo.py` for `src/foo.py`), no hardcoded directory assumptions. The solution must construct a deterministic dependency bridge.
2. **Architectural Purity** — dynamically resolve the target source by parsing the AST of the failing test; trace the actual `import` statements within the test module to map the failing test node back to the specific source file it exercises.
3. **DRY** — no new Python parser. Leverage the existing AST utilities. *Substrate audit result:* `opportunity_miner_sensor.py` and `doc_staleness_sensor.py` were audited first per the mandate — the miner's `_import_fan_out` only counts top-level import segments (discards dotted names) and its `_get_module_name` is a string heuristic (mandate-1-forbidden); doc_staleness never touches `ast.Import` at all. The repo's **sanctioned import-graph layer** is `reverse_dep_resolver.py` (stdlib-only; full `import`/`from`/`as`/relative handling; already reused by `target_stratification.py`, `autonomous_pr_pipeline.py`, `phase_runners/slice4b_runner.py`) — this plan extends it rather than duplicating it. Extend the existing `TestFailure` signal schema to carry both test-locus and resolved source-locus.
4. **Bulletproof** — account for indirect imports, aliased imports (`import x as y`), and framework-level abstractions (conftest fixtures) that might obscure the direct source link. If the source cannot be deterministically resolved, fail-fast with a clear attribution error rather than blindly mutating the test file.

Repo-wide conventions (CLAUDE.md): Python 3.9+ (no `asyncio.timeout`), `from __future__ import annotations` in every file, all tunables env-var-driven with sensible defaults, zero hardcoded model names, async-first (the attributor is sync-pure but only called off the hot loop at signal-birth). **Every commit ends with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.**

Evidence anchors (from the Run #16 investigation, 2026-07-11):
- The pin: `backend/core/ouroboros/governance/intent/test_watcher.py:444` — `target_files=(f.file_path,)` where `file_path = test_id.split("::")[0]` (the test file, definitionally).
- The double-bind: `doubleword_provider.py:2490-2512` — `file_scope_mismatch` rejects candidates whose paths don't intersect `ctx.target_files`, so a CORRECT source patch is rejected while test-file edits pass.
- The kill: `verify_gate.py:50-54` — `pass_rate=0.75 < 1.00` because the source bug survives every test-file-scoped APPLY.
- Suite-wide feasibility (measured over 2,972 test files): ~73% trivially resolvable via direct `backend.` imports, ~17% recoverable via `mock.patch`/`monkeypatch.setattr` target strings, ~6% multi-source (2+ backend subtrees — carry all, bounded), ~5% dynamic/no-backend (→ fail-fast unresolved).
- Traceback-only attribution is INSUFFICIENT for the Run-16 class: an assertion failure's deepest in-repo frame is the *test* line, not the source — hence imports are primary, traceback frames are a ranking tie-breaker only.

## File Structure

- `backend/core/ouroboros/governance/reverse_dep_resolver.py` — MODIFY: factor the per-module import extraction (currently inlined at lines 164-182) into public `extract_module_imports()`; add `build_module_to_path()` (the inverse mapper — the rglob loop at 149-155 already computes it and discards it).
- `backend/core/ouroboros/governance/intent/test_source_attribution.py` — CREATE: the attributor. `Attribution` dataclass, `AttributionUnresolved` typed error, `attribute_test_to_sources()`, patch-target secondary extraction, TTL-cached module map, and the pure enforcement predicate `unattributed_test_scope_violation()`.
- `backend/core/ouroboros/governance/intent/signals.py` — MODIFY: versioned `TestFailureAttribution` evidence schema + validator (mirrors `VisionSignalEvidence` discipline at lines 112-295).
- `backend/core/ouroboros/governance/intent/test_watcher.py` — MODIFY: `process_failures` composes attributed `target_files` + evidence block (lines 431-454).
- `backend/core/ouroboros/governance/orchestrator.py` — MODIFY: one deterministic gate call at the SemanticGuardian invocation site (test-file-only mutation on an unresolved-attribution op → `APPROVAL_REQUIRED`).
- Tests: `tests/governance/test_reverse_dep_import_extraction.py`, `tests/governance/intent/test_source_attribution.py`, `tests/governance/intent/test_source_attribution_schema.py`, `tests/governance/intent/test_watcher_attribution.py`, `tests/governance/test_attribution_scope_gate.py`, `tests/governance/intent/test_attribution_e2e_leaf_predicates.py`.
- Docs: CLAUDE.md subsystem bullet; `docs/memory_topics/intake/project_slice6_test_source_attribution.md`.

New env knobs (all registered in Task 7): `JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED` (default `true`), `JARVIS_ATTRIBUTION_MAX_SOURCE_FILES` (default `8`), `JARVIS_ATTRIBUTION_MODULE_MAP_TTL_S` (default `300`), `JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED` (default `true`). Test-dir classification reuses the EXISTING `JARVIS_TEST_DIR_NAMES` (TestRunner's knob) — config-driven, not hardcoded (mandate 1).

---

### Task 1: Factor import extraction + module→path map into `reverse_dep_resolver.py`

**Files:**
- Modify: `backend/core/ouroboros/governance/reverse_dep_resolver.py` (extraction loop at lines 156-182; rglob loop at 149-155)
- Test: `tests/governance/test_reverse_dep_import_extraction.py` (create)

**Interfaces:**
- Consumes: existing `_module_from_relpath`, `_add_relative_import_edges`, `_ast` (module aliases already in the file).
- Produces (later tasks rely on these EXACT signatures):
  - `extract_module_imports(tree: ast.Module, module: str, is_init: bool) -> Set[str]` — absolute dotted import targets of one parsed module. For `from x import y` emits BOTH `x` and `x.y`; resolves relative imports against `module`; never raises on well-formed trees.
  - `build_module_to_path(root: str) -> Dict[str, str]` — `{dotted_module: repo_relative_path}` for every `*.py` under root, deterministic (sorted walk, first-wins on collision).
- Behavioral invariant: `_build_forward_import_graph` output is byte-identical pre/post refactor (existing reverse-dep suite pins it).

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/test_reverse_dep_import_extraction.py
"""Regression spine — Slice 6 Task 1: shared import-extraction +
module→path primitives factored from the forward-graph builder
(DRY mandate: one extractor, two consumers)."""
from __future__ import annotations

import ast
import textwrap

from backend.core.ouroboros.governance.reverse_dep_resolver import (
    _build_forward_import_graph,
    build_module_to_path,
    extract_module_imports,
)


def _extract(src: str, module: str = "tests.governance.test_x", is_init: bool = False):
    return extract_module_imports(ast.parse(textwrap.dedent(src)), module, is_init)


def test_plain_import() -> None:
    assert "backend.core.foo" in _extract("import backend.core.foo")


def test_aliased_import() -> None:
    # ``import x as y`` — the alias never obscures the real target (mandate 4)
    assert "backend.core.foo" in _extract("import backend.core.foo as f")


def test_from_import_emits_module_and_qualified() -> None:
    got = _extract("from backend.core.foo import bar")
    assert "backend.core.foo" in got
    assert "backend.core.foo.bar" in got


def test_from_import_aliased_symbol() -> None:
    got = _extract("from backend.core.foo import bar as b")
    assert "backend.core.foo.bar" in got  # alias.name, not asname


def test_relative_import_resolved_against_package() -> None:
    got = extract_module_imports(
        ast.parse("from . import sibling"),
        "backend.core.pkg.mod",
        False,
    )
    assert "backend.core.pkg.sibling" in got


def test_extraction_matches_forward_graph(tmp_path) -> None:
    """The factored extractor and the graph builder must agree — proof the
    refactor didn't fork semantics."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("from pkg import b\nimport os\n")
    (pkg / "b.py").write_text("")
    graph = _build_forward_import_graph(str(tmp_path))
    src = (pkg / "a.py").read_text()
    direct = extract_module_imports(ast.parse(src), "pkg.a", False)
    assert graph["pkg.a"] == direct


def test_build_module_to_path(tmp_path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("")
    (tmp_path / "top.py").write_text("")
    mapping = build_module_to_path(str(tmp_path))
    assert mapping["pkg"] == "pkg/__init__.py"
    assert mapping["pkg.mod"] == "pkg/mod.py"
    assert mapping["top"] == "top.py"


def test_build_module_to_path_skips_non_py(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("")
    assert build_module_to_path(str(tmp_path)) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_reverse_dep_import_extraction.py -q`
Expected: FAIL — `ImportError: cannot import name 'extract_module_imports'`

- [ ] **Step 3: Implement — factor the extractor, add the mapper**

In `reverse_dep_resolver.py`, insert after `_module_from_relpath` (line 84):

```python
def extract_module_imports(
    tree: "_ast.Module",
    module: str,
    is_init: bool,
) -> Set[str]:
    """Absolute dotted import targets of one parsed module (Slice 6).

    THE single import extractor — factored verbatim from the
    ``_build_forward_import_graph`` walk so the forward graph (source→tests,
    Gate 2) and the test→source attribution bridge (Slice 6) share one
    implementation. ``from x import y`` emits BOTH ``x`` and ``x.y``
    (exact-match-first resolution downstream); ``import x as y`` records
    ``x`` (``alias.name``, never the alias); relative imports resolve
    against *module* via CPython's algorithm (``_add_relative_import_edges``).
    """
    imports: Set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                if alias.name:
                    imports.add(alias.name)
        elif isinstance(node, _ast.ImportFrom):
            if node.level and node.level > 0:
                _add_relative_import_edges(imports, module, is_init, node)
            else:
                mod = node.module or ""
                if mod:
                    imports.add(mod)
                    for alias in node.names:
                        if alias.name:
                            imports.add(f"{mod}.{alias.name}")
    return imports


def build_module_to_path(root: str) -> Dict[str, str]:
    """``{dotted_module: repo-relative path}`` for every ``*.py`` under
    *root* (Slice 6) — the inverse of ``_module_from_relpath``, captured
    from the same walk the forward-graph builder performs. Deterministic:
    sorted traversal, first-wins on the rare ``pkg/__init__.py`` vs
    ``pkg.py`` collision. Unreadability of individual entries is not an
    error (mirrors the graph builder's skip discipline)."""
    mapping: Dict[str, str] = {}
    root_path = Path(root)
    for py_file in sorted(root_path.rglob("*.py")):
        if not py_file.is_file():
            continue
        rel = os.path.relpath(str(py_file), root).replace("\\", "/")
        module = _module_from_relpath(rel)
        if module and module not in mapping:
            mapping[module] = rel
    return mapping
```

Then replace the inlined walk in `_build_forward_import_graph` (the `imports: Set[str] = graph.setdefault(...)` block through the end of the `for node` loop, lines 164-182) with:

```python
        is_init_flag = is_init
        graph.setdefault(module, set()).update(
            extract_module_imports(tree, module, is_init_flag)
        )
```

(keep the existing `is_init` computation at line 156 — only the walk body moves).

- [ ] **Step 4: Run new + existing suites**

Run: `python3 -m pytest tests/governance/test_reverse_dep_import_extraction.py -q && python3 -m pytest tests/ -q -k "reverse_dep"`
Expected: ALL PASS (the `-k reverse_dep` sweep proves the refactor is behavior-preserving)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/reverse_dep_resolver.py tests/governance/test_reverse_dep_import_extraction.py
git commit -m "refactor(slice6): factor extract_module_imports + build_module_to_path from forward-graph builder

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: The attributor — `test_source_attribution.py`

**Files:**
- Create: `backend/core/ouroboros/governance/intent/test_source_attribution.py`
- Test: `tests/governance/intent/test_source_attribution.py` (create)

**Interfaces:**
- Consumes (Task 1): `extract_module_imports(tree, module, is_init) -> Set[str]`, `build_module_to_path(root) -> Dict[str, str]`; existing `_module_from_relpath(rel) -> str`, `_relpath_under_root(path, root) -> str`, `_is_test_module(module, dir_names) -> bool`.
- Produces (Tasks 4/5 rely on these EXACT names):
  - `class AttributionUnresolved(Exception)` with `.reason: str` (one of `"test_outside_root" | "test_file_missing" | "parse_error" | "no_first_party_source_imports"`) and `.detail: str`.
  - `@dataclass(frozen=True) class Attribution: test_locus: str; source_loci: Tuple[str, ...]; method: str; evidence_kinds: Tuple[str, ...]` (paths repo-relative; `method` is `"direct_import"` or `"direct_import+patch_target"`; `evidence_kinds[i]` ∈ `{"direct_import","patch_target"}` per locus).
  - `attribution_enabled() -> bool` (env `JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED`, default true).
  - `attribute_test_to_sources(test_file: str, *, repo_root: str, traceback_frames: Sequence[str] = ()) -> Attribution` — raises `AttributionUnresolved`, never returns an empty `source_loci`.
  - `unattributed_test_scope_violation(intake_evidence_json: str, candidate_files: Sequence[str]) -> Optional[str]` — the Task-5 gate predicate: a human-readable violation string when the op's attribution is `unresolved` AND every candidate file is the test locus (or a test module); `None` otherwise. Fail-soft: malformed JSON → `None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/intent/test_source_attribution.py
"""Regression spine — Slice 6 Task 2: the deterministic AST test→source
attribution bridge. Every case here maps to a mandate: no path heuristics
(a tmp repo whose layout deliberately breaks tests/→src/ mirroring),
alias/relative/indirect handling, multi-source bounding, and fail-fast
on unresolvable attribution."""
from __future__ import annotations

import json
import textwrap

import pytest

from backend.core.ouroboros.governance.intent.test_source_attribution import (
    Attribution,
    AttributionUnresolved,
    attribute_test_to_sources,
    attribution_enabled,
    unattributed_test_scope_violation,
)


@pytest.fixture()
def repo(tmp_path):
    """A tmp repo whose test path deliberately does NOT mirror the source
    path (mandate 1: any tests/foo→src/foo heuristic would fail here)."""
    src = tmp_path / "backend" / "core" / "widgets"
    src.mkdir(parents=True)
    (tmp_path / "backend" / "__init__.py").write_text("")
    (tmp_path / "backend" / "core" / "__init__.py").write_text("")
    (src / "__init__.py").write_text("")
    (src / "gadget.py").write_text("def spin():\n    return 1\n")
    (src / "helper.py").write_text("def aid():\n    return 2\n")
    tdir = tmp_path / "tests" / "unit_checks"   # ≠ backend/core/widgets
    tdir.mkdir(parents=True)
    return tmp_path, tdir


def _write_test(tdir, body: str, name: str = "test_gadget.py"):
    p = tdir / name
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_direct_from_import_resolves(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from backend.core.widgets.gadget import spin
        def test_spin():
            assert spin() == 1
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert attr.source_loci == ("backend/core/widgets/gadget.py",)
    assert attr.test_locus == "tests/unit_checks/test_gadget.py"
    assert attr.method == "direct_import"


def test_aliased_module_import_resolves(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        import backend.core.widgets.gadget as g
        def test_spin():
            assert g.spin() == 1
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert "backend/core/widgets/gadget.py" in attr.source_loci


def test_multi_source_carries_all_bounded(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    monkeypatch.setenv("JARVIS_ATTRIBUTION_MAX_SOURCE_FILES", "8")
    tf = _write_test(tdir, """
        from backend.core.widgets.gadget import spin
        from backend.core.widgets.helper import aid
        def test_both():
            assert spin() + aid() == 3
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert set(attr.source_loci) == {
        "backend/core/widgets/gadget.py",
        "backend/core/widgets/helper.py",
    }


def test_max_source_cap_enforced(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    monkeypatch.setenv("JARVIS_ATTRIBUTION_MAX_SOURCE_FILES", "1")
    tf = _write_test(tdir, """
        from backend.core.widgets.gadget import spin
        from backend.core.widgets.helper import aid
        def test_both():
            assert spin() + aid() == 3
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert len(attr.source_loci) == 1


def test_patch_target_secondary_signal(repo, monkeypatch) -> None:
    """mock.patch target strings recover indirection (the ~17% class)."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from unittest import mock
        def test_spun():
            with mock.patch("backend.core.widgets.gadget.spin", return_value=9):
                assert True
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert "backend/core/widgets/gadget.py" in attr.source_loci
    assert "patch_target" in attr.evidence_kinds


def test_traceback_frames_rank_first(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from backend.core.widgets.gadget import spin
        from backend.core.widgets.helper import aid
        def test_both():
            assert spin() + aid() == 3
    """)
    attr = attribute_test_to_sources(
        tf, repo_root=str(root),
        traceback_frames=("backend/core/widgets/helper.py",),
    )
    assert attr.source_loci[0] == "backend/core/widgets/helper.py"


def test_test_infra_imports_excluded(repo, monkeypatch) -> None:
    """Importing a sibling test helper must NOT attribute to test infra —
    classification is config-driven via JARVIS_TEST_DIR_NAMES (mandate 1:
    no hardcoded directory assumption)."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    (tdir / "__init__.py").write_text("")
    (root / "tests" / "__init__.py").write_text("")
    (tdir / "helpers.py").write_text("X = 1\n")
    tf = _write_test(tdir, """
        from tests.unit_checks.helpers import X
        from backend.core.widgets.gadget import spin
        def test_spin():
            assert spin() == X
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert attr.source_loci == ("backend/core/widgets/gadget.py",)


def test_unresolved_no_first_party_imports(repo, monkeypatch) -> None:
    """Fail-fast (mandate 4): stdlib-only test → typed error, never a
    silent test-file-scoped fallback."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        import os
        def test_env():
            assert os.sep
    """)
    with pytest.raises(AttributionUnresolved) as exc:
        attribute_test_to_sources(tf, repo_root=str(root))
    assert exc.value.reason == "no_first_party_source_imports"


def test_unresolved_parse_error(repo, monkeypatch) -> None:
    root, tdir = repo
    tf = _write_test(tdir, "def broken(:\n")
    with pytest.raises(AttributionUnresolved) as exc:
        attribute_test_to_sources(tf, repo_root=str(root))
    assert exc.value.reason == "parse_error"


def test_unresolved_missing_file(repo) -> None:
    root, _ = repo
    with pytest.raises(AttributionUnresolved) as exc:
        attribute_test_to_sources("tests/nope/test_ghost.py", repo_root=str(root))
    assert exc.value.reason == "test_file_missing"


def test_deterministic_across_calls(repo, monkeypatch) -> None:
    """Same inputs → identical output tuple (mandate 1: deterministic)."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from backend.core.widgets.helper import aid
        from backend.core.widgets.gadget import spin
        def test_both():
            assert spin() + aid() == 3
    """)
    a = attribute_test_to_sources(tf, repo_root=str(root))
    b = attribute_test_to_sources(tf, repo_root=str(root))
    assert a == b


def test_master_switch(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED", "false")
    assert attribution_enabled() is False
    monkeypatch.delenv("JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED", raising=False)
    assert attribution_enabled() is True


# ---- the Task-5 gate predicate (pure function, unit-tested here) ----

def _ev(status: str, test_locus: str = "tests/unit_checks/test_gadget.py") -> str:
    return json.dumps({"attribution": {
        "schema_version": 1, "status": status, "test_locus": test_locus,
        "source_loci": [], "method": "", "reason": "no_first_party_source_imports",
    }})


def test_gate_fires_on_unresolved_test_only_mutation(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    msg = unattributed_test_scope_violation(
        _ev("unresolved"), ["tests/unit_checks/test_gadget.py"],
    )
    assert msg is not None and "unresolved" in msg


def test_gate_silent_when_resolved(monkeypatch) -> None:
    assert unattributed_test_scope_violation(
        _ev("resolved"), ["tests/unit_checks/test_gadget.py"],
    ) is None


def test_gate_silent_when_candidate_touches_source(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    assert unattributed_test_scope_violation(
        _ev("unresolved"),
        ["backend/core/widgets/gadget.py", "tests/unit_checks/test_gadget.py"],
    ) is None


def test_gate_fail_soft_on_malformed_evidence() -> None:
    assert unattributed_test_scope_violation("{not json", ["x.py"]) is None
    assert unattributed_test_scope_violation("", ["x.py"]) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/intent/test_source_attribution.py -q`
Expected: FAIL — `ModuleNotFoundError: ... test_source_attribution`

- [ ] **Step 3: Implement the attributor**

```python
# backend/core/ouroboros/governance/intent/test_source_attribution.py
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
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/governance/intent/test_source_attribution.py -q`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intent/test_source_attribution.py tests/governance/intent/test_source_attribution.py
git commit -m "feat(slice6): deterministic AST test->source attribution bridge + unattributed-scope gate predicate

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Versioned `TestFailureAttribution` evidence schema in `signals.py`

**Files:**
- Modify: `backend/core/ouroboros/governance/intent/signals.py` (add after the `VisionSignalEvidence` block, which ends ~line 295)
- Test: `tests/governance/intent/test_source_attribution_schema.py` (create)

**Interfaces:**
- Produces (Task 4 relies on): `TEST_FAILURE_ATTRIBUTION_SCHEMA_VERSION = 1`; `build_attribution_evidence(*, status: str, test_locus: str, source_loci: Sequence[str] = (), method: str = "", reason: str = "") -> Dict[str, Any]`; `validate_attribution_evidence(obj: Any) -> Tuple[bool, str]` (returns `(ok, error_message)`).
- The evidence block lives under the `"attribution"` key of `IntentSignal.evidence` — the schema-versioned discipline `VisionSignalEvidence` established, applied to the TestFailure lane per mandate 3.

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/intent/test_source_attribution_schema.py
"""Slice 6 Task 3 — versioned attribution evidence schema (mirrors the
VisionSignalEvidence validate discipline; TestFailure evidence was
previously schema-free)."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.intent.signals import (
    TEST_FAILURE_ATTRIBUTION_SCHEMA_VERSION,
    build_attribution_evidence,
    validate_attribution_evidence,
)


def test_build_resolved_block() -> None:
    block = build_attribution_evidence(
        status="resolved",
        test_locus="tests/g/test_leaf.py",
        source_loci=["backend/core/leaf.py"],
        method="direct_import",
    )
    assert block["schema_version"] == TEST_FAILURE_ATTRIBUTION_SCHEMA_VERSION
    ok, err = validate_attribution_evidence(block)
    assert ok, err


def test_build_unresolved_block_requires_reason() -> None:
    block = build_attribution_evidence(
        status="unresolved",
        test_locus="tests/g/test_leaf.py",
        reason="no_first_party_source_imports",
    )
    ok, err = validate_attribution_evidence(block)
    assert ok, err


@pytest.mark.parametrize("mutation,expected_err", [
    ({"status": "banana"}, "status"),
    ({"source_loci": "not-a-list"}, "source_loci"),
    ({"test_locus": ""}, "test_locus"),
    ({"schema_version": 99}, "schema_version"),
])
def test_validator_rejects(mutation, expected_err) -> None:
    block = build_attribution_evidence(
        status="resolved",
        test_locus="tests/g/test_leaf.py",
        source_loci=["backend/core/leaf.py"],
        method="direct_import",
    )
    block.update(mutation)
    ok, err = validate_attribution_evidence(block)
    assert not ok
    assert expected_err in err


def test_resolved_requires_nonempty_source_loci() -> None:
    block = build_attribution_evidence(
        status="resolved", test_locus="tests/g/test_leaf.py",
        source_loci=[], method="direct_import",
    )
    ok, err = validate_attribution_evidence(block)
    assert not ok and "source_loci" in err
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/intent/test_source_attribution_schema.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_attribution_evidence'`

- [ ] **Step 3: Implement in `signals.py`** (insert after the Vision validator block, keeping its conventions)

```python
# ---------------------------------------------------------------------------
# TestFailure attribution evidence (Slice 6) — schema-versioned, mirrors the
# VisionSignalEvidence discipline. Lives under evidence["attribution"].
# ---------------------------------------------------------------------------

TEST_FAILURE_ATTRIBUTION_SCHEMA_VERSION = 1

_ATTRIBUTION_STATUSES = ("resolved", "unresolved", "disabled")


def build_attribution_evidence(
    *,
    status: str,
    test_locus: str,
    source_loci: Sequence[str] = (),
    method: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    """Construct the ``evidence['attribution']`` block (Slice 6).

    ``status``: ``resolved`` (source_loci non-empty, method set) |
    ``unresolved`` (reason set — the typed fail-fast) | ``disabled``
    (master switch off; scope stays legacy test-locus)."""
    return {
        "schema_version": TEST_FAILURE_ATTRIBUTION_SCHEMA_VERSION,
        "status": status,
        "test_locus": test_locus,
        "source_loci": list(source_loci),
        "method": method,
        "reason": reason,
    }


def validate_attribution_evidence(obj: Any) -> Tuple[bool, str]:
    """(ok, error). Structural validation only — deterministic, no IO."""
    if not isinstance(obj, dict):
        return False, "attribution evidence must be a dict"
    if obj.get("schema_version") != TEST_FAILURE_ATTRIBUTION_SCHEMA_VERSION:
        return False, (
            f"schema_version must be {TEST_FAILURE_ATTRIBUTION_SCHEMA_VERSION}"
        )
    status = obj.get("status")
    if status not in _ATTRIBUTION_STATUSES:
        return False, f"status must be one of {_ATTRIBUTION_STATUSES}"
    test_locus = obj.get("test_locus")
    if not isinstance(test_locus, str) or not test_locus:
        return False, "test_locus must be a non-empty string"
    loci = obj.get("source_loci")
    if not isinstance(loci, list) or not all(
        isinstance(p, str) and p for p in loci
    ):
        return False, "source_loci must be a list of non-empty strings"
    if status == "resolved":
        if not loci:
            return False, "resolved attribution requires non-empty source_loci"
        if not obj.get("method"):
            return False, "resolved attribution requires method"
    if status == "unresolved" and not obj.get("reason"):
        return False, "unresolved attribution requires reason"
    return True, ""
```

(`Sequence`, `Dict`, `Any`, `Tuple` are already imported in signals.py for the Vision block; verify and extend the import line only if missing.)

- [ ] **Step 4: Run schema tests + full signals suite**

Run: `python3 -m pytest tests/governance/intent/test_source_attribution_schema.py -q && python3 -m pytest tests/ -q -k "signals or vision_signal"`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intent/signals.py tests/governance/intent/test_source_attribution_schema.py
git commit -m "feat(slice6): versioned TestFailure attribution evidence schema (test-locus + source-loci)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Wire attribution into `TestWatcher.process_failures`

**Files:**
- Modify: `backend/core/ouroboros/governance/intent/test_watcher.py:424-454` (the streak/mint loop)
- Test: `tests/governance/intent/test_watcher_attribution.py` (create)

**Interfaces:**
- Consumes: Task 2 `attribute_test_to_sources` / `AttributionUnresolved` / `attribution_enabled`; Task 3 `build_attribution_evidence`.
- Produces: signals whose `target_files == (*source_loci, test_locus)` when resolved; `(test_locus,)` + `evidence["attribution"]["status"]=="unresolved"` on fail-fast; byte-identical legacy behavior when the master switch is off (`status=="disabled"`). This is THE chokepoint — both the subprocess poll path and the plugin/sensor path converge on `process_failures`, so one wiring site covers both (verified in the Run-16 investigation).

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/intent/test_watcher_attribution.py
"""Slice 6 Task 4 — the pin (test_watcher.py:444 `target_files=(f.file_path,)`)
is replaced by attributed scope. Uses a real tmp repo (no mocks of the
attributor — feedback_fakes_must_mirror_real_contract)."""
from __future__ import annotations

import textwrap

import pytest

from backend.core.ouroboros.governance.intent.test_watcher import (
    TestFailure,
    TestWatcher,
)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    src = tmp_path / "backend" / "mod"
    src.mkdir(parents=True)
    (tmp_path / "backend" / "__init__.py").write_text("")
    (src / "__init__.py").write_text("")
    (src / "engine.py").write_text("def go():\n    return 1\n")
    tdir = tmp_path / "tests"
    tdir.mkdir()
    (tdir / "test_engine.py").write_text(textwrap.dedent("""
        from backend.mod.engine import go
        def test_go():
            assert go() == 1
    """))
    return tmp_path


def _fail(path: str) -> TestFailure:
    return TestFailure(
        test_id=f"{path}::test_go",
        file_path=path,
        error_text="AssertionError: boom",
    )


def _stable_signal(watcher, failure):
    """Two consecutive runs → stable signal on the second."""
    assert watcher.process_failures([failure]) == []
    signals = watcher.process_failures([failure])
    assert len(signals) == 1
    return signals[0]


def test_resolved_scope_contains_source_and_test(repo) -> None:
    w = TestWatcher(repo="jarvis", repo_path=str(repo))
    sig = _stable_signal(w, _fail("tests/test_engine.py"))
    assert sig.target_files == (
        "backend/mod/engine.py",   # source-locus FIRST (primary repair target)
        "tests/test_engine.py",    # test-locus retained (legit test fixes stay in scope)
    )
    att = sig.evidence["attribution"]
    assert att["status"] == "resolved"
    assert att["source_loci"] == ["backend/mod/engine.py"]
    assert att["test_locus"] == "tests/test_engine.py"


def test_unresolved_fail_fast_keeps_test_scope_and_marks_evidence(repo) -> None:
    (repo / "tests" / "test_lonely.py").write_text(
        "import os\ndef test_x():\n    assert os.sep\n"
    )
    w = TestWatcher(repo="jarvis", repo_path=str(repo))
    sig = _stable_signal(w, _fail("tests/test_lonely.py"))
    assert sig.target_files == ("tests/test_lonely.py",)
    att = sig.evidence["attribution"]
    assert att["status"] == "unresolved"
    assert att["reason"] == "no_first_party_source_imports"


def test_master_switch_off_is_byte_identical_legacy(repo, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED", "false")
    w = TestWatcher(repo="jarvis", repo_path=str(repo))
    sig = _stable_signal(w, _fail("tests/test_engine.py"))
    assert sig.target_files == ("tests/test_engine.py",)
    assert sig.evidence["attribution"]["status"] == "disabled"


def test_attributor_crash_never_blocks_signal(repo, monkeypatch) -> None:
    """A broken attributor must degrade to legacy scope, not eat the
    signal (fail-soft on unexpected faults; fail-FAST is reserved for
    the typed AttributionUnresolved)."""
    import backend.core.ouroboros.governance.intent.test_watcher as tw

    def _boom(*a, **k):
        raise RuntimeError("attributor exploded")

    monkeypatch.setattr(tw, "attribute_test_to_sources", _boom)
    w = TestWatcher(repo="jarvis", repo_path=str(repo))
    sig = _stable_signal(w, _fail("tests/test_engine.py"))
    assert sig.target_files == ("tests/test_engine.py",)


def test_evidence_signature_unchanged_for_dedup_continuity(repo) -> None:
    """The dedup 'signature' field format must not change (existing
    dedup behavior keyed on it)."""
    w = TestWatcher(repo="jarvis", repo_path=str(repo))
    sig = _stable_signal(w, _fail("tests/test_engine.py"))
    assert sig.evidence["signature"] == "AssertionError: boom:tests/test_engine.py"
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/intent/test_watcher_attribution.py -q`
Expected: FAIL — resolved-scope test asserts source file in target_files; current code emits `(test_file,)` only. (The import of `attribute_test_to_sources` from `test_watcher` also fails until wired.)

- [ ] **Step 3: Wire the mint block** — in `test_watcher.py`, add to the module imports:

```python
from backend.core.ouroboros.governance.intent.test_source_attribution import (
    AttributionUnresolved,
    attribute_test_to_sources,
    attribution_enabled,
)
from backend.core.ouroboros.governance.intent.signals import (
    build_attribution_evidence,
)
```

(fold into the existing `signals` import if one exists — check the top of the file.)

Then replace the mint block (lines 431-454, the `evidence` dict through `signals.append(signal)`) with:

```python
                evidence: Dict[str, Any] = {
                    "signature": f"{f.error_text}:{f.file_path}",
                    "test_id": f.test_id,
                    "streak": streak,
                    "error_text": f.error_text,
                }
                # Repair Context Bridge (Slice 1): additive traceback enrichment.
                if f.traceback_evidence:
                    evidence.update(f.traceback_evidence)

                # Slice 6 — deterministic test→source attribution. Resolved:
                # scope = (*sources, test) so APPLY can repair the module
                # under test AND the file_scope_mismatch guard passes correct
                # source patches. Unresolved: typed fail-fast — scope stays
                # test-locus, evidence carries the error, and the orchestrator
                # attribution gate forbids blind test-only mutation. Any
                # UNEXPECTED attributor fault degrades to legacy scope
                # (fail-soft: never eat a real failure signal).
                target_files: Tuple[str, ...] = (f.file_path,)
                if not attribution_enabled():
                    evidence["attribution"] = build_attribution_evidence(
                        status="disabled", test_locus=f.file_path,
                    )
                else:
                    try:
                        _tb_frames = tuple(
                            fr.get("file", "") if isinstance(fr, dict) else str(fr)
                            for fr in (evidence.get("traceback_frames") or ())
                        )
                        attr = attribute_test_to_sources(
                            f.file_path,
                            repo_root=self.repo_path,
                            traceback_frames=_tb_frames,
                        )
                        target_files = (*attr.source_loci, attr.test_locus)
                        evidence["attribution"] = build_attribution_evidence(
                            status="resolved",
                            test_locus=attr.test_locus,
                            source_loci=attr.source_loci,
                            method=attr.method,
                        )
                        logger.info(
                            "[Attribution] %s -> %s (method=%s)",
                            f.file_path, list(attr.source_loci), attr.method,
                        )
                    except AttributionUnresolved as exc:
                        evidence["attribution"] = build_attribution_evidence(
                            status="unresolved",
                            test_locus=f.file_path,
                            reason=exc.reason,
                        )
                        logger.warning(
                            "[Attribution] FAIL-FAST unresolved for %s: %s "
                            "— scope stays test-locus; blind test mutation "
                            "gated at the orchestrator",
                            f.file_path, exc,
                        )
                    except Exception:  # noqa: BLE001 — fail-soft, never eat the signal
                        evidence["attribution"] = build_attribution_evidence(
                            status="unresolved",
                            test_locus=f.file_path,
                            reason="attributor_fault",
                        )
                        logger.warning(
                            "[Attribution] attributor fault for %s — legacy "
                            "scope retained", f.file_path, exc_info=True,
                        )

                signal = IntentSignal(
                    source="intent:test_failure",
                    target_files=target_files,
                    repo=self.repo,
                    description=(
                        f"Stable test failure: {f.test_id} "
                        f"(streak={streak}): {f.error_text}"
                    ),
                    evidence=evidence,
                    confidence=confidence,
                    stable=True,
                )
                signals.append(signal)
```

(`Tuple` must be present in the file's `typing` import — add if missing.)

- [ ] **Step 4: Run new tests + the full watcher/sensor suites**

Run: `python3 -m pytest tests/governance/intent/test_watcher_attribution.py -q && python3 -m pytest tests/ -q -k "test_watcher or test_failure_sensor"`
Expected: new suite ALL PASS. If any existing watcher/sensor test pins `target_files == (test_file,)` on a repo-real test fixture, it now legitimately carries source loci — update those pins to the new contract (they are pinning the Run-16 bug) and note each in the commit body.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intent/test_watcher.py tests/governance/intent/test_watcher_attribution.py
git commit -m "feat(slice6): TestWatcher mints attributed scope (source-loci + test-locus) with typed fail-fast

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Orchestrator attribution gate — no blind test-file mutation

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (at the SemanticGuardian post-VALIDATE invocation — locate with `grep -n "SemanticGuard" backend/core/ouroboros/governance/orchestrator.py`)
- Test: `tests/governance/test_attribution_scope_gate.py` (create)

**Interfaces:**
- Consumes: Task 2 `unattributed_test_scope_violation(intake_evidence_json, candidate_files) -> Optional[str]` (pure, already fully unit-tested in Task 2) and `ctx.intake_evidence_json` (existing `OperationContext` field, `op_context.py:1024`).
- Produces: when the predicate returns a violation, the op's risk tier is escalated exactly the way a SemanticGuardian HARD finding escalates at that site (→ `APPROVAL_REQUIRED`), with the violation string logged as `[Attribution] gate: <message> op=<id>`. NOT a rejection — a human gate: an unresolved-attribution test-file fix might be legitimate (the test itself may be wrong), so it needs eyes, not a retry loop.

- [ ] **Step 1: Write the failing integration test**

The test drives the gate through the same seam the guardian uses. First read the guardian invocation site (grep above) to identify the enclosing method and how hard findings map to the risk decision; the test then mirrors an existing SemanticGuardian escalation test — find one with `grep -rn "APPROVAL_REQUIRED" tests/governance/ -l | xargs grep -ln "SemanticGuard\|semantic_guard" | head -3` and copy its harness pattern. The assertion contract:

```python
# tests/governance/test_attribution_scope_gate.py
"""Slice 6 Task 5 — an op whose evidence carries attribution
status=unresolved MUST NOT auto-apply a candidate that mutates only
test loci: risk escalates to APPROVAL_REQUIRED (mirrors SemanticGuardian
hard-finding escalation at the same site)."""
from __future__ import annotations

import json

# Harness imports mirror the chosen SemanticGuardian escalation test —
# the implementer copies that file's fixture pattern (orchestrator or
# phase-runner level, whichever that suite uses).


UNRESOLVED_EVIDENCE = json.dumps({
    "attribution": {
        "schema_version": 1,
        "status": "unresolved",
        "test_locus": "tests/test_engine.py",
        "source_loci": [],
        "method": "",
        "reason": "no_first_party_source_imports",
    }
})


def test_unresolved_test_only_candidate_escalates_to_approval(...):
    # ctx with intake_evidence_json=UNRESOLVED_EVIDENCE,
    # candidate files == ["tests/test_engine.py"]
    # -> assert final risk tier is APPROVAL_REQUIRED and the log/decision
    #    reason contains "attribution_unresolved_test_scope"
    ...


def test_resolved_attribution_never_escalates(...):
    # same candidate, evidence status="resolved" -> tier unchanged
    ...


def test_gate_master_switch_off(...):
    # JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED=false -> tier unchanged
    ...
```

(The `...` bodies are filled by copying the existing escalation-test harness — the three assertions above are the contract; the predicate itself is already fully unit-tested in Task 2, so this test's job is ONLY the wiring + escalation mapping.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/test_attribution_scope_gate.py -q`
Expected: FAIL — escalation does not occur (gate not wired).

- [ ] **Step 3: Wire the gate** — at the SemanticGuardian invocation site in `orchestrator.py`, immediately after guardian findings are collected and before the risk decision is finalized, insert:

```python
        # Slice 6 — attribution scope gate: an unresolved-attribution op
        # whose candidate mutates ONLY test loci is the Run-16 blind class.
        # Escalate to human approval (not reject: the test itself may be
        # the legitimate fix target — that judgment needs eyes).
        try:
            from backend.core.ouroboros.governance.intent.test_source_attribution import (  # noqa: E501
                unattributed_test_scope_violation,
            )
            _attr_violation = unattributed_test_scope_violation(
                getattr(ctx, "intake_evidence_json", "") or "",
                _candidate_file_paths,   # the same file list the guardian inspected
            )
        except Exception:  # noqa: BLE001 — gate is protective, never fatal
            _attr_violation = None
        if _attr_violation:
            logger.warning(
                "[Attribution] gate: %s op=%s", _attr_violation, ctx.op_id,
            )
            # escalate exactly as a SemanticGuardian HARD finding does at
            # this site (same variable/flow — see adjacent hard-finding
            # branch), with pattern name "attribution_unresolved_test_scope".
```

`_candidate_file_paths` = whatever local the guardian invocation already iterates (single `file_path` or the multi-file candidate list — reuse it verbatim; do not recompute). The escalation lines duplicate the adjacent hard-finding branch's mechanics with the new pattern name.

- [ ] **Step 4: Run the gate test + guardian regression**

Run: `python3 -m pytest tests/governance/test_attribution_scope_gate.py -q && python3 -m pytest tests/ -q -k "semantic_guard"`
Expected: ALL PASS (guardian's 47 regression cases untouched).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py tests/governance/test_attribution_scope_gate.py
git commit -m "feat(slice6): orchestrator attribution gate — unresolved-attribution test-only mutations require approval

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: End-to-end Run-#16 scenario pin

**Files:**
- Test: `tests/governance/intent/test_attribution_e2e_leaf_predicates.py` (create)

**Interfaces:**
- Consumes: everything above, against the REAL repo tree (not a tmp fixture) — the exact Run-16 pair: `tests/governance/a1_ignition_vector/test_leaf_predicates.py` → `backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py`.

- [ ] **Step 1: Write the test (it should pass immediately if Tasks 1-4 are correct — it is the scenario pin, red only if the bridge regresses)**

```python
# tests/governance/intent/test_attribution_e2e_leaf_predicates.py
"""Slice 6 Task 6 — THE Run #16 scenario, pinned against the real repo:
the exact signal that died at VERIFY (pass_rate=0.75) must now carry
the source under test in target_files. If this test ever goes red, the
attribution bridge has regressed and autonomous test-failure repair is
structurally dead again (ops can only mutate the test file)."""
from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance.intent.test_source_attribution import (
    attribute_test_to_sources,
)
from backend.core.ouroboros.governance.intent.test_watcher import (
    TestFailure,
    TestWatcher,
)

_REPO = str(Path(__file__).resolve().parents[3])
_TEST = "tests/governance/a1_ignition_vector/test_leaf_predicates.py"
_SOURCE = "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py"


def test_run16_pair_attributes_directly() -> None:
    attr = attribute_test_to_sources(_TEST, repo_root=_REPO)
    assert _SOURCE in attr.source_loci
    assert attr.method == "direct_import"


def test_run16_signal_scope_contains_source() -> None:
    w = TestWatcher(repo="jarvis", repo_path=_REPO)
    f = TestFailure(
        test_id=f"{_TEST}::test_clamp01",
        file_path=_TEST,
        error_text="AssertionError: clamp01(2.0) != 1.0",
    )
    w.process_failures([f])
    signals = w.process_failures([f])
    assert len(signals) == 1
    assert _SOURCE in signals[0].target_files
    assert _TEST in signals[0].target_files
    assert signals[0].evidence["attribution"]["status"] == "resolved"
```

- [ ] **Step 2: Run it**

Run: `python3 -m pytest tests/governance/intent/test_attribution_e2e_leaf_predicates.py -q`
Expected: ALL PASS. (If `test_run16_pair_attributes_directly` fails, debug the bridge against the real tree BEFORE proceeding — this is the acceptance bar.)

- [ ] **Step 3: Full slice regression sweep**

Run: `python3 -m pytest tests/governance/test_reverse_dep_import_extraction.py tests/governance/intent/test_source_attribution.py tests/governance/intent/test_source_attribution_schema.py tests/governance/intent/test_watcher_attribution.py tests/governance/test_attribution_scope_gate.py tests/governance/intent/test_attribution_e2e_leaf_predicates.py -q && python3 -m pytest tests/ -q -k "reverse_dep or test_watcher or test_failure_sensor or semantic_guard"`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add tests/governance/intent/test_attribution_e2e_leaf_predicates.py
git commit -m "test(slice6): pin the Run #16 leaf_predicates scenario end-to-end

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Documentation + flag registry

**Files:**
- Modify: `CLAUDE.md` (Key Subsystems list)
- Modify: `backend/core/ouroboros/governance/flag_registry_seed.py` (register the 4 new flags — follow the existing seed-entry shape in that file)
- Create: `docs/memory_topics/intake/project_slice6_test_source_attribution.md`

- [ ] **Step 1: CLAUDE.md bullet** (add under Key Subsystems, after the TestRunner entry):

```markdown
- **Test→Source Attribution Bridge — Slice 6** (`intent/test_source_attribution.py` + `reverse_dep_resolver.extract_module_imports`/`build_module_to_path`): TestFailure signals carry BOTH loci — `target_files=(*source_loci, test_locus)` resolved by deterministic AST import tracing of the failing test module (alias/relative/mock.patch-target aware; traceback frames rank, never decide). Unresolvable → typed `AttributionUnresolved` fail-fast: scope stays test-locus, evidence `attribution.status=unresolved`, and the orchestrator gate escalates test-only mutations to APPROVAL_REQUIRED (kills the Run-16 blind class). Evidence block schema-versioned (`build_attribution_evidence`, mirrors Vision discipline). Masters: `JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED` + `JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED` default-TRUE; knobs `JARVIS_ATTRIBUTION_MAX_SOURCE_FILES` (8), `JARVIS_ATTRIBUTION_MODULE_MAP_TTL_S` (300).
```

- [ ] **Step 2: flag registry seed entries** — add the four flags with type/category/source_file/example per the file's existing entry format.

- [ ] **Step 3: memory topic file** — `docs/memory_topics/intake/project_slice6_test_source_attribution.md`: the Run-16 evidence chain (pin at `test_watcher.py:444`, double-bind at `doubleword_provider.py` scope check, VERIFY kill), the mandate set, the substrate-audit rationale (why `reverse_dep_resolver` over miner/staleness), and the feasibility distribution (73/17/6/5).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md backend/core/ouroboros/governance/flag_registry_seed.py docs/memory_topics/intake/project_slice6_test_source_attribution.md
git commit -m "docs(slice6): attribution bridge subsystem docs + flag registry + memory topic

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Verification (whole slice)

1. All six new suites green + the `-k "reverse_dep or test_watcher or test_failure_sensor or semantic_guard or signals"` sweep green.
2. The acceptance bar is **Run #16 re-fired**: `scripts/ignite_a1_soak.py --max-wall-seconds 5000` (Docker up, no chaos pre-arm — driver injects post-boot). Success signals, in order: chaos inject red → TestFailure signal whose debug.log line shows `[Attribution] tests/.../test_leaf_predicates.py -> ['backend/.../leaf_predicates.py']` → op `target_files` contains the SOURCE file → adversary's manifest full-file repair now passes the scope gate → APPLY targets the source → VERIFY pass_rate=1.0 → AutoCommit. That run is conducted per `feedback_agent_conducted_soak_delegation` and is NOT part of this plan's tasks (user decides when to ignite).

## Deferred (explicitly out of scope, YAGNI)

- Transitive import closure (test → helper module → source): direct imports cover the measured 73% + patch targets 17%; add depth-2 only if a live run shows a real miss (`attribution.method` telemetry will say so).
- Dynamic `importlib.import_module("literal")` string extraction (~8% of files, mostly resolvable statically) — same telemetry-gated follow-up.
- Rewriting `SemanticTriage` REDIRECT to mutate scope (OperationContext immutability makes that its own slice).
- conftest fixture *tracing*: measured reality — the repo's conftest fixtures are test infrastructure (env isolation, sys.path), not source-module wrappers; the config-driven test-tree exclusion already classifies them correctly. Revisit only on live evidence.
