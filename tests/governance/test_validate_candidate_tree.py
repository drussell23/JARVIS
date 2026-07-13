"""Slice 9 — VALIDATE must exercise the CANDIDATE, not the broken tree.

Run #19: the correct source repair failed VALIDATE with fc=test because
pytest ran from the main repo root against the still-broken working tree
while the candidate's fix sat inert in a side sandbox. This test builds a
tiny broken repo + a correct candidate and drives the REAL
_run_validation_core seam: candidate-tree ON -> passes; OFF -> the legacy
behavior fails (pinning exactly the Run-19 class)."""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def broken_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -p no:cacheprovider\n")
    (repo / ".gitignore").write_text("__pycache__/\n")
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "mod.py").write_text("def f():\n    return 1\n")
    (repo / "tests" / "test_mod.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
        "from pkg.mod import f\n\n"
        "def test_f():\n    assert f() == 1\n"
    )
    git("add", "-A")
    git("commit", "-qm", "base")
    # Break the WORKING TREE (uncommitted — the battle-test chaos shape).
    (repo / "pkg" / "mod.py").write_text("def f():\n    return 2\n")
    return repo


_FIXED = "def f():\n    return 1\n"
_BROKEN = "def f():\n    return 2\n"


def _run_validation_for(repo, file_rel: str, content: str, monkeypatch, tree_enabled: bool):
    monkeypatch.setenv(
        "JARVIS_VALIDATE_CANDIDATE_TREE_ENABLED",
        "true" if tree_enabled else "false",
    )
    from backend.core.ouroboros.governance import orchestrator as om

    orch = object.__new__(om.Orchestrator)
    # Minimal config seam: _run_validation_core reads project_root and the
    # boot-time validation runner. Verified by reading _run_validation_core
    # (only self._config, self._validation_runner, and three @staticmethods
    # are dereferenced before the candidate-tree block).
    orch._config = om.OrchestratorConfig(project_root=repo)
    from backend.core.ouroboros.governance.test_runner import (
        LanguageRouter, PythonAdapter,
    )
    orch._validation_runner = LanguageRouter(
        repo_root=repo, adapters={"python": PythonAdapter(repo_root=repo)},
    )

    class _Ctx:
        op_id = "op-slice9-tree-pin"
        intake_evidence_json = ""
        target_files = ("pkg/mod.py", "tests/test_mod.py")

    candidate = {"file_path": file_rel, "full_content": content}
    return asyncio.run(orch._run_validation_core(_Ctx(), candidate, 120.0))


def _run_validation(repo, monkeypatch, tree_enabled: bool):
    return _run_validation_for(repo, "pkg/mod.py", _FIXED, monkeypatch, tree_enabled)


def test_candidate_tree_on_correct_repair_validates_green(broken_repo, monkeypatch):
    result = _run_validation(broken_repo, monkeypatch, tree_enabled=True)
    assert result.passed, f"fc={result.failure_class} err={result.error}"


def test_candidate_tree_on_wrong_repair_fails(broken_repo, monkeypatch):
    """Wrong candidate (keeps the broken content) must FAIL fc=test —
    proving the tree run exercises the candidate, not vacuously passing."""
    result = _run_validation_for(
        broken_repo, "pkg/mod.py", _BROKEN,
        monkeypatch, tree_enabled=True,
    )
    assert not result.passed
    assert result.failure_class == "test"


def test_legacy_path_pins_run19_class(broken_repo, monkeypatch):
    """Flag OFF: the legacy side-sandbox path fails fc=test on the correct
    repair — THE Run-19 class, pinned so we notice if legacy semantics
    ever silently change."""
    result = _run_validation(broken_repo, monkeypatch, tree_enabled=False)
    assert not result.passed
    assert result.failure_class == "test"


# ---------------------------------------------------------------------------
# Review Important: fail-soft must cover candidate-tree MATERIALIZATION only
# — a BlockedPathError (or any other exception) out of the actual
# `_tree_runner.run()` test-execution call must classify+RETURN directly,
# never silently trigger a second, differently-anchored legacy VALIDATE run
# that may not reproduce a genuine Slice-8 security rejection.
#
# Real-construction note: driving a genuine BlockedPathError THROUGH
# `_tree_runner.run()` in this block is structurally not reachable — the
# tree-anchored LanguageRouter is always constructed with
# `repo_root=_troot` and every `_tree_changed` entry is built as
# `_troot / _rel` (always contained under `_troot`), so `_normalize`'s
# step-1 `repo_root` containment check always succeeds first and
# BlockedPathError can never fire for these inputs (see test_runner.py
# `_normalize`, checked before writing this test). The orchestrator itself
# acknowledges this asymmetry with the extracted `_map_tree_run_exception`
# helper, so per the task's stated fallback: direct construction tests on
# that helper (proving BlockedPathError -> security, generic Exception ->
# infra, byte-identical to the legacy handler shapes) plus an AST pin that
# BOTH except clauses wrapping the `_tree_runner.run()` call actually
# delegate to it (catching a future "someone re-inlines a raw
# ValidationResult(...) and silently drifts from the legacy shape, or
# routes it through the materialization fail-soft try instead" regression).
# ---------------------------------------------------------------------------


def test_budget_skip_falls_back_to_legacy_run19_class(broken_repo, monkeypatch):
    """Slice 9 final review (Important): budget-aware skip — when the
    remaining pipeline budget is under JARVIS_VALIDATE_TREE_MIN_BUDGET_S,
    candidate-tree materialization must be skipped even though the flag is
    ON, landing on the SAME legacy fc=test Run-19 class as the flag-off
    path (test_legacy_path_pins_run19_class) — proving the skip actually
    reroutes to legacy rather than silently no-op'ing."""
    monkeypatch.setenv("JARVIS_VALIDATE_TREE_MIN_BUDGET_S", "999999")
    result = _run_validation(broken_repo, monkeypatch, tree_enabled=True)
    assert not result.passed
    assert result.failure_class == "test"


def test_runnable_precheck_never_constructs_repair_sandbox(
    broken_repo, monkeypatch, caplog
):
    """Slice 9 final review (Important): a candidate with NO runnable file
    (.md-only) must never enter the candidate-tree block at all — proven
    by the absence of the "[Validation] candidate-tree run" log line that
    only fires after a real RepairSandbox + LanguageRouter run. No mocking
    of RepairSandbox itself; this drives the REAL _run_validation_core
    seam exactly like the other tests in this file."""
    caplog.set_level(
        "INFO", logger="backend.core.ouroboros.governance.orchestrator",
    )
    result = _run_validation_for(
        broken_repo, "README.md", "# hello\n", monkeypatch, tree_enabled=True,
    )
    assert result.passed, f"fc={result.failure_class} err={result.error}"
    assert "validation skipped: non-code file" in (result.short_summary or "")
    assert not any(
        "[Validation] candidate-tree run" in rec.message
        for rec in caplog.records
    )
    assert not any(
        "candidate-tree materialization failed" in rec.message
        for rec in caplog.records
    )


def test_map_tree_run_exception_blocked_path_classifies_security():
    from backend.core.ouroboros.governance import orchestrator as om

    exc = om.BlockedPathError("Path /etc/passwd resolves outside repo root")
    result = om._map_tree_run_exception(exc, t0=0.0)

    assert result.passed is False
    assert result.best_candidate is None
    assert result.failure_class == "security"
    assert result.error == str(exc)
    assert result.short_summary == f"BlockedPathError: {str(exc)[:280]}"
    assert result.adapter_names_run == ()


def test_map_tree_run_exception_generic_classifies_infra():
    from backend.core.ouroboros.governance import orchestrator as om

    exc = RuntimeError("pytest subprocess exploded")
    result = om._map_tree_run_exception(exc, t0=0.0)

    assert result.passed is False
    assert result.best_candidate is None
    assert result.failure_class == "infra"
    assert result.error == str(exc)
    assert result.short_summary == f"runner exception: {str(exc)[:200]}"
    assert result.adapter_names_run == ()


def _calls_tree_runner_run(node: ast.AST) -> bool:
    """True if *node*'s subtree contains a call to ``_tree_runner.run(``."""
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "_tree_runner"
        ):
            return True
    return False


def _handler_exc_name(handler: ast.ExceptHandler):
    return handler.type.id if isinstance(handler.type, ast.Name) else None


class TestRun19LeafPairEndToEnd:
    """THE Run #19 scenario against the REAL repo: mutate the leaf source
    in a scratch clone's working tree (the chaos shape), hand VALIDATE the
    correct candidate, and require candidate-tree validation to pass under
    node policy. Uses a shallow file-copy clone of just the involved
    packages to keep the tree materialization fast and hermetic."""

    def test_leaf_repair_validates_green_under_node_policy(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(
            "JARVIS_SANDBOX_PREFIXES", "/nonexistent-sandbox-prefix",
        )
        real = Path(__file__).resolve().parents[2]
        leaf_rel = Path("backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py")
        test_rel = Path("tests/governance/a1_ignition_vector/test_leaf_predicates.py")

        repo = tmp_path / "repo"
        for rel in (leaf_rel, test_rel):
            dst = repo / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text((real / rel).read_text())
        # package __init__ chain for imports (source side) + root pytest.ini
        for parent in (leaf_rel.parent, *leaf_rel.parent.parents):
            if parent == Path("."):
                break
            init = repo / parent / "__init__.py"
            init.parent.mkdir(parents=True, exist_ok=True)
            if not init.exists():
                init.write_text("")
        (repo / "pytest.ini").write_text("[pytest]\naddopts = -p no:cacheprovider\n")
        (repo / ".gitignore").write_text("__pycache__/\n")

        def git(*args):
            subprocess.run(["git", *args], cwd=repo, check=True,
                           capture_output=True)
        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        git("add", "-A")
        git("commit", "-qm", "base")

        correct = (repo / leaf_rel).read_text()
        # Chaos: invert clamp01's clamping branches in the WORKING TREE
        # (uncommitted) so the real test_clamp01 assertions genuinely fail
        # (clamp01(-5) returns 1 instead of 0, clamp01(7) returns 0 instead
        # of 1). Deterministic, constructed by reading the real leaf file
        # first (not a blind string-replace).
        broken = correct.replace(
            "    if x < 0:\n"
            "        return 0\n"
            "    if x > 1:\n"
            "        return 1\n"
            "    return x\n",
            "    if x < 0:\n"
            "        return 1\n"
            "    if x > 1:\n"
            "        return 0\n"
            "    return x\n",
        )
        assert broken != correct, "chaos mutation did not match clamp01's body"
        (repo / leaf_rel).write_text(broken)

        # Prove the chaos genuinely breaks the real test standalone before
        # relying on it inside VALIDATE.
        standalone = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_rel), "-q"],
            cwd=repo, capture_output=True, text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert standalone.returncode != 0, (
            "chaos mutation did not break the copied test standalone:\n"
            f"{standalone.stdout}\n{standalone.stderr}"
        )
        assert "test_clamp01" in standalone.stdout

        result = _run_validation_for(
            repo, str(leaf_rel), correct, monkeypatch, tree_enabled=True,
        )
        assert result.passed, f"fc={result.failure_class} err={result.error}"


def test_tree_run_except_clauses_delegate_to_map_helper():
    """AST pin: the try/except wrapping the `_tree_runner.run()` call (and
    ONLY that call — not the outer materialization fail-soft try) has
    exactly two handlers (BlockedPathError, Exception), each of which is a
    bare `return _map_tree_run_exception(exc, t0)` — proving the run-time
    exception path returns instead of falling back, and can never silently
    regress into a raw inline ValidationResult(...) or into the
    materialization except block."""
    from backend.core.ouroboros.governance import orchestrator as om

    source = inspect.getsource(om)
    tree = ast.parse(source)

    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Try) and _calls_tree_runner_run(node)
    ]
    assert candidates, "no Try node wraps a _tree_runner.run() call"

    # The innermost matching Try is the smallest by total node count — the
    # outer materialization fail-soft try also structurally contains the
    # call (nested many levels down) but has far more nodes.
    target = min(candidates, key=lambda n: sum(1 for _ in ast.walk(n)))

    assert len(target.handlers) == 2, (
        f"expected exactly 2 except clauses on the run() try, "
        f"got {len(target.handlers)}"
    )
    exc_names = {_handler_exc_name(h) for h in target.handlers}
    assert exc_names == {"BlockedPathError", "Exception"}, exc_names

    for handler in target.handlers:
        assert len(handler.body) == 1, (
            "handler body must be a single bare return statement"
        )
        stmt = handler.body[0]
        assert isinstance(stmt, ast.Return), (
            f"{_handler_exc_name(handler)} handler must RETURN, not fall "
            f"through/reassign — got {type(stmt).__name__}"
        )
        call = stmt.value
        assert (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_map_tree_run_exception"
        ), (
            f"{_handler_exc_name(handler)} handler must return "
            f"_map_tree_run_exception(...), got {ast.dump(stmt)}"
        )

    # And the OUTER materialization try (the one with the WARNING log +
    # legacy-fallback semantics) must NOT be this same node — it has its
    # own distinct single `except Exception as _tree_exc` handler that
    # sets multi=None / _tree_used=False (no return, no map-helper call).
    outer_candidates = [
        node
        for node in candidates
        if node is not target
    ]
    assert outer_candidates, "expected a distinct outer materialization try"
    outer = max(outer_candidates, key=lambda n: sum(1 for _ in ast.walk(n)))
    assert len(outer.handlers) == 1
    outer_handler = outer.handlers[0]
    assert _handler_exc_name(outer_handler) == "Exception"
    assert not any(
        isinstance(s, ast.Return) for s in ast.walk(outer_handler)
    ), "materialization except must fall back (no return), not map+return"
