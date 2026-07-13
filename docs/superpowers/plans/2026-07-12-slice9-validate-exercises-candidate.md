# Slice 9 — VALIDATE Exercises the Candidate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kill the Run #19 blocker at its root — VALIDATE runs pytest against the real (still-broken) tree while the candidate's fix sits inert in a side sandbox — plus the diagnosed L2 lane stack (wrong test scoping, `backend/pytest.ini` hijack, invisible failures), the auditor's chaos-lineage reject scoping, and the pre-GATE sandbox write-escape clamp.

**Architecture:** VALIDATE gains a **candidate tree**: a working-tree-faithful full materialization (reusing `RepairSandbox` — git-worktree strategy extended with a dirty-delta overlay so its baseline matches the working tree that TestWatcher actually observed) with the candidate files applied on top, validated by a **per-op `LanguageRouter` anchored at the tree root** (the existing Slice-12AE per-op-router pattern, reused verbatim). Test discovery then finds the sibling test *in the tree*, and pytest runs *from the tree root* (`TestRunner._run_pytest`: `effective_cwd = cwd or repo_root`) with the tree's own root `pytest.ini` — so a correct repair validates green against the broken baseline and a wrong one fails. Any materialization fault fails soft to the legacy side-sandbox path (today's behavior). The L2 lane gets the same baseline (working-tree mirror), real failing-test targets threaded from the op's attributed scope, a pinned pytest config (`-c <tree>/pytest.ini` — kills the `backend/pytest.ini` `--cov`/`-n` usage-error hijack, rc=4, reproduced live), and honest failure logging (the `FAILED (unknown)` label was a `getattr` on a field `SandboxValidationResult` never has).

**Tech Stack:** Python 3.9+ stdlib only. No new dependencies. `from __future__ import annotations` everywhere.

## Global Constraints

- Python 3.9+ — no `asyncio.timeout`; use `asyncio.wait_for`. `from __future__ import annotations` in all files.
- Env-var driven, no hardcoded paths. New flags (BOOL, default `true`): `JARVIS_SANDBOX_WORKING_TREE_MIRROR_ENABLED`, `JARVIS_VALIDATE_CANDIDATE_TREE_ENABLED`, `JARVIS_L2_TEST_TARGET_THREADING_ENABLED`, `JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS` (script-side).
- Fail-SOFT discipline for the new VALIDATE path: any candidate-tree materialization/apply fault → log WARNING + fall through to the LEGACY side-sandbox path byte-identically (never a new hard failure class). The write-escape clamp (Task 6) is the one fail-CLOSED addition: an escaping path is a `security` rejection.
- Security may only get stricter: the Slice-8 declared-roots gate stays intact; the clamp adds containment at the WRITE site.
- Master-off for every new flag restores current behavior byte-identically.
- Node-policy in tests only: `monkeypatch.setenv("JARVIS_SANDBOX_PREFIXES", "/nonexistent-sandbox-prefix")`.
- TDD per task; run tests from repo root `python3 -m pytest <path> -v`.
- Commits: conventional, `git add` only named files, footer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Known pre-existing failures to NOT chase: `test_vector_observation_path.py` (×3), merkle/phase11/l2-fixture collection errors, `tests/governance/phase_runner/` (~22 pre-existing).

---

### Task 1: Working-tree-faithful mirror in `RepairSandbox`

**Files:**
- Modify: `backend/core/ouroboros/governance/repair_sandbox.py` (ctor at ~112; `_setup` at ~145-175; new `_overlay_working_tree_delta` after `_git_worktree_add`)
- Test: Create `tests/governance/test_repair_sandbox_working_tree_mirror.py`

**Interfaces:**
- Consumes: existing `RepairSandbox(repo_root: Path, test_timeout_s: float)`, `_git_worktree_add`, `_rsync_copy`, `sandbox_root`.
- Produces: `RepairSandbox(repo_root, test_timeout_s, mirror_working_tree: Optional[bool] = None)` — `None` reads env `JARVIS_SANDBOX_WORKING_TREE_MIRROR_ENABLED` (default true). When active and the git-worktree strategy succeeds, `_overlay_working_tree_delta(tmpdir)` copies the working tree's dirty delta (modified/added/untracked files copied in; deleted files removed) so the sandbox baseline == the working tree, not HEAD. Rsync strategy already mirrors the working tree — unchanged. Tasks 2 and 4 rely on this baseline.

**Why:** TestWatcher observes failures against the WORKING TREE (battle-test chaos is an uncommitted mutation), but the preferred worktree strategy materializes HEAD — the two strategies had silently different baselines. A repair validated against HEAD can neither be proven to fix the observed failure nor use uncommitted collaborators it depends on.

- [ ] **Step 1: Write the failing tests**

```python
"""Slice 9 — RepairSandbox working-tree mirror.

The worktree strategy materializes HEAD; battle-test chaos (and any real
uncommitted state) lives in the WORKING TREE — the baseline TestWatcher
actually observed. The mirror overlays the dirty delta so both sandbox
strategies share the working-tree baseline."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.repair_sandbox import RepairSandbox


@pytest.fixture
def dirty_repo(tmp_path):
    """A tiny git repo with one committed file, one uncommitted
    modification, one untracked file, and one deleted-in-worktree file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "committed.py").write_text("x = 1\n")
    (repo / "doomed.py").write_text("y = 2\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    (repo / "committed.py").write_text("x = 999  # dirty\n")   # modified
    (repo / "untracked.py").write_text("z = 3\n")              # untracked
    (repo / "doomed.py").unlink()                              # deleted
    return repo


def test_mirror_on_baseline_is_working_tree(dirty_repo, monkeypatch):
    monkeypatch.delenv("JARVIS_SANDBOX_WORKING_TREE_MIRROR_ENABLED", raising=False)

    async def _run():
        async with RepairSandbox(dirty_repo, 30.0) as sb:
            root = sb.sandbox_root
            assert (root / "committed.py").read_text() == "x = 999  # dirty\n"
            assert (root / "untracked.py").read_text() == "z = 3\n"
            assert not (root / "doomed.py").exists()
    asyncio.run(_run())


def test_mirror_off_restores_head_baseline(dirty_repo, monkeypatch):
    async def _run():
        async with RepairSandbox(dirty_repo, 30.0, mirror_working_tree=False) as sb:
            root = sb.sandbox_root
            assert (root / "committed.py").read_text() == "x = 1\n"
            assert (root / "doomed.py").exists()
            assert not (root / "untracked.py").exists()
    asyncio.run(_run())


def test_env_kill_switch(dirty_repo, monkeypatch):
    monkeypatch.setenv("JARVIS_SANDBOX_WORKING_TREE_MIRROR_ENABLED", "false")

    async def _run():
        async with RepairSandbox(dirty_repo, 30.0) as sb:
            assert (sb.sandbox_root / "committed.py").read_text() == "x = 1\n"
    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/test_repair_sandbox_working_tree_mirror.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'mirror_working_tree'` and/or the baseline assertion (`x = 999` vs `x = 1`).

- [ ] **Step 3: Implement**

In `repair_sandbox.py`:

(a) Module-level helper near the other imports/env reads:

```python
def _working_tree_mirror_enabled() -> bool:
    """Slice 9 (default ON): the sandbox baseline mirrors the WORKING
    TREE (what TestWatcher actually observed), not HEAD. OFF restores
    the legacy HEAD-baseline worktree strategy byte-identically."""
    return os.environ.get(
        "JARVIS_SANDBOX_WORKING_TREE_MIRROR_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")
```

(Add `import os` if absent.)

(b) Ctor: add keyword `mirror_working_tree: Optional[bool] = None` and store `self._mirror_working_tree = (_working_tree_mirror_enabled() if mirror_working_tree is None else mirror_working_tree)`.

(c) In `_setup`, in the worktree-success branch (after `self._worktree_mode = True`, before `return`), insert:

```python
            if self._mirror_working_tree:
                try:
                    await self._overlay_working_tree_delta(tmpdir)
                except Exception as exc:  # noqa: BLE001 — fail-soft: HEAD
                    # baseline is degraded-but-functional; rsync strategy
                    # is the heavyweight fallback, not worth forcing here.
                    _logger.warning(
                        "repair_sandbox: working-tree overlay failed (%s) — "
                        "sandbox baseline is HEAD, not the working tree", exc,
                    )
```

(d) New method after `_git_worktree_add`:

```python
    async def _overlay_working_tree_delta(self, tmpdir: Path) -> None:
        """Copy the working tree's dirty delta onto the HEAD worktree so
        the sandbox baseline matches what TestWatcher observed. Uses
        ``git status --porcelain=v1 -z`` (NUL-safe); modified/added/
        untracked files are copied in, deleted files removed. Renames
        (``R``) arrive as two NUL-separated paths — both handled."""
        proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain=v1", "-z",
            "--untracked-files=all",
            cwd=str(self._repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        if proc.returncode:
            raise RuntimeError("git status failed for working-tree overlay")
        entries = stdout_b.decode(errors="replace").split("\0")
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            code, rel = entry[:2], entry[3:]
            if code[0] == "R":  # rename: next entry is the ORIGINAL path
                orig = entries[i] if i < len(entries) else ""
                i += 1
                if orig:
                    (tmpdir / orig).unlink(missing_ok=True)
            src = self._repo_root / rel
            dst = tmpdir / rel
            if src.exists() and src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            elif not src.exists():
                dst.unlink(missing_ok=True)
```

(`shutil` is already imported. `Optional` — check the typing import.)

- [ ] **Step 4: Run new tests + neighboring repair suites**

Run: `python3 -m pytest tests/governance/test_repair_sandbox_working_tree_mirror.py tests/governance/test_repair_sandbox_node_policy.py -v && python3 -m pytest tests/governance -k "repair_sandbox or repair_engine" -q --continue-on-collection-errors 2>&1 | tail -2`
Expected: new tests PASS; neighboring suites green (modulo the plan's known pre-existing).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/repair_sandbox.py tests/governance/test_repair_sandbox_working_tree_mirror.py
git commit -m "feat(slice9): RepairSandbox mirrors the working tree — the baseline TestWatcher actually observed, not HEAD

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: VALIDATE candidate-tree execution

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` — inside `_run_validation_core`'s runner block (the `with tempfile.TemporaryDirectory(prefix="ouroboros_validate_")` block at ~12785-12850; `_all_files` is built at ~12633 via `self._iter_candidate_files(candidate)`)
- Test: Create `tests/governance/test_validate_candidate_tree.py`

**Interfaces:**
- Consumes: Task 1's `RepairSandbox(repo_root, test_timeout_s, mirror_working_tree=None)` + `apply_full_content(content, file_path)` + `sandbox_root`; the existing Slice-12AE per-op `LanguageRouter` construction pattern (orchestrator ~12750-12768); `_RUNNABLE_EXTENSIONS`.
- Produces: with `JARVIS_VALIDATE_CANDIDATE_TREE_ENABLED` (default true), VALIDATE materializes the candidate tree and the router runs anchored AT the tree — the candidate is actually exercised. Helper `_candidate_tree_enabled() -> bool` (module level). Fail-soft: any tree fault → WARNING + legacy path.

**Why (Run #19, one grep):** `[Validation] FAILED op=… fc=test summary=[python:FAIL] …test_clamp01` — the legacy path writes candidates to an empty side-tempdir but `PythonAdapter` runs pytest from the MAIN repo root, importing the still-broken real source; a repair op structurally cannot pass VALIDATE until APPLY.

- [ ] **Step 1: Write the failing test**

```python
"""Slice 9 — VALIDATE must exercise the CANDIDATE, not the broken tree.

Run #19: the correct source repair failed VALIDATE with fc=test because
pytest ran from the main repo root against the still-broken working tree
while the candidate's fix sat inert in a side sandbox. This test builds a
tiny broken repo + a correct candidate and drives the REAL
_run_validation_core seam: candidate-tree ON -> passes; OFF -> the legacy
behavior fails (pinning exactly the Run-19 class)."""
from __future__ import annotations

import asyncio
import subprocess
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


def _run_validation(repo, monkeypatch, tree_enabled: bool):
    monkeypatch.setenv(
        "JARVIS_VALIDATE_CANDIDATE_TREE_ENABLED",
        "true" if tree_enabled else "false",
    )
    from backend.core.ouroboros.governance import orchestrator as om

    orch = object.__new__(om.Orchestrator)
    # Minimal config seam: _run_validation_core reads project_root and the
    # boot-time validation runner. Mirror the real attribute names — READ
    # _run_validation_core first and adjust construction to the actual
    # attributes it touches (config, runner, semaphores); keep this the
    # smallest faithful construction that reaches the runner block.
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

    candidate = {"file_path": "pkg/mod.py", "full_content": _FIXED}
    return asyncio.run(orch._run_validation_core(_Ctx(), candidate, 120.0))


def test_candidate_tree_on_correct_repair_validates_green(broken_repo, monkeypatch):
    result = _run_validation(broken_repo, monkeypatch, tree_enabled=True)
    assert result.passed, f"fc={result.failure_class} err={result.error}"


def test_candidate_tree_on_wrong_repair_fails(broken_repo, monkeypatch):
    """Wrong candidate (keeps the broken content) must FAIL fc=test —
    proving the tree run exercises the candidate, not vacuously passing."""
    # Refactor _run_validation into
    # _run_validation_for(repo, file_rel, content, monkeypatch, tree_enabled)
    # and drive it with the BROKEN content:
    result = _run_validation_for(
        broken_repo, "pkg/mod.py", "def f():\n    return 2\n",
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
```

NOTE to implementer: `_run_validation_core` is a large method on a fully-initialized Orchestrator; the `object.__new__` construction above is the INTENT (drive the real seam with minimal state). READ the method first and set exactly the attributes it dereferences on this path (config/runner/etc.); if a hard dependency makes the bare construction impractical, extract the runner block into a module-level coroutine `_execute_candidate_tree_validation(project_root, validation_runner, ctx, all_files, remaining_s) -> Optional[MultiAdapterResult-shaped result]` and unit-test THAT, with a thin call from `_run_validation_core` plus an AST pin that the call exists. Choose whichever keeps the test REAL (actual RepairSandbox, actual router, actual pytest subprocess — no mocks). Fill in `test_candidate_tree_on_wrong_repair_fails` accordingly (wrong candidate = broken content → `passed=False`, `fc="test"`).

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/test_validate_candidate_tree.py -v`
Expected: `test_candidate_tree_on_correct_repair_validates_green` FAILS on current code (the correct repair fails `fc=test` — the Run-19 class); the legacy pin may already pass.

- [ ] **Step 3: Implement**

In `orchestrator.py`:

(a) Module-level, near the other env helpers:

```python
def _candidate_tree_enabled() -> bool:
    """Slice 9 (default ON): VALIDATE materializes a working-tree-faithful
    candidate tree (RepairSandbox + dirty overlay + candidate files applied)
    and anchors the LanguageRouter AT the tree — so validation exercises
    the CANDIDATE, not the still-broken real tree (Run #19 root cause).
    OFF restores the legacy side-sandbox path byte-identically."""
    return os.environ.get(
        "JARVIS_VALIDATE_CANDIDATE_TREE_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")
```

(b) Inside `_run_validation_core`, immediately BEFORE the existing `with tempfile.TemporaryDirectory(prefix="ouroboros_validate_") as sandbox_str:` block, insert the candidate-tree attempt. On success it produces `multi` (the same `MultiAdapterResult` the legacy block produces) and skips the legacy block; on ANY fault it logs and falls through:

```python
        # ── Slice 9: candidate-tree validation (default ON) ─────────────
        # Run #19 root cause: the legacy path writes candidates into an
        # EMPTY side-tempdir while pytest runs from the main repo root —
        # the candidate is never exercised, so a repair op whose failing
        # test stays red until APPLY structurally cannot pass VALIDATE.
        # Here: materialize a working-tree-faithful full tree
        # (RepairSandbox + Slice-9 dirty overlay), apply the candidate
        # files INTO it, and run a per-op LanguageRouter anchored AT the
        # tree root (the Slice-12AE per-op-router pattern) — discovery
        # finds the sibling tests in the tree and pytest runs from the
        # tree root with the tree's own pytest.ini. Fail-SOFT: any fault
        # falls through to the legacy path (today's behavior).
        multi = None
        _tree_used = False
        if _candidate_tree_enabled() and _all_files:
            try:
                from backend.core.ouroboros.governance.repair_sandbox import (
                    RepairSandbox,
                )
                from backend.core.ouroboros.governance.test_runner import (
                    CppAdapter,
                    LanguageRouter,
                    PythonAdapter,
                )
                async with RepairSandbox(
                    _ae_effective_repo_root, max(remaining_s, 30.0),
                ) as _tree:
                    _troot = _tree.sandbox_root
                    _tree_changed: list = []
                    for _fp, _fc in _all_files:
                        _rel = Path(_fp)
                        if _rel.is_absolute():
                            try:
                                _rel = _rel.resolve().relative_to(
                                    Path(_ae_effective_repo_root).resolve()
                                )
                            except ValueError:
                                raise RuntimeError(
                                    f"candidate path outside repo: {_fp}"
                                )
                        await _tree.apply_full_content(_fc, str(_rel))
                        _tf = _troot / _rel
                        if _tf.suffix in _RUNNABLE_EXTENSIONS:
                            _tree_changed.append(_tf)
                    if _tree_changed:
                        _tree_runner = LanguageRouter(
                            repo_root=_troot,
                            adapters={
                                "python": PythonAdapter(repo_root=_troot),
                                "cpp": CppAdapter(repo_root=_troot),
                            },
                        )
                        multi = await _tree_runner.run(
                            changed_files=tuple(_tree_changed),
                            sandbox_dir=_troot,
                            timeout_budget_s=remaining_s,
                            op_id=ctx.op_id,
                            original_paths={
                                p: _troot / p.relative_to(_troot)
                                for p in _tree_changed
                            },
                        )
                        _tree_used = True
                        logger.info(
                            "[Validation] candidate-tree run op=%s files=%d "
                            "passed=%s",
                            ctx.op_id[:12], len(_tree_changed),
                            getattr(multi, "passed", None),
                        )
            except Exception as _tree_exc:  # noqa: BLE001 — fail-soft
                multi = None
                _tree_used = False
                logger.warning(
                    "[Validation] candidate-tree materialization failed "
                    "(%s) — falling back to legacy side-sandbox path op=%s",
                    _tree_exc, ctx.op_id[:12],
                )
```

(c) Wrap the existing legacy `with tempfile.TemporaryDirectory(...)` block in `if not _tree_used:` (indent it one level — do this mechanically and carefully; the block ends where `multi` is consumed into the compact ValidationResult in "Step 5" of the method). The post-block result-mapping code consumes `multi` identically for both paths.

(Note: `original_paths` for tree files maps each file to itself — files are IN the tree, so `_normalize`'s repo-root branch resolves them; the identity map is belt-and-suspenders for the shape-preference path. `BlockedPathError` from the tree runner propagates to the existing `except BlockedPathError` handler → `fc="security"` — the Slice-8 gate keeps working, now anchored at the tree.)

- [ ] **Step 4: Run the new tests + the Slice-8 battery for no-regression**

Run: `python3 -m pytest tests/governance/test_validate_candidate_tree.py tests/governance/test_validate_sandbox_declared_roots.py tests/governance/test_validation_failure_logging.py -v`
Expected: PASS (all — the Slice-8 declared-roots and §7 logging pins must stay green; the legacy path is intact under the flag).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py tests/governance/test_validate_candidate_tree.py
git commit -m "feat(slice9): VALIDATE exercises the candidate — working-tree-faithful candidate tree with per-op router (Run #19 root cause)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Leaf-pair e2e pin under node policy

**Files:**
- Modify: `tests/governance/test_validate_candidate_tree.py` (append)

**Interfaces:**
- Consumes: Task 2's candidate-tree path; the real repo's leaf pair (`backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py` + `tests/governance/a1_ignition_vector/test_leaf_predicates.py`).

- [ ] **Step 1: Write the test**

```python
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
        monkeypatch.setenv("JARVIS_VALIDATE_CANDIDATE_TREE_ENABLED", "true")
        real = Path(__file__).resolve().parents[2]
        leaf_rel = Path("backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py")
        test_rel = Path("tests/governance/a1_ignition_vector/test_leaf_predicates.py")

        repo = tmp_path / "repo"
        for rel in (leaf_rel, test_rel):
            dst = repo / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text((real / rel).read_text())
        # package __init__ chain for imports + root pytest.ini
        for parent in (leaf_rel.parent, *leaf_rel.parent.parents):
            if parent == Path("."):
                break
            init = repo / parent / "__init__.py"
            init.parent.mkdir(parents=True, exist_ok=True)
            if not init.exists():
                init.write_text("")
        (repo / "pytest.ini").write_text("[pytest]\naddopts = -p no:cacheprovider\n")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo,
                       check=True, capture_output=True)

        correct = (repo / leaf_rel).read_text()
        # Chaos: break clamp01 in the WORKING TREE (uncommitted).
        (repo / leaf_rel).write_text(
            correct.replace("return", "return  # chaos\n    raise ValueError()\n    return", 1)
            if "return" in correct else correct + "\nraise ValueError()\n"
        )

        result = _run_validation_for(  # implementer: reuse/extract the
            repo, leaf_rel, correct, monkeypatch)  # Task-2 helper
        assert result.passed, f"fc={result.failure_class} err={result.error}"
```

Implementer note: reuse the Task-2 construction helper (rename it `_run_validation_for(repo, file_rel, content, monkeypatch)` and refactor Task 2's tests onto it — one seam, three tests). The chaos mutation just needs to make `test_leaf_predicates.py` fail against the mutated file and pass against `correct`; if the string-replace above doesn't reliably break it, mutate `clamp01`'s body explicitly (read the real file first and construct the broken variant deterministically).

- [ ] **Step 2: Run — expected PASS** (Task 2 already landed the behavior; this pins the real-pair scenario). If it FAILS, the tree path has a real gap — stop and report, don't massage the test.

Run: `python3 -m pytest tests/governance/test_validate_candidate_tree.py -v`

- [ ] **Step 3: Commit**

```bash
git add tests/governance/test_validate_candidate_tree.py
git commit -m "test(slice9): pin the Run #19 leaf-pair scenario end-to-end under node policy

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: L2 lane — real test targets, pinned pytest config, honest failure logging

**Files:**
- Modify: `backend/core/ouroboros/governance/repair_engine.py` (test-target scoping block at ~1110-1131 — the "Known limitation" comment; the result-label line at ~1153)
- Modify: `backend/core/ouroboros/governance/repair_sandbox.py` (`run_tests` cmd construction at ~385-402)
- Test: Create `tests/governance/test_repair_lane_test_targets.py`

**Interfaces:**
- Consumes: `ctx.target_files` (for attributed TestFailure ops: `(*source_loci, test_locus)` — test loci are identifiable via `_is_test_infra`-style path shape); `SandboxValidationResult` (fields: `passed/stdout/stderr/returncode/duration_s`).
- Produces: (1) module-level `_resolve_l2_test_targets(ctx, fallback: Tuple[str, ...]) -> Tuple[str, ...]` in `repair_engine.py` — returns the TEST-shaped entries of `ctx.target_files` when any exist (env `JARVIS_L2_TEST_TARGET_THREADING_ENABLED`, default true), else `fallback` (the legacy scoping); (2) `run_tests` pins pytest config with `"-c", str(sandbox / "pytest.ini")` when that file exists in the sandbox; (3) the iteration-result log replaces the phantom `getattr(svr, 'failure_class', 'unknown')` with `rc=<returncode>` + a 200-char stdout/stderr tail at INFO on failure.

**Why (diagnosed live):** L2 scoped pytest to the CANDIDATE's file — for a source repair that's a source file with zero tests; worse, any pytest invocation under `backend/` picks up `backend/pytest.ini` whose `--cov`/`-n` addopts are unrecognized in the runtime → usage error rc=4 → `passed=False`, labeled `FAILED (unknown)` by a `getattr` on a field the dataclass never has. Three defects, one lane.

- [ ] **Step 1: Write the failing tests**

```python
"""Slice 9 — the L2 lane's three diagnosed defects, pinned.

(1) Test scoping: L2 scoped pytest to the CANDIDATE file (a source file,
zero tests). With attributed scope the failing TEST is in
ctx.target_files — thread it. (2) backend/pytest.ini hijack: scoping
under backend/ picks up addopts (--cov/-n) the runtime doesn't have →
rc=4 usage error. Pin the config to the sandbox root's pytest.ini.
(3) 'FAILED (unknown)': getattr on a field SandboxValidationResult never
has — log rc + output tail instead."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.repair_engine import (
    _resolve_l2_test_targets,
)


class _Ctx:
    def __init__(self, targets):
        self.target_files = targets


class TestResolveL2TestTargets:
    def test_attributed_scope_threads_the_test_locus(self):
        ctx = _Ctx((
            "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py",
            "tests/governance/a1_ignition_vector/test_leaf_predicates.py",
        ))
        assert _resolve_l2_test_targets(ctx, ("fallback.py",)) == (
            "tests/governance/a1_ignition_vector/test_leaf_predicates.py",
        )

    def test_no_test_shaped_targets_falls_back(self):
        ctx = _Ctx(("backend/x/a.py",))
        assert _resolve_l2_test_targets(ctx, ("backend/x/a.py",)) == (
            "backend/x/a.py",
        )

    def test_missing_target_files_falls_back(self):
        assert _resolve_l2_test_targets(object(), ("f.py",)) == ("f.py",)

    def test_kill_switch_falls_back(self, monkeypatch):
        monkeypatch.setenv("JARVIS_L2_TEST_TARGET_THREADING_ENABLED", "false")
        ctx = _Ctx(("src.py", "tests/test_src.py"))
        assert _resolve_l2_test_targets(ctx, ("src.py",)) == ("src.py",)


def test_run_tests_pins_sandbox_pytest_ini(tmp_path):
    """The pytest cmd must carry -c <sandbox>/pytest.ini when it exists —
    structurally pinned by inspecting the constructed argv (source-level
    assertion; the subprocess behavior is covered by Task 2's e2e)."""
    src = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/repair_sandbox.py"
    ).read_text()
    assert '"-c"' in src and 'pytest.ini' in src, (
        "run_tests does not pin the pytest config — backend/pytest.ini "
        "hijack (rc=4 usage error) is live"
    )


def test_no_phantom_failure_class_label(tmp_path):
    src = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/repair_engine.py"
    ).read_text()
    assert "getattr(svr, \"failure_class\"" not in src.replace("'", '"'), (
        "the phantom failure_class getattr still labels every L2 failure "
        "'(unknown)'"
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/test_repair_lane_test_targets.py -v`
Expected: FAIL — `ImportError: cannot import name '_resolve_l2_test_targets'`; both source-pin tests fail.

- [ ] **Step 3: Implement**

(a) `repair_engine.py` module level:

```python
def _l2_test_threading_enabled() -> bool:
    return os.environ.get(
        "JARVIS_L2_TEST_TARGET_THREADING_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")


def _resolve_l2_test_targets(ctx: Any, fallback: Tuple[str, ...]) -> Tuple[str, ...]:
    """Slice 9: run L2's sandbox pytest against the op's REAL failing
    test(s). Attributed TestFailure scope is ``(*source_loci,
    test_locus)`` — the test-shaped entries are the ones pytest can
    actually exercise; scoping to the CANDIDATE file (the legacy
    behavior, kept as *fallback*) collects zero tests for source repairs.
    Deterministic path-shape check (no imports of the attribution
    module — same convention TestRunner's JARVIS_TEST_DIR_NAMES uses)."""
    if not _l2_test_threading_enabled():
        return fallback
    targets = getattr(ctx, "target_files", None) or ()
    dir_names = {
        p.strip() for p in os.environ.get(
            "JARVIS_TEST_DIR_NAMES", "tests",
        ).split(",") if p.strip()
    }
    test_shaped = tuple(
        t for t in targets
        if str(t).replace("\\", "/").split("/", 1)[0] in dir_names
        or Path(str(t)).name.startswith("test_")
    )
    return test_shaped or fallback
```

(Check `os`, `Any`, `Tuple` imports.)

(b) At the scoping site (~1120-1131), route both branches through the resolver:

```python
                        if _is_multi:
                            test_targets = _resolve_l2_test_targets(
                                ctx, tuple(p for p, _ in _multi_files),
                            )
                        else:
                            test_targets = _resolve_l2_test_targets(
                                ctx, (file_path,) if file_path else (),
                            )
```

(READ the enclosing scope first: `ctx` must be reachable there — `_run_inner` receives it; thread it down if the block lives in a helper without `ctx`.)

(c) The label line (~1153): replace `getattr(svr, "failure_class", "unknown")`-style rendering with honest facts:

```python
                        _logger.info(
                            "🔧 [L2 Repair] Iteration %d/%d tests: %s (rc=%s)",
                            iteration, budget.max_iterations,
                            "✅ PASSED" if svr.passed else "❌ FAILED",
                            svr.returncode,
                        )
                        if not svr.passed:
                            _tail = (svr.stdout or svr.stderr or "")[-200:]
                            _logger.info(
                                "🔧 [L2 Repair] Iteration %d failure tail: %s",
                                iteration, _tail,
                            )
```

(Anchor on the actual current log call — read it first; keep the emoji/format family of the file.)

(d) `repair_sandbox.py` `run_tests`, in the cmd construction (before `cmd.extend(test_targets)`):

```python
        _ini = sandbox / "pytest.ini"
        if _ini.exists():
            # Pin the config: without this, targets under backend/ make
            # pytest adopt backend/pytest.ini whose --cov/-n addopts are
            # unrecognized in the runtime → usage error rc=4 (Run 18/19
            # 'FAILED (unknown)' contributor).
            cmd.extend(["-c", str(_ini)])
```

(READ the surrounding function for the actual `sandbox` variable name.)

- [ ] **Step 4: Run new tests + repair suites**

Run: `python3 -m pytest tests/governance/test_repair_lane_test_targets.py -v && python3 -m pytest tests/governance -k "repair_sandbox or repair_engine or repair_lane" -q --continue-on-collection-errors 2>&1 | tail -2`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/repair_engine.py backend/core/ouroboros/governance/repair_sandbox.py tests/governance/test_repair_lane_test_targets.py
git commit -m "fix(slice9): L2 runs the REAL failing tests with a pinned pytest config and honest failure logging

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Auditor chaos-lineage reject scoping

**Files:**
- Modify: `scripts/a1_graduation_auditor.py` (`_correlate_flag_signal` ~1650-1670)
- Test: `tests/scripts/test_a1_flag_audit_corroboration.py` (append)

**Interfaces:**
- Consumes: existing `self.resumed_ops`, `self.lineage: OpLineageGraph` (`has_chaos_target()`, `in_chaos_lineage(op_id)`), `self.observed_unrelated_flag_rejects`.
- Produces: a corroborated REJECT on an op OUTSIDE the audited scope (resumed-ops set OR, new, the chaos lineage) is recorded as unrelated, not a poisoning REJECT. Env `JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS` (default true). When `op_id` is not supplied by the caller, parse it from the line (`op=op-…` token) before giving up.

**Why (Run #19):** three iron_gate flags were poisoned by a GENUINE, self-corroborated `exploration_insufficient` rejection on background op `op-019f58a7` — the gate working correctly on an op that has nothing to do with the audited chaos repair. The existing unrelated-reject lane only activates for `resumed_ops` audits; single-session audits globally correlate (the auditor's own docstring admits it).

- [ ] **Step 1: Write the failing tests**

Append to `tests/scripts/test_a1_flag_audit_corroboration.py` (reuse its module-load + `_fresh_auditor` pattern; extend `_fresh_auditor` to accept `chaos_files=("backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py",)` passed as `chaos_manifest_path=None` + direct `a.lineage = auditor_mod.OpLineageGraph(list(chaos_files))` after construction — read the ctor first and mirror how lineage is built):

```python
_GENUINE_IRON_GATE_REJECT_UNRELATED_OP = (
    "2026-07-12T16:27:44 [Ouroboros.Orchestrator] WARNING [Orchestrator] "
    "Iron Gate — exploration_insufficient: 0/2 (attempt=1) "
    "op=op-019f58a7-f623-756b-bf73-0b4309aaaaaa"
)


def _auditor_with_chaos_lineage(chaos_op: str = "op-chaos-1"):
    a = _fresh_auditor_families(["JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED"])
    a.lineage = auditor_mod.OpLineageGraph(
        ["backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py"],
    )
    # Register the chaos op so the lineage is non-empty and scoping is live.
    a.lineage.observe(chaos_op, target_files=[
        "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py",
    ])  # mirror the graph's real ingestion API — read OpLineageGraph first
    return a


def test_unrelated_op_reject_does_not_poison(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_with_chaos_lineage()
    a._correlate_flag_signal(_GENUINE_IRON_GATE_REJECT_UNRELATED_OP)
    assert not any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )
    assert any("iron_gate" in x for x in a.observed_unrelated_flag_rejects)


def test_chaos_lineage_op_reject_still_poisons(monkeypatch):
    a = _auditor_with_chaos_lineage(chaos_op="op-019f58a7-f623-756b-bf73-0b4309aaaaaa")
    a._correlate_flag_signal(_GENUINE_IRON_GATE_REJECT_UNRELATED_OP)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )


def test_lineage_scoping_kill_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", "false")
    a = _auditor_with_chaos_lineage()
    a._correlate_flag_signal(_GENUINE_IRON_GATE_REJECT_UNRELATED_OP)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )
```

(`_fresh_auditor_families` = tiny generalization of the file's `_fresh_auditor` taking a flag list; refactor in place. The `a.lineage.observe(...)` call is illustrative — READ `OpLineageGraph`'s real node-ingestion method (`_OpNode` construction in `ingest_*`) and use the real API; the test intent is: chaos op registered → `in_chaos_lineage(chaos_op) is True`, unrelated op → False.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/scripts/test_a1_flag_audit_corroboration.py -v -k "lineage or unrelated_op"`
Expected: FAIL — the unrelated-op reject poisons (`false_positive_rejected` True).

- [ ] **Step 3: Implement**

(a) Env helper next to `_corroborated_rejects_enabled`:

```python
def _lineage_scoped_rejects_enabled() -> bool:
    return os.environ.get(
        "JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", "true",
    ).strip().lower() not in ("0", "false", "no", "off")
```

(b) In `_correlate_flag_signal`, extend the existing unrelated-reject guard. Current shape:

```python
            if hit_reject:
                if self.resumed_ops and op_id and op_id not in self.resumed_ops:
                    self.observed_unrelated_flag_rejects.append(...)
                    continue
```

New shape (op_id parsed from the line when the caller didn't supply it; chaos-lineage scoping added):

```python
            if hit_reject:
                _op = op_id
                if not _op:
                    _m = re.search(r"op=(op-[0-9a-f-]+)", text)
                    _op = _m.group(1) if _m else None
                _outside_resumed = (
                    self.resumed_ops and _op and _op not in self.resumed_ops
                )
                _outside_chaos = (
                    not self.resumed_ops
                    and _lineage_scoped_rejects_enabled()
                    and _op is not None
                    and self.lineage.has_chaos_target()
                    and not self.lineage.in_chaos_lineage(_op)
                )
                if _outside_resumed or _outside_chaos:
                    self.observed_unrelated_flag_rejects.append(
                        "%s:op=%s" % (family, _op)
                    )
                    continue
```

(`re` is already imported at module level — verify. Lines with NO parseable op stay globally correlated — fail-CLOSED: an unattributable rejection still poisons, per the auditor's honest-audit discipline.)

- [ ] **Step 4: Run the auditor suites**

Run: `python3 -m pytest tests/scripts/ -q 2>&1 | tail -2`
Expected: green (modulo known pre-existing).

- [ ] **Step 5: Commit**

```bash
git add scripts/a1_graduation_auditor.py tests/scripts/test_a1_flag_audit_corroboration.py
git commit -m "fix(slice9): flag-audit rejects scoped to the chaos lineage in single-session audits — a gate firing correctly elsewhere is not a misfire

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Pre-GATE sandbox write-escape clamp

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` — the legacy VALIDATE sandbox write loop (`for _fp, _fc in _all_files:` inside the `tempfile.TemporaryDirectory` block, ~12839+ post-Task-2 renumbering; anchor on `_sandbox_file = sandbox / _rel`)
- Test: Create `tests/governance/test_validate_sandbox_write_clamp.py`

**Interfaces:**
- Consumes: the legacy write loop; `ValidationResult` (`failure_class="security"` convention).
- Produces: a candidate `file_path` whose resolved sandbox write target escapes the sandbox (e.g. `../../../tmp/evil.py`) is REJECTED before any byte is written: the loop raises `BlockedPathError` (imported from `test_runner`) with the offending path; the existing `except BlockedPathError` handler already maps it to `fc="security"`. Also applied to the Task-2 candidate-tree apply loop (its `relative_to` check already rejects absolute-outside; add the same resolve-containment check for `..` relatives).

**Why (Slice-8 final review, Important #1):** `sandbox / _rel` with a `..`-containing model-chosen `file_path` writes LLM-controlled content OUTSIDE the tempdir during VALIDATE — before any risk gate has fired. Slice 8's routing gate catches the path AFTER the write lands; the clamp stops the write itself.

- [ ] **Step 1: Write the failing test**

```python
"""Slice 9 — pre-GATE sandbox write-escape clamp (Slice-8 final review I1).

A model-chosen file_path of ../../x escapes tempfile sandboxes via
`sandbox / rel` + mkdir(parents=True) + write_text — an arbitrary-write
primitive that fires during VALIDATE, before any risk gate. The clamp
rejects the candidate as fc='security' BEFORE any byte lands."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def test_dotdot_candidate_is_security_rejected_before_write(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import orchestrator as om
    from backend.core.ouroboros.governance.test_runner import (
        LanguageRouter, PythonAdapter,
    )

    escape_target = tmp_path / "escaped_evil.py"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("JARVIS_VALIDATE_CANDIDATE_TREE_ENABLED", "false")

    orch = object.__new__(om.Orchestrator)
    orch._config = om.OrchestratorConfig(project_root=repo)
    orch._validation_runner = LanguageRouter(
        repo_root=repo, adapters={"python": PythonAdapter(repo_root=repo)},
    )

    class _Ctx:
        op_id = "op-slice9-clamp"
        intake_evidence_json = ""
        target_files = ("x.py",)

    # Enough ../ to escape any tempdir depth, then an absolute-ish landing
    # inside tmp_path so we can assert nothing was written there.
    rel_escape = "../" * 12 + str(escape_target).lstrip("/")
    candidate = {"file_path": rel_escape, "full_content": "print('pwned')\n"}
    result = asyncio.run(orch._run_validation_core(_Ctx(), candidate, 30.0))

    assert not result.passed
    assert result.failure_class == "security"
    assert not escape_target.exists(), "the escaped write LANDED — clamp inert"
```

(Same construction-seam note as Task 2 — reuse the established minimal-Orchestrator pattern from `tests/governance/test_validate_candidate_tree.py`.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/test_validate_sandbox_write_clamp.py -v`
Expected: FAIL — either the file EXISTS (write landed) or `fc` is not `security`.

- [ ] **Step 3: Implement**

In the legacy write loop, immediately after `_sandbox_file` is computed and BEFORE `mkdir`/`write_text`:

```python
                # Slice 9 — write-escape clamp (Slice-8 final review I1):
                # a model-chosen ``..``-containing file_path must not
                # write outside the sandbox. Resolve WITHOUT touching the
                # filesystem and prove containment before any byte lands.
                # BlockedPathError maps to fc='security' via the existing
                # handler — same class as the routing gate, caught at the
                # WRITE now instead of after it.
                _resolved_target = Path(os.path.normpath(str(_sandbox_file)))
                if not str(_resolved_target).startswith(
                    os.path.normpath(str(sandbox)) + os.sep
                ):
                    from backend.core.ouroboros.governance.test_runner import (
                        BlockedPathError,
                    )
                    raise BlockedPathError(
                        f"candidate file_path {_fp!r} escapes the VALIDATE "
                        "sandbox — write refused (security gate)"
                    )
```

And in the Task-2 candidate-tree apply loop, extend the existing relative-path handling with the same normpath containment proof against `_troot` before `apply_full_content` (identical block, `sandbox` → `_troot`, using the pre-computed `_troot / _rel`).

(Note: `os.path.normpath` — pure string normalization, no FS access, collapses `..` — is the right primitive here; `Path.resolve()` would touch the FS and follow symlinks that don't exist yet. Verify the `except BlockedPathError` handler encloses the write loop — it wraps the runner call; if the loop sits OUTSIDE the try, widen the try to start before the loop (read the block; keep the handler's body untouched).)

- [ ] **Step 4: Run clamp test + Slice-8/9 VALIDATE batteries**

Run: `python3 -m pytest tests/governance/test_validate_sandbox_write_clamp.py tests/governance/test_validate_candidate_tree.py tests/governance/test_validate_sandbox_declared_roots.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py tests/governance/test_validate_sandbox_write_clamp.py
git commit -m "fix(slice9): pre-GATE write-escape clamp — a ..-containing candidate path cannot write outside the VALIDATE sandbox

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Docs — flag registry, CLAUDE.md, memory topic, ledger

**Files:**
- Modify: `backend/core/ouroboros/governance/flag_registry_seed.py` (attribution/validate block: `— 7 flags (Slices 7+8)` → `— 10 flags (Slices 7-9)`)
- Modify: `CLAUDE.md` (Slice 6/7/8 attribution bullet + L2 Repair bullet)
- Modify: `docs/memory_topics/intake/project_slice6_test_source_attribution.md` (append Slice 9 section)
- Modify: `.superpowers/sdd/progress.md` (on disk only — gitignored, exclude from commit)

- [ ] **Step 1: Three FlagSpecs** (mirror existing shape; `since="2026-07-12"`, `category=Category.SAFETY` for the mirror + candidate-tree, `posture_relevance=_HARDEN_CRITICAL`):

```python
    FlagSpec(
        name="JARVIS_SANDBOX_WORKING_TREE_MIRROR_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 9: RepairSandbox's git-worktree strategy overlays the "
            "working tree's dirty delta so the sandbox baseline matches "
            "what TestWatcher actually observed (HEAD-only baselines "
            "validated repairs against the wrong world). OFF restores "
            "the legacy HEAD baseline."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/repair_sandbox.py",
        example="true",
        since="2026-07-12",
        posture_relevance=_HARDEN_CRITICAL,
    ),
    FlagSpec(
        name="JARVIS_VALIDATE_CANDIDATE_TREE_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 9 (Run #19 root cause): VALIDATE materializes a "
            "working-tree-faithful candidate tree (RepairSandbox + "
            "candidate files applied) and anchors a per-op LanguageRouter "
            "AT the tree, so validation exercises the CANDIDATE instead "
            "of the still-broken real tree. Fail-soft: any tree fault "
            "falls back to the legacy side-sandbox path. OFF restores "
            "legacy byte-identically."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/orchestrator.py",
        example="true",
        since="2026-07-12",
        posture_relevance=_HARDEN_CRITICAL,
    ),
    FlagSpec(
        name="JARVIS_L2_TEST_TARGET_THREADING_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 9: L2's sandbox pytest runs against the op's REAL "
            "failing test(s) (the test-shaped entries of "
            "ctx.target_files) instead of the candidate file (which for "
            "source repairs contains zero tests). OFF restores legacy "
            "candidate-file scoping."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/repair_engine.py",
        example="true",
        since="2026-07-12",
        posture_relevance=_HARDEN_CRITICAL,
    ),
```

(`JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS` is script-side — CLAUDE.md/memory-topic mention only, NO FlagSpec, same rule as Slice 8.)

- [ ] **Step 2: CLAUDE.md** — append to the attribution bullet after the Slice 8 sentence:

```
Slice 9: VALIDATE exercises the candidate — working-tree-faithful candidate tree (`RepairSandbox` + dirty-delta overlay, `JARVIS_SANDBOX_WORKING_TREE_MIRROR_ENABLED`) with a per-op router anchored at the tree (`JARVIS_VALIDATE_CANDIDATE_TREE_ENABLED`, fail-soft to legacy); L2 runs the REAL failing tests with a pinned pytest config (`JARVIS_L2_TEST_TARGET_THREADING_ENABLED`; the `backend/pytest.ini` rc=4 hijack is dead) and honest rc+tail failure logs; pre-GATE `..` write-escape clamp (fc='security' before any byte lands); flag-audit rejects scoped to the chaos lineage in single-session audits (`JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS`).
```

And in the **L2 Repair** bullet add one clause: `Slice 9: sandbox baseline = working tree; real failing-test scoping; honest failure logs.`

- [ ] **Step 3: memory topic** — append `## Slice 9 — VALIDATE exercises the candidate (2026-07-12)` (~25 lines matching the file's density: Run-19 one-grep diagnosis, the three-defect L2 stack incl. the pytest.ini rc=4 repro, the working-tree-baseline principle, the flags). Ledger block on disk.

- [ ] **Step 4: Verify + commit**

Run: `python3 -m pytest tests/governance/test_flag_registry_seed_truth.py -q`
Expected: PASS.

```bash
git add backend/core/ouroboros/governance/flag_registry_seed.py CLAUDE.md docs/memory_topics/intake/project_slice6_test_source_attribution.md
git commit -m "docs(slice9): flag registry + CLAUDE.md + memory topic for candidate-tree validation and the L2 lane fixes

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Whole-slice verification sweep

**Files:** none — verification only.

- [ ] **Step 1: Full touched-surface battery**

```bash
python3 -m pytest tests/governance/test_repair_sandbox_working_tree_mirror.py tests/governance/test_validate_candidate_tree.py tests/governance/test_repair_lane_test_targets.py tests/governance/test_validate_sandbox_write_clamp.py tests/scripts/test_a1_flag_audit_corroboration.py tests/governance/test_validate_sandbox_declared_roots.py tests/governance/test_validation_failure_logging.py tests/governance/test_repair_sandbox_node_policy.py tests/test_ouroboros_governance/test_multi_file_coverage_gate.py tests/governance/test_attribution_scope_gate.py tests/governance/test_flag_registry_seed_truth.py -q
```

Expected: all PASS.

- [ ] **Step 2: The Run-19 class, by hand** — broken tiny repo + correct candidate through the real seam (the Task 2 test IS this; re-run it standalone and paste the output).

- [ ] **Step 3: Report** — counts + outputs; hand off to whole-branch review, then Run #20 (operator-conducted from the main session).

---

## Verification for the arc (post-plan, main session)

Run #20 acceptance: `[Attribution]` → sig-op `[source, test]` → coverage pass → **VALIDATE GREEN via candidate tree** → GATE (NOTIFY floor fires for test-only; source repair is SAFE/NOTIFY) → APPLY on source → post-apply VERIFY `pass_rate=1.0` → AutoCommit → `proven=true` + `twelve_flag_audit_passed=true` + `intervention_lock_clean=true`. This is the A1 gate.
