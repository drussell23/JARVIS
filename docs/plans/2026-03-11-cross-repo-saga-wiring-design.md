# Cross-Repo Saga Wiring — B+ Production Hardening Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Harden the existing cross-repo saga infrastructure into a production-safe autonomous pipeline with branch isolation, deterministic locks, base-SHA pinning, and ff-only promotion.

**Architecture:** Hub-and-spoke. JARVIS (hub) initiates all operations via GLS/Orchestrator (single-writer authority). Prime and Reactor-core (spokes) are patch targets only. This is a hardening/wiring pass over an existing ~85% complete saga path — not greenfield architecture.

**Tech Stack:** Python 3.12+, asyncio, git subprocess, fcntl file locks, pytest (e2e with real J-Prime generation)

---

## 1. Architecture Overview

### Authority Chain (unchanged)

```
IntakeLayerService → UnifiedIntakeRouter → GovernedLoopService.submit()
    → GovernedOrchestrator → SagaApplyStrategy
```

### What Exists (production-ready, no changes)

| Component | File | Status |
|-----------|------|--------|
| OperationContext multi-repo fields | `op_context.py` | Complete — `repo_scope`, `dependency_edges`, `apply_plan`, `repo_snapshots`, `saga_id` |
| Schema 2c.1 parser | `providers.py:_parse_multi_repo_response()` | Complete — builds `Dict[str, RepoPatch]` from J-Prime JSON |
| PrimeProvider generation | `providers.py:PrimeProvider.generate()` | Complete — connects to J-Prime, returns `GenerationResult` |
| Codegen prompt builder | `providers.py:_build_codegen_prompt()` | Complete — multi-repo aware (`repo_roots` param) |
| CrossRepoVerifier | `saga/cross_repo_verifier.py` | Complete — 3-tier: lint, contracts, integration |
| Orchestrator saga lifecycle | `orchestrator.py:_execute_saga_apply()` | Complete — verify/compensate/stuck handling |
| RepoRegistry | `multi_repo/registry.py` | Complete — jarvis/prime/reactor via env vars |
| IntakeLayerService fan-out | `intake/intake_layer_service.py` | Complete — one sensor set per registered repo |
| Topological sort | `saga/saga_apply_strategy.py:_topological_sort()` | Complete — Kahn's algorithm |
| DAG cycle detection | `op_context.py:_validate_dag()` | Complete — at OperationContext construction |

### 6 Gaps to Wire

| # | Gap | Where | Complexity |
|---|-----|-------|------------|
| 1 | B+ branch management | `saga_apply_strategy.py` | Medium — ephemeral branch create/promote/cleanup |
| 2 | Clean-tree precheck | `saga_apply_strategy.py:_phase_a_preflight()` | Small — `git status --porcelain=v1` per repo |
| 3 | Per-repo operation locks | New `saga/repo_lock.py` replacing stub | Medium — asyncio + fcntl, sorted acquisition |
| 4 | Git commit on ephemeral branch | `saga_apply_strategy.py:_apply_patch()` | Small — `git commit` after `git add` |
| 5 | Enhanced ledger artifact | `saga_apply_strategy.py:_emit_sub_event()` | Small — structured `SagaLedgerArtifact` |
| 6 | Promote gate + base SHA pinning | `saga_apply_strategy.py` | Medium — TARGET_MOVED + ancestry check |

### What Does NOT Change

- GovernedOrchestrator FSM (single-writer invariant preserved)
- IntakeLayerService sensor architecture
- Provider generation pipeline
- OperationContext phase transitions
- CrossRepoVerifier verification tiers
- SagaMessageBus (optional/adjacent — observability only, not execution-critical)

---

## 2. B+ Branch Management

All apply/verify/promote/rollback happens on ephemeral branches. Target branches are never touched until ff-only promotion after full success.

### Lifecycle

```
Phase A (preflight):
  1. Acquire per-repo locks (deterministic sorted order)
  2. Assert clean working tree per repo (git status --porcelain=v1)
  3. Capture base_sha per repo (git rev-parse HEAD)
  4. Capture original ref per repo (branch name + SHA; handle detached HEAD)
  5. Create ephemeral branch: ouroboros/saga-<op_id>/<repo> from base_sha

Phase B (apply):
  6. Checkout ephemeral branch
  7. Write files + git add + git commit (deterministic commit identity)
  8. If patch yields no diff → SKIPPED (no commit, not failure)
  9. Repeat for each repo in topological order

Phase V (verify — on ephemeral branches):
  10. CrossRepoVerifier runs lint/test/contracts against ephemeral branch state

Phase P (promote — only on full success):
  11. For each repo in topological order:
      a. Verify target branch HEAD == base_sha (TARGET_MOVED gate)
      b. Verify base_sha is ancestor of saga branch (ancestry gate)
      c. git checkout <target_branch>
      d. git merge --ff-only ouroboros/saga-<op_id>/<repo>
      e. Record promoted_sha + promote_order_index in ledger
      f. Delete ephemeral branch
  12. If any repo promotion fails: SAGA_PARTIAL_PROMOTE (see Section 4)

Phase C (compensate — on ANY failure):
  13. For each repo (reverse topological order):
      a. git checkout <original_branch> (or original SHA if detached)
      b. If keep_failed_saga_branches=False: git branch -D ephemeral
      c. If keep_failed_saga_branches=True: leave branch for forensics
  14. Target branches untouched (never modified on failure)
  15. Release per-repo locks (in finally block)
  16. Record rollback_reason in ledger
```

### Key Invariants

- Target branch is NEVER checked out during apply/verify
- All file writes happen on ephemeral branch only
- Promotion is ff-only — if target moved since `base_sha`, emit `TARGET_MOVED` and abort
- Lock acquisition order: `sorted(repo_scope)` — deterministic, prevents cross-saga deadlock
- Compensation = checkout original + optionally delete branch (no file-level restore)
- No git stash anywhere in saga path

### Detached HEAD Handling

```python
async def _capture_original_ref(self, repo: str) -> Tuple[str, str]:
    """Returns (branch_name_or_HEAD, sha). Handles detached HEAD."""
    try:
        branch = await self._git(repo, ["symbolic-ref", "--short", "HEAD"])
    except subprocess.CalledProcessError:
        branch = "HEAD"  # detached
    sha = await self._git(repo, ["rev-parse", "HEAD"])
    return branch.strip(), sha.strip()
```

Restoration on compensation: use SHA if `original_ref == "HEAD"` (detached), branch name otherwise.

### Clean-Tree Policy

```python
async def _assert_clean_worktree(self, repo: str) -> None:
    result = await self._git(repo, ["status", "--porcelain=v1"])
    dirty = [l for l in result.splitlines() if l and not l.startswith("?? ")]
    if dirty:
        raise RuntimeError(f"dirty_worktree:{repo}:{len(dirty)} tracked changes")
```

- Tracked modified/staged files → hard fail
- Untracked files (`??`) → allowed
- Ignored files → allowed (not shown by porcelain)

### No-Op Commit Semantics

If a repo's patch yields no actual diff after file writes:
- `git diff --cached --quiet` returns 0 → no changes staged
- Treat as `SKIPPED` in `RepoSagaStatus` (not failure, not success)
- Ledger artifact emits `event: "skipped_no_diff"` with repo name
- Does NOT affect other repos in the saga — only that repo is skipped
- Analytics: `SKIPPED` is a valid non-terminal state, distinct from success/failure

### Branch Name Safety

```python
def _safe_branch_name(op_id: str, repo: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", op_id)[:64]
    safe_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", repo)[:32]
    return f"ouroboros/saga-{safe_id}/{safe_repo}"
```

### Changes to SagaApplyStrategy

New instance fields:
```python
self._saga_branches: Dict[str, str] = {}      # repo → ephemeral branch name
self._original_branches: Dict[str, str] = {}  # repo → original branch/ref
self._original_shas: Dict[str, str] = {}      # repo → original SHA
self._base_shas: Dict[str, str] = {}          # repo → pinned base SHA
self._keep_failed_branches: bool = keep_failed_saga_branches  # default True
```

New methods:
```python
async def _create_ephemeral_branch(self, repo: str, op_id: str) -> str
async def _promote_ephemeral_branch(self, repo: str) -> str  # returns promoted_sha
async def _cleanup_ephemeral_branch(self, repo: str) -> None
async def _assert_clean_worktree(self, repo: str) -> None
async def _check_promote_safe(self, repo: str) -> None  # TARGET_MOVED + ancestry
async def _capture_original_ref(self, repo: str) -> Tuple[str, str]
async def _git(self, repo: str, args: List[str], env: Optional[Dict] = None) -> str
async def _git_rc(self, repo: str, args: List[str]) -> int  # return code only
```

---

## 3. Per-Repo Operation Locks

### Two-Tier Design

| Tier | Scope | Mechanism | Purpose |
|------|-------|-----------|---------|
| In-process | Single JARVIS process | `asyncio.Lock` per repo | Prevent concurrent sagas within same event loop |
| Cross-process | Multiple processes/restarts | File lock (`fcntl.flock`) per repo | Prevent concurrent sagas across processes, survive crashes |

**Platform assumption:** macOS and Linux only (`fcntl.flock`). Not Windows-compatible. Documented in module docstring.

### Implementation

```python
# New file: backend/core/ouroboros/governance/saga/repo_lock.py

class RepoLockManager:
    """Two-tier repo-level locks for saga exclusivity.

    Lock files: <repo_root>/.jarvis/saga.lock
    Acquisition order: always sorted(repo_names) to prevent deadlock.
    """

    _async_locks: Dict[str, asyncio.Lock]   # in-process tier
    _file_fds: Dict[str, int]               # fd for fcntl.flock

    async def acquire(self, repos: List[str], repo_roots: Dict[str, Path]) -> None:
        """Acquire both tiers in sorted order. Raises on timeout."""

    async def release(self, repos: List[str]) -> None:
        """Release both tiers. Safe to call multiple times."""

    def cleanup_stale_locks(self, repo_roots: Dict[str, Path]) -> List[str]:
        """Check for stale lock files (dead PID). Remove and return list of cleaned repos."""

    def detect_orphan_branches(self, repo_roots: Dict[str, Path]) -> List[str]:
        """Scan repos for ouroboros/saga-* branches. Return list for health endpoint."""
```

### Stale Lock Recovery

Lock files contain the owning PID:
```
# .jarvis/saga.lock content:
{"pid": 12345, "saga_id": "op_20260311_abc123", "acquired_at_ns": 1710000000000}
```

On `cleanup_stale_locks()`:
1. Read PID from lock file
2. `os.kill(pid, 0)` — if `ProcessLookupError`, PID is dead → remove lock
3. If PID alive but lock age > 30 minutes → log WARNING (potential hang, don't auto-remove)
4. Return list of cleaned repo names

### Always-Release Guarantee

```python
# In SagaApplyStrategy.execute():
async def execute(self, ctx, patch_map) -> SagaApplyResult:
    repos = self._resolve_apply_order(ctx)
    await self._lock_manager.acquire(repos, self._repo_roots)
    try:
        # Full lifecycle: Phase A → B → V → P or C
        return await self._execute_locked(ctx, patch_map, repos)
    finally:
        await self._lock_manager.release(repos)
```

`asyncio.CancelledError` is `BaseException` in Python 3.9+ — `finally` block executes on cancellation. No lock leaks.

### Crash Recovery for Orphan Branches

On startup, `RepoLockManager.detect_orphan_branches()` scans for `ouroboros/saga-*` branches. Policy:
- Log WARNING with branch names
- Surface in health endpoint
- Do NOT auto-delete (human decision required)
- Operator can manually `git branch -D` after forensic review

---

## 4. Promotion Atomicity & Failure States

### Promote Gates (per repo)

```python
async def _check_promote_safe(self, repo: str) -> None:
    target_branch = self._original_branches[repo]
    base_sha = self._base_shas[repo]
    saga_branch = self._saga_branches[repo]

    # Gate 1: target hasn't moved
    current_target = await self._git(repo, ["rev-parse", target_branch])
    if current_target.strip() != base_sha:
        raise RuntimeError(f"TARGET_MOVED:{repo}:{base_sha}→{current_target.strip()}")

    # Gate 2: base_sha is ancestor of saga branch tip (no unintended commits)
    rc = await self._git_rc(repo, [
        "merge-base", "--is-ancestor", base_sha, saga_branch
    ])
    if rc != 0:
        raise RuntimeError(f"ANCESTRY_VIOLATION:{repo}:{base_sha} not ancestor of {saga_branch}")
```

### Partial Promotion Policy

If repo N promotion fails after repos 1..N-1 already promoted:

```
Terminal state: SAGA_PARTIAL_PROMOTE

Actions:
  1. Record which repos promoted vs which failed in ledger
  2. Pause cross-repo saga intake only (local/single-repo ops continue)
  3. Emit postmortem with full state
  4. Log operator instructions for manual resolution
  5. Do NOT attempt to revert promoted repos (no force-push, no revert commits)

Rationale: Reverting ff-only merges requires force-push or revert commits.
Both violate deterministic recovery. Human reviews partial state and decides.
```

New terminal state:
```python
class SagaTerminalState(str, Enum):
    ...
    SAGA_PARTIAL_PROMOTE = "saga_partial_promote"
```

### Promotion Failure State Machine

```
SAGA_PARTIAL_PROMOTE triggers:
  → controller.pause(scope="cross_repo_saga")  # only cross-repo paused
  → local single-repo ops continue unaffected
  → comm.emit_postmortem(root_cause="saga_partial_promote")
  → ledger entry with promoted_repos + failed_repos + boundary_repo

Resume requires:
  → Human reviews partial state
  → Human runs: /ouroboros resume-saga <saga_id>  (or manual git cleanup)
  → controller.resume(scope="cross_repo_saga")
```

### Scoped Pause

`controller.pause()` gains a `scope` parameter:
```python
async def pause(self, scope: str = "all") -> None:
    """Pause intake. scope='all' | 'cross_repo_saga'"""
```

Backward-compatible default `scope="all"`.

### Commit Identity Schema

```
[ouroboros] {description_first_72_chars}

op_id: {op_id}
saga_id: {saga_id}
repo: {repo_name}
base_sha: {base_sha}
phase: apply
schema_version: 3.0
```

Git author/committer set deterministically:
```python
env = {
    **os.environ,
    "GIT_AUTHOR_NAME": "JARVIS Ouroboros",
    "GIT_AUTHOR_EMAIL": "ouroboros@jarvis.local",
    "GIT_COMMITTER_NAME": "JARVIS Ouroboros",
    "GIT_COMMITTER_EMAIL": "ouroboros@jarvis.local",
}
```

### Hook Policy

Saga commits use `--no-verify`. Rationale: CrossRepoVerifier Tier 1 already runs ruff lint + pytest on changed files. Pre-commit hooks on machine-generated commits add nondeterminism without additional safety.

---

## 5. Enhanced Ledger Artifacts

### SagaLedgerArtifact

```python
@dataclass(frozen=True)
class SagaLedgerArtifact:
    saga_id: str
    op_id: str
    event: str                          # lifecycle event name
    repo: str                           # "*" for saga-wide events
    original_ref: str                   # branch name or "HEAD" (detached)
    original_sha: str                   # SHA at saga start
    base_sha: str                       # pinned base SHA
    saga_branch: str                    # ouroboros/saga-<op_id>/<repo>
    promoted_sha: str                   # "" if not promoted
    promote_order_index: int            # -1 if N/A
    rollback_reason: str                # "" on success
    partial_promote_boundary_repo: str  # "" if clean
    kept_forensics_branches: bool       # True if branches retained
    skipped_no_diff: bool               # True if repo had no actual changes
    timestamp_ns: int                   # time.monotonic_ns()
```

### Emission Points

| Phase | Event | Key fields |
|-------|-------|-----------|
| A (preflight) | `prepare` | base_sha, original_ref, original_sha, saga_branch |
| B (apply) | `apply_repo` | repo, saga_branch, base_sha |
| B (skip) | `skipped_no_diff` | repo, skipped_no_diff=True |
| B (fail) | `apply_failed` | repo, rollback_reason |
| V (verify fail) | `verify_failed` | rollback_reason, kept_forensics_branches |
| P (promote) | `promote_repo` | repo, promoted_sha, promote_order_index |
| P (partial fail) | `partial_promote` | partial_promote_boundary_repo, rollback_reason |
| C (compensate) | `compensate_repo` | repo, rollback_reason, kept_forensics_branches |

### Ledger/State Mapping

`SAGA_PARTIAL_PROMOTE` maps to `OperationState.FAILED` in the ledger (same as `SAGA_STUCK`). The `reason_code` field distinguishes them:
- `reason_code="saga_stuck"` → compensation failed
- `reason_code="saga_partial_promote"` → promotion partially completed

Any code that maps terminal states to ledger states (orchestrator, health endpoint, analytics) must handle the new enum value explicitly.

---

## 6. E2E Test Gates

### Test Tier Separation

| Tier | Tests | When | Environment |
|------|-------|------|-------------|
| CI-safe (deterministic) | Gate 1 tests 1-10 (except `test_g1_generate_multi_repo_patches`) | Every PR | Mock git repos, no J-Prime needed |
| J-Prime acceptance | `test_g1_generate_multi_repo_patches` + all Gate 2 | Nightly / manual | Real J-Prime endpoint required |

Gate 1 tests 2-10 use local git repos with synthetic commits — no network dependency. Only `test_g1_generate_multi_repo_patches` and Gate 2 require live J-Prime.

### Gate 1 — Deterministic Sentinel E2E

Setup: One controlled sentinel file per repo with ~15 if-branches.

| # | Test | Proves | GO criteria |
|---|------|--------|-------------|
| 1 | `test_g1_generate_multi_repo_patches` | J-Prime returns valid 2c.1 | `GenerationResult.candidates` non-empty, all 3 repo keys |
| 2 | `test_g1_saga_branch_lifecycle` | Ephemeral branches created, target untouched | Branch exists, target HEAD unchanged |
| 3 | `test_g1_commit_identity` | Commit message matches schema | op_id, saga_id, repo, base_sha in message |
| 4 | `test_g1_verify_passes` | CrossRepoVerifier passes on clean patches | `VerifyResult.passed is True` |
| 5 | `test_g1_promote_ff_only` | FF-only promotion advances target | Target HEAD == saga tip, branch deleted |
| 6 | `test_g1_rollback_on_verify_failure` | Induced failure → clean compensation | Target HEAD == base_sha |
| 7 | `test_g1_target_moved_abort` | Advance target between apply/promote → abort | `SAGA_ABORTED`, target untouched |
| 8 | `test_g1_dirty_tree_rejected` | Dirty working tree → preflight fail | `dirty_worktree` reason code |
| 9 | `test_g1_deterministic_lock_order` | Two concurrent sagas → no deadlock | Both complete within 10s |
| 10 | `test_g1_orphan_branch_detection` | Orphan saga branch detected on restart | `detect_orphan_branches()` returns name |

**Gate 1 GO/NO-GO:**
- GO: All 10 tests pass, generation uses real J-Prime (test 1), total < 10 min
- NO-GO: Any failure → stop, return root-cause + reason code, do not proceed to Gate 2

### Gate 2 — Real Backlog Acceptance E2E

| # | Test | Proves | GO criteria |
|---|------|--------|-------------|
| 1 | `test_g2_real_backlog_e2e` | Full pipeline intake→promote | `SAGA_SUCCEEDED` |
| 2 | `test_g2_generation_variability` | Two runs, compare outputs | Both succeed; structural diff logged |
| 3 | `test_g2_failure_transparency` | All failures have reason codes | No silent swallowing |

**Gate 2 GO/NO-GO:**
- GO: Test 1 passes at least once, all failures have explicit reason codes
- NO-GO: Silent failure or `SAGA_STUCK`/`SAGA_PARTIAL_PROMOTE` → stop, return root-cause

### Required Output Per Test

- Model used (`brain_id`, `brain_model`)
- Token count + latency (`generation_duration_s`)
- Phase trail (ordered sequence of ledger events)
- Full `SagaLedgerArtifact` entries
- Variability report (Gate 2 only)

### Test Infrastructure

```
tests/e2e/
├── conftest.py                    # fixtures: temp git repos, prime client, gate order
├── test_gate1_sentinel.py         # Gate 1: 10 tests
├── test_gate2_backlog.py          # Gate 2: 3 tests
└── fixtures/
    ├── sentinel_jarvis.py         # controlled complex file
    └── synthetic_backlog.json     # fallback multi-repo backlog entry
```

Gate order enforced via `@pytest.mark.dependency`.

---

## 7. File-Level Delta Map

### Modified Files

| File | Current | After | What changes |
|------|---------|-------|-------------|
| `saga/saga_apply_strategy.py` | ~451 lines | ~650 lines | B+ branch lifecycle, git commit, try/finally locks, detached HEAD, clean-tree, promote gates, forensics flag, no-op skip |
| `saga/saga_types.py` | ~91 lines | ~140 lines | `SAGA_PARTIAL_PROMOTE` state, `SagaLedgerArtifact` dataclass |
| `orchestrator.py` | ~1237 lines | ~1270 lines | Handle `SAGA_PARTIAL_PROMOTE`, scoped pause |
| `governed_loop_service.py` | ~2088 lines | ~2095 lines | Pass `keep_failed_saga_branches` config, orphan branches in health |

### New Files

| File | Est. lines | Purpose |
|------|-----------|---------|
| `saga/repo_lock.py` | ~120 | `RepoLockManager`: two-tier locks, stale cleanup, orphan detection |
| `tests/e2e/conftest.py` | ~80 | Shared fixtures |
| `tests/e2e/test_gate1_sentinel.py` | ~300 | Gate 1: 10 deterministic tests |
| `tests/e2e/test_gate2_backlog.py` | ~150 | Gate 2: 3 acceptance tests |
| `tests/e2e/fixtures/sentinel_jarvis.py` | ~30 | Controlled sentinel |
| `tests/e2e/fixtures/synthetic_backlog.json` | ~20 | Fallback backlog entry |

### Confirmed Unchanged

- `providers.py` — schema 2c.1 parsing already builds RepoPatch
- `op_context.py` — multi-repo fields already present
- `cross_repo_verifier.py` — works on whatever branch is checked out
- `intake_layer_service.py` — sensor fan-out already works
- `change_engine.py` — single-repo path unchanged
- `comms/voice_narrator.py` — narration hooks untouched
- `brain_selection_policy.yaml` — routing unchanged

### Total Delta

- ~200 lines modified in existing files
- ~700 lines new code
- ~900 lines total

### Implementation Order

```
1. saga_types.py           → new terminal state + ledger artifact type
2. saga/repo_lock.py       → new file: locks
3. saga_apply_strategy.py  → B+ branch lifecycle (depends on 1+2)
4. orchestrator.py         → PARTIAL_PROMOTE handling (depends on 1)
5. governed_loop_service.py → config wiring (depends on 3)
6. tests/e2e/*             → all tests (depends on 1-5)
```

---

## 8. Backcompat & Migration

### New Enums / Reason Codes

| Addition | Impact |
|----------|--------|
| `SagaTerminalState.SAGA_PARTIAL_PROMOTE` | Any switch/match on terminal states must handle new variant. Orchestrator, health endpoint, analytics. |
| Reason codes: `TARGET_MOVED`, `ANCESTRY_VIOLATION`, `dirty_worktree`, `skipped_no_diff` | Ledger consumers and dashboards need to recognize these. |

### Configuration Defaults

| Config | Default | Meaning |
|--------|---------|---------|
| `keep_failed_saga_branches` | `True` | Failed saga branches retained for forensics. Safe default. |
| `controller.pause(scope=)` | `"all"` | Backward-compatible. Existing callers unchanged. |
| `saga.lock` file format | JSON with PID | New file in `.jarvis/`. No migration — created on first saga run. |

### Rollout Strategy

Feature-flagged via environment variable:
```
JARVIS_SAGA_BRANCH_ISOLATION=true   # enable B+ branch management (default: false initially)
```

When `false`: existing behavior (direct-to-HEAD apply, file-level compensation). No branch creation, no locks, no promote gates.

When `true`: full B+ lifecycle as designed.

Rollout sequence:
1. Deploy with flag `false` (no behavior change)
2. Enable on dev/staging
3. Run Gate 1 tests
4. Enable on production
5. Run Gate 2 tests
6. Remove flag (B+ becomes permanent)

### Rollback Strategy

If B+ causes issues after deployment:
1. Set `JARVIS_SAGA_BRANCH_ISOLATION=false` → reverts to existing behavior immediately
2. Clean up orphan saga branches manually
3. No schema migration needed — ledger entries with saga artifacts are backward-compatible (extra fields ignored by older consumers)

---

## 9. GO/NO-GO Criteria

### Gate 1 GO (required for any cross-repo operation)

- [ ] All 10 Gate 1 tests pass
- [ ] Real J-Prime generation (test 1) returns valid 2c.1 with patches for 3 repos
- [ ] Promote + rollback lifecycle verified end-to-end
- [ ] Ledger artifacts complete at every emission point
- [ ] No lock leaks under cancellation
- [ ] Total Gate 1 runtime < 10 minutes

### Gate 2 GO (required for autonomous backlog processing)

- [ ] `test_g2_real_backlog_e2e` passes at least once
- [ ] All failures have explicit reason codes (no silent swallowing)
- [ ] Variability between runs is documented

### NO-GO (any of these stops deployment)

- Any Gate 1 test fails
- Silent failure (missing reason code)
- `SAGA_STUCK` or `SAGA_PARTIAL_PROMOTE` in Gate 2
- Lock leak detected under cancellation
- Orphan branch not detected on restart
