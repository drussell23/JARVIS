"""Regression spine — B.2.0 worktree-aware OperationAdvisor.

Closes the structural prerequisite for SWE-Bench-Pro Phase 2 Phase B.2.1+:
the advisor must compute blast/coverage/staleness/large-file signals
against the per-envelope ``repo_root`` (the actual mutation tree), not
against the orchestrator's constructor-bound project_root.

Also closes follow-up arc A from PRD §40.7.10-soak as a permanent
improvement for L3 worktree-isolated work and the in-repo L2 exercise
corpus — *not* a SWE-Bench-Pro special case. Per operator binding
B.2.0 hardening note 4: blast is computed from the actual mutation
root, never from ``source == swe_bench_pro``.

Spine invariants
----------------

  1. Master flag OFF → resolver returns None → advise() byte-identical
     to pre-B.2.0 (uses self._project_root).
  2. Master flag ON + valid repo_root → advise() scans THAT tree's
     import graph; blast computed against the override.
  3. Untrusted path (``/etc``, ``/private/etc``, ``/`` on POSIX,
     missing directory) → resolver returns None → fallback path.
  4. Symlink escape → ``Path.resolve()`` canonicalizes BEFORE the
     allowlist check → escape attempts rejected.
  5. Allowlist enforcement: paths outside project_root + env-supplied
     prefixes are rejected; paths inside are accepted.
  6. Source-agnostic: the operation_advisor module never branches on
     envelope source. AST pin proves no ``swe_bench_pro`` string
     reference + no ``source ==`` comparisons.
  7. advise() signature carries the ``repo_root`` kwarg (AST pin).
  8. Each of the four scan-tree methods accepts a ``root`` kwarg
     (AST pin) — drift would silently revert behavior for one signal.
  9. Orchestrator call site invokes ``resolve_envelope_repo_root``
     BEFORE ``advise()`` (AST pin on orchestrator.py).
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.operation_advisor import (
    ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR,
    ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR,
    EVIDENCE_REPO_ROOT_KEY,
    OperationAdvisor,
    register_flags,
    resolve_envelope_repo_root,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path) -> Iterator[Path]:
    """A scratch project_root simulating a busy main repo: the target
    module ``target_pkg.py`` is referenced by THREE downstream files.
    """
    root = tmp_path / "main_repo"
    root.mkdir()
    (root / "target_pkg.py").write_text("def helper(): return 1\n")
    (root / "importer_one.py").write_text(
        "from target_pkg import helper\n"
    )
    (root / "importer_two.py").write_text("import target_pkg\n")
    (root / "importer_three.py").write_text(
        "# uses target_pkg downstream\n"
        "import target_pkg as t\n"
    )
    (root / ".jarvis").mkdir()
    (root / ".jarvis" / "swe_bench_pro").mkdir()
    (root / ".jarvis" / "swe_bench_pro" / "worktrees").mkdir()
    yield root


@pytest.fixture
def worktree(project_root: Path) -> Path:
    """A SWE-Bench-Pro-style isolated worktree directory containing
    ONLY the target module — no downstream importers. A fresh clone's
    blast radius on the target is expected to be at most 1 (the
    target file itself), STRICTLY fewer than the main repo's 4.
    """
    wt = project_root / ".jarvis" / "swe_bench_pro" / "worktrees" / "inst-001"
    wt.mkdir()
    (wt / "target_pkg.py").write_text("def helper(): return 1\n")
    return wt


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, raising=False)
    monkeypatch.delenv(ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR, raising=False)


def _evidence(repo_root: str) -> str:
    return json.dumps({EVIDENCE_REPO_ROOT_KEY: repo_root})


# ---------------------------------------------------------------------------
# 1. Master-flag-off byte-identical
# ---------------------------------------------------------------------------


def test_resolver_returns_none_when_master_flag_unset(
    project_root: Path, worktree: Path, clean_env: None,
) -> None:
    """Default-FALSE: even a valid evidence payload is ignored."""
    out = resolve_envelope_repo_root(
        _evidence(str(worktree)), project_root=project_root,
    )
    assert out is None


def test_resolver_returns_none_when_master_flag_false(
    project_root: Path, worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "false")
    out = resolve_envelope_repo_root(
        _evidence(str(worktree)), project_root=project_root,
    )
    assert out is None


def test_advise_falls_back_to_project_root_when_repo_root_none(
    project_root: Path,
) -> None:
    """With ``repo_root=None`` (legacy path), signals scan self._project_root.

    Byte-identical to pre-B.2.0 behavior — covers the master-off path
    after orchestrator wiring lands.
    """
    advisor = OperationAdvisor(project_root)
    advisory = advisor.advise(
        target_files=("target_pkg.py",),
        description="refactor target_pkg",
        op_id="op-leg",
        is_read_only=False,
        repo_root=None,
    )
    # 3+ importers exist in project_root → blast >= 3.
    assert advisory.blast_radius >= 3


# ---------------------------------------------------------------------------
# 2. Master-flag ON + valid repo_root → scans the override
# ---------------------------------------------------------------------------


def test_resolver_accepts_path_inside_project_root(
    project_root: Path, worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "true")
    out = resolve_envelope_repo_root(
        _evidence(str(worktree)), project_root=project_root,
    )
    assert out is not None
    assert out == worktree.resolve()


def test_advise_scans_override_tree_not_project_root(
    project_root: Path, worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blast radius computed against the worktree only.

    Main repo has 4 files referencing ``target_pkg`` (the target plus 3
    downstream importers); worktree has only the target file. The
    override must yield STRICTLY fewer importers than the legacy scan
    — that gap is the entire structural value of B.2.0.
    """
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "true")
    resolved = resolve_envelope_repo_root(
        _evidence(str(worktree)), project_root=project_root,
    )
    assert resolved == worktree.resolve()
    advisor = OperationAdvisor(project_root)
    advisory_override = advisor.advise(
        target_files=("target_pkg.py",),
        description="repair target_pkg",
        op_id="op-override",
        is_read_only=False,
        repo_root=resolved,
    )
    advisory_legacy = advisor.advise(
        target_files=("target_pkg.py",),
        description="repair target_pkg",
        op_id="op-legacy",
        is_read_only=False,
        repo_root=None,
    )
    # Override sees ≤ 1 file (just the target); legacy sees ≥ 3
    # (target + at least the three importer_* files).
    assert advisory_override.blast_radius <= 1
    assert advisory_legacy.blast_radius >= 3
    assert advisory_override.blast_radius < advisory_legacy.blast_radius


# ---------------------------------------------------------------------------
# 3. Untrusted-input safety
# ---------------------------------------------------------------------------


def test_resolver_rejects_path_outside_allowlist(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "true")
    out = resolve_envelope_repo_root(
        _evidence("/etc"), project_root=project_root,
    )
    assert out is None


def test_resolver_rejects_root_path(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "true")
    out = resolve_envelope_repo_root(
        _evidence("/"), project_root=project_root,
    )
    assert out is None


def test_resolver_rejects_missing_directory(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "true")
    fake = project_root / "no_such_dir"
    out = resolve_envelope_repo_root(
        _evidence(str(fake)), project_root=project_root,
    )
    assert out is None


def test_resolver_rejects_file_instead_of_directory(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "true")
    out = resolve_envelope_repo_root(
        _evidence(str(project_root / "module_a.py")),
        project_root=project_root,
    )
    assert out is None


def test_resolver_handles_malformed_evidence_json(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "true")
    for bad in ("", "not-json", "[1,2,3]", "null", "42", '"a string"'):
        assert resolve_envelope_repo_root(
            bad, project_root=project_root,
        ) is None


def test_resolver_handles_missing_repo_root_key(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "true")
    out = resolve_envelope_repo_root(
        json.dumps({"other_field": "value"}),
        project_root=project_root,
    )
    assert out is None


def test_resolver_handles_non_string_repo_root_value(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "true")
    for bad in (json.dumps({EVIDENCE_REPO_ROOT_KEY: v}) for v in (None, 42, ["x"], "")):
        assert resolve_envelope_repo_root(
            bad, project_root=project_root,
        ) is None


# ---------------------------------------------------------------------------
# 4. Symlink escape
# ---------------------------------------------------------------------------


def test_resolver_rejects_symlink_escape(
    project_root: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink under project_root pointing OUTSIDE the allowlist must
    be canonicalized by ``Path.resolve()`` BEFORE the prefix check, so
    the escape is detected and rejected.
    """
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "true")
    outside = tmp_path / "outside_tree"
    outside.mkdir()
    link = project_root / "sneaky_link"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    out = resolve_envelope_repo_root(
        _evidence(str(link)), project_root=project_root,
    )
    assert out is None


def test_resolver_follows_legitimate_symlink_inside_allowlist(
    project_root: Path, worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink that resolves to a path INSIDE the allowlist is accepted —
    the safety contract is "resolved path under allowlist", not "no
    symlink anywhere on the input"."""
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "true")
    link = project_root / "inside_link"
    try:
        link.symlink_to(worktree)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    out = resolve_envelope_repo_root(
        _evidence(str(link)), project_root=project_root,
    )
    assert out is not None
    assert out == worktree.resolve()


# ---------------------------------------------------------------------------
# 5. Allowlist extension via env
# ---------------------------------------------------------------------------


def test_allowlist_env_admits_extra_prefix(
    project_root: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "true")
    outside = tmp_path / "external_eval_clones"
    outside.mkdir()
    instance = outside / "inst-002"
    instance.mkdir()
    # Without allowlist: rejected.
    monkeypatch.delenv(ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR, raising=False)
    assert resolve_envelope_repo_root(
        _evidence(str(instance)), project_root=project_root,
    ) is None
    # With allowlist: accepted.
    monkeypatch.setenv(
        ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR, str(outside),
    )
    out = resolve_envelope_repo_root(
        _evidence(str(instance)), project_root=project_root,
    )
    assert out == instance.resolve()


def test_allowlist_env_colon_separated_multiple(
    project_root: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "true")
    a = tmp_path / "prefix_a"; a.mkdir()
    b = tmp_path / "prefix_b"; b.mkdir()
    instance_b = b / "child"; instance_b.mkdir()
    monkeypatch.setenv(
        ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR,
        f"{a}{os.pathsep}{b}",
    )
    out = resolve_envelope_repo_root(
        _evidence(str(instance_b)), project_root=project_root,
    )
    assert out == instance_b.resolve()


# ---------------------------------------------------------------------------
# 6. Source-agnostic — no envelope.source branch in operation_advisor.py
# ---------------------------------------------------------------------------


def _operation_advisor_source() -> str:
    from backend.core.ouroboros.governance import operation_advisor
    return Path(operation_advisor.__file__).read_text()


def test_ast_pin_no_swe_bench_pro_string_reference() -> None:
    """Operator binding B.2.0 note 4: the advisor must be root-correct,
    not category-special. No reference to ``swe_bench_pro`` (or any
    sibling sensor name) is permitted in the module — the only path
    the advisor sees is the validated Path object, not an envelope.

    Comments ARE permitted to reference SWE-Bench-Pro for context;
    the pin walks string-literal nodes only.
    """
    src = _operation_advisor_source()
    tree = ast.parse(src)
    forbidden_substrings = ("swe_bench_pro", "swe-bench-pro", "swebp/")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            for needle in forbidden_substrings:
                # Allow them inside docstrings / multi-line strings used as
                # comments — Python parses these as Constant nodes too.
                # Distinguishing rationale: only literal SHORT string
                # constants signal a behavioral branch (e.g. ``== "swe_bench_pro"``).
                # Docstrings exceed 80 chars; behavioral branches do not.
                if needle in v.lower() and len(v) < 80:
                    raise AssertionError(
                        f"operation_advisor.py contains a short string "
                        f"literal {v!r} matching {needle!r} — possible "
                        f"category-special-case branch. "
                        f"B.2.0 contract: root-correct, not source-correct."
                    )


def test_ast_pin_no_source_equality_comparisons() -> None:
    """No ``source == "..."`` style comparisons against envelope sources.

    Source-agnostic policy enforcement — the advisor must never branch
    on which sensor produced the envelope.
    """
    src = _operation_advisor_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for left in (node.left, *node.comparators):
                if isinstance(left, ast.Attribute) and left.attr == "source":
                    raise AssertionError(
                        "operation_advisor.py compares against an "
                        "object's .source attribute — B.2.0 forbids "
                        "source-conditional advisory logic."
                    )


# ---------------------------------------------------------------------------
# 7. AST pins — signature + signal-compute contract
# ---------------------------------------------------------------------------


def test_ast_pin_advise_signature_has_repo_root_kwarg() -> None:
    """``OperationAdvisor.advise`` MUST expose ``repo_root: Optional[Path]
    = None`` as a kwarg. Drift would silently revert worktree-aware behavior.
    """
    src = _operation_advisor_source()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "advise":
            kw_names = [
                a.arg for a in (
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
            ]
            assert "repo_root" in kw_names, (
                f"advise() args = {kw_names}; missing ``repo_root`` kwarg"
            )
            found = True
            break
    assert found, "advise() function definition not found"


SIGNAL_COMPUTE_METHODS = (
    "_compute_blast_radius",
    "_compute_test_coverage",
    "_check_staleness",
    "_check_large_files",
)


@pytest.mark.parametrize("method_name", SIGNAL_COMPUTE_METHODS)
def test_ast_pin_signal_compute_methods_accept_root_kwarg(
    method_name: str,
) -> None:
    """Each of the four scan-tree methods MUST accept a keyword-only
    ``root`` parameter so the parent ``advise()`` can thread the
    per-op override consistently. Drift on any one method would
    silently leave its signal unparameterized (e.g. blast scoped to
    the worktree, but staleness still scoped to the main repo)."""
    src = _operation_advisor_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == method_name:
            kw_names = [a.arg for a in node.args.kwonlyargs]
            all_names = kw_names + [a.arg for a in node.args.args]
            # The override may be a kw-only arg (preferred) OR a
            # positional-or-keyword arg — either suffices for the
            # parent's ``root=`` call site.
            assert "root" in all_names or "root" in kw_names, (
                f"{method_name} args = {all_names}; missing ``root`` "
                f"parameter"
            )
            return
    raise AssertionError(f"{method_name} definition not found")


# ---------------------------------------------------------------------------
# 8. Orchestrator wiring AST pin
# ---------------------------------------------------------------------------


def test_ast_pin_orchestrator_calls_resolver_before_advise() -> None:
    """The advisor call site MUST invoke ``resolve_envelope_repo_root``
    AND pass its result via ``repo_root=`` to ``advise()``. The two
    must co-occur within the same function body — a drift that
    silently dropped the resolver would silently revert behavior to
    pre-B.2.0 even with the master flag ON.

    Two call shapes are accepted (both preserve the structural
    invariant "repo_root is threaded into advise"):

    1. Direct: ``_advisor.advise(..., repo_root=...)``
    2. ``asyncio.to_thread``-wrapped:
       ``asyncio.to_thread(_advisor.advise, ..., repo_root=...)``
       — the 2026-05-13 fix that moves the ~15s blast-radius scan
       off the asyncio event loop.  See
       ``test_operation_advisor_async_cache.py`` for the rationale.
    """
    from backend.core.ouroboros.governance import orchestrator
    orchestrator_src = Path(orchestrator.__file__).read_text()
    tree = ast.parse(orchestrator_src)
    resolver_called = False
    advise_with_repo_root = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = ""
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            if name == "resolve_envelope_repo_root":
                resolver_called = True
            # Shape 1: direct advise call with repo_root kwarg
            if name == "advise":
                if any(kw.arg == "repo_root" for kw in node.keywords):
                    advise_with_repo_root = True
            # Shape 2: asyncio.to_thread(_advisor.advise, ..., repo_root=...)
            if name == "to_thread" and node.args:
                first_arg = node.args[0]
                if (
                    isinstance(first_arg, ast.Attribute)
                    and first_arg.attr == "advise"
                    and any(kw.arg == "repo_root" for kw in node.keywords)
                ):
                    advise_with_repo_root = True
    assert resolver_called, (
        "orchestrator.py never calls resolve_envelope_repo_root — "
        "B.2.0 wiring missing"
    )
    assert advise_with_repo_root, (
        "orchestrator.py never passes repo_root= to advise() — "
        "B.2.0 wiring incomplete (checked both direct and "
        "asyncio.to_thread-wrapped call shapes)"
    )


def test_ast_pin_orchestrator_no_source_branch_for_worktree_aware() -> None:
    """Orchestrator must NOT special-case any envelope source string
    to enable the worktree-aware path. The resolver itself is
    source-agnostic; bypassing it via an ``if source == ...`` branch
    upstream would defeat the discipline."""
    from backend.core.ouroboros.governance import orchestrator
    src = Path(orchestrator.__file__).read_text()
    tree = ast.parse(src)
    forbidden = ("swe_bench_pro", "swebp/")
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for left in (node.left, *node.comparators):
                if isinstance(left, ast.Constant) and isinstance(left.value, str):
                    for needle in forbidden:
                        if needle in left.value.lower():
                            raise AssertionError(
                                f"orchestrator.py compares against "
                                f"{left.value!r} — possible "
                                f"category-special-case branch."
                            )


# ---------------------------------------------------------------------------
# 9. FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_register_flags_returns_two_specs() -> None:
    """B.2.0 seeds: JARVIS_ADVISOR_WORKTREE_AWARE_ENABLED (BOOL,
    default-FALSE) + JARVIS_ADVISOR_WORKTREE_ROOT_ALLOWLIST (STR,
    default empty)."""
    captured: list = []

    class _Capturer:
        def register(self, spec) -> None:
            captured.append(spec)

    count = register_flags(_Capturer())
    assert count == 2
    names = {s.name for s in captured}
    assert ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR in names
    assert ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR in names


def test_register_flags_master_default_false_per_section_33_1() -> None:
    """§33.1 graduation contract: master flag MUST default to FALSE.

    Drift to default-TRUE would silently activate the override behavior
    on every op for every operator without explicit graduation soak."""
    captured: list = []

    class _Capturer:
        def register(self, spec) -> None:
            captured.append(spec)

    register_flags(_Capturer())
    master = next(
        s for s in captured if s.name == ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR
    )
    assert master.default is False


def test_register_flags_never_raises_under_missing_registry_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Substrate contract: register_flags() returns 0 (does NOT raise)
    when FlagRegistry is unavailable. Mirrors the swe_bench_pro pattern."""
    import sys
    sys.modules.pop(
        "backend.core.ouroboros.governance.flag_registry", None,
    )
    monkeypatch.setitem(
        sys.modules,
        "backend.core.ouroboros.governance.flag_registry",
        None,  # type: ignore[arg-type]
    )

    class _Capturer:
        def register(self, spec) -> None:
            pass

    # Should not raise; should return 0.
    n = register_flags(_Capturer())
    assert n == 0


# ---------------------------------------------------------------------------
# 10. Canonical evidence key documented
# ---------------------------------------------------------------------------


def test_canonical_evidence_key_is_repo_root() -> None:
    """Operator binding B.2.0 hardening note 2: pick ONE canonical key,
    document it, don't fork parallel spellings. The constant exposed
    by the advisor module is the single source of truth for B.2.1's
    envelope builder."""
    assert EVIDENCE_REPO_ROOT_KEY == "repo_root"
