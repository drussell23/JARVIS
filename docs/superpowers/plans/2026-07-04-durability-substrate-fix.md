# Durability Substrate Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the durability chain (ledger rows, AutoCommit, transaction lifecycle) structurally correct under ANY workspace overlay — no literal paths, no permission-swallowing, no host detection — and make Phase-8b non-arrival a thrown exception instead of silence.

**Architecture:** (1) Extend the existing `workspace_resolver` contract with a pure, env-driven durable-path re-anchor (`resolve_durable_path`): when the harness declares an overlay pair (`JARVIS_DURABLE_REROOT_FROM` -> `JARVIS_TRINITY_ROOT`), any durable write targeting the overlay root is re-anchored onto the writable durable root by pure path algebra; with the pair unset (real node / normal local) it is the identity function. (2) Wire it at the single chokepoint every durable JSONL write already flows through (`cross_process_jsonl`'s 4 entry points) — zero per-caller edits. (3) Harness wires `JARVIS_AUTO_COMMIT_WORKSPACE` (already consumed by `AutoCommitter._effective_repo_root`) to the session worktree. (4) A `TransactionLifecycleError` sentinel in `slice4b_runner`: an op that records APPLIED but exits the runner without reaching Phase 8b raises loudly up the orchestrator tree. (5) Loop Deadman armed structurally in the ignition env composition.

**Tech Stack:** Python 3.9+ stdlib (`pathlib`, `os`), existing `workspace_resolver.py` / `cross_process_jsonl.py` / `slice4b_runner.py` / `isomorphic_a1_local.py`. pytest TDD.

## Root Cause (evidence, session bt-iso-1783130209)

- 36× `[CrossProcessJSONL] parent mkdir failed: [Errno 13] Permission denied: '/opt/trinity'` — durable writers derive paths from the iso-overlaid project root (`/opt/trinity/jarvis`), unwritable on the macOS host → ledger rows `written=False` (op-944c).
- The harness ALREADY exports `JARVIS_TRINITY_ROOT` → a real writable per-run dir; no writer consumes it (wired-but-inert #5). `workspace_resolver.py` exists precisely to kill literal-path divergence; the durable writers bypass it.
- op-944c recorded APPLIED, mutated disk (git-verified, +11/−8), passed VERIFY 21/21 — then the runner went silent before Phase 8b (AutoCommit). Every branch of Phase 8b logs; nothing logged → non-arrival, and nothing raised. Silence at a transactional boundary is itself the defect.

## Global Constraints

- Python 3.9+; `from __future__ import annotations` at top of every touched module; ASCII-only in added lines.
- NO host-detection conditionals (no `platform`/`sys.platform`/`os.uname` checks). NO catching `PermissionError`/`OSError` to paper over unwritable paths (the existing fail-soft in `flock_append_lines` stays as-is — we fix WHY it fires, we do not add more swallowing).
- NO literal paths anywhere: `JARVIS_TRINITY_ROOT`, `JARVIS_DURABLE_REROOT_FROM`, `JARVIS_AUTO_COMMIT_WORKSPACE` are read from `os.environ` AT CALL TIME. Identity behavior when unset (real node / plain local = byte-identical).
- DRY: the ONLY new path logic lives in `workspace_resolver.py`; `cross_process_jsonl` calls it at its entry points; no per-caller edits, no new directory-creator/scrubber utilities.
- `TransactionLifecycleError` must NOT convert `asyncio.CancelledError` (log CRITICAL, re-raise the cancellation); must not fire on legitimate non-APPLIED exits (verify_regression/FAILED paths return before the sentinel arms).
- Kill switches: `JARVIS_DURABLE_REROOT_ENABLED` (default `true`; off = identity even when the pair is set), `JARVIS_TXN_LIFECYCLE_GUARD_ENABLED` (default `true`; off = legacy silent behavior). Byte-identical legacy when off.

---

## Task 1: `resolve_durable_path` in `workspace_resolver.py`

**Files:**
- Modify: `backend/core/ouroboros/governance/workspace_resolver.py` (append; extend `__all__`)
- Test: `tests/governance/test_workspace_resolver_durable.py` (new)

**Interfaces:**
- Produces: `resolve_durable_path(path: Union[str, Path]) -> Path` — pure, deterministic, never raises. Rules, in order:
  1. `JARVIS_DURABLE_REROOT_ENABLED` false → return `Path(path)` unchanged.
  2. Read `JARVIS_DURABLE_REROOT_FROM` and `JARVIS_TRINITY_ROOT` at call time. Either unset/empty → identity.
  3. If `Path(path)` is relative to `FROM` (use `Path.resolve()`-free lexical containment — `os.path.commonpath` or `relative_to` in try/except `ValueError` ONLY, which is path algebra, not permission-swallowing) → return `TO / path.relative_to(FROM)`.
  4. Otherwise identity.
- Any internal exception → return the input unchanged (fail-soft to legacy; matches the module's existing "never raises" contract).

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_workspace_resolver_durable.py
from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance.workspace_resolver import resolve_durable_path


def test_identity_when_pair_unset(monkeypatch):
    monkeypatch.delenv("JARVIS_DURABLE_REROOT_FROM", raising=False)
    monkeypatch.delenv("JARVIS_TRINITY_ROOT", raising=False)
    p = Path("/opt/trinity/jarvis/.jarvis/ledger.jsonl")
    assert resolve_durable_path(p) == p  # real node / plain local: byte-identical


def test_reanchors_overlay_path_onto_durable_root(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DURABLE_REROOT_FROM", "/opt/trinity/jarvis")
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", str(tmp_path / "trinity_root"))
    out = resolve_durable_path(Path("/opt/trinity/jarvis/.jarvis/a1_lineage.jsonl"))
    assert out == tmp_path / "trinity_root" / ".jarvis" / "a1_lineage.jsonl"


def test_non_overlay_path_untouched(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DURABLE_REROOT_FROM", "/opt/trinity/jarvis")
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", str(tmp_path))
    p = Path.cwd() / ".jarvis" / "local.jsonl"
    assert resolve_durable_path(p) == p  # only overlay-rooted paths re-anchor


def test_kill_switch_reverts_to_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DURABLE_REROOT_ENABLED", "false")
    monkeypatch.setenv("JARVIS_DURABLE_REROOT_FROM", "/opt/trinity/jarvis")
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", str(tmp_path))
    p = Path("/opt/trinity/jarvis/.jarvis/x.jsonl")
    assert resolve_durable_path(p) == p


def test_never_raises_on_garbage(monkeypatch):
    monkeypatch.setenv("JARVIS_DURABLE_REROOT_FROM", "\x00bad")
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", "also-bad")
    out = resolve_durable_path("/opt/trinity/jarvis/.jarvis/x.jsonl")
    assert isinstance(out, Path)  # fail-soft identity, never raises
```

- [ ] **Step 2: Run — fails** (`python3 -m pytest tests/governance/test_workspace_resolver_durable.py -v` → ImportError)

- [ ] **Step 3: Implement** (append to `workspace_resolver.py`; add `resolve_durable_path` to `__all__`; reuse the module's existing style)

```python
_ENV_REROOT_ENABLED = "JARVIS_DURABLE_REROOT_ENABLED"
_ENV_REROOT_FROM = "JARVIS_DURABLE_REROOT_FROM"
_ENV_DURABLE_ROOT = "JARVIS_TRINITY_ROOT"


def resolve_durable_path(path):
    """Re-anchor a DURABLE-state write path from a declared workspace
    overlay root onto the harness-provided durable root.

    Pure path algebra, env-driven at call time, identity when the
    overlay pair is not declared (real node / plain local). The
    harness that OWNS the overlay declares both ends:

        JARVIS_DURABLE_REROOT_FROM=/opt/trinity/jarvis   (overlay root)
        JARVIS_TRINITY_ROOT=<writable per-run durable root>

    No host detection, no permission probing, no literal paths.
    NEVER raises -- any error returns the input unchanged."""
    try:
        p = Path(path)
        raw = os.environ.get(_ENV_REROOT_ENABLED, "true").strip().lower()
        if raw not in ("1", "true", "yes", "on"):
            return p
        src = os.environ.get(_ENV_REROOT_FROM, "").strip()
        dst = os.environ.get(_ENV_DURABLE_ROOT, "").strip()
        if not src or not dst:
            return p
        try:
            rel = p.relative_to(src)
        except ValueError:
            return p  # not under the overlay root -- untouched
        return Path(dst) / rel
    except Exception:  # noqa: BLE001 -- module contract: never raises
        try:
            return Path(path)
        except Exception:  # noqa: BLE001
            return Path(".")
```

- [ ] **Step 4: Run — 5 passed.**
- [ ] **Step 5: Commit** — `git add backend/core/ouroboros/governance/workspace_resolver.py tests/governance/test_workspace_resolver_durable.py && git commit -m "feat(workspace): resolve_durable_path -- env-declared overlay re-anchor for durable writes (pure path algebra)"`

---

## Task 2: Chokepoint wiring in `cross_process_jsonl.py`

**Files:**
- Modify: `backend/core/ouroboros/governance/cross_process_jsonl.py` — the 4 public entry points (`flock_append_line` ~353, `flock_critical_section` ~381, `flock_append_lines` ~425, `async_flock_critical_section` ~504)
- Test: `tests/governance/test_cross_process_jsonl_durable.py` (new)

**Interfaces:**
- Consumes: `resolve_durable_path` (Task 1).
- Change: at the TOP of each entry point, before any `Path(path)` use: `path = resolve_durable_path(path)` (lazy import inside the function to avoid import cycles, matching the codebase convention). `lock_path` derives from the RESOLVED target (it already derives from `target`), so lock and data stay co-located. NO other logic changes; the existing fail-soft mkdir stays exactly as-is.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_cross_process_jsonl_durable.py
from __future__ import annotations

import json
from pathlib import Path

from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
    flock_append_lines,
)


def test_append_reanchors_overlay_path(monkeypatch, tmp_path):
    """The bt-iso-1783130209 failure shape: a writer targets the overlay
    root; with the pair declared, the write LANDS in the durable root."""
    monkeypatch.setenv("JARVIS_DURABLE_REROOT_FROM", "/opt/trinity/jarvis")
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", str(tmp_path / "troot"))
    ok = flock_append_line(
        "/opt/trinity/jarvis/.jarvis/test_ledger.jsonl",
        json.dumps({"op": "x", "state": "applied"}),
    )
    assert ok is True  # written=True -- the 944c failure mode killed
    landed = tmp_path / "troot" / ".jarvis" / "test_ledger.jsonl"
    assert landed.exists()
    assert json.loads(landed.read_text().splitlines()[0])["state"] == "applied"
    # the flock lock co-locates with the resolved target
    assert not Path("/opt/trinity").exists()  # nothing touched the overlay root


def test_append_lines_reanchors_and_batches(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DURABLE_REROOT_FROM", "/opt/trinity/jarvis")
    monkeypatch.setenv("JARVIS_TRINITY_ROOT", str(tmp_path / "troot"))
    ok = flock_append_lines(
        "/opt/trinity/jarvis/.jarvis/batch.jsonl", ["{}", "{}"],
    )
    assert ok is True
    assert len((tmp_path / "troot" / ".jarvis" / "batch.jsonl").read_text().splitlines()) == 2


def test_identity_when_pair_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_DURABLE_REROOT_FROM", raising=False)
    monkeypatch.delenv("JARVIS_TRINITY_ROOT", raising=False)
    target = tmp_path / "plain.jsonl"
    assert flock_append_line(str(target), "{}") is True
    assert target.exists()  # legacy path byte-identical
```

- [ ] **Step 2: Run — first two tests fail** (write attempts `/opt/trinity` → `ok is False`).
- [ ] **Step 3: Implement** — in each of the 4 entry points, first statement of the `try` body:

```python
        from backend.core.ouroboros.governance.workspace_resolver import (
            resolve_durable_path,
        )
        path = resolve_durable_path(path)
```

(For `flock_critical_section`/`async_flock_critical_section` apply to their path/lock derivation input the same way — read each function first; the resolved path must feed BOTH the target and the lock derivation.)

- [ ] **Step 4: Run new tests + the module's existing suite** — `python3 -m pytest tests/governance/test_cross_process_jsonl_durable.py tests/governance/ -k "cross_process or flock" -q` → all pass.
- [ ] **Step 5: Commit** — `git commit -m "feat(durability): route all CrossProcessJSONL entry points through resolve_durable_path (kills written=False under overlay)"`

---

## Task 3: Harness declares the overlay pair + AutoCommit workspace

**Files:**
- Modify: `scripts/isomorphic_a1_local.py` (the env-composition block — find via `grep -n "JARVIS_TRINITY_ROOT" scripts/isomorphic_a1_local.py`, add the two new keys beside it)
- Test: `tests/battle_test/test_iso_env_durable_pair.py` (new; match the existing iso-env test file conventions — check `ls tests/battle_test/ | grep iso` first and extend the existing env-composition test module if one exists instead of creating a parallel one)

**Change:** where the driver composes the child env (it already sets `JARVIS_TRINITY_ROOT`), add:
- `JARVIS_DURABLE_REROOT_FROM=<the iso overlay root>` (the same value the driver passes as the isomorphic repo root, e.g. its `iso_root`/`/opt/trinity/jarvis` variable — use the VARIABLE, never a literal),
- `JARVIS_AUTO_COMMIT_WORKSPACE=<the session auto worktree path>` IF the driver/harness knows it at compose time; if the worktree is created later by the harness, set it in `battle_test/harness.py` at worktree creation instead (find via `grep -n "ouroboros__auto__" backend/core/ouroboros/battle_test/harness.py` — set `os.environ["JARVIS_AUTO_COMMIT_WORKSPACE"] = str(worktree_path)` right where the auto worktree is materialized; `AutoCommitter._effective_repo_root` already consumes and VALIDATES it, falling back safely — zero committer changes).

- [ ] **Step 1: failing test** — assert the composed env dict contains both keys with values derived from the driver's own variables (not literals). Write against the driver's env-compose function (read it first; it returned "147 keys" in the live run, so there is a compose function to unit-test).
- [ ] **Step 2-4: implement, run, commit** — `git commit -m "feat(iso): declare durable-reroot pair + auto-commit workspace in composed env (harness owns the overlay ends)"`

---

## Task 4: `TransactionLifecycleError` — Phase-8b non-arrival is LOUD

**Files:**
- Create: `backend/core/ouroboros/governance/transaction_lifecycle.py` (tiny: the exception + the env gate)
- Modify: `backend/core/ouroboros/governance/phase_runners/slice4b_runner.py` (sentinel around the APPLIED→Phase-8b span: arm after `_record_ledger(ctx, OperationState.APPLIED, ...)` at ~line 947; disarm at the first statement of Phase 8b at ~line 1310; enforce in a `finally`)
- Test: `tests/governance/test_transaction_lifecycle_guard.py` (new)

**Interfaces:**
- `class TransactionLifecycleError(RuntimeError)` with `op_id` and `boundary` attrs.
- `txn_lifecycle_guard_enabled() -> bool` — env `JARVIS_TXN_LIFECYCLE_GUARD_ENABLED`, default True.

**Runner change shape (adapt names to the actual code):**

```python
        # arm immediately after the APPLIED ledger row
        _txn_commit_stage_reached = False
        try:
            ... existing 8a scoped tests / benchmark / gates ...
            # first statement of Phase 8b:
            _txn_commit_stage_reached = True
            ... existing Phase 8b + rest ...
        finally:
            if not _txn_commit_stage_reached and txn_lifecycle_guard_enabled():
                logger.critical(
                    "[TxnLifecycle] op=%s recorded APPLIED but NEVER reached "
                    "the auto-commit stage -- transactional boundary breached",
                    ctx.op_id,
                )
                # CancelledError/exceptions already in flight propagate on
                # their own (finally does not swallow); ONLY raise when the
                # block is exiting WITHOUT an active exception:
                import sys as _sys
                if _sys.exc_info()[0] is None:
                    raise TransactionLifecycleError(ctx.op_id, "phase_8b_auto_commit")
```

Semantics the tests must pin: (a) normal path (reaches 8b) → no raise, no CRITICAL; (b) a silent early `return` between APPLIED and 8b → `TransactionLifecycleError` raised; (c) an exception in the span (incl. `asyncio.CancelledError`) → CRITICAL logged, ORIGINAL exception propagates (not converted); (d) legitimate pre-APPLIED failure exits (verify_regression path returns before the sentinel arms) → untouched; (e) kill switch off → legacy silence. NOTE: the verify_regression rollback path between APPLIED and 8b DOES return legitimately — the implementer must disarm the sentinel on that path too (`_txn_commit_stage_reached = True` before its `return`, or arm-scope placed after it): a rolled-back FAILED op is a CLOSED transaction, not a breach. Test (f) pins that.

- [ ] Steps: failing tests (unit-test the guard shape with a minimal async fn mimicking the spans; plus a source-level assertion that `slice4b_runner.py` contains the arm/disarm/finally trio) → implement → run `tests/governance/test_transaction_lifecycle_guard.py` + `python3 -m pytest tests/governance/ -k "slice4b or apply" -q` → commit `git commit -m "feat(txn): TransactionLifecycleError -- APPLIED ops that never reach auto-commit now raise loudly (kill silent boundary exits)"`

---

## Task 5: Arm the Loop Deadman structurally + full regression

**Files:**
- Modify: `scripts/isomorphic_a1_local.py` env composition (same block as Task 3): `JARVIS_LOOP_DEADMAN_TIMEOUT_S` (default "8") and `JARVIS_LOOP_DEADMAN_STACK_DUMP` (default "true") — `setdefault` semantics so an operator override wins (LESSON: `setdefault` LOST to the compose-env manifest once before, #69824 — verify the compose function's precedence and use whichever mechanism actually lands the key, with a test).
- Test: extend the Task-3 env test: both keys present in the composed env.
- Full sweep: `python3 -m pytest tests/governance/test_workspace_resolver_durable.py tests/governance/test_cross_process_jsonl_durable.py tests/governance/test_transaction_lifecycle_guard.py tests/battle_test/ -k "iso_env or durable or lifecycle" -q` plus `python3 -m pytest tests/governance/ -k "workspace or cross_process or slice4b" -q`.
- Ledger entry + commit.

---

## Self-Review

1. **Mandate coverage:** #1 root-cause (resolution layer only; no OS checks — the design has zero `platform` reads; no new permission catches — only the pre-existing fail-soft remains) → Tasks 1-2. #2 dynamic (all three env keys read at call time; identity when unset) → Tasks 1-3. #3 DRY (one resolver function; 4-line chokepoint wiring; zero per-caller edits; AutoCommitter untouched — its existing env seam is wired by the harness) → Tasks 1-3. #4 bulletproof (thrown `TransactionLifecycleError`, CancelledError-safe, rollback-path-safe; Deadman armed structurally) → Tasks 4-5.
2. **Placeholder scan:** Task 3/5 contain grep-first lookup steps with exact commands (compose-function location, worktree-creation site) — lookups, not placeholders; all code shapes given.
3. **Type consistency:** `resolve_durable_path(path) -> Path` consumed identically in Task 2; env key names identical across Tasks 1/3/5; `TransactionLifecycleError(op_id, boundary)` consistent in Task 4.
4. **Honest limits:** this fixes the durability WRITE class + makes the silent-exit class impossible to miss. The exact line where 944c's runner stopped remains undiagnosed — Task 4's guard + Task 5's Deadman convert any recurrence into a stack-trace-bearing, loudly-failing event on the next ignition, which is the correct instrument to close it.
