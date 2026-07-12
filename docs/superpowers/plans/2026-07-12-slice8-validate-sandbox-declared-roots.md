# Slice 8 — VALIDATE Sandbox Declared-Roots Contract + Audit/Gate Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kill the Run #18 blocker at its root — VALIDATE's own security gate rejects the sandbox VALIDATE itself creates under the real node's no-`/tmp` policy — plus the four hardening workstreams the Run #18 autopsy and the Slice 7 final review queued (§7 validate-failure logging, flag-audit REJECT false positives, resolved-attribution containment, test-only NOTIFY_APPLY floor).

**Architecture:** The root problem is a split source of truth: `orchestrator._run_validation_core` writes candidates into a `tempfile.TemporaryDirectory` (lands in `$TMPDIR`) while `test_runner._normalize` accepts only `repo_root ∪ _effective_sandbox_prefixes()` — so with `JARVIS_SANDBOX_PREFIXES=/nonexistent-sandbox-prefix` (the real `/opt/trinity` node policy, mirrored by `IsomorphicEnv`) every runnable candidate self-destructs `BlockedPathError → failure_class='security'`. The fix is a **declared-roots contract inside the router**: `LanguageRouter.run` already receives `sandbox_dir` (the per-op sandbox the orchestrator itself created — trusted, explicit) and `original_paths` (sandbox→repo mapping) — the containment gate now honors them: a changed file is legitimate iff it is under `repo_root`, under the *declared* `sandbox_dir`, or (legacy fallback for callers that declare nothing) under the env prefixes. Normalization prefers the **original repo-relative shape** via `original_paths` (fixing the latent routing bug where sandbox files collapse to `path.name` and directory-shape rules like `^mlforge/` can never match), then the sandbox-relative shape, then the legacy bare filename. One seam heals both VALIDATE paths — the extracted `validate_runner.py:290` calls the shared `orch._run_validation` (verified, not duplicated). Security direction is unchanged and provenance is *stricter*: the primary allow is an explicit per-call contract, not an ambient env guess.

**Tech Stack:** Python 3.9+ stdlib only. No new dependencies. `from __future__ import annotations` everywhere.

## Global Constraints

- Python 3.9+ — no `asyncio.timeout`; use `asyncio.wait_for`. `from __future__ import annotations` required in all files.
- Env-var driven config with sensible defaults; **no hardcoded paths or model names**. New flags (all BOOL, default `true`): `JARVIS_ATTRIBUTION_CONTAINMENT_ENABLED`, `JARVIS_ATTRIBUTION_TEST_ONLY_NOTIFY_ENABLED`, `JARVIS_A1_AUDIT_CORROBORATED_REJECTS`.
- Security posture may only get STRICTER or equal: `BlockedPathError` still raises for any path outside `repo_root ∪ declared sandbox_dir ∪ env prefixes`; containment adds a new rejection class; the NOTIFY floor only raises tiers (stricter-wins), never lowers.
- Byte-identical legacy behavior for callers that declare nothing: default kwargs (`sandbox_dir=None`, `original_paths=None`) preserve today's exact semantics.
- Anything wired at a phase seam must land on BOTH execution paths where two exist (inline orchestrator + extracted phase runner) with an AST wiring pin — the Slice-6 T5 lesson. VALIDATE itself is single-seam (`_run_validation` is shared); GATE risk-floor wiring (Task 7) is dual-path.
- The node-policy environment for tests is `monkeypatch.setenv("JARVIS_SANDBOX_PREFIXES", "/nonexistent-sandbox-prefix")` — the exact value `IsomorphicEnv` uses (`backend/core/ouroboros/battle_test/isomorphic_env.py:55`). Never bake this string into product code.
- TDD per task: failing test first (RED), implement, GREEN. Run tests from the repo root: `python3 -m pytest <path> -v`.
- Commit style: conventional commits, `git add` only named files (never `-A`/`.`), footer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Known pre-existing failures to NOT chase: `tests/governance/intent/test_vector_observation_path.py` (3× `_poll_once_lock`), merkle/phase11/l2-fixture collection errors.

---

### Task 1: Declared-roots contract in `test_runner._normalize` / `_route` / `LanguageRouter.run`

**Files:**
- Modify: `backend/core/ouroboros/governance/test_runner.py` (`_normalize` at ~214-235, `_route` at ~239-256, `LanguageRouter.run` `_route` call at ~668)
- Test: Create `tests/governance/test_validate_sandbox_declared_roots.py`

**Interfaces:**
- Consumes: existing `_effective_sandbox_prefixes()`, `BlockedPathError`, `_ADAPTER_RULES`.
- Produces: `_normalize(path: Path, repo_root: Path, sandbox_dir: Optional[Path] = None, original_paths: Optional[Dict[Path, Path]] = None) -> str` and `_route(changed_files: Tuple[Path, ...], repo_root: Path, sandbox_dir: Optional[Path] = None, original_paths: Optional[Dict[Path, Path]] = None) -> FrozenSet[str]`. `LanguageRouter.run` forwards its existing `sandbox_dir`/`original_paths` parameters into `_route`. Tasks 2-3 rely on these exact signatures.

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/test_validate_sandbox_declared_roots.py`:

```python
"""Slice 8 — declared-roots containment contract for the VALIDATE sandbox.

Run #18 root cause (repro'd in a 2s isolation harness): the orchestrator
writes candidates into a $TMPDIR sandbox, but test_runner._normalize only
accepted repo_root + env-prefix paths — under the real node's no-/tmp
policy (JARVIS_SANDBOX_PREFIXES=/nonexistent-sandbox-prefix, the exact
value IsomorphicEnv mirrors) EVERY runnable candidate self-destructed
BlockedPathError → failure_class='security'. The fix: the gate honors the
caller-DECLARED sandbox_dir + original_paths that LanguageRouter.run
already receives. These tests pin the contract."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.test_runner import (
    BlockedPathError,
    _normalize,
    _route,
)

_NODE_POLICY = "/nonexistent-sandbox-prefix"  # isomorphic_env.py:55 value


@pytest.fixture
def node_policy(monkeypatch):
    monkeypatch.setenv("JARVIS_SANDBOX_PREFIXES", _NODE_POLICY)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture
def sandbox(tmp_path):
    sb = tmp_path / "ouroboros_validate_x"
    sb.mkdir()
    return sb


class TestDeclaredSandboxContainment:
    def test_node_policy_declared_sandbox_passes(self, node_policy, repo, sandbox):
        """THE Run #18 repro: under node policy, a candidate in the
        DECLARED per-op sandbox must not be security-blocked."""
        f = sandbox / "backend" / "core" / "leaf.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")
        norm = _normalize(f, repo, sandbox_dir=sandbox)
        assert norm == "backend/core/leaf.py"  # sandbox-relative SHAPE, not bare name

    def test_original_paths_shape_wins(self, node_policy, repo, sandbox):
        """Routing fidelity: the ORIGINAL repo-relative shape is preferred
        so directory-shape adapter rules (^mlforge/ etc.) match sandbox
        copies exactly as they match in-repo files."""
        f = sandbox / "mlforge" / "kernel.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")
        norm = _normalize(
            f, repo, sandbox_dir=sandbox,
            original_paths={f: repo / "mlforge" / "kernel.py"},
        )
        assert norm == "mlforge/kernel.py"

    def test_node_policy_undeclared_still_blocked(self, node_policy, repo, sandbox):
        """No declaration → legacy env-prefix policy governs; under node
        policy the tempdir is outside it → still a security rejection."""
        f = sandbox / "x.py"
        f.write_text("x = 1\n")
        with pytest.raises(BlockedPathError):
            _normalize(f, repo)

    def test_outside_declared_sandbox_still_blocked(self, node_policy, repo, sandbox, tmp_path):
        """Declaring sandbox A does NOT whitelist unrelated tempdir B —
        the gate is per-call and exact."""
        other = tmp_path / "unrelated"
        other.mkdir()
        f = other / "evil.py"
        f.write_text("x = 1\n")
        with pytest.raises(BlockedPathError):
            _normalize(f, repo, sandbox_dir=sandbox)

    def test_repo_root_containment_unchanged(self, node_policy, repo):
        f = repo / "pkg" / "mod.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")
        assert _normalize(f, repo, sandbox_dir=None) == "pkg/mod.py"

    def test_legacy_prefix_fallback_byte_identical(self, monkeypatch, repo, sandbox):
        """No declaration + default prefixes → today's exact behavior
        (bare filename) is preserved for undeclared callers."""
        monkeypatch.delenv("JARVIS_SANDBOX_PREFIXES", raising=False)
        f = sandbox / "sub" / "thing.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")
        assert _normalize(f, repo) == "thing.py"


class TestRouteWithDeclaredSandbox:
    def test_route_shape_fidelity_under_node_policy(self, node_policy, repo, sandbox):
        """mlforge/ files must route to python+cpp even as sandbox copies —
        the latent path.name collapse made this impossible before."""
        f = sandbox / "mlforge" / "kernel.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")
        required = _route(
            (f,), repo, sandbox_dir=sandbox,
            original_paths={f: repo / "mlforge" / "kernel.py"},
        )
        assert required == frozenset({"python", "cpp"})

    def test_route_default_python_for_backend_file(self, node_policy, repo, sandbox):
        f = sandbox / "backend" / "mod.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n")
        required = _route((f,), repo, sandbox_dir=sandbox)
        assert required == frozenset({"python"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_validate_sandbox_declared_roots.py -v`
Expected: FAIL — `TypeError: _normalize() got an unexpected keyword argument 'sandbox_dir'` (and same for `_route`).

- [ ] **Step 3: Implement the contract**

In `backend/core/ouroboros/governance/test_runner.py` replace `_normalize` (lines ~214-235) with:

```python
def _normalize(
    path: Path,
    repo_root: Path,
    sandbox_dir: Optional[Path] = None,
    original_paths: Optional[Dict[Path, Path]] = None,
) -> str:
    """Resolve *path* to a repo-relative POSIX string for adapter routing.

    Declared-roots containment contract (Slice 8): a changed file is
    legitimate iff it lives under *repo_root*, under the caller-DECLARED
    *sandbox_dir* (the per-op temp sandbox the orchestrator itself created
    and passed down through ``LanguageRouter.run``), or — legacy fallback
    for callers that declare nothing — under the env-policy prefixes from
    :func:`_effective_sandbox_prefixes`. Anything else raises
    :class:`BlockedPathError`. Direction unchanged, provenance STRICTER:
    the primary allow is an explicit per-call contract, not an ambient
    env guess. Run #18 root cause: under the node's no-/tmp policy the
    env guess rejected the orchestrator's OWN validate sandbox.

    Routing shape: prefer the ORIGINAL repo-relative shape via
    *original_paths* (so directory-shape adapter rules match sandbox
    copies exactly as in-repo files), then the sandbox-relative shape,
    then (legacy prefixes) the bare filename — the pre-Slice-8 behavior.
    """
    try:
        resolved = path.resolve()
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        resolved = path.resolve()
    # Declared sandbox: map back to the original repo-relative shape when
    # the caller provided the mapping (truthful adapter routing).
    if original_paths:
        orig = original_paths.get(path) or original_paths.get(resolved)
        if orig is not None:
            try:
                return (
                    Path(orig).resolve()
                    .relative_to(repo_root.resolve())
                    .as_posix()
                )
            except ValueError:
                pass  # original itself outside repo — fall through
    if sandbox_dir is not None:
        try:
            return resolved.relative_to(Path(sandbox_dir).resolve()).as_posix()
        except ValueError:
            pass  # not under the declared sandbox — fall through
    # Legacy env-prefix fallback — byte-identical pre-Slice-8 behavior for
    # callers that do not declare a sandbox.
    resolved_str = str(resolved)
    if any(resolved_str.startswith(p) for p in _effective_sandbox_prefixes()):
        return path.name
    raise BlockedPathError(
        f"Path {path} resolves outside repo root {repo_root}"
        + (
            f" and declared sandbox {sandbox_dir}"
            if sandbox_dir is not None else ""
        )
        + ". Pipeline CANCELLED — security gate."
    )
```

Replace `_route` (lines ~239-256) signature and the `_normalize` call:

```python
def _route(
    changed_files: Tuple[Path, ...],
    repo_root: Path,
    sandbox_dir: Optional[Path] = None,
    original_paths: Optional[Dict[Path, Path]] = None,
) -> FrozenSet[str]:
    """Return union of required adapters across all changed files.

    Uses first-matching rule per file (table order), union across all files.
    Raises BlockedPathError if any file is outside the declared-roots
    contract (repo_root ∪ declared sandbox_dir ∪ legacy env prefixes).
    """
    required: set[str] = set()
    for path in changed_files:
        norm = _normalize(
            path, repo_root,
            sandbox_dir=sandbox_dir, original_paths=original_paths,
        )
        for rule in _ADAPTER_RULES:
            if rule.pattern.match(norm):
                required.update(rule.adapters)
                break
    return frozenset(required)
```

(Keep the loop body identical to the current one apart from the `_normalize` call — read the current lines 246-256 first and preserve anything else verbatim.)

In `LanguageRouter.run` (~line 668) replace:

```python
        required_names = _route(changed_files, self._repo_root)
```

with:

```python
        required_names = _route(
            changed_files, self._repo_root,
            sandbox_dir=sandbox_dir, original_paths=original_paths,
        )
```

- [ ] **Step 4: Run new tests + the module's existing suite**

Run: `python3 -m pytest tests/governance/test_validate_sandbox_declared_roots.py -v && python3 -m pytest tests -k "test_runner or language_router" -q`
Expected: new tests PASS; pre-existing test_runner suites PASS unchanged.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/test_runner.py tests/governance/test_validate_sandbox_declared_roots.py
git commit -m "fix(slice8): declared-roots containment contract in the VALIDATE router — gate honors the sandbox it is handed (Run #18 root cause)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: End-to-end node-policy pin + orchestrator call-site wiring pin

**Files:**
- Modify: `tests/governance/test_validate_sandbox_declared_roots.py` (append)

**Interfaces:**
- Consumes: Task 1's router contract; `LanguageRouter`, `PythonAdapter` from `test_runner`; the real repo's `backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py` + `tests/governance/a1_ignition_vector/test_leaf_predicates.py` (the Run-#18 pair).
- Produces: regression pins only — no product code.

- [ ] **Step 1: Write the failing e2e test (fails on pre-Task-1 code; passes after — verify by inspection that it exercises the full router)**

Append to `tests/governance/test_validate_sandbox_declared_roots.py`:

```python
import ast as _ast
import asyncio
import tempfile

_REPO = Path(__file__).resolve().parents[2]
_LEAF_REL = Path("backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py")


class TestRun18EndToEnd:
    def test_run18_repro_full_router_under_node_policy(self, node_policy):
        """THE Run #18 scenario, end-to-end through the REAL router +
        PythonAdapter + real pytest on the real leaf pair: under node
        policy, the declared per-op sandbox must validate green — not
        die BlockedPathError → fc='security'."""
        from backend.core.ouroboros.governance.test_runner import (
            LanguageRouter,
            PythonAdapter,
        )
        router = LanguageRouter(
            repo_root=_REPO,
            adapters={"python": PythonAdapter(repo_root=_REPO)},
        )
        content = (_REPO / _LEAF_REL).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="ouroboros_validate_") as sb:
            sbp = Path(sb)
            f = sbp / _LEAF_REL
            f.parent.mkdir(parents=True)
            f.write_text(content, encoding="utf-8")
            result = asyncio.run(router.run(
                changed_files=(f,),
                sandbox_dir=sbp,
                timeout_budget_s=120,
                op_id="slice8-pin",
                original_paths={f: _REPO / _LEAF_REL},
            ))
        assert result.passed, (
            f"fc={result.failure_class}: "
            f"{result.dominant_failure and result.dominant_failure.test_result.stdout[:300]}"
        )


class TestOrchestratorDeclaresContract:
    """AST pin: the VALIDATE call site must keep declaring sandbox_dir +
    original_paths to the router — deleting either silently reverts the
    Run #18 class (the wired-but-inert lesson, structurally enforced)."""

    def test_run_validation_forwards_declared_roots(self):
        src = (
            _REPO / "backend" / "core" / "ouroboros" / "governance"
            / "orchestrator.py"
        ).read_text(encoding="utf-8")
        tree = _ast.parse(src)
        hits = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                fn = node.func
                if getattr(fn, "attr", "") == "run" and any(
                    k.arg == "sandbox_dir" for k in node.keywords
                ):
                    hits.append({k.arg for k in node.keywords})
        assert any(
            {"sandbox_dir", "original_paths"} <= kw for kw in hits
        ), "no .run(sandbox_dir=..., original_paths=...) call in orchestrator"
```

- [ ] **Step 2: Run to verify both pass on the fixed code**

Run: `python3 -m pytest tests/governance/test_validate_sandbox_declared_roots.py -v`
Expected: PASS (all, including Task 1's). To prove the e2e pin is non-vacuous, temporarily `git stash` and confirm it FAILS on the pre-Task-1 code with `BlockedPathError`, then `git stash pop`. Record both outputs in your report.

- [ ] **Step 3: Commit**

```bash
git add tests/governance/test_validate_sandbox_declared_roots.py
git commit -m "test(slice8): Run #18 e2e node-policy pin + orchestrator declared-roots wiring pin

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Sweep the remaining validation-sandbox sites (L2 repair lane) under node policy

**Files:**
- Test: Create `tests/governance/test_repair_sandbox_node_policy.py`
- Modify (ONLY if the test proves red): the minimal site it implicates — expected candidates are `backend/core/ouroboros/governance/repair_engine.py` (its TestRunner/pytest invocation) or `backend/core/ouroboros/governance/repair_sandbox.py`.

**Interfaces:**
- Consumes: `RepairSandbox` (`repair_sandbox.py`, git-worktree/rsync full-tree sandbox, `sandbox_root` property); `TestRunner` from `test_runner.py`.
- Produces: a pinned answer to "does the L2 lane share the Run-#18 class?" — Run #18's L2 iterations reported `tests: ❌ FAILED (unknown)` which may be this same class masked.

- [ ] **Step 1: Write the diagnostic test (it MUST codify the correct behavior; if it fails, the fix is in scope)**

```python
"""Slice 8 sweep: the L2 repair lane's sandbox test runs must survive the
node's no-/tmp sandbox-prefix policy. RepairSandbox materializes a FULL
tree (git worktree/rsync) in a mkdtemp dir; if any path gate anchored at
the MAIN repo_root judges files inside it, the L2 lane inherits the
Run #18 class (masked as 'unknown' failures)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_NODE_POLICY = "/nonexistent-sandbox-prefix"


@pytest.fixture
def node_policy(monkeypatch):
    monkeypatch.setenv("JARVIS_SANDBOX_PREFIXES", _NODE_POLICY)


def test_testrunner_anchored_at_sandbox_root_survives_node_policy(
    node_policy, tmp_path
):
    """A TestRunner whose repo_root IS the sandbox tree (how the repair
    lane must anchor) treats in-sandbox tests as safe — no vacuous pass,
    no skip — even under node policy."""
    from backend.core.ouroboros.governance.test_runner import _is_safe_path

    sandbox_tree = tmp_path / "jarvis_repair_sandbox_x"
    test_file = sandbox_tree / "tests" / "test_ok.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_ok():\n    assert True\n")
    assert _is_safe_path(test_file, sandbox_tree) is True


def test_repair_engine_test_invocation_is_sandbox_anchored(node_policy):
    """Structural pin: grep-level assertion that the repair engine's test
    execution anchors its runner/pytest cwd at the SANDBOX root, not the
    main repo root judging sandbox paths. Read the source and assert the
    anchoring expression exists; if this fails, the L2 lane shares the
    Run #18 class and the minimal fix (anchor the runner at
    sandbox.sandbox_root) is in scope for this task."""
    src = (
        _REPO / "backend" / "core" / "ouroboros" / "governance"
        / "repair_engine.py"
    ).read_text(encoding="utf-8")
    assert "sandbox_root" in src, (
        "repair_engine.py never references sandbox_root — its test runs "
        "cannot be sandbox-anchored; L2 inherits the Run #18 class"
    )
```

- [ ] **Step 2: Run the diagnostic**

Run: `python3 -m pytest tests/governance/test_repair_sandbox_node_policy.py -v`
Expected: unknown a priori — this is the sweep. GREEN → the L2 lane is structurally sound; commit the pins as regression armor and note it. RED → implement the MINIMAL anchoring fix in the implicated file (anchor the repair-lane TestRunner/pytest at `sandbox.sandbox_root`; do NOT widen any prefix whitelist), re-run to GREEN, and describe the exact fix in your report.

- [ ] **Step 3: Run the neighboring repair suites for no-regression**

Run: `python3 -m pytest tests -k "repair_sandbox or repair_engine" -q`
Expected: PASS (modulo the plan's known pre-existing failures).

- [ ] **Step 4: Commit**

```bash
git add tests/governance/test_repair_sandbox_node_policy.py
# plus repair_engine.py / repair_sandbox.py ONLY if Step 2 went red and you fixed it
git commit -m "test(slice8): pin the L2 repair lane sandbox anchoring under node policy

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: §7 observability — failed VALIDATE results must be visible in the log

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` — inside `async def _run_validation` (~line 12484; the thin wrapper whose body calls `self._run_validation_core(ctx, candidate, remaining_s)` at ~12497)
- Test: Create `tests/governance/test_validation_failure_logging.py`

**Interfaces:**
- Consumes: `ValidationResult` (fields: `passed`, `failure_class`, `short_summary`, `error`, `adapter_names_run`).
- Produces: a WARNING log line `[Validation] FAILED op=<id> fc=<class> summary=<...> error=<...> adapters=<...>` emitted for EVERY failed validation on BOTH FSM paths (they share this wrapper). Run #18's `BlockedPathError` detail was recoverable NOWHERE (debug.log, verdict, telemetry) — this closes that §7 hole at the single seam.

- [ ] **Step 1: Write the failing test**

```python
"""Slice 8 §7: a failed ValidationResult must surface its failure_class +
short_summary + error in the log at WARNING — Run #18's security-class
rejection (BlockedPathError) was recoverable nowhere post-hoc."""
from __future__ import annotations

import asyncio
import logging

import pytest


def test_failed_validation_logs_summary(caplog, monkeypatch):
    from backend.core.ouroboros.governance import orchestrator as orch_mod

    orch = object.__new__(orch_mod.Orchestrator)  # no __init__ — seam test

    class _Ctx:
        op_id = "op-slice8-logpin-0000"

    failed = orch_mod.ValidationResult(
        passed=False,
        best_candidate=None,
        validation_duration_s=0.01,
        error="BlockedPathError: Path /tmp/x resolves outside repo root /r",
        failure_class="security",
        short_summary="BlockedPathError: Path /tmp/x ...",
        adapter_names_run=(),
    )

    async def _fake_core(self, ctx, candidate, remaining_s):
        return failed

    monkeypatch.setattr(
        orch_mod.Orchestrator, "_run_validation_core", _fake_core,
    )
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            orch_mod.Orchestrator._run_validation(orch, _Ctx(), {}, 10.0)
        )
    assert result is failed
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "[Validation] FAILED" in joined
    assert "fc=security" in joined
    assert "BlockedPathError" in joined
    assert "op-slice8-logp" in joined
```

Note: if `ValidationResult`'s constructor requires different/extra fields, mirror the real dataclass exactly (read its definition in `orchestrator.py` first) — the test must construct it faithfully, not loosely.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/governance/test_validation_failure_logging.py -v`
Expected: FAIL — `assert "[Validation] FAILED" in joined` (no such log emitted today).

- [ ] **Step 3: Implement the log at the shared seam**

In `orchestrator.py`, inside `_run_validation`, immediately after the line `result = await self._run_validation_core(ctx, candidate, remaining_s)` (~12497) and before whatever the wrapper does next, insert:

```python
        # Slice 8 §7 — a failed validation MUST be operator-visible.
        # Run #18: a BlockedPathError → fc='security' rejection carried
        # its reason ONLY inside the ValidationResult; nothing logged it,
        # so the blocked path was unrecoverable post-hoc. Single seam:
        # both the inline FSM and the extracted validate_runner call
        # this wrapper.
        try:
            if result is not None and not result.passed:
                logger.warning(
                    "[Validation] FAILED op=%s fc=%s summary=%s error=%s adapters=%s",
                    str(getattr(ctx, "op_id", ""))[:12],
                    result.failure_class or "",
                    (result.short_summary or "")[:200],
                    (result.error or "")[:280],
                    ",".join(result.adapter_names_run or ()),
                )
        except Exception:  # noqa: BLE001 — logging must never perturb VALIDATE
            pass
```

(If the wrapper's body differs from this shape, anchor on the `_run_validation_core` call and place the block directly after it; do not restructure anything else.)

- [ ] **Step 4: Run to verify GREEN + spot the seam is shared**

Run: `python3 -m pytest tests/governance/test_validation_failure_logging.py -v && grep -c "_run_validation(" backend/core/ouroboros/governance/phase_runners/validate_runner.py`
Expected: PASS; grep count ≥ 1 (extracted path calls the same wrapper — no second site to wire).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py tests/governance/test_validation_failure_logging.py
git commit -m "feat(slice8): log failed VALIDATE results at the shared seam — §7 closes the invisible BlockedPathError class

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Flag-audit corroborated-rejects — kill the substring false-positive

**Files:**
- Modify: `scripts/a1_graduation_auditor.py` (`_correlate_flag_signal` at ~1604-1641; `__init__` list inits near line 1155; the export dict near line 1770)
- Test: Create `tests/scripts/test_a1_flag_audit_corroboration.py`

**Interfaces:**
- Consumes: `_FAMILY_SIGNALS` (line ~217; e.g. `semantic_guardian.rejected = ("APPROVAL_REQUIRED", "removed_import_still_referenced")`, `.evaluated = ("[SemanticGuard]", "semantic_guard")`).
- Produces: REJECT classification requires same-line family corroboration — `hit_reject` only counts when the line ALSO matches one of the family's `evaluated` markers. Gated `JARVIS_A1_AUDIT_CORROBORATED_REJECTS` (default true). Uncorroborated hits are recorded (never silently dropped — §7) in a new `self.uncorroborated_reject_lines` list exported alongside `observed_unrelated_flag_rejects`.

**Why:** Run #18's `twelve_flag_audit_passed=false` came from a `[CommProtocol] INTENT` payload line carrying `'risk_tier': 'APPROVAL_REQUIRED'` (stamped legitimately by the Slice-6 attribution gate) — the bare `"APPROVAL_REQUIRED"` substring family-blindly poisoned `semantic_guardian` flags to REJECTED. The family's own genuine rejection lines carry its `[SemanticGuard]` tag on the same line, so corroboration keeps true rejects red.

- [ ] **Step 1: Write the failing tests**

```python
"""Slice 8 — corroborated-rejects rule for the A1 flag audit.

Run #18 false-red: a CommProtocol INTENT payload carrying
risk_tier='APPROVAL_REQUIRED' (stamped by the Slice-6 attribution gate)
matched semantic_guardian's bare 'APPROVAL_REQUIRED' rejected-marker and
poisoned the family to REJECTED → twelve_flag_audit_passed=false. A REJECT
must be corroborated by the family's own voice on the same line."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "a1_graduation_auditor", _REPO / "scripts" / "a1_graduation_auditor.py",
)
assert _spec is not None and _spec.loader is not None
auditor_mod = importlib.util.module_from_spec(_spec)
sys.modules["a1_graduation_auditor"] = auditor_mod
_spec.loader.exec_module(auditor_mod)

# The REAL Run-18 poisoning line (truncated as the auditor stores it):
_RUN18_INTENT_LINE = (
    "2026-07-12T00:28:42 [backend.core.ouroboros.governance.comm_protocol] "
    "INFO [CommProtocol] INTENT op=op-019f5539-f107-74e0 seq=1 payload="
    "{'goal': 'x', 'risk_tier': 'APPROVAL_REQUIRED'}"
)
_GENUINE_GUARD_REJECT_LINE = (
    "2026-07-12T00:29:01 [Ouroboros.Orchestrator] INFO [SemanticGuard] "
    "op=op-x findings=1 pattern=removed_import_still_referenced "
    "risk=APPROVAL_REQUIRED"
)


def _fresh_auditor():
    # Same construction pattern as tests/scripts/test_a1_provenance_auditor.py.
    # One semantic_guardian-family flag is enough: family_for_flag maps the
    # SEMANTIC_GUARDIAN needle in the name to the family.
    return auditor_mod.A1GraduationAuditor(
        flags=["JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED"],
        strict=True,
        chaos_manifest_path=None,
        lineage_scoping_enabled=False,
    )


def test_intent_payload_line_does_not_poison(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_AUDIT_CORROBORATED_REJECTS", raising=False)
    a = _fresh_auditor()
    a._correlate_flag_signal(_RUN18_INTENT_LINE)
    assert not any(
        st.false_positive_rejected
        for st in a._by_family.get("semantic_guardian", ())
    )
    assert any(
        "semantic_guardian" in x for x in a.uncorroborated_reject_lines
    )


def test_genuine_guard_rejection_still_poisons(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_AUDIT_CORROBORATED_REJECTS", raising=False)
    a = _fresh_auditor()
    a._correlate_flag_signal(_GENUINE_GUARD_REJECT_LINE)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("semantic_guardian", ())
    )


def test_kill_switch_restores_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_AUDIT_CORROBORATED_REJECTS", "false")
    a = _fresh_auditor()
    a._correlate_flag_signal(_RUN18_INTENT_LINE)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("semantic_guardian", ())
    )
```

Add `import sys` to the test's imports. The two assertion targets (`false_positive_rejected`, `uncorroborated_reject_lines`) are the contract.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/scripts/test_a1_flag_audit_corroboration.py -v`
Expected: FAIL — `AttributeError: ... 'uncorroborated_reject_lines'` (or the INTENT line poisons under legacy logic).

- [ ] **Step 3: Implement corroboration in `_correlate_flag_signal`**

Add near the auditor's other env helpers (module level):

```python
def _corroborated_rejects_enabled() -> bool:
    return os.environ.get(
        "JARVIS_A1_AUDIT_CORROBORATED_REJECTS", "true",
    ).strip().lower() not in ("0", "false", "no", "off")
```

In `__init__` next to `self.observed_unrelated_flag_rejects: List[str] = []` (line ~1155) add:

```python
        self.uncorroborated_reject_lines: List[str] = []
```

In `_correlate_flag_signal`, replace:

```python
            hit_reject = any(m and m in text for m in rejected_markers)
            hit_eval = any(m and m in text for m in evaluated_markers)
            if hit_reject:
```

with:

```python
            hit_reject = any(m and m in text for m in rejected_markers)
            hit_eval = any(m and m in text for m in evaluated_markers)
            if hit_reject and not hit_eval and _corroborated_rejects_enabled():
                # Family-blind substring (e.g. a CommProtocol INTENT payload
                # carrying risk_tier=APPROVAL_REQUIRED, stamped legitimately
                # by the Slice-6 attribution gate) — not this family's own
                # gate speaking. Run-18 false-red class. Recorded, never
                # silently dropped (§7).
                self.uncorroborated_reject_lines.append(
                    "%s:%s" % (family, text[:120])
                )
                hit_reject = False
            if hit_reject:
```

In the export dict (near line 1770, next to `"observed_unrelated_flag_rejects": list(...)`) add:

```python
                "uncorroborated_reject_lines": list(
                    self.uncorroborated_reject_lines
                ),
```

- [ ] **Step 4: Run to verify GREEN + auditor suite no-regression**

Run: `python3 -m pytest tests/scripts/test_a1_flag_audit_corroboration.py tests/scripts/ -q`
Expected: PASS (modulo known pre-existing failures elsewhere; the tests/scripts suite itself green).

- [ ] **Step 5: Commit**

```bash
git add scripts/a1_graduation_auditor.py tests/scripts/test_a1_flag_audit_corroboration.py
git commit -m "fix(slice8): flag-audit REJECT requires same-line family corroboration — kills the Run-18 INTENT-payload false red

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Resolved-attribution containment at the coverage gate

**Files:**
- Modify: `backend/core/ouroboros/governance/multi_file_coverage_gate.py` (env const block ~line 52; helpers after `subset_coverage_enabled`; `check_candidate` body)
- Test: `tests/test_ouroboros_governance/test_multi_file_coverage_gate.py` (append)

**Interfaces:**
- Consumes: Slice 7's `_attribution_resolved(intake_evidence_json)`, `subset_coverage_enabled()`, `REASON_PREFIX`, `_candidate_paths`, `_normalize_path`.
- Produces: with resolved attribution + `JARVIS_ATTRIBUTION_CONTAINMENT_ENABLED` (default true), a candidate touching ANY file outside the attributed `target_files` is rejected `(reason, offending_paths)` with `reason` starting `REASON_PREFIX + ": scope_containment"`. Provider-agnostic — this closes the gap the Slice-7 final review named: the DW-only `file_scope_mismatch` guard checks intersection, and TestFailure ops route to Claude where NO scope guard runs.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ouroboros_governance/test_multi_file_coverage_gate.py`:

```python
# ---------------------------------------------------------------------------
# Slice 8 — resolved-attribution containment (candidate ⊆ attributed loci)
# ---------------------------------------------------------------------------


class TestAttributionContainment:
    """With RESOLVED attribution the scope is authoritative: any write
    outside the attributed loci is suspect (final-review I1: no ⊆ guard
    exists anywhere else, and none at all on the Claude route)."""

    def test_resolved_candidate_outside_loci_rejected(self):
        cand = {"files": [
            {"file_path": _SOURCE, "full_content": "a = 1\n"},
            {"file_path": "backend/somewhere/else.py", "full_content": "b = 2\n"},
        ]}
        result = check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        )
        assert result is not None
        reason, offending = result
        assert reason.startswith(REASON_PREFIX + ": scope_containment")
        assert offending == ["backend/somewhere/else.py"]

    def test_resolved_full_coverage_plus_extra_rejected(self):
        """Containment fires even at full coverage — stricter than
        pre-Slice-7 for resolved-attribution ops ONLY (deliberate)."""
        cand = {"files": [
            {"file_path": _SOURCE, "full_content": "a = 1\n"},
            {"file_path": _TEST, "full_content": "b = 2\n"},
            {"file_path": "backend/extra.py", "full_content": "c = 3\n"},
        ]}
        result = check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        )
        assert result is not None and "scope_containment" in result[0]

    def test_resolved_subset_within_loci_still_passes(self):
        cand = {"file_path": _SOURCE, "full_content": "a = 1\n"}
        assert check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        ) is None

    def test_unresolved_extra_paths_keep_legacy_pass(self):
        """No resolved attribution → legacy semantics (extras allowed at
        full coverage) — byte-identical for plain multi-file ops."""
        cand = {"files": [
            {"file_path": _SOURCE, "full_content": "a = 1\n"},
            {"file_path": _TEST, "full_content": "b = 2\n"},
            {"file_path": "backend/extra.py", "full_content": "c = 3\n"},
        ]}
        assert check_candidate(cand, [_SOURCE, _TEST]) is None

    def test_containment_master_off_restores_slice7(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ATTRIBUTION_CONTAINMENT_ENABLED", "false")
        cand = {"files": [
            {"file_path": _SOURCE, "full_content": "a = 1\n"},
            {"file_path": _TEST, "full_content": "b = 2\n"},
            {"file_path": "backend/extra.py", "full_content": "c = 3\n"},
        ]}
        assert check_candidate(
            cand, [_SOURCE, _TEST], intake_evidence_json=_RESOLVED,
        ) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_multi_file_coverage_gate.py -v -k Containment`
Expected: FAIL — the two rejection tests get `None` (no containment exists).

- [ ] **Step 3: Implement**

In `multi_file_coverage_gate.py`, after `_ENV_SUBSET` add:

```python
_ENV_CONTAINMENT = "JARVIS_ATTRIBUTION_CONTAINMENT_ENABLED"
```

After `subset_coverage_enabled()` add:

```python
def containment_enabled() -> bool:
    """Slice 8 (default ON): with RESOLVED attribution the attributed
    loci are authoritative — a candidate touching any path outside them
    is rejected. Provider-agnostic: unlike the DW-only intersection
    guard, this runs at the gate for every route (final-review I1)."""
    raw = os.environ.get(_ENV_CONTAINMENT, "true").strip().lower()
    return raw not in ("false", "0", "no", "off")
```

In `check_candidate`, restructure the tail so `_attribution_resolved` is evaluated once and containment fires BEFORE the full-coverage early return. Replace from the line `covered = _candidate_paths(candidate, project_root)` through the end of the subset-waiver block with:

```python
    covered = _candidate_paths(candidate, project_root)
    missing = [t for t in normalized_targets if t and t not in covered]

    attr_resolved = _attribution_resolved(intake_evidence_json)

    # Slice 8 — containment: with RESOLVED attribution the attributed
    # loci are the authoritative write-set; any candidate path outside
    # them is rejected regardless of coverage. Fires before the
    # full-coverage early return (extras at full coverage are equally
    # out-of-scope). Non-attributed ops keep legacy semantics.
    if attr_resolved and containment_enabled():
        _target_set = {t for t in normalized_targets if t}
        _outside = sorted(p for p in covered if p and p not in _target_set)
        if _outside:
            reason = (
                f"{REASON_PREFIX}: scope_containment: resolved-attribution "
                f"candidate touches {len(_outside)} file(s) outside the "
                f"attributed loci: {', '.join(_outside[:5])}"
            )
            logger.warning("[MultiFileCoverageGate] %s", reason)
            return (reason, _outside)

    if not missing:
        return None

    covered_targets = len(normalized_targets) - len(missing)
    if (
        covered_targets >= 1
        and subset_coverage_enabled()
        and attr_resolved
    ):
        # Slice 7 (Run #17): resolved-attribution scope is PERMISSIVE —
        # covering >=1 target suffices. (Containment above has already
        # bounded the candidate to the attributed loci when enabled.)
        logger.info(
            "[MultiFileCoverageGate] subset-coverage waiver: resolved "
            "attribution — candidate covers %d/%d target file(s)",
            covered_targets,
            len(normalized_targets),
        )
        return None
```

Keep the existing strict-rejection `reason = (...)` build after this, unchanged. Also update the module-docstring Contract section: append one bullet:

```
- Slice 8 containment: with ``attribution.status == "resolved"`` and
  ``JARVIS_ATTRIBUTION_CONTAINMENT_ENABLED`` truthy, ANY candidate path
  outside the attributed ``target_files`` rejects
  (``scope_containment``) — even at full coverage. The attributed loci
  are the authoritative write-set for the op.
```

And fix the Slice-7 waiver comment you replace so it no longer claims containment lives elsewhere (Task 6 IS the containment now — reference it plainly).

- [ ] **Step 4: Run the full gate file**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_multi_file_coverage_gate.py -v`
Expected: PASS — all pre-existing (incl. Slice 7 subset + `test_multi_target_extra_paths_in_files_list_ok` which has no attribution evidence) + 5 new.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/multi_file_coverage_gate.py tests/test_ouroboros_governance/test_multi_file_coverage_gate.py
git commit -m "feat(slice8): resolved-attribution containment — attributed loci are the authoritative write-set (closes the no-⊆-guard gap on every route)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: NOTIFY_APPLY floor for resolved-attribution test-only candidates (BOTH GATE paths)

**Files:**
- Modify: `backend/core/ouroboros/governance/intent/test_source_attribution.py` (append after `unattributed_test_scope_violation`; extract the shared normalizer)
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (new helper after `_attribution_scope_risk_floor` ~line 513; wire at the GATE risk site ~line 9077 right after the existing `_attribution_scope_risk_floor` call)
- Modify: `backend/core/ouroboros/governance/phase_runners/gate_runner.py` (import at ~line 130 block; wire at ~line 408 after the existing floor call)
- Test: `tests/governance/intent/test_source_attribution.py` (predicate tests) + `tests/governance/test_attribution_scope_gate.py` (helper + AST wiring pin)

**Interfaces:**
- Consumes: `_attribution_dict`, `_is_test_infra`, `_test_dir_names`, `_relpath_under_root` (existing in `test_source_attribution.py`); `RiskTier` in orchestrator.
- Produces:
  - `test_only_notify_floor_enabled() -> bool` (env `JARVIS_ATTRIBUTION_TEST_ONLY_NOTIFY_ENABLED`, default true)
  - `resolved_test_only_scope(intake_evidence_json: str, candidate_files: Sequence[str], *, repo_root: str = "") -> Optional[str]` — pure predicate, fail-soft None
  - `_normalize_candidate_paths(candidate_files: Sequence[str], repo_root: str) -> List[str]` — extracted shared normalizer (refactor `unattributed_test_scope_violation` onto it)
  - orchestrator helper `_attribution_test_only_notify_floor(ctx, candidate_file_paths, risk_tier, *, repo_root="") -> Tuple[RiskTier, Optional[str]]`

**Why:** Slice 7's subset waiver correctly lets a test-only candidate pass coverage on resolved attribution (the test may BE the fix target). But pre-Slice-7, full-coverage demands incidentally forced friction on that lane; the Slice-7 final review flagged the residual: a green-tier assertion-weakening test edit could now auto-apply and VERIFY green. The floor makes that lane operator-visible (NOTIFY_APPLY = diff preview + delay), never blocking (it is legitimate), never downgrading stricter tiers.

- [ ] **Step 1: Write the failing predicate tests**

Append to `tests/governance/intent/test_source_attribution.py`:

```python
class TestResolvedTestOnlyScope:
    _RESOLVED = json.dumps({"attribution": {
        "status": "resolved",
        "test_locus": "tests/governance/x/test_a.py",
        "source_loci": ["backend/x/a.py"],
    }})

    def test_test_only_candidate_flags(self):
        msg = resolved_test_only_scope(
            self._RESOLVED, ["tests/governance/x/test_a.py"],
        )
        assert msg is not None
        assert "test_only" in msg or "test loci" in msg

    def test_source_candidate_none(self):
        assert resolved_test_only_scope(
            self._RESOLVED, ["backend/x/a.py"],
        ) is None

    def test_mixed_candidate_none(self):
        assert resolved_test_only_scope(
            self._RESOLVED,
            ["backend/x/a.py", "tests/governance/x/test_a.py"],
        ) is None

    def test_unresolved_none(self):
        j = json.dumps({"attribution": {"status": "unresolved"}})
        assert resolved_test_only_scope(
            j, ["tests/governance/x/test_a.py"],
        ) is None

    def test_absolute_paths_normalized(self, tmp_path):
        root = str(tmp_path)
        j = self._RESOLVED
        assert resolved_test_only_scope(
            j, [str(tmp_path / "tests/governance/x/test_a.py")],
            repo_root=root,
        ) is not None

    def test_master_off_none(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ATTRIBUTION_TEST_ONLY_NOTIFY_ENABLED", "false",
        )
        assert resolved_test_only_scope(
            self._RESOLVED, ["tests/governance/x/test_a.py"],
        ) is None

    def test_malformed_evidence_none(self):
        assert resolved_test_only_scope(
            "{not json", ["tests/governance/x/test_a.py"],
        ) is None
```

(Add `resolved_test_only_scope` to the file's named-import block.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/governance/intent/test_source_attribution.py -v -k ResolvedTestOnly`
Expected: FAIL — `ImportError: cannot import name 'resolved_test_only_scope'`.

- [ ] **Step 3: Implement predicate + shared normalizer**

In `test_source_attribution.py`:

(a) Extract the normalization loop currently inside `unattributed_test_scope_violation` into:

```python
def _normalize_candidate_paths(
    candidate_files: Sequence[str], repo_root: str,
) -> list:
    """Repo-relative POSIX normalization for candidate paths — shared by
    the unresolved scope gate (Slice 6) and the test-only NOTIFY floor
    (Slice 8). Absolute paths under *repo_root* relativize via
    ``_relpath_under_root``; everything else gets slash/``./`` cleanup."""
    normalized = []
    for f in candidate_files:
        _norm = str(f).replace("\\", "/")
        if repo_root:
            _rel = _relpath_under_root(str(f), repo_root)
            if _rel:
                normalized.append(_rel)
                continue
        if _norm.startswith("./"):
            _norm = _norm[2:]
        normalized.append(_norm)
    return normalized
```

and replace that loop in `unattributed_test_scope_violation` with `normalized = _normalize_candidate_paths(candidate_files, repo_root)`.

(b) Append:

```python
def test_only_notify_floor_enabled() -> bool:
    return os.environ.get(
        "JARVIS_ATTRIBUTION_TEST_ONLY_NOTIFY_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")


def resolved_test_only_scope(
    intake_evidence_json: str,
    candidate_files: Sequence[str],
    *,
    repo_root: str = "",
) -> Optional[str]:
    """Slice 8: attribution RESOLVED + candidate mutates ONLY test loci.

    That lane is legitimate (the test may genuinely be the fix target —
    the whole point of the Slice-7 subset waiver) but sensitive: an
    assertion-weakening test edit auto-applies green and VERIFY passes by
    construction. Returns an advisory message — the caller floors risk at
    NOTIFY_APPLY (operator-visible diff, stricter-wins, never blocks,
    never downgrades) — or ``None``. Fail-soft on malformed evidence."""
    if not test_only_notify_floor_enabled() or not candidate_files:
        return None
    attribution = _attribution_dict(intake_evidence_json)
    if str(attribution.get("status", "")) != "resolved":
        return None
    dir_names = _test_dir_names()
    test_locus = str(attribution.get("test_locus", ""))
    normalized = _normalize_candidate_paths(candidate_files, repo_root)
    if all(
        f == test_locus or _is_test_infra(f, dir_names) for f in normalized
    ):
        return (
            "attribution_resolved_test_only_scope: attribution resolved "
            f"but the candidate mutates only test loci {normalized} — "
            "floored to NOTIFY_APPLY for operator visibility"
        )
    return None
```

- [ ] **Step 4: Run predicate tests + existing module suite**

Run: `python3 -m pytest tests/governance/intent/test_source_attribution.py -v`
Expected: PASS (new + all pre-existing, incl. the refactored `unattributed_test_scope_violation` cases).

- [ ] **Step 5: Write the failing wiring tests (helper + AST pin, both GATE paths)**

Append to `tests/governance/test_attribution_scope_gate.py`:

```python
# ---------------------------------------------------------------------------
# Slice 8 — test-only NOTIFY_APPLY floor: helper + dual-path wiring pin
# ---------------------------------------------------------------------------

import ast as _s8_ast
import json as _s8_json
from pathlib import Path as _S8Path

_S8_GOV = _S8Path(__file__).resolve().parents[2] / "backend" / "core" / "ouroboros" / "governance"


def test_notify_floor_raises_green_to_notify():
    from backend.core.ouroboros.governance.orchestrator import (
        RiskTier,
        _attribution_test_only_notify_floor,
    )

    class _Ctx:
        intake_evidence_json = _s8_json.dumps({"attribution": {
            "status": "resolved", "test_locus": "tests/x/test_a.py",
        }})

    tier, advisory = _attribution_test_only_notify_floor(
        _Ctx(), ["tests/x/test_a.py"], RiskTier.SAFE_AUTO,
    )
    assert tier is RiskTier.NOTIFY_APPLY
    assert advisory is not None


def test_notify_floor_never_downgrades():
    from backend.core.ouroboros.governance.orchestrator import (
        RiskTier,
        _attribution_test_only_notify_floor,
    )

    class _Ctx:
        intake_evidence_json = _s8_json.dumps({"attribution": {
            "status": "resolved", "test_locus": "tests/x/test_a.py",
        }})

    tier, _ = _attribution_test_only_notify_floor(
        _Ctx(), ["tests/x/test_a.py"], RiskTier.APPROVAL_REQUIRED,
    )
    assert tier is RiskTier.APPROVAL_REQUIRED


def _s8_calls(path, name):
    tree = _s8_ast.parse(path.read_text(encoding="utf-8"))
    return [
        n for n in _s8_ast.walk(tree)
        if isinstance(n, _s8_ast.Call)
        and (getattr(n.func, "id", "") == name or getattr(n.func, "attr", "") == name)
    ]


def test_notify_floor_wired_on_both_gate_paths():
    """The Slice-6 T5 lesson, pinned: one path wired + one inert is a red
    test forever."""
    for rel in ("orchestrator.py", "phase_runners/gate_runner.py"):
        calls = _s8_calls(_S8_GOV / rel, "_attribution_test_only_notify_floor")
        assert calls, f"{rel}: _attribution_test_only_notify_floor not wired"
```

- [ ] **Step 6: Run to verify wiring tests fail**

Run: `python3 -m pytest tests/governance/test_attribution_scope_gate.py -v -k notify_floor`
Expected: FAIL — `ImportError` / no calls found.

- [ ] **Step 7: Implement helper + wire both paths**

(a) `orchestrator.py`, immediately after `_attribution_scope_risk_floor` (ends ~line 513):

```python
def _attribution_test_only_notify_floor(
    ctx: Any,
    candidate_file_paths: Sequence[str],
    risk_tier: RiskTier,
    *,
    repo_root: str = "",
) -> Tuple[RiskTier, Optional[str]]:
    """Slice 8 companion to :func:`_attribution_scope_risk_floor`:
    RESOLVED attribution + test-only candidate → floor at NOTIFY_APPLY
    (operator-visible diff+delay; legitimate lane, so a notify, not an
    approval). Stricter-wins; fail-SOFT (any fault → no escalation)."""
    try:
        from backend.core.ouroboros.governance.intent.test_source_attribution import (  # noqa: E501
            resolved_test_only_scope,
        )
        advisory = resolved_test_only_scope(
            getattr(ctx, "intake_evidence_json", "") or "",
            candidate_file_paths,
            repo_root=repo_root,
        )
    except Exception:  # noqa: BLE001 — floor is protective, never fatal
        return risk_tier, None
    if not advisory:
        return risk_tier, None
    if risk_tier.value < RiskTier.NOTIFY_APPLY.value:
        risk_tier = RiskTier.NOTIFY_APPLY
    return risk_tier, advisory
```

(b) Wire in `orchestrator.py` at the GATE risk site: find the existing block around line 9077:

```python
                    risk_tier, _attr_violation = _attribution_scope_risk_floor(
```

and directly AFTER that call's full statement (including its argument lines and any logging tied to `_attr_violation`), add:

```python
                    risk_tier, _attr_test_only = _attribution_test_only_notify_floor(
                        ctx,
                        [fp for fp, _ in _pairs],
                        risk_tier,
                        repo_root=str(self._config.project_root),
                    )
                    if _attr_test_only:
                        logger.info(
                            "[Orchestrator] attribution test-only NOTIFY floor: %s op=%s",
                            _attr_test_only, ctx.op_id[:12],
                        )
```

Match the surrounding block's exact candidate-paths expression: the existing `_attribution_scope_risk_floor` call at this site already passes the candidate file list — reuse the SAME expression it uses (read it first; if it passes something other than `[fp for fp, _ in _pairs]`, mirror that exactly) and the same `repo_root` argument it uses.

(c) Wire in `gate_runner.py`: add `_attribution_test_only_notify_floor` to the existing import block at ~line 130 (next to `_attribution_scope_risk_floor`), then after the call at ~line 408 add the same block with the receiver spellings that file uses (`orch._config.project_root` etc. — mirror the neighboring `_attribution_scope_risk_floor` call's arguments exactly).

- [ ] **Step 8: Run wiring tests + both files import-sanity**

Run: `python3 -m pytest tests/governance/test_attribution_scope_gate.py -v && python3 -c "import backend.core.ouroboros.governance.orchestrator, backend.core.ouroboros.governance.phase_runners.gate_runner; print('imports ok')"`
Expected: PASS + `imports ok`.

- [ ] **Step 9: Commit**

```bash
git add backend/core/ouroboros/governance/intent/test_source_attribution.py backend/core/ouroboros/governance/orchestrator.py backend/core/ouroboros/governance/phase_runners/gate_runner.py tests/governance/intent/test_source_attribution.py tests/governance/test_attribution_scope_gate.py
git commit -m "feat(slice8): NOTIFY_APPLY floor for resolved-attribution test-only candidates on BOTH GATE paths

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Docs — flag registry, CLAUDE.md, memory topic, ledger

**Files:**
- Modify: `backend/core/ouroboros/governance/flag_registry_seed.py` (append to the attribution block — currently `— 5 flags (Slice 7 added subset coverage)`; becomes `— 7 flags (Slices 7+8)`)
- Modify: `CLAUDE.md` (the Slice 6/7 attribution bullet + the TestRunner bullet)
- Modify: `docs/memory_topics/intake/project_slice6_test_source_attribution.md` (append a Slice 8 section)
- Modify: `.superpowers/sdd/progress.md` (append one summary block; note: this file is gitignored — edit on disk, exclude from the commit)

- [ ] **Step 1: Two FlagSpecs** (mirror the existing entries' shape exactly; `since="2026-07-12"`, `category=Category.SAFETY`, `posture_relevance=_HARDEN_CRITICAL`):

```python
    FlagSpec(
        name="JARVIS_ATTRIBUTION_CONTAINMENT_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 8: with RESOLVED test->source attribution the "
            "attributed loci are the authoritative write-set — the "
            "MultiFileCoverageGate rejects any candidate path outside "
            "them (scope_containment), even at full coverage. Runs at "
            "the gate for EVERY provider route (the DW-only "
            "file_scope_mismatch guard checks intersection only). OFF "
            "restores Slice-7 semantics."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/multi_file_coverage_gate.py",
        example="true",
        since="2026-07-12",
        posture_relevance=_HARDEN_CRITICAL,
    ),
    FlagSpec(
        name="JARVIS_ATTRIBUTION_TEST_ONLY_NOTIFY_ENABLED",
        type=FlagType.BOOL, default=True,
        description=(
            "Slice 8: when attribution is RESOLVED and the candidate "
            "mutates ONLY test loci, the risk tier is floored at "
            "NOTIFY_APPLY (operator-visible diff + delay; stricter-wins, "
            "never a downgrade, never a block) on BOTH GATE paths. "
            "Closes the Slice-7 review's residual: an assertion-weakening "
            "test edit could auto-apply green and VERIFY passes by "
            "construction."
        ),
        category=Category.SAFETY,
        source_file="backend/core/ouroboros/governance/intent/test_source_attribution.py",
        example="true",
        since="2026-07-12",
        posture_relevance=_HARDEN_CRITICAL,
    ),
```

- [ ] **Step 2: CLAUDE.md** — in the Slice 6/7 attribution bullet, after the Slice 7 sentence, append:

```
Slice 8: declared-roots containment contract in the VALIDATE router (`test_runner._normalize` honors the caller-declared `sandbox_dir`/`original_paths` — kills the Run-18 `BlockedPathError→security` class under the node's no-`/tmp` policy and restores directory-shape adapter routing for sandbox copies); resolved-attribution containment at the coverage gate (`JARVIS_ATTRIBUTION_CONTAINMENT_ENABLED`) + test-only NOTIFY_APPLY floor on BOTH GATE paths (`JARVIS_ATTRIBUTION_TEST_ONLY_NOTIFY_ENABLED`); failed VALIDATE results logged at the shared `_run_validation` seam (§7); flag-audit REJECTs require same-line family corroboration (`JARVIS_A1_AUDIT_CORROBORATED_REJECTS`).
```

- [ ] **Step 3: memory topic Slice 8 section** (~20 lines: Run-18 root cause + 2s repro, declared-roots design, the four hardening items, flag names) appended to `docs/memory_topics/intake/project_slice6_test_source_attribution.md`; ledger block appended to `.superpowers/sdd/progress.md`.

- [ ] **Step 4: Verify seed truth + commit**

Run: `python3 -m pytest tests/governance/test_flag_registry_seed_truth.py -q`
Expected: PASS.

```bash
git add backend/core/ouroboros/governance/flag_registry_seed.py CLAUDE.md docs/memory_topics/intake/project_slice6_test_source_attribution.md
git commit -m "docs(slice8): flag registry + CLAUDE.md + memory topic for the declared-roots contract and gate hardening

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Whole-slice verification sweep

**Files:** none — verification only.

- [ ] **Step 1: Full touched-surface battery**

```bash
python3 -m pytest tests/governance/test_validate_sandbox_declared_roots.py tests/governance/test_repair_sandbox_node_policy.py tests/governance/test_validation_failure_logging.py tests/scripts/test_a1_flag_audit_corroboration.py tests/test_ouroboros_governance/test_multi_file_coverage_gate.py tests/governance/intent/test_source_attribution.py tests/governance/test_attribution_scope_gate.py tests/governance/intent/test_attribution_e2e_leaf_predicates.py tests/governance/test_flag_registry_seed_truth.py -q
```

Expected: all PASS.

- [ ] **Step 2: The original Run-18 repro, one more time, by hand**

```bash
JARVIS_SANDBOX_PREFIXES=/nonexistent-sandbox-prefix python3 - << 'EOF'
import asyncio, tempfile
from pathlib import Path
repo = Path.cwd()
rel = Path("backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py")
from backend.core.ouroboros.governance.test_runner import LanguageRouter, PythonAdapter
router = LanguageRouter(repo_root=repo, adapters={"python": PythonAdapter(repo_root=repo)})
with tempfile.TemporaryDirectory(prefix="ouroboros_validate_") as sb:
    f = Path(sb) / rel; f.parent.mkdir(parents=True); f.write_text((repo/rel).read_text())
    r = asyncio.run(router.run(changed_files=(f,), sandbox_dir=Path(sb), timeout_budget_s=120,
                               op_id="sweep", original_paths={f: repo/rel}))
    print("passed:", r.passed, "fc:", r.failure_class)
EOF
```

Expected: `passed: True fc: none` (was `BlockedPathError` before this slice).

- [ ] **Step 3: Report** — test counts, repro output, then hand off to the whole-branch review + Run #19 (operator-conducted from the main session, `--max-wall-seconds 5000`, headless).

---

## Verification for the arc (post-plan, main session)

Run #19 acceptance: re-fire the isomorphic A1 driver; watch `[Attribution]` → sig-op `[source, test]` → GENERATE passes coverage → **VALIDATE green (no `fc='security'`)** → APPLY on the source → `pass_rate=1.0` → AutoCommit → `proven=true` + `twelve_flag_audit_passed=true`.
