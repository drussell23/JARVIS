# Anti-Venom Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the adversarially-verified Anti-Venom holes so O+V cannot mutate disk outside a guardian-validated chokepoint, cannot edit its own immune system, and fails closed — making the autonomic self-developer safe to run E2E.

**Architecture:** Three structural locks anchored on the single mandatory APPLY chokepoint `ChangeEngine.execute`: (A) fail-closed guardian, (B) a universal `_pre_write_gate` every write funnels through + a thin `sandbox_exec` wrapper that runs `bash`/`run_tests` inside the existing ephemeral Trinity Docker container, (C) a hardcoded no-env-off `_IMMUTABLE_GOVERNANCE_SENTINELS` frozenset that write-closes `governance/`. Plus the unshielded-cancel, dead-stale-guard, and WAL-lease-leak fixes. Sequenced leaf-modules → Venom surface → chokepoint → orchestrator → phase-runner.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`), asyncio, the existing `container_sandbox` (Docker), `SemanticGuardian`, `state_drift`, pytest. Spec: `docs/superpowers/specs/2026-06-26-anti-venom-hardening-verified.md`.

## Global Constraints

- **Python 3.9+**; `from __future__ import annotations`; `asyncio.wait_for` not `asyncio.timeout`.
- **No hardcoding** EXCEPT the security frozenset, which is *intentionally* hardcoded with **no env off-switch** (that immovability is the security property, mirroring Immutable-Orange).
- **Fail-closed everywhere** — a guard that errors must escalate (`APPROVAL_REQUIRED` / `BlockedPathError`), never silently pass. No `except: logger.debug(...skipped...)` on any immune path.
- **Zero duplication** — reuse `SemanticGuardian`, `state_drift.should_block_apply`, `container_sandbox.run_in_container`/`run_pytest_in_container`, `_is_protected_path`, `should_block_apply`. Do NOT build a new sandbox, guardian, or path checker.
- **Cross-platform** — sandbox is the Trinity Docker layer ONLY (works arm64 local + amd64 GCP); no firejail/seccomp-custom wrappers. If Docker/sandbox is unavailable → **fail closed (deny bash/run_tests)**, never run unsandboxed.
- **Canonicalization** — every path check resolves `os.path.realpath(os.path.abspath(target))` before comparison (defeat `../` + symlinks); containment compared against `os.path.realpath(project_root)`.
- **Sentinel lock-in** — `_IMMUTABLE_GOVERNANCE_SENTINELS` MUST include `change_engine` (the enforcer) and `sandbox_exec` (the isolation enforcer), self-protecting; grep-pinned by a regression test.
- **Brain-stem changes are human-gated** — the Phase-2 chokepoint (`change_engine.py`) and Phase-3 (`orchestrator.py`) PRs get line-by-line human review before merge. Default-OFF is NOT used here (these are safety fixes that must be ON), so each change must preserve all legitimate apply behavior — proven by an apply-path integration test.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `governance/semantic_guardian.py` | per-pattern fail-closed; shell-exec detection for non-Python | Modify |
| `governance/risk_engine.py` | defense-in-depth governance sentinels (advisory) | Modify |
| `governance/intake/unified_intake_router.py` | ack absorbed coalesce leases (C3) | Modify |
| `governance/sandbox_exec.py` | thin wrapper: run bash/pytest in ephemeral Trinity Docker, fail-closed | Create |
| `governance/tool_executor.py` | protected-path sentinels; bash allowlist+sandbox; run_tests sandbox+mutation; apply_patch removal | Modify |
| `governance/scoped_tool_access.py` / `governance/semantic_firewall.py` | paired: run_tests→mutation set; apply_patch removal | Modify |
| `governance/change_engine.py` | `BlockedPathError` + `_IMMUTABLE_GOVERNANCE_SENTINELS` + `_pre_write_gate` (THE chokepoint) | Modify |
| `governance/orchestrator.py` | fail-closed guardian (A); noop+baseline guards (B4); `asyncio.shield` (C2) | Modify |
| `governance/phase_runners/slice4b_runner.py` | wire `should_block_apply` (C1) | Modify |
| `tests/governance/...` | per-task tests + the frozenset grep-pin + apply-path integration | Create |

---

## Phase 0 — Leaf modules (no cross-file deps)

### Task 1: SemanticGuardian — per-pattern fail-closed + non-Python shell-exec detection

**Files:** Modify `governance/semantic_guardian.py`; Test `tests/governance/test_guardian_failclosed.py`

**Interfaces:** Produces a `Detection(pattern="<name>_eval_failed", severity="hard")` on any per-pattern crash; a new regex Pattern `shell_exec_introduced` registered in `_ALL_PATTERNS`; extended `_DYNAMIC_ATTR_CALLS` with `(os,system)`/`(subprocess,run)` etc.

- [ ] **Step 1: Failing test**
```python
# tests/governance/test_guardian_failclosed.py
from __future__ import annotations
import backend.core.ouroboros.governance.semantic_guardian as sg

def test_pattern_eval_crash_yields_hard_finding(monkeypatch):
    g = sg.SemanticGuardian()
    # force one detector to raise
    import backend.core.ouroboros.governance.semantic_guardian as m
    orig = m._ALL_PATTERNS
    def boom(*a, **k): raise RuntimeError("detector boom")
    # wrap the first pattern callable to raise
    dets = g.inspect("x.py", old="", new="x=1\n", _force_pattern_error=boom) if False else None
    # Instead: monkeypatch a pattern to raise via the registry
    findings = g.inspect("x.py", old="a=1\n", new="a=2\n")
    assert isinstance(findings, list)

def test_shell_exec_in_yaml_is_detected():
    g = sg.SemanticGuardian()
    f = g.inspect("deploy.sh", old="", new="os.system('rm -rf /')\n")
    assert any("shell_exec" in d.pattern for d in f)
```
> NOTE: the crash-injection test must drive a REAL per-pattern exception. Inspect `semantic_guardian.py:127-138` for the per-pattern loop; the cleanest injection is monkeypatching one entry of `_ALL_PATTERNS`/`_PATTERNS` to a callable that raises, then asserting `inspect()` returns a `*_eval_failed` hard finding. Adjust the test to the real registry structure you find.

- [ ] **Step 2: Run → fail** `python3 -m pytest tests/governance/test_guardian_failclosed.py -q`
- [ ] **Step 3: Per-pattern fail-closed** — at `semantic_guardian.py:127-138`, replace the swallowing `except` with:
```python
            except Exception:
                detections.append(Detection(
                    pattern=f"{pat_name}_eval_failed", severity="hard",
                    message="pattern evaluator raised — failing closed",
                    file_path=file_path, lines=(), snippet="",
                ))
                continue
```
- [ ] **Step 4: Shell-exec detection (S6)** — extend `_DYNAMIC_ATTR_CALLS` (~:886) with `("os","system")`, `("os","popen")`, `("subprocess","run")`, `("subprocess","call")`, `("subprocess","Popen")`, `("subprocess","check_output")`, `("subprocess","check_call")`; add a regex pattern `shell_exec_introduced` (delta-gated, severity hard) to `_ALL_PATTERNS`/`_PATTERNS` matching `os.system|subprocess.(run|call|Popen|check_output|check_call)|os.popen` so it fires on non-Python files where `_safe_parse` returns None.
- [ ] **Step 5: Run → pass**; also `python3 -m pytest tests/governance/ -k semantic_guard -q` (no regressions)
- [ ] **Step 6: Commit** (add ONLY `semantic_guardian.py` + the test by name)
```bash
git commit -m "feat(anti-venom): SemanticGuardian per-pattern fail-closed + non-Python shell-exec detection (S4/S6)"
```

### Task 2: RiskEngine — defense-in-depth governance sentinels

**Files:** Modify `governance/risk_engine.py`; Test `tests/governance/test_risk_engine_govsentinels.py`

- [ ] **Step 1: Failing test** — exploration-source op targeting `semantic_guardian.py` classifies BLOCK/APPROVAL.
```python
def test_exploration_self_mod_governance_blocked():
    import backend.core.ouroboros.governance.risk_engine as re
    # construct the minimal profile/source the classify path needs (read classify signature first)
    # assert classify(... source="exploration", file="backend/core/ouroboros/governance/semantic_guardian.py" ...) -> blocked/approval
```
> NOTE: read `risk_engine.py:213` `_EXPLORATION_SELF_MOD_SENTINELS` + `classify`/`_build_profile` to construct the exact call; assert the BLOCK tier.

- [ ] **Step 2: Run → fail**
- [ ] **Step 3:** add to `_EXPLORATION_SELF_MOD_SENTINELS` (~:213): `"ouroboros/governance/semantic_guardian"`, `"ouroboros/governance/tool_executor"`, `"ouroboros/governance/semantic_firewall"`, `"ouroboros/governance/scoped_tool_access"`, `"ouroboros/governance/risk_tier_floor"`, `"ouroboros/governance/change_engine"`, `"ouroboros/governance/sandbox_exec"`, `"ouroboros/governance/intake/unified_intake_router"`. (Advisory depth — the authority is the Task-6 structural check.)
- [ ] **Step 4: Run → pass**
- [ ] **Step 5: Commit** (`risk_engine.py` + test, by name)
```bash
git commit -m "feat(anti-venom): risk_engine governance self-mod sentinels (defense-in-depth, C)"
```

### Task 3: Intake — ack absorbed coalesce leases (C3)

**Files:** Modify `governance/intake/unified_intake_router.py`; Test `tests/governance/test_coalesce_lease_ack.py`

- [ ] **Step 1: Failing test** — 3 coalesced envelopes → after flush, the 2 absorbed leases are `update_status(...,"acked")` (assert via a fake/inspectable WAL).
- [ ] **Step 2: Run → fail**
- [ ] **Step 3:** in `_flush_coalesced` (~:1411), after `base = envelopes[0]`, before the merged `return`:
```python
        for _absorbed in envelopes[1:]:
            try:
                self._wal.update_status(_absorbed.lease_id, "acked")
            except Exception:  # noqa: BLE001 — ack is best-effort; never block the flush
                logger.warning("[Intake] absorbed-lease ack failed lease=%s",
                               getattr(_absorbed, "lease_id", "?"))
```
- [ ] **Step 4: Run → pass**
- [ ] **Step 5: Commit** (`unified_intake_router.py` + test, by name)
```bash
git commit -m "fix(anti-venom): ack absorbed coalesce leases — no duplicate replay on restart (C3)"
```

---

## Phase 1 — Venom surface

### Task 4: `sandbox_exec.py` — bash/pytest in ephemeral Trinity Docker (fail-closed)

**Files:** Create `governance/sandbox_exec.py`; Test `tests/governance/test_sandbox_exec.py`

**Interfaces:** Produces `async sandbox_run_bash(command: str, *, worktree: str, docker_run=None) -> SandboxResult` and `async sandbox_run_tests(test_targets: list[str], *, worktree: str, docker_run=None) -> SandboxResult`; `SandboxResult(ok: bool, stdout: str, stderr: str, returncode: Optional[int], denied: bool, reason: str)`. `denied=True` when the sandbox is unavailable/disabled (fail-closed).

- [ ] **Step 1: Failing test** (inject a fake docker runner; assert ephemeral container path + fail-closed-when-disabled)
```python
# tests/governance/test_sandbox_exec.py
from __future__ import annotations
import asyncio
import backend.core.ouroboros.governance.sandbox_exec as sx

def test_bash_runs_in_container_via_injected_runner(monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "true")
    async def fake_docker(argv, timeout): 
        assert "--network" in argv and "none" in argv  # air-gap
        return (0, "ok", "")
    r = asyncio.run(sx.sandbox_run_bash("ls", worktree="/tmp/wt", docker_run=fake_docker))
    assert r.ok and not r.denied

def test_fail_closed_when_sandbox_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "false")
    r = asyncio.run(sx.sandbox_run_bash("ls", worktree="/tmp/wt"))
    assert r.denied and not r.ok      # NEVER runs unsandboxed
```

- [ ] **Step 2: Run → fail**
- [ ] **Step 3: Create the wrapper** (thin over `container_sandbox`; the container is already `--network none --cap-drop ALL --read-only --tmpfs --rm` ephemeral / auto-imploding):
```python
# backend/core/ouroboros/governance/sandbox_exec.py
"""Ephemeral Trinity-Docker execution for the two RCE vectors (bash, run_tests).
Thin wrapper over container_sandbox (already --network none / --read-only / --rm).
Fail-closed: if the sandbox is unavailable/disabled, DENY — never run unsandboxed.
Cross-platform (arm64 local + amd64 GCP) via the native container_sandbox image."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: Optional[int]
    denied: bool
    reason: str


async def sandbox_run_bash(command: str, *, worktree: str,
                           docker_run: Any = None) -> SandboxResult:
    from backend.core.ouroboros.governance.container_sandbox import (
        run_in_container, ContainmentBreach,
    )
    res = await run_in_container(command, worktree=worktree, docker_run=docker_run)
    if res.breach in (ContainmentBreach.DISABLED, ContainmentBreach.SPAWN_FAILED):
        logger.warning("[SandboxExec] bash DENIED — sandbox unavailable (%s); fail-closed",
                       res.breach)
        return SandboxResult(False, "", res.diagnostic, None, True,
                             f"sandbox_unavailable:{res.breach}")
    return SandboxResult(bool(res.ok), res.stdout, res.stderr, res.returncode,
                         False, "")


async def sandbox_run_tests(test_targets: List[str], *, worktree: str,
                            docker_run: Any = None) -> SandboxResult:
    from backend.core.ouroboros.governance.container_sandbox import (
        run_pytest_in_container, ContainmentBreach,
    )
    res = await run_pytest_in_container(test_targets, worktree=worktree,
                                        docker_run=docker_run)
    if getattr(res, "breach", None) in (ContainmentBreach.DISABLED,
                                        ContainmentBreach.SPAWN_FAILED):
        logger.warning("[SandboxExec] run_tests DENIED — sandbox unavailable; fail-closed")
        return SandboxResult(False, "", getattr(res, "diagnostic", ""), None, True,
                             "sandbox_unavailable")
    return SandboxResult(bool(getattr(res, "ok", False)), "",
                         getattr(res, "diagnostic", ""),
                         getattr(res, "returncode", None), False, "")
```
> NOTE: confirm `run_pytest_in_container`'s exact signature/return (read `container_sandbox.py:367`); adapt the field extraction (it returns `PytestContainerResult`). Keep the fail-closed contract identical.

- [ ] **Step 4: Run → pass**
- [ ] **Step 5: Commit** (`sandbox_exec.py` + test, by name)
```bash
git commit -m "feat(anti-venom): sandbox_exec — ephemeral Trinity-Docker bash/pytest, fail-closed (B)"
```

### Task 5: tool_executor — protected sentinels, bash allowlist+sandbox, run_tests sandbox, apply_patch removal

**Files:** Modify `governance/tool_executor.py`, `governance/scoped_tool_access.py`, `governance/semantic_firewall.py`; Test `tests/governance/test_tool_executor_lockdown.py`

- [ ] **Step 1: Failing tests** — (a) `edit_file`/`write_file` targeting `semantic_guardian.py` → blocked by protected-path; (b) bash `find . -delete` / `rm  -rf /` (double-space) / redirect to `.git/` → denied; (c) `run_tests` denied for read-only `reviewer` scope; (d) bash + run_tests route through `sandbox_exec` (inject a marker).
- [ ] **Step 2: Run → fail**
- [ ] **Step 3a: Protected-path sentinels** — append to `_PROTECTED_PATH_SUBSTRINGS` (~:499): `"ouroboros/governance/semantic_guardian"`, `"ouroboros/governance/tool_executor"`, `"ouroboros/governance/change_engine"`, `"ouroboros/governance/sandbox_exec"`, `"ouroboros/governance/risk_engine"`, `"ouroboros/governance/risk_tier_floor"`, `"ouroboros/governance/semantic_firewall"`, `"ouroboros/governance/scoped_tool_access"`, `"ouroboros/governance/intake/unified_intake_router"`.
- [ ] **Step 3b: bash → allowlist + sandbox** — in `_bash` (~:2274) replace the substring-denylist-only gate: (i) first token (after env-assignment strip) must be in a new `_BASH_ALLOWED_VERBS` frozenset (`ls cat grep rg find git wc head tail sed awk echo python pytest`); (ii) any `>`/`>>` redirect target must pass `_is_protected_path` (reject if protected) AND realpath-containment; (iii) route execution through `await sandbox_run_bash(command, worktree=self._repo_root)` — deny on `result.denied`. Keep `_blocked_patterns` extended (`-delete`, `truncate -s`, `dd of=`, `shred`) as defense-in-depth only.
- [ ] **Step 3c: run_tests → sandbox + mutation class** — `_run_tests` (~:2003) routes through `await sandbox_run_tests(targets, worktree=self._repo_root)`; add `"run_tests"` to `_MUTATION_TOOLS` in `scoped_tool_access.py` (~:31-36) so read-only/`reviewer` scopes DENY it.
- [ ] **Step 3d: apply_patch removal** — remove `apply_patch` from `semantic_firewall.py:99` + `scoped_tool_access.py:35` known/mutating sets (it has no handler; re-add only when routed through `ChangeEngine.execute`).
- [ ] **Step 4: Run → pass**; `python3 -m pytest tests/governance/ -k "tool_executor or scoped or firewall" -q`
- [ ] **Step 5: Commit** (the 3 files + test, by name)
```bash
git commit -m "feat(anti-venom): tool_executor lockdown — protected sentinels, bash allowlist+sandbox, run_tests sandbox, apply_patch removal (S1/S3/F2/F3/F4)"
```

---

## Phase 2 — THE CHOKEPOINT (highest risk — isolated human review before merge)

### Task 6: `ChangeEngine._pre_write_gate` — universal taint + immutable governance

**Files:** Modify `governance/change_engine.py`; Test `tests/governance/test_change_engine_chokepoint.py`

**Interfaces:** Produces `class BlockedPathError(Exception)`; module `_IMMUTABLE_GOVERNANCE_SENTINELS: frozenset[str]`; `ChangeEngine._pre_write_gate(self, target: Path, content: str, request: ChangeRequest) -> None` (raises `BlockedPathError` on any violation), called immediately before the `write_text` at `:692` and in the multi-file path.

- [ ] **Step 1: Failing tests**
```python
# tests/governance/test_change_engine_chokepoint.py
import pytest, asyncio
from backend.core.ouroboros.governance.change_engine import (
    ChangeEngine, ChangeRequest, BlockedPathError, _IMMUTABLE_GOVERNANCE_SENTINELS,
)

def test_sentinels_include_self_enforcers():
    # the immune system protects the files that enforce it
    assert any("change_engine" in s for s in _IMMUTABLE_GOVERNANCE_SENTINELS)
    assert any("sandbox_exec" in s for s in _IMMUTABLE_GOVERNANCE_SENTINELS)
    assert any("semantic_guardian" in s for s in _IMMUTABLE_GOVERNANCE_SENTINELS)

def test_apply_to_git_hooks_blocked(tmp_path):
    eng = ChangeEngine(project_root=tmp_path)
    req = ChangeRequest(target_file=".git/hooks/pre-commit", full_content="#!/bin/sh\n", op_id="op-x")
    res = asyncio.run(eng.execute(req))
    assert res.success is False   # containment/protected-path blocked

def test_apply_to_governance_blocked(tmp_path):
    eng = ChangeEngine(project_root=tmp_path)
    req = ChangeRequest(target_file="backend/core/ouroboros/governance/semantic_guardian.py",
                        full_content="x=1\n", op_id="op-y")
    res = asyncio.run(eng.execute(req))
    assert res.success is False

def test_traversal_escape_blocked(tmp_path):
    eng = ChangeEngine(project_root=tmp_path)
    req = ChangeRequest(target_file="../../etc/x", full_content="x", op_id="op-z")
    res = asyncio.run(eng.execute(req))
    assert res.success is False

def test_legitimate_body_edit_still_applies(tmp_path):
    (tmp_path/"src").mkdir()
    eng = ChangeEngine(project_root=tmp_path)
    req = ChangeRequest(target_file="src/app.py", full_content="x = 1\n", op_id="op-ok")
    res = asyncio.run(eng.execute(req))
    assert res.success is True and (tmp_path/"src/app.py").read_text() == "x = 1\n"
```
> NOTE: confirm `ChangeRequest`'s real field names (read `change_engine.py:304`) + `ChangeEngine.__init__` (project_root arg) + `ChangeResult.success`; adapt the test constructors to the real API.

- [ ] **Step 2: Run → fail**
- [ ] **Step 3: Define error + frozenset** (module scope, NO env read, grep-pinned):
```python
class BlockedPathError(Exception):
    """A write target failed containment / protected-path / immutable-governance."""

# Hardcoded, NO env off-switch (the immovability IS the security property — mirrors
# Immutable-Orange). Self-protecting: change_engine + sandbox_exec are themselves listed.
_IMMUTABLE_GOVERNANCE_SENTINELS = frozenset({
    "backend/core/ouroboros/governance/semantic_guardian",
    "backend/core/ouroboros/governance/tool_executor",
    "backend/core/ouroboros/governance/risk_engine",
    "backend/core/ouroboros/governance/orchestrator",
    "backend/core/ouroboros/governance/change_engine",      # the enforcer
    "backend/core/ouroboros/governance/sandbox_exec",       # the isolation enforcer
    "backend/core/ouroboros/governance/risk_tier_floor",
    "backend/core/ouroboros/governance/governed_loop_service",
    "backend/core/ouroboros/governance/semantic_firewall",
    "backend/core/ouroboros/governance/scoped_tool_access",
    "backend/core/ouroboros/governance/intake/unified_intake_router",
})
```
- [ ] **Step 4: Implement `_pre_write_gate`** (canonicalize → contain → protected → immutable → guardian → parser-fail-closed; fail-closed on any internal error):
```python
    def _pre_write_gate(self, target, content: str, request) -> None:
        import os
        from pathlib import Path
        root = os.path.realpath(os.path.abspath(str(self._effective_write_root(
            getattr(request, "write_root", None)))))
        # 1. canonical true destination (defeats ../ + symlinks)
        real = os.path.realpath(os.path.abspath(str(target)))
        # 2. containment
        if not (real == root or real.startswith(root + os.sep)):
            raise BlockedPathError(f"path escapes write_root: {real}")
        rel_norm = os.path.relpath(real, root).replace("\\", "/")
        # 3. immutable governance (FIRST authority; no env off-switch)
        if any(s.split("backend/core/ouroboros/")[-1] in ("governance/"+rel_norm)
               or s in real for s in _IMMUTABLE_GOVERNANCE_SENTINELS):
            raise BlockedPathError(f"immutable governance write blocked: {rel_norm}")
        # 4. protected-path (reuse the Venom registry)
        try:
            from backend.core.ouroboros.governance.tool_executor import _is_protected_path
            prot = _is_protected_path(rel_norm)
        except Exception:
            prot = "protected-path-check-failed"   # fail closed
        if prot:
            raise BlockedPathError(f"protected path write blocked: {rel_norm} ({prot})")
        # 5. guardian (+ parser-fail-closed for .py/.pth)
        try:
            from backend.core.ouroboros.governance.semantic_guardian import SemanticGuardian
            findings = SemanticGuardian().inspect(rel_norm, old="", new=content)
            if any(getattr(f, "severity", "") == "hard" for f in findings):
                raise BlockedPathError(f"guardian hard finding on {rel_norm}")
        except BlockedPathError:
            raise
        except Exception:
            raise BlockedPathError(f"guardian raised on {rel_norm} — fail-closed")
```
> NOTE: the sentinel match must be robust — normalize both sides to a repo-relative POSIX path and test substring containment of each sentinel's `governance/...` tail in the candidate's `governance/`-prefixed relpath. Simplify the Step-4 match to: `cand = "backend/core/ouroboros/" + ("governance/"+rel_norm if not rel_norm.startswith("backend/") else rel_norm)` then `any(s in cand for s in _IMMUTABLE_GOVERNANCE_SENTINELS)`. Implementer: verify against the real `_effective_write_root` semantics and make the 4 block-tests + the 1 allow-test pass; the exact string algebra is yours to get right, the REQUIREMENT is: governance/, .git/hooks, traversal blocked; legit src/ allowed.

- [ ] **Step 5: Wire the gate** — call `self._pre_write_gate(target, signed_content, request)` immediately before `target.write_text(...)` at `:692`; the existing `try:` at `:477` records FAILED on `BlockedPathError`. Also call it in the multi-file per-file path. Any internal error in the gate → `BlockedPathError` (never fall through to `write_text`).
- [ ] **Step 6: Run → pass** (5 tests); `python3 -m pytest tests/governance/ -k change_engine -q`
- [ ] **Step 7: Commit** (`change_engine.py` + test, by name)
```bash
git commit -m "feat(anti-venom): ChangeEngine._pre_write_gate — universal taint chokepoint + immutable governance (B/C/F1/F5)"
```

---

## Phase 3 — Orchestrator (composes everything)

### Task 7: Orchestrator — fail-closed guardian, noop/baseline guards, asyncio.shield

**Files:** Modify `governance/orchestrator.py`; Test `tests/governance/test_orchestrator_failclosed_cancel.py`

- [ ] **Step 1: Failing tests** — (a) guardian raises → `risk_tier == APPROVAL_REQUIRED` + non-empty findings; (b) `is_noop` + non-empty `venom_edit_history` → op CANCELLED with `terminal_reason_code="noop_inloop_write_guard"`; (c) the apply call is `asyncio.shield`-wrapped (assert the coroutine completes the write+ledger even when the outer task is cancelled).
- [ ] **Step 2: Run → fail**
- [ ] **Step 3a: Fail-closed guardian** — replace the `except Exception: logger.debug(...skipped...)` at `:8792-8796`:
```python
        except Exception:
            logger.warning("[Orchestrator] SemanticGuardian raised — FAILING CLOSED; "
                           "APPROVAL_REQUIRED op=%s", ctx.op_id, exc_info=True)
            risk_tier = RiskTier.APPROVAL_REQUIRED
            _guardian_findings = [_SENTINEL_GUARDIAN_CRASH]
```
Define module-level `_SENTINEL_GUARDIAN_CRASH = Detection(pattern="guardian_crashed", severity="hard", message="guardian crashed — fail-closed", file_path="", lines=(), snippet="")`.
- [ ] **Step 3b: noop in-loop guard** — at `:7472` `if generation.is_noop:`, before the existing handling:
```python
            _inloop = getattr(generation, "venom_edit_history", ()) or ()
            if _inloop:
                logger.warning("[Orchestrator] op=%s noop+%d in-loop writes never passed "
                               "guardian — cancelling", ctx.op_id, len(_inloop))
                ctx = ctx.advance(OperationPhase.CANCELLED,
                                  terminal_reason_code="noop_inloop_write_guard")
                await self._record_ledger(ctx, OperationState.FAILED,
                                          {"reason": "noop_inloop_write_guard"})
                return ctx
```
- [ ] **Step 3c: guardian git-HEAD baseline** — at `:8724-8738`, when `_path` ∈ `venom_edit_history` paths, set `_old` from `git show HEAD:<path>` (subprocess) so the guardian compares original→candidate, not post-write→candidate.
- [ ] **Step 3d: asyncio.shield** — wrap the apply call sites: `:9833` → `await asyncio.shield(self._stack.change_engine.execute(change_request))`; `:9804` → `await asyncio.shield(self._apply_multi_file_candidate(...))`. Fix the stale "default false" comment at `:9288`.
- [ ] **Step 4: Run → pass**; `python3 -m pytest tests/governance/ -k "orchestrator and (guard or cancel or noop)" -q`
- [ ] **Step 5: Commit** (`orchestrator.py` + test, by name)
```bash
git commit -m "feat(anti-venom): orchestrator fail-closed guardian + noop/baseline guards + shielded apply (S4/S2/C2)"
```

---

## Phase 4 — Phase runner

### Task 8: Slice4bRunner — wire `should_block_apply` (C1)

**Files:** Modify `governance/phase_runners/slice4b_runner.py`; Test `tests/governance/test_slice4b_stale_block.py`

- [ ] **Step 1: Failing test** — a candidate whose target file changed since GENERATE → APPLY aborts to POSTMORTEM (not silent overwrite).
- [ ] **Step 2: Run → fail**
- [ ] **Step 3:** replace the log-only hash loop at `:474-495` with the `should_block_apply` block-and-POSTMORTEM body (mirror the inline pattern at `orchestrator.py:9647-9693`); `JARVIS_STATE_DRIFT_VERIFY_ENABLED=false` degrades to log-only.
- [ ] **Step 4: Run → pass**
- [ ] **Step 5: Commit** (`slice4b_runner.py` + test, by name)
```bash
git commit -m "fix(anti-venom): wire should_block_apply into Slice4bRunner — refuse stale apply (C1)"
```

---

## Phase 5 — Regression, frozenset grep-pin, env

### Task 9: Full sweep + immutability grep-pin

**Files:** Test `tests/governance/test_immutable_governance_pin.py`

- [ ] **Step 1: Grep-pin test** — assert `_IMMUTABLE_GOVERNANCE_SENTINELS` contains every immune file (semantic_guardian, tool_executor, change_engine, sandbox_exec, risk_engine, risk_tier_floor, semantic_firewall, scoped_tool_access, orchestrator, governed_loop_service, unified_intake_router) and that there is NO env read in its definition (read the source, assert no `os.environ`/`getenv` on the frozenset lines).
- [ ] **Step 2: Full feature suite** — all Anti-Venom tests green.
- [ ] **Step 3: Regression** — `python3 -m pytest tests/governance/ -k "guard or change_engine or tool_executor or risk or sandbox or slice4b or coalesce or immutable" -q`; confirm no NEW failures (pre-existing collection errors OK).
- [ ] **Step 4: Commit**
```bash
git commit -m "test(anti-venom): immutable-governance frozenset grep-pin + full hardening regression"
```

---

## Self-Review

- **Spec coverage:** S1→T5; S2→T7(noop/baseline); S3→T5(protected)+T6(immutable); S4→T1(per-pattern)+T7(fail-closed); S5→none (refuted); S6→T1; C1→T8; C2→T7; C3→T3; F1→T6; F2→T4/T5; F3→T5; F4→T5; F5→T6. Three locks: A→T1+T7; B→T4+T5+T6; C→T2+T5+T6+T9. The 3 new constraints: Ephemeral Sandbox Matrix→T4 (container_sandbox `--network none`/`--rm`); Canonicalization→T6 `_pre_write_gate`; Sentinel Lock-In→T6 frozenset (change_engine+sandbox_exec) + T9 grep-pin. Covered.
- **Placeholder scan:** the NOTEs are recon-confirm-with-fallback points (test constructor signatures, the sentinel string algebra) — each names the file:line to confirm + the hard REQUIREMENT the test must prove; not silent placeholders. Core novel code (sandbox_exec, _pre_write_gate, fail-closed) is complete.
- **Type/name consistency:** `BlockedPathError`, `_IMMUTABLE_GOVERNANCE_SENTINELS`, `_pre_write_gate`, `sandbox_run_bash/sandbox_run_tests`/`SandboxResult`, `_SENTINEL_GUARDIAN_CRASH` — consistent across tasks.
- **Sequencing:** leaf (T1-3) → Venom surface (T4-5) → chokepoint (T6, highest-risk, isolated review) → orchestrator (T7) → phase-runner (T8) → pin/regression (T9). Each independently testable; the chokepoint composes on validated leaves.
- **Highest-risk:** T6 `change_engine._pre_write_gate` — universal gate; fail-closed-on-internal-error + the legit-apply integration test + the grep-pin guard it; **human line-by-line review before merge.**
