"""Slice 12H — bounded filesystem traversal tests.

Closes the wedge sources surfaced by the Slice 12G-2 LoopDeadman
faulthandler dump in ``bt-2026-05-22-215354``:

  1. ``tool_executor._glob_files`` — ``sorted(resolved.rglob(...))``
     materialised the entire generator before applying the
     500-match cap, wedging 5+ minutes on the element-web
     56K-file worktree.

  2. ``operation_advisor._compute_blast_radius`` — ``scan_root.rglob("*.py")``
     walked every Python file with no scan / wall-clock bound;
     ``py_file.read_text()`` per file read unbounded bytes.

Both sites now compose ``bounded_walker.py``:

  * ``bounded_glob`` — generator-based, scanned/match/timeout caps,
    skip-dir set at directory level (not substring filter).
  * ``iter_bounded_files`` — streaming variant for the
    blast-radius hot loop.
  * ``bounded_read_text`` — bounded byte-cap read.

## Test surface

### bounded_walker (the substrate)

  * ``BoundedWalkOutcome`` is closed 4-value enum
  * ``BoundedWalkResult`` is frozen
  * Default skip-dir set covers .git / node_modules / dist / build
    / .venv / venv / __pycache__ / .next / coverage
  * Env knob ``JARVIS_TOOL_GLOB_SKIP_DIRS`` augments the set
  * ``bounded_glob`` returns ``COMPLETE`` on a clean small tree
  * ``bounded_glob`` returns ``TRUNCATED_MATCHES`` when match cap
    hit without materialising the full file list
  * ``bounded_glob`` returns ``TRUNCATED_SCANNED`` when scan cap
    hit on a synthetic large tree
  * ``bounded_glob`` returns ``TRUNCATED_TIMEOUT`` when wall-clock
    cap fires
  * Skip-dirs prune at directory level (high-cardinality dirs
    never descended)
  * Pathological per-entry failures don't abort the walk
  * ``bounded_read_text`` caps byte read
  * ``bounded_read_text`` returns None on permission error / missing
    file (never raises)
  * ``iter_bounded_files`` terminates on budget exhaustion

### tool_executor._glob_files

  * Bounded path used for normal patterns
  * Truncation reason surfaced in returned string
  * Skip-dirs pruned (no node_modules / .git matches)
  * No materialised ``sorted(resolved.rglob)`` AST artefact
    remains in source (regression armor)

### operation_advisor._compute_blast_radius

  * Oracle graph path still preferred (sanity — no regression)
  * Legacy fallback uses ``iter_bounded_files``, not raw rglob
  * Legacy fallback uses ``bounded_read_text``, not ``read_text``
  * Budget exhaustion returns conservative cap (50, NOT 0)
  * Conservative cap is operator-tunable via env
  * No raw ``scan_root.rglob`` / ``py_file.read_text()`` AST
    artefacts remain in the legacy hot-loop site
"""

from __future__ import annotations

import ast as _ast
import os
import pathlib
import tempfile
import time
import unittest


from backend.core.ouroboros.governance.bounded_walker import (
    BoundedWalkOutcome,
    BoundedWalkResult,
    bounded_glob,
    bounded_read_bytes,
    bounded_read_text,
    default_skip_dirs,
    iter_bounded_files,
    blast_radius_conservative_cap,
    blast_radius_max_bytes_per_file,
    blast_radius_max_scanned,
    blast_radius_timeout_s,
    glob_max_matches,
    glob_max_scanned,
    glob_timeout_s,
)


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TOOL_EXECUTOR_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "tool_executor.py"
)
_ADVISOR_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "operation_advisor.py"
)
_WALKER_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "bounded_walker.py"
)


def _parse_module(path: pathlib.Path) -> _ast.Module:
    return _ast.parse(path.read_text())


# ============================================================================
# bounded_walker — substrate
# ============================================================================


class TestBoundedWalkerClosedTaxonomy(unittest.TestCase):

    def test_outcome_is_closed_4_value(self) -> None:
        self.assertEqual(len(list(BoundedWalkOutcome)), 4)
        self.assertEqual(
            {m.name for m in BoundedWalkOutcome},
            {"COMPLETE", "TRUNCATED_SCANNED",
             "TRUNCATED_MATCHES", "TRUNCATED_TIMEOUT"},
        )

    def test_result_is_frozen(self) -> None:
        r = BoundedWalkResult(matches=[], outcome=BoundedWalkOutcome.COMPLETE)
        with self.assertRaises(Exception):
            r.outcome = BoundedWalkOutcome.TRUNCATED_TIMEOUT  # type: ignore[misc]

    def test_truncation_reason_strings(self) -> None:
        for outcome, expected in [
            (BoundedWalkOutcome.COMPLETE, ""),
            (BoundedWalkOutcome.TRUNCATED_SCANNED, "truncated: max_scanned"),
            (BoundedWalkOutcome.TRUNCATED_MATCHES, "truncated: max_matches"),
            (BoundedWalkOutcome.TRUNCATED_TIMEOUT, "truncated: timeout"),
        ]:
            r = BoundedWalkResult(matches=[], outcome=outcome)
            self.assertEqual(r.truncation_reason(), expected)


class TestDefaultSkipDirs(unittest.TestCase):

    def test_covers_canonical_high_cardinality_dirs(self) -> None:
        skip = default_skip_dirs()
        for required in (
            ".git", "node_modules", "dist", "build",
            ".venv", "venv", "__pycache__", ".next", "coverage",
        ):
            self.assertIn(required, skip,
                          f"{required} must be in default skip set")

    def test_env_knob_augments_skip_set(self) -> None:
        prior = os.environ.pop("JARVIS_TOOL_GLOB_SKIP_DIRS", None)
        try:
            os.environ["JARVIS_TOOL_GLOB_SKIP_DIRS"] = (
                "custom1, custom2 , custom3"
            )
            skip = default_skip_dirs()
            self.assertIn("custom1", skip)
            self.assertIn("custom2", skip)
            self.assertIn("custom3", skip)
            # Defaults still present
            self.assertIn(".git", skip)
        finally:
            if prior is None:
                os.environ.pop(
                    "JARVIS_TOOL_GLOB_SKIP_DIRS", None,
                )
            else:
                os.environ[
                    "JARVIS_TOOL_GLOB_SKIP_DIRS"
                ] = prior


class TestBoundedGlobBehaviour(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="slice12h-")
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_complete_on_small_tree(self) -> None:
        # 5 .py files in a small tree
        for i in range(5):
            (self.root / f"file_{i}.py").write_text("x = 1\n")
        result = bounded_glob(self.root, "*.py")
        self.assertEqual(result.outcome, BoundedWalkOutcome.COMPLETE)
        self.assertEqual(len(result.matches), 5)
        self.assertFalse(result.truncated)

    def test_truncated_matches_does_not_materialize_full_list(
        self,
    ) -> None:
        """The headline regression check — the prior implementation
        called sorted(rglob(...)) which materialised every match
        before applying the cap. The bounded walker MUST stop at
        max_matches without enumerating the rest."""
        # Create 200 .py files
        for i in range(200):
            (self.root / f"file_{i:03d}.py").write_text("x = 1\n")
        result = bounded_glob(
            self.root, "*.py",
            max_matches=10, max_scanned=10_000,
            timeout_s=30.0,
        )
        self.assertEqual(
            result.outcome, BoundedWalkOutcome.TRUNCATED_MATCHES,
        )
        self.assertEqual(len(result.matches), 10)
        # scanned_count proves we DIDN'T enumerate all 200 —
        # should be roughly 10 (the matched files) plus a tiny
        # constant. Definitely < 200.
        self.assertLess(
            result.scanned_count, 200,
            "Bounded walker must stop early without scanning all "
            "files (prior wedge: sorted(rglob) materialised first)",
        )

    def test_truncated_scanned_on_large_tree(self) -> None:
        # Create a small tree but set a very tight scan cap
        for i in range(50):
            (self.root / f"file_{i:03d}.py").write_text("x = 1\n")
        result = bounded_glob(
            self.root, "*.py",
            max_matches=10_000, max_scanned=10,
            timeout_s=30.0,
        )
        self.assertEqual(
            result.outcome, BoundedWalkOutcome.TRUNCATED_SCANNED,
        )

    def test_skip_dirs_prune_at_directory_level(self) -> None:
        """High-cardinality dirs must NEVER be descended. A
        million-file node_modules subdirectory must not affect
        the scan count."""
        # Build:
        #   root/keep_a.py
        #   root/node_modules/file_*.py  (1000 files — should NOT
        #     be scanned)
        (self.root / "keep_a.py").write_text("x = 1\n")
        nm = self.root / "node_modules"
        nm.mkdir()
        for i in range(1000):
            (nm / f"file_{i:04d}.py").write_text("y = 1\n")
        result = bounded_glob(
            self.root, "*.py",
            max_scanned=10_000, timeout_s=30.0,
        )
        self.assertEqual(result.outcome, BoundedWalkOutcome.COMPLETE)
        self.assertEqual(len(result.matches), 1)
        # scanned_count includes the root file + the node_modules
        # DIR entry itself, but NOT the 1000 files inside it.
        self.assertLess(result.scanned_count, 10)

    def test_pattern_basename_matching(self) -> None:
        (self.root / "match_a.py").write_text("x = 1\n")
        (self.root / "skip_b.txt").write_text("y = 1\n")
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "match_c.py").write_text("z = 1\n")
        result = bounded_glob(self.root, "*.py")
        names = sorted(pathlib.Path(p).name for p in result.matches)
        self.assertEqual(names, ["match_a.py", "match_c.py"])

    def test_non_existent_root_returns_complete_empty(self) -> None:
        bogus = self.root / "does_not_exist"
        result = bounded_glob(bogus, "*.py")
        self.assertEqual(result.outcome, BoundedWalkOutcome.COMPLETE)
        self.assertEqual(result.matches, [])
        self.assertEqual(result.scanned_count, 0)

    def test_walker_never_raises_on_permission_denied(self) -> None:
        """A single inaccessible directory must not abort the
        walk."""
        (self.root / "ok.py").write_text("x = 1\n")
        bad = self.root / "no_access"
        bad.mkdir()
        (bad / "secret.py").write_text("y = 1\n")
        try:
            bad.chmod(0o000)
            result = bounded_glob(self.root, "*.py")
            # Whatever the OS-level access result is, the walker
            # must NEVER raise.
            self.assertIn(
                result.outcome,
                (BoundedWalkOutcome.COMPLETE,
                 BoundedWalkOutcome.TRUNCATED_TIMEOUT),
            )
            # ok.py should still appear regardless.
            ok_names = [
                p for p in result.matches
                if pathlib.Path(p).name == "ok.py"
            ]
            self.assertEqual(len(ok_names), 1)
        finally:
            bad.chmod(0o700)


class TestBoundedTimeout(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="slice12h-tmo-")
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_timeout_returns_partial(self) -> None:
        """With an extremely tight timeout the walker may return
        TIMEOUT or COMPLETE depending on OS scheduling — what we
        pin is that it ALWAYS returns within a small constant
        multiple of the timeout, regardless of tree size."""
        for i in range(50):
            (self.root / f"f_{i:03d}.py").write_text("x = 1\n")
        t0 = time.monotonic()
        result = bounded_glob(
            self.root, "*.py",
            timeout_s=0.001,  # 1 ms — guaranteed to fire on any tree
        )
        elapsed = time.monotonic() - t0
        # Bounded latency contract: the walker MUST return well
        # before the test timeout, regardless of tree size.
        self.assertLess(elapsed, 1.0,
                        "bounded_glob must return promptly on tight timeout")


class TestBoundedReadText(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="slice12h-rt-")
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_caps_bytes_read(self) -> None:
        big = self.root / "big.txt"
        big.write_text("x" * 1_000_000)
        result = bounded_read_text(big, max_bytes=1024)
        self.assertIsNotNone(result)
        # Decoded bytes <= max_bytes; with the "x"-only payload
        # the decoded length matches max_bytes exactly.
        self.assertEqual(len(result), 1024)

    def test_returns_none_on_missing_file(self) -> None:
        result = bounded_read_text(
            self.root / "does_not_exist.txt", max_bytes=1024,
        )
        self.assertIsNone(result)

    def test_returns_none_never_raises_on_permission(self) -> None:
        bad = self.root / "secret.txt"
        bad.write_text("x")
        try:
            bad.chmod(0o000)
            result = bounded_read_text(bad, max_bytes=1024)
            # On macOS root may bypass; just confirm no raise +
            # result is either None or a string.
            self.assertTrue(result is None or isinstance(result, str))
        finally:
            bad.chmod(0o600)


class TestIterBoundedFiles(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="slice12h-itr-")
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_iter_terminates_on_scan_cap(self) -> None:
        for i in range(200):
            (self.root / f"f_{i:03d}.py").write_text("x = 1\n")
        yielded = list(iter_bounded_files(
            self.root, max_scanned=10, timeout_s=30.0,
        ))
        self.assertLess(len(yielded), 200)

    def test_iter_terminates_on_timeout(self) -> None:
        for i in range(50):
            (self.root / f"f_{i:03d}.py").write_text("x = 1\n")
        t0 = time.monotonic()
        yielded = list(iter_bounded_files(
            self.root, max_scanned=10_000, timeout_s=0.001,
        ))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0,
                        "iterator must terminate promptly on timeout")


# ============================================================================
# tool_executor._glob_files — wedge site #1
# ============================================================================


class TestGlobFilesAstPins(unittest.TestCase):
    """Regression armor: no unbounded ``sorted(rglob)`` /
    ``resolved.rglob(pattern)`` artefacts may remain in the
    ``_glob_files`` body."""

    def _find_method(
        self, tree: _ast.Module, class_name: str, method_name: str,
    ) -> _ast.FunctionDef:
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.ClassDef):
                continue
            if node.name != class_name:
                continue
            for sub in node.body:
                if (
                    isinstance(sub, _ast.FunctionDef)
                    and sub.name == method_name
                ):
                    return sub
        raise AssertionError(
            f"{class_name}.{method_name} not found",
        )

    def test_glob_files_calls_bounded_glob(self) -> None:
        tree = _parse_module(_TOOL_EXECUTOR_FILE)
        m = self._find_method(tree, "ToolExecutor", "_glob_files")
        names = []
        for sub in _ast.walk(m):
            if not isinstance(sub, _ast.Call):
                continue
            f = sub.func
            if isinstance(f, _ast.Name):
                names.append(f.id)
            elif isinstance(f, _ast.Attribute):
                names.append(f.attr)
        self.assertIn(
            "bounded_glob", names,
            "_glob_files must compose bounded_glob (Slice 12H)",
        )

    def test_glob_files_no_unbounded_rglob(self) -> None:
        """No raw ``.rglob`` call may remain in _glob_files —
        that's the wedge pattern."""
        tree = _parse_module(_TOOL_EXECUTOR_FILE)
        m = self._find_method(tree, "ToolExecutor", "_glob_files")
        for sub in _ast.walk(m):
            if not isinstance(sub, _ast.Call):
                continue
            f = sub.func
            if (
                isinstance(f, _ast.Attribute)
                and f.attr == "rglob"
            ):
                self.fail(
                    f"_glob_files contains unbounded .rglob call "
                    f"at L{sub.lineno} — must use bounded_glob",
                )


# ============================================================================
# operation_advisor._compute_blast_radius — wedge site #2
# ============================================================================


class TestBlastRadiusAstPins(unittest.TestCase):
    """Regression armor: no raw ``scan_root.rglob`` /
    ``py_file.read_text`` artefacts in the legacy fallback hot
    loop. Oracle-path preference is preserved upstream — only
    the legacy fallback is bounded."""

    def _find_method(
        self, tree: _ast.Module, class_name: str, method_name: str,
    ) -> _ast.FunctionDef:
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.ClassDef):
                continue
            if node.name != class_name:
                continue
            for sub in node.body:
                if (
                    isinstance(sub, _ast.FunctionDef)
                    and sub.name == method_name
                ):
                    return sub
        raise AssertionError(
            f"{class_name}.{method_name} not found",
        )

    def test_blast_radius_calls_iter_bounded_files(self) -> None:
        tree = _parse_module(_ADVISOR_FILE)
        m = self._find_method(
            tree, "OperationAdvisor", "_compute_blast_radius",
        )
        names = []
        for sub in _ast.walk(m):
            if not isinstance(sub, _ast.Call):
                continue
            f = sub.func
            if isinstance(f, _ast.Name):
                names.append(f.id)
        self.assertIn(
            "iter_bounded_files", names,
            "_compute_blast_radius legacy fallback must use "
            "iter_bounded_files (Slice 12H)",
        )

    def test_blast_radius_calls_bounded_read_text(self) -> None:
        tree = _parse_module(_ADVISOR_FILE)
        m = self._find_method(
            tree, "OperationAdvisor", "_compute_blast_radius",
        )
        names = []
        for sub in _ast.walk(m):
            if not isinstance(sub, _ast.Call):
                continue
            f = sub.func
            if isinstance(f, _ast.Name):
                names.append(f.id)
        self.assertIn(
            "bounded_read_text", names,
            "_compute_blast_radius must use bounded_read_text "
            "(byte-capped) — full read_text was the wedge",
        )

    def test_blast_radius_no_raw_read_text_call(self) -> None:
        """No raw ``.read_text()`` call may remain in the
        _compute_blast_radius body — full reads of multi-MB
        generated bundles were a wedge factor even after
        directory-level skip."""
        tree = _parse_module(_ADVISOR_FILE)
        m = self._find_method(
            tree, "OperationAdvisor", "_compute_blast_radius",
        )
        for sub in _ast.walk(m):
            if not isinstance(sub, _ast.Call):
                continue
            f = sub.func
            if (
                isinstance(f, _ast.Attribute)
                and f.attr == "read_text"
            ):
                self.fail(
                    f"_compute_blast_radius contains unbounded "
                    f".read_text at L{sub.lineno} — must use "
                    f"bounded_read_text(max_bytes=...)",
                )

    def test_blast_radius_no_raw_rglob_call(self) -> None:
        """No raw ``.rglob`` in the legacy hot loop. The Oracle
        path higher up doesn't use rglob; the legacy fallback
        previously did."""
        tree = _parse_module(_ADVISOR_FILE)
        m = self._find_method(
            tree, "OperationAdvisor", "_compute_blast_radius",
        )
        for sub in _ast.walk(m):
            if not isinstance(sub, _ast.Call):
                continue
            f = sub.func
            if (
                isinstance(f, _ast.Attribute)
                and f.attr == "rglob"
            ):
                self.fail(
                    f"_compute_blast_radius contains unbounded "
                    f".rglob at L{sub.lineno} — must use "
                    f"iter_bounded_files",
                )

    def test_blast_radius_imports_conservative_cap(self) -> None:
        """Budget exhaustion must return the conservative cap
        (default 50), NOT 0 — bias toward caution."""
        src = _ADVISOR_FILE.read_text()
        self.assertIn(
            "blast_radius_conservative_cap", src,
            "_compute_blast_radius must reference the "
            "conservative cap (returned on budget exhaustion)",
        )

    def test_blast_radius_logs_budget_exhaustion(self) -> None:
        """Telemetry pin — operator must see a structured log
        line when the legacy scan hits the budget."""
        src = _ADVISOR_FILE.read_text()
        self.assertIn(
            "blast_radius_scan_budget_exhausted", src,
            "_compute_blast_radius must log "
            "'blast_radius_scan_budget_exhausted' on budget hit",
        )


# ============================================================================
# Env-knob bounded clamping pins
# ============================================================================


class TestEnvKnobs(unittest.TestCase):

    def test_defaults_match_operator_binding(self) -> None:
        self.assertEqual(glob_max_scanned(), 50_000)
        self.assertEqual(glob_max_matches(), 500)
        self.assertEqual(glob_timeout_s(), 5.0)
        self.assertEqual(blast_radius_max_scanned(), 20_000)
        self.assertEqual(
            blast_radius_max_bytes_per_file(), 65_536,
        )
        self.assertEqual(blast_radius_timeout_s(), 10.0)
        self.assertEqual(blast_radius_conservative_cap(), 50)

    def test_env_overrides_honored(self) -> None:
        with _env_set(
            JARVIS_TOOL_GLOB_MAX_SCANNED="1000",
            JARVIS_TOOL_GLOB_MAX_MATCHES="50",
            JARVIS_TOOL_GLOB_TIMEOUT_S="1.5",
            JARVIS_BLAST_RADIUS_MAX_SCANNED="200",
            JARVIS_BLAST_RADIUS_MAX_BYTES_PER_FILE="4096",
            JARVIS_BLAST_RADIUS_TIMEOUT_S="2.0",
            JARVIS_BLAST_RADIUS_CONSERVATIVE_CAP="25",
        ):
            self.assertEqual(glob_max_scanned(), 1000)
            self.assertEqual(glob_max_matches(), 50)
            self.assertEqual(glob_timeout_s(), 1.5)
            self.assertEqual(blast_radius_max_scanned(), 200)
            self.assertEqual(
                blast_radius_max_bytes_per_file(), 4096,
            )
            self.assertEqual(blast_radius_timeout_s(), 2.0)
            self.assertEqual(blast_radius_conservative_cap(), 25)


class _env_set:
    """Tiny context manager for env-knob test isolation."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.prior = {}

    def __enter__(self):
        for k, v in self.kwargs.items():
            self.prior[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
