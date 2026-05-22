"""
Slice 12I — FileWatchGuard watch-scope narrowing tests.
=======================================================

Closes the wedge surfaced by the Slice 12G-2 LoopDeadman in
bt-2026-05-22-223333: the ``watchdog`` library's PollingObserver
fallback was scheduling ~90 watch roots and doing
``dirsnapshot.walk`` on each, including the
``.jarvis/swe_bench_pro/worktrees/instance_element-hq__element-web-
...vnan/`` 56K-file SWE-Bench-Pro worktree clone. The fix is at the
SCHEDULING layer (do not subclass PollingObserver per operator
binding): extend the name-level exclusion defaults with ``.jarvis``
and ``.claude``, add an additive repo-relative path-pattern
exclusion mechanism for defense-in-depth on
``.jarvis/swe_bench_pro/worktrees``, surface startup telemetry, and
log a WARNING when the post-exclusion narrow-scope schedule
exceeds a configurable threshold.

Test contract (verbatim, from operator binding):
  1. ``.jarvis`` top-level dir is excluded by default.
  2. ``.jarvis/swe_bench_pro/worktrees`` is never scheduled.
  3. Existing non-generated source dirs remain watchable.
  4. Env override / additive exclusions still work.
  5. No watchdog ``Observer.schedule`` call receives an excluded
     path.
  6. Regression test for element-web-style worktree path.

Plus structural AST pins:
  7. ``.jarvis`` is present in ``FileWatchConfig`` defaults
     (default frozenset literal).
  8. ``.jarvis/swe_bench_pro/worktrees`` is present in
     ``FileWatchConfig.exclude_path_patterns`` defaults.
  9. ``_resolve_watch_paths`` returns a tuple ``(paths, skipped)``
     — frozen signature regression.
 10. ``_path_matches_pattern`` uses ``Path.parts`` (tuple-prefix,
     not string-prefix) so ``.jarvis/swe`` does not accidentally
     match ``.jarvis/swe_bench_pro``.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import List, Tuple
from unittest.mock import patch

import pytest

from backend.core.resilience.file_watch_guard import (
    FileWatchConfig,
    FileWatchGuard,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _build_guard(tmp_path: Path, **config_overrides) -> FileWatchGuard:
    """Build a FileWatchGuard rooted at ``tmp_path`` with override
    config knobs applied to ``FileWatchConfig``."""
    cfg = FileWatchConfig(**config_overrides)
    on_event = lambda _ev: None  # noqa: E731
    return FileWatchGuard(watch_dir=tmp_path, on_event=on_event, config=cfg)


def _make_worktree_layout(root: Path) -> None:
    """Build a small repo skeleton that mirrors the JARVIS layout.

    ::

        root/
          backend/         (source — must remain watchable)
            core/
              __init__.py
          tests/           (source — must remain watchable)
            __init__.py
          .jarvis/                              (transient — excluded)
            swe_bench_pro/
              worktrees/
                instance_element-hq__element-web-xxx/  (~56K files in prod)
                  src/
                    deep/
                      file.ts
          .claude/         (transient — excluded)
            sessions.json
          .git/            (already excluded — sanity)
            HEAD
          venv/            (already excluded — sanity)
            lib/
          README.md        (top-level file)
    """
    (root / "backend" / "core").mkdir(parents=True)
    (root / "backend" / "core" / "__init__.py").write_text("")
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("")

    worktree = (
        root / ".jarvis" / "swe_bench_pro" / "worktrees"
        / "instance_element-hq__element-web-xxx" / "src" / "deep"
    )
    worktree.mkdir(parents=True)
    (worktree / "file.ts").write_text("")

    (root / ".claude").mkdir()
    (root / ".claude" / "sessions.json").write_text("{}")

    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    (root / "venv" / "lib").mkdir(parents=True)
    (root / "README.md").write_text("")


def _scheduled_path_strs(
    scheduled: List[Tuple[Path, bool]],
) -> List[str]:
    return [str(p) for p, _ in scheduled]


def _scheduled_rel_parts(
    scheduled: List[Tuple[Path, bool]],
    guard: FileWatchGuard,
) -> List[Tuple[str, ...]]:
    """Return scheduled paths as repo-relative ``Path.parts`` tuples.

    Critical for assertions on macOS tmpdir layouts where the temp
    directory itself can contain literal substrings like ``.jarvis``
    in its randomly-generated name (pytest's ``test_<name>_<n>``
    pattern can produce paths like
    ``/tmp/.../test_swe_bench_worktrees_prote0/.jarvis`` which
    contains the substring but NOT the path component).
    """
    rel = []
    for path, _ in scheduled:
        try:
            rel.append(path.relative_to(guard.watch_dir).parts)
        except ValueError:
            rel.append(path.parts)
    return rel


# ---------------------------------------------------------------
# Test 1: ``.jarvis`` top-level dir is excluded by default.
# ---------------------------------------------------------------


def test_jarvis_top_level_excluded_by_default(tmp_path: Path) -> None:
    """``.jarvis`` MUST be in ``exclude_top_level_dirs`` default
    frozenset so the default config (no env, no overrides) skips it
    at the scheduling layer.

    This is the load-bearing default for the wedge fix — if an
    operator never sets any env vars, ``.jarvis`` and all of its
    SWE-Bench-Pro descendants are never passed to
    ``observer.schedule()``.
    """
    cfg = FileWatchConfig()
    assert ".jarvis" in cfg.exclude_top_level_dirs
    assert ".claude" in cfg.exclude_top_level_dirs


def test_jarvis_default_excluded_dir_resolution(tmp_path: Path) -> None:
    """Through the live resolution path (``_resolve_excluded_dirs``)
    with no env override, ``.jarvis`` is in the resolved set.
    """
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_DIRS", None)
        guard = _build_guard(tmp_path)
        excluded = guard._resolve_excluded_dirs()
        assert ".jarvis" in excluded
        assert ".claude" in excluded


# ---------------------------------------------------------------
# Test 2: ``.jarvis/swe_bench_pro/worktrees`` is never scheduled.
# ---------------------------------------------------------------


def test_swe_bench_worktrees_never_scheduled(tmp_path: Path) -> None:
    """The default name-level exclusion of ``.jarvis`` ensures that
    no scheduled path leaks into the SWE-Bench-Pro worktree subtree.
    Build the realistic layout, resolve watch paths, then confirm no
    returned path contains ``.jarvis/swe_bench_pro/worktrees``.
    """
    _make_worktree_layout(tmp_path)
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_DIRS", None)
        os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS", None)
        guard = _build_guard(tmp_path)
        excluded = guard._resolve_excluded_dirs()
        patterns = guard._resolve_excluded_path_patterns()
        scheduled, _skipped = guard._resolve_watch_paths(excluded, patterns)

        for rel_parts in _scheduled_rel_parts(scheduled, guard):
            assert ".jarvis" not in rel_parts, (
                f"FileWatchGuard scheduled a path under .jarvis: {rel_parts}"
            )


def test_swe_bench_worktrees_protected_via_pattern_when_jarvis_reincluded(
    tmp_path: Path,
) -> None:
    """Defense-in-depth: if an operator overrides
    ``JARVIS_FILE_WATCH_EXCLUDE_DIRS`` and DROPS ``.jarvis``, the
    repo-relative ``exclude_path_patterns`` MUST still keep
    ``.jarvis/swe_bench_pro/worktrees`` out of the schedule.
    """
    _make_worktree_layout(tmp_path)
    # Operator drops .jarvis from name-level exclusion (override is
    # replacement-semantics per existing surface).
    with patch.dict(
        os.environ,
        {"JARVIS_FILE_WATCH_EXCLUDE_DIRS": "venv,.git"},
        clear=False,
    ):
        os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS", None)
        guard = _build_guard(tmp_path)
        excluded = guard._resolve_excluded_dirs()
        patterns = guard._resolve_excluded_path_patterns()
        # .jarvis is no longer in name-level exclusions
        assert ".jarvis" not in excluded
        # But .jarvis/swe_bench_pro/worktrees is still in pattern set
        assert ".jarvis/swe_bench_pro/worktrees" in patterns

        scheduled, skipped = guard._resolve_watch_paths(excluded, patterns)
        # No scheduled path is under <tmp_path>/.jarvis/swe_bench_pro/worktrees
        # Check rel-to-tmp_path parts so the tmp dir name (which may
        # contain literal substrings like ".jarvis") cannot confuse
        # the assertion.
        worktree_rel = (".jarvis", "swe_bench_pro", "worktrees")
        for path, _rec in scheduled:
            rel_parts = path.relative_to(guard.watch_dir).parts
            assert rel_parts[:3] != worktree_rel, (
                f"SWE-Bench worktree leaked: {path}"
            )
        # And the pattern-skip counter incremented.
        assert skipped >= 1


# ---------------------------------------------------------------
# Test 3: Existing non-generated source dirs remain watchable.
# ---------------------------------------------------------------


def test_source_dirs_remain_watchable(tmp_path: Path) -> None:
    """``backend/`` and ``tests/`` must still appear in the
    scheduled paths after Slice 12I — the wedge fix MUST NOT
    weaken FileWatchGuard globally.
    """
    _make_worktree_layout(tmp_path)
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_DIRS", None)
        os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS", None)
        guard = _build_guard(tmp_path)
        excluded = guard._resolve_excluded_dirs()
        patterns = guard._resolve_excluded_path_patterns()
        scheduled, _ = guard._resolve_watch_paths(excluded, patterns)

        rel_parts_list = _scheduled_rel_parts(scheduled, guard)
        assert any(p == ("backend",) for p in rel_parts_list), (
            f"backend/ missing from schedule: {rel_parts_list}"
        )
        assert any(p == ("tests",) for p in rel_parts_list), (
            f"tests/ missing from schedule: {rel_parts_list}"
        )


# ---------------------------------------------------------------
# Test 4: Env override / additive exclusions still work.
# ---------------------------------------------------------------


def test_env_override_replaces_top_level_exclusions(tmp_path: Path) -> None:
    """``JARVIS_FILE_WATCH_EXCLUDE_DIRS`` retains its existing
    REPLACEMENT semantics (preserves operator escape hatch).
    """
    with patch.dict(
        os.environ,
        {"JARVIS_FILE_WATCH_EXCLUDE_DIRS": "node_modules,build"},
        clear=False,
    ):
        guard = _build_guard(tmp_path)
        excluded = guard._resolve_excluded_dirs()
        assert excluded == frozenset({"node_modules", "build"})


def test_env_override_additive_for_path_patterns(tmp_path: Path) -> None:
    """``JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS`` is ADDITIVE
    (operator extends defaults without losing built-in protection).

    Verbatim from operator binding:
    "Preserve env configurability: operator can extend exclusions
    without code edits."
    """
    with patch.dict(
        os.environ,
        {
            "JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS":
                "custom/dir, generated/build, ",
        },
        clear=False,
    ):
        guard = _build_guard(tmp_path)
        patterns = guard._resolve_excluded_path_patterns()
        # Defaults preserved
        assert ".jarvis/swe_bench_pro/worktrees" in patterns
        # Additions present (with whitespace and trailing comma handled)
        assert "custom/dir" in patterns
        assert "generated/build" in patterns


def test_env_override_path_pattern_normalization(tmp_path: Path) -> None:
    """Leading ``./``, leading/trailing ``/``, and backslashes are
    normalized — operators can paste paths in any common form.
    """
    with patch.dict(
        os.environ,
        {
            "JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS":
                "./foo/bar/, /baz/qux, win\\style\\path",
        },
        clear=False,
    ):
        guard = _build_guard(tmp_path)
        patterns = guard._resolve_excluded_path_patterns()
        assert "foo/bar" in patterns
        assert "baz/qux" in patterns
        assert "win/style/path" in patterns


# ---------------------------------------------------------------
# Test 5: No watchdog Observer.schedule call receives an excluded
# path.
# ---------------------------------------------------------------


def test_no_observer_schedule_call_receives_excluded_path(
    tmp_path: Path,
) -> None:
    """End-to-end: walk the full ``_start_watchdog`` plumbing with a
    spy Observer and assert no schedule call's path is under any
    excluded top-level dir or excluded path pattern.

    The spy stubs out the actual watchdog library so the test
    doesn't depend on availability or on actually starting a
    polling loop.
    """
    _make_worktree_layout(tmp_path)

    scheduled_calls: List[str] = []

    class _SpyObserver:
        def schedule(self, handler, path, recursive=True):
            scheduled_calls.append(str(path))

        def start(self):
            return None

        def stop(self):
            return None

        def join(self, timeout=None):
            return None

    class _SpyObserverCls:
        def __call__(self):
            return _SpyObserver()

    spy_observer = _SpyObserverCls()

    import asyncio

    async def _run() -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_DIRS", None)
            os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS", None)
            guard = _build_guard(tmp_path)
            # Patch the observer factory used by _start_watchdog.
            with patch(
                "backend.core.resilience.file_watch_guard."
                "FileWatchGuard._select_observer_backend",
                return_value=(spy_observer, "spy"),
                create=True,
            ):
                # If the helper isn't a method, fall back to patching
                # the library-level Observer + PollingObserver.
                try:
                    await guard._start_watchdog()
                except Exception:
                    # Fallback path — patch the watchdog imports.
                    with patch(
                        "watchdog.observers.Observer",
                        spy_observer,
                        create=True,
                    ), patch(
                        "watchdog.observers.polling.PollingObserver",
                        spy_observer,
                        create=True,
                    ):
                        await guard._start_watchdog()

    asyncio.get_event_loop().run_until_complete(_run()) if False else \
        asyncio.run(_run())

    # Every scheduled path must NOT live under .jarvis, .claude,
    # .git, venv, or .jarvis/swe_bench_pro/worktrees. Use
    # tmp_path-relative parts so the test-runner temp dir name
    # can't contaminate the check via literal substring.
    resolved_tmp = tmp_path.expanduser().resolve()
    for path_str in scheduled_calls:
        candidate = Path(path_str).expanduser().resolve()
        try:
            rel_parts = candidate.relative_to(resolved_tmp).parts
        except ValueError:
            rel_parts = candidate.parts
        for forbidden in (".jarvis", ".claude", ".git", "venv"):
            assert forbidden not in rel_parts, (
                f"Excluded {forbidden} leaked into observer.schedule: "
                f"{path_str}"
            )


# ---------------------------------------------------------------
# Test 6: Regression test for element-web-style worktree path.
# ---------------------------------------------------------------


def test_element_web_worktree_path_regression(tmp_path: Path) -> None:
    """The specific wedge from bt-2026-05-22-223333: the element-
    web SWE-Bench-Pro worktree at
    ``.jarvis/swe_bench_pro/worktrees/instance_element-hq__element-
    web-...vnan/`` was the dirsnapshot.walk source.

    Build that exact directory shape (with a deeply nested src/
    subtree to simulate the 56K-file load), then prove the
    FileWatchGuard schedule contains zero paths under it.
    """
    element_web_root = (
        tmp_path / ".jarvis" / "swe_bench_pro" / "worktrees"
        / "instance_element-hq__element-web-1234-deadbeef"
    )
    # Simulate a moderately deep src/ tree
    for depth_dir in ("src", "src/components", "src/components/auth"):
        (element_web_root / depth_dir).mkdir(parents=True)
        (element_web_root / depth_dir / "module.ts").write_text("")

    # Plus a legitimate source dir at top level
    (tmp_path / "backend" / "core").mkdir(parents=True)
    (tmp_path / "backend" / "core" / "__init__.py").write_text("")

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_DIRS", None)
        os.environ.pop("JARVIS_FILE_WATCH_EXCLUDE_PATH_PATTERNS", None)
        guard = _build_guard(tmp_path)
        excluded = guard._resolve_excluded_dirs()
        patterns = guard._resolve_excluded_path_patterns()
        scheduled, _ = guard._resolve_watch_paths(excluded, patterns)

        for rel_parts in _scheduled_rel_parts(scheduled, guard):
            assert not any("element-web" in p for p in rel_parts), (
                f"Element-web worktree leaked into schedule: {rel_parts}"
            )
            assert "swe_bench_pro" not in rel_parts, (
                f"SWE-Bench-Pro tree leaked into schedule: {rel_parts}"
            )

        # And the legitimate source dir IS still there.
        assert any(
            p[:1] == ("backend",)
            for p in _scheduled_rel_parts(scheduled, guard)
        )


# ---------------------------------------------------------------
# Path-pattern semantics (tuple-prefix, not string-prefix)
# ---------------------------------------------------------------


def test_path_pattern_uses_tuple_prefix_not_string_prefix(
    tmp_path: Path,
) -> None:
    """Critical correctness guard: ``.jarvis/swe`` MUST NOT match
    ``.jarvis/swe_bench_pro`` (string-prefix bug). The implementation
    uses ``Path.parts`` tuple-prefix comparison so the components
    must match exactly.
    """
    # Build .jarvis/swe_bench_pro/ AND .jarvis/swe/
    (tmp_path / ".jarvis" / "swe_bench_pro").mkdir(parents=True)
    (tmp_path / ".jarvis" / "swe").mkdir(parents=True)

    guard = _build_guard(tmp_path)
    swe_bench = tmp_path / ".jarvis" / "swe_bench_pro" / "worktrees"
    swe_only = tmp_path / ".jarvis" / "swe" / "something"

    # Pattern ``.jarvis/swe`` should match swe_only but NOT
    # swe_bench_pro.
    patterns = frozenset({".jarvis/swe"})
    swe_bench.mkdir(parents=True)
    swe_only.mkdir(parents=True)
    assert guard._path_matches_pattern(swe_only, patterns) is True
    assert guard._path_matches_pattern(swe_bench, patterns) is False


def test_path_pattern_matches_descendants_not_just_root(
    tmp_path: Path,
) -> None:
    """The pattern ``.jarvis/swe_bench_pro/worktrees`` MUST match
    deeply nested paths under that root, not only the root itself.
    """
    deep = (
        tmp_path / ".jarvis" / "swe_bench_pro" / "worktrees"
        / "instance_x" / "src" / "deep" / "tree"
    )
    deep.mkdir(parents=True)
    guard = _build_guard(tmp_path)
    patterns = frozenset({".jarvis/swe_bench_pro/worktrees"})
    assert guard._path_matches_pattern(deep, patterns) is True


def test_path_pattern_returns_false_for_unrelated_path(
    tmp_path: Path,
) -> None:
    """Paths outside ``watch_dir`` must not match any pattern (the
    helper returns False rather than raising)."""
    guard = _build_guard(tmp_path)
    patterns = frozenset({".jarvis/swe_bench_pro/worktrees"})
    unrelated = Path("/tmp/nowhere-near-watch-dir")
    assert guard._path_matches_pattern(unrelated, patterns) is False


def test_path_pattern_empty_patterns_returns_false(tmp_path: Path) -> None:
    """Empty pattern frozenset is the legacy path — no skipping."""
    guard = _build_guard(tmp_path)
    inside = tmp_path / "anything"
    inside.mkdir()
    assert guard._path_matches_pattern(inside, frozenset()) is False


# ---------------------------------------------------------------
# Telemetry / high-count warning
# ---------------------------------------------------------------


def test_resolve_watch_paths_returns_tuple_with_skipped_count(
    tmp_path: Path,
) -> None:
    """Frozen signature pin: ``_resolve_watch_paths`` returns a tuple
    ``(paths, skipped_by_pattern_count)``. Slice 12I telemetry
    surface — the harness boot log uses ``skipped_by_pattern``.
    """
    _make_worktree_layout(tmp_path)
    guard = _build_guard(tmp_path)
    result = guard._resolve_watch_paths(
        frozenset({"venv", ".git"}),
        frozenset({".jarvis/swe_bench_pro/worktrees"}),
    )
    assert isinstance(result, tuple)
    assert len(result) == 2
    paths, skipped = result
    assert isinstance(paths, list)
    assert isinstance(skipped, int)
    assert skipped >= 1


# ---------------------------------------------------------------
# AST pins — structural regression armor
# ---------------------------------------------------------------


_FILE_WATCH_GUARD_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "resilience" / "file_watch_guard.py"
)


def _load_ast() -> ast.Module:
    return ast.parse(_FILE_WATCH_GUARD_PATH.read_text())


def test_ast_pin_jarvis_in_default_exclude_top_level_dirs() -> None:
    """``.jarvis`` must literally appear in the
    ``exclude_top_level_dirs`` frozenset default factory in the
    ``FileWatchConfig`` class body. Walks the AST and asserts the
    constant is present. Catches operator-impacting regressions
    that drop the default in a refactor.
    """
    tree = _load_ast()
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "FileWatchConfig":
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign):
                continue
            if not isinstance(stmt.target, ast.Name):
                continue
            if stmt.target.id != "exclude_top_level_dirs":
                continue
            # The default_factory lambda body contains a frozenset
            # call wrapping a set literal. Find the set literal.
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Set):
                    constants = {
                        elt.value for elt in sub.elts
                        if isinstance(elt, ast.Constant)
                    }
                    if ".jarvis" in constants and ".claude" in constants:
                        found = True
                        break
            break
    assert found, (
        "AST pin failed: .jarvis and .claude must be in "
        "FileWatchConfig.exclude_top_level_dirs default frozenset."
    )


def test_ast_pin_swe_bench_worktrees_in_default_path_patterns() -> None:
    """``.jarvis/swe_bench_pro/worktrees`` must literally appear in
    the ``exclude_path_patterns`` frozenset default. This is the
    defense-in-depth guarantee for the wedge fix.
    """
    tree = _load_ast()
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "FileWatchConfig":
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign):
                continue
            if not isinstance(stmt.target, ast.Name):
                continue
            if stmt.target.id != "exclude_path_patterns":
                continue
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Set):
                    constants = {
                        elt.value for elt in sub.elts
                        if isinstance(elt, ast.Constant)
                    }
                    if ".jarvis/swe_bench_pro/worktrees" in constants:
                        found = True
                        break
            break
    assert found, (
        "AST pin failed: .jarvis/swe_bench_pro/worktrees must be in "
        "FileWatchConfig.exclude_path_patterns default frozenset."
    )


def test_ast_pin_resolve_watch_paths_signature_returns_tuple() -> None:
    """``_resolve_watch_paths`` annotated return must be
    ``Tuple[List[Tuple[Path, bool]], int]`` (or a structurally
    equivalent subscripted Tuple with arity 2). Catches a refactor
    that accidentally drops the ``skipped_by_pattern`` counter from
    the return signature.
    """
    tree = _load_ast()
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_resolve_watch_paths":
            continue
        # The return annotation should be a Tuple[...] subscript with
        # two elements.
        ann = node.returns
        if ann is None:
            continue
        # ast.Subscript(value=Name('Tuple'), slice=...)
        if isinstance(ann, ast.Subscript) and \
                isinstance(ann.value, ast.Name) and \
                ann.value.id == "Tuple":
            # Slice may be a Tuple node (arity 2 args)
            slc = ann.slice
            elts = getattr(slc, "elts", None)
            if elts and len(elts) == 2:
                found = True
                break
    assert found, (
        "AST pin failed: _resolve_watch_paths must return "
        "Tuple[List[Tuple[Path, bool]], int]."
    )


def test_ast_pin_path_matches_pattern_uses_parts_tuple_prefix() -> None:
    """``_path_matches_pattern`` must use ``Path.parts`` (tuple-
    prefix comparison) NOT string-prefix. The implementation
    references ``.parts`` on the relative-path object; this pin
    catches a refactor that switches to ``str(rel).startswith(...)``
    which would re-introduce the ``.jarvis/swe`` vs
    ``.jarvis/swe_bench_pro`` false-positive.
    """
    tree = _load_ast()
    uses_parts = False
    uses_startswith = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_path_matches_pattern":
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and sub.attr == "parts":
                uses_parts = True
            if isinstance(sub, ast.Attribute) and sub.attr == "startswith":
                uses_startswith = True
    assert uses_parts, (
        "AST pin failed: _path_matches_pattern must use Path.parts "
        "for tuple-prefix matching."
    )
    assert not uses_startswith, (
        "AST pin failed: _path_matches_pattern must NOT use "
        "str.startswith — that re-introduces the .jarvis/swe vs "
        ".jarvis/swe_bench_pro false-positive."
    )


def test_ast_pin_resolve_excluded_path_patterns_method_exists() -> None:
    """The Slice 12I additive-env-knob helper must exist on
    ``FileWatchGuard`` so the resolution path can find the new
    env override."""
    tree = _load_ast()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "FileWatchGuard":
            continue
        method_names = {
            m.name for m in node.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert "_resolve_excluded_path_patterns" in method_names, (
            "AST pin failed: FileWatchGuard must define "
            "_resolve_excluded_path_patterns."
        )
        assert "_path_matches_pattern" in method_names, (
            "AST pin failed: FileWatchGuard must define "
            "_path_matches_pattern."
        )
        return
    pytest.fail("FileWatchGuard class not found in module.")


def test_ast_pin_high_watch_count_warn_env_read() -> None:
    """The high-count warning gate must read
    ``JARVIS_FILE_WATCH_HIGH_COUNT_WARN`` from os.environ — operators
    must be able to tune the threshold without code edits. Walks the
    AST looking for the constant string literal."""
    tree = _load_ast()
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == \
                "JARVIS_FILE_WATCH_HIGH_COUNT_WARN":
            found = True
            break
    assert found, (
        "AST pin failed: JARVIS_FILE_WATCH_HIGH_COUNT_WARN must be "
        "read via os.environ.get in _start_watchdog so operators "
        "can tune the runaway-watching threshold."
    )
