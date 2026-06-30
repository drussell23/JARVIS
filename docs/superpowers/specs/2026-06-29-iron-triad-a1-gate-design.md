# Iron Triad — Cryptographically-Chained Autonomous-PR Gates to Close the A1 Gate (2026-06-29)

**Status:** design (approved direction; pending spec review → implementation plan)
**Author:** Derek J. Russell + O+V
**Branch:** `feat/iron-triad-a1-gate`
**Supersedes/builds on:** `docs/superpowers/plans/2026-06-27-isomorphic-local-sandbox.md` (MERGED PR #69735), `docs/memory_topics/intake/project_a1_live_fire_result.md`

---

## 1. Problem — the C+ root cause

O+V's cognitive loop is **proven** (190 A1Trace hops, 4× live) but it has **never completed one full autonomous cycle end-to-end** — detect gap → generate → validate → apply → **open its own PR** — on a real node. Across 13+ cloud runs, each died on a *different* integration bug that passed unit tests and failed only live ("unit-green-fails-live"). That gap **is** the C+ EXECUTION grade.

The A1 success criterion is the verdict `A1_DISPATCH_PROVEN` (`scripts/a1_graduation_auditor.py:1173`), requiring **all** of: 5 A1Trace hops in order, `fsm_classify_to_applied`, the 12-flag cognitive audit, a clean intervention-lock, **`autonomous_pr_observed`**, and verifiable lineage.

This spec closes the gate by binding the autonomous cycle with three **fail-closed** gates on the *single mandatory path* an op traverses to reach a PR — and chaining them with unforgeable capability tokens so that **skipping a gate is a type error, not a policy violation**. This directly answers the operator mandate: *no workarounds, no shortcuts, no hardcoding; advanced, asynchronous, dynamic, adaptive, robust; reuse existing architecture, zero duplication.*

### Why "wiring-first"
The recurring failure mode in this codebase (`feedback_security_filter_must_be_wired`, hit 3× last arc) is **a guard that exists but is not on the mandatory path**. The scouting for this spec confirmed it again: the hardened container executor `container_sandbox.run_in_container()` exists and is wired into Venom's bash/run_tests tools, but **the generated candidate code is never executed in a container as a pre-APPLY gate** (`grep run_in_container` over orchestrator/phase_runners/candidate_generator = 0 hits). Each gate below is ~90% reuse + a thin wiring layer.

---

## 2. The mandatory path and where each gate sits

```
per-op DAGProofChain (HMAC-SHA256, per-process secret, state-bound, hash-chained)
──────────────────────────────────────────────────────────────────────────────────►
GENERATE ─ IronGate(explore/ascii) ─[① SANDBOX EXEC LOCK]─ VALIDATE ─ GATE(SemGuard/REVIEW)
   ─ APPLY ─ VERIFY(scoped tests) ─[② BLAST RADIUS]─ auto-commit ─[③ PR LINTER]─ gh_pr_create(tok₁,tok₂,tok₃)
            │ mints SandboxExecutionToken          │ mints BlastRadiusClearedToken     │ mints LintClearedToken
            └──────────────── chain ───────────────┴──────────────── chain ────────────┘  verify_chain() or refuse
```

---

## 3. The Cryptographic DAG Token Enforcer (the spine)

**New module:** `backend/core/ouroboros/governance/dag_capability_token.py`

A capability-security pattern: each gate, on success, mints an **unforgeable, state-bound, hash-chained** token. The terminal PR-creation function requires the specific typed token objects as **mandatory arguments**, and re-verifies the chain before invoking `gh`. The AI cannot *physically* construct a valid token without (a) the per-process secret and (b) the exact repository state the gate validated.

### Token structure
```python
@dataclass(frozen=True)
class CapabilityToken:
    kind: TokenKind                 # SANDBOX_EXECUTION | BLAST_RADIUS_CLEARED | LINT_CLEARED
    op_id: str
    state_binding: str              # SHA-256 the gate actually validated (candidate-hash / tree-SHA / diff-hash)
    prev_hash: str                  # hash of the previous token in the chain ("" for the first)
    payload: Mapping[str, str]      # gate-specific evidence (exit_code, test_set_hash, lint_rating, ...)
    issued_monotonic: float
    sig: str                        # HMAC-SHA256(secret, canonical(kind,op_id,state_binding,prev_hash,payload))
```

Typed aliases `SandboxExecutionToken`, `BlastRadiusClearedToken`, `LintClearedToken` (frozen subclasses pinned to one `kind`) so a function signature can demand the *specific* type — an `isinstance` + chain verify enforce it at runtime; the type annotation enforces it for any static checker.

### `DAGProofChain` (per-op accumulator)
- `mint(kind, op_id, state_binding, payload, prev) -> CapabilityToken` — HMAC-signs; links `prev_hash`.
- `verify(token) -> bool` — recomputes HMAC; rejects on tamper.
- `verify_chain(tokens: Sequence[CapabilityToken], *, op_id) -> bool` — every token verifies, `op_id` matches across all, `prev_hash` links are intact and in canonical order, and the final token's kind is `LINT_CLEARED`.

### Security properties
- **Unforgeable:** HMAC-SHA256 with a secret from `secrets.token_bytes(32)` minted at orchestrator init, held only in memory, **never logged or persisted**. A buggy/compromised path cannot fabricate a token literal.
- **Replay-proof:** `state_binding` ties each token to the exact SHA it validated; a stale token fails against a mutated tree.
- **Skip-proof:** the PR wrapper signature *requires* the token objects; there is no code path that opens a PR without them.
- **Tamper-evident chain:** `prev_hash` forms a mini hash-chain; reordering or omitting a gate breaks `verify_chain`.

### Failure semantics
Gates do not return falsy tokens. On failure they **raise** a typed exception that the FSM routes to terminate/rollback/Cryo-DLQ. A missing token therefore can never be silently coerced to "passed."

Env: `JARVIS_A1_TOKEN_ENFORCER_ENABLED` (default **false** → wrapper accepts `None` tokens and skips chain verify = byte-identical legacy).

---

## 4. Gate ① — Isomorphic Execution Lock (pre-APPLY, fail-closed, L4-only)

**New module:** `backend/core/ouroboros/governance/pre_apply_exec_lock.py`
**Wired:** orchestrator, after the Iron Gate, before VALIDATE (candidate exists, nothing written to the real tree yet).

```python
async def acquire_sandbox_execution_token(
    *, op_id, candidate_files, repo_root, chain, prev_token=None
) -> SandboxExecutionToken           # raises SandboxLockFailed | RequiresCloudExecution
```

**Behavior (reuse-heavy):**
1. **Daemon pre-flight** — `container_sandbox.docker_available()`. (Also run **once asynchronously at A1-loop start** so Docker absence is known before the op reaches APPLY, not discovered mid-DAG.)
2. **No Docker → strict, no downgrade.** Raise `RequiresCloudExecution`. Phase 1: terminate DAG + flag the op `REQUIRES_CLOUD_EXECUTION`. Phase 2: route execution to the GCP node (§7). **Never** fall back to process isolation.
3. Materialize the candidate into an **isolated git worktree** (`WorktreeManager`, COW) under **`IsomorphicEnv`** (forces `/opt/trinity/jarvis` path, disjoint cwd, disabled `/tmp` whitelist — `JARVIS_SANDBOX_PREFIXES`).
4. Execute in the **hardened container** (`run_in_container`: `--network none --cap-drop ALL --read-only --pids-limit`): import/compile the changed modules + run the candidate's own scoped tests.
5. **Exit 0** → mint `SandboxExecutionToken` bound to `op_id`, `state_binding = sha256(candidate_files)`, `payload = {exit_code: "0", iso_fingerprint, image}`. **Non-zero / container spawn failure** → `SandboxLockFailed` → terminate DAG, rollback, **zero writes to the real tree**.

Token is "strictly tied to a successful L4 container exit code" per mandate.
Env: `JARVIS_A1_SANDBOX_LOCK_ENABLED` (default false); composes under `JARVIS_RUNTIME_SANDBOX_ENABLED`.

---

## 5. Gate ② — Cryptographic Blast-Radius Verification (post-VERIFY, pre-commit)

**New module:** `backend/core/ouroboros/governance/blast_radius_verify.py`
**Wired:** `phase_runners/slice4b_runner.py`, between the verify gate (~:1034) and Phase 8b auto-commit (:1091). Pre-op tree-SHA captured alongside the existing `WorkspaceCheckpointManager.create_checkpoint` (~:415).

```python
async def acquire_blast_radius_token(
    *, op_id, ctx, pre_op_tree_sha, best_candidate, chain, prev_token
) -> BlastRadiusClearedToken         # raises BlastRadiusBreach | BlastRadiusGraphFailure
```

**Behavior:**
1. **Reverse-dependency closure** — compute the full set of tests that touch the modified AST via `call_graph_blast` / `blast_radius_adapter` (reverse-deps of `ctx.target_files ∪ _iter_candidate_files(best_candidate)`).
2. **Fail-Closed Graph Guard** — any parse error, cyclic-dependency crash, or invalid graph → raise `BlastRadiusGraphFailure` → tree-SHA rollback + `intake_dlq.append_dlq(reason="blast_radius_graph_failure")` → POSTMORTEM. **No marker fallback, no skip.**
3. **Run that exact set, no retry** (strict flake isolation — A1 phase). One failure = hard failure.
4. **Out-of-scope failure** (failing test's source ∉ scope) **or any failure** → `WorkspaceCheckpointManager.restore_checkpoint(pre_op_tree_sha)`; **assert** `current_tree_sha == pre_op_tree_sha` (cryptographic rollback proof); `append_dlq(reason="blast_radius_breach")`; advance POSTMORTEM mirroring the existing `verify_regression` terminal block.
5. **All pass** → mint `BlastRadiusClearedToken`(prev=`sandbox_token`, `state_binding = pre_op_tree_sha`, `payload = {post_tree_sha, test_set_hash, n_tests}`).

Rollback is the **non-destructive tree-SHA checkpoint restore**, never `git reset --hard` (which would nuke concurrent autonomous work and fight `git_index_guard`).
Env: `JARVIS_A1_BLAST_RADIUS_ENABLED` (default false).

---

## 6. Gate ③ — Autonomous PR Linter (pre-`gh pr create`, blocking)

**New module:** `backend/core/ouroboros/governance/pr_self_linter.py`
**Wired:** `orange_pr_reviewer.py::create_review_pr`, before the `git push` / `gh pr create` (~:303–324).

```python
async def acquire_lint_cleared_token(
    *, op_id, diff, description, chain, prev_token
) -> LintClearedToken                # raises LintRejected
```

**Behavior:** a **blocking** LLM architectural-rules critique. Reuse `self_critique.build_critique_prompt` / `parse_critique_json` / `collect_op_diff` and `DoublewordProvider.prompt_only(response_format={"type":"json_object"})` (cheapest bounded structured call). Critique the diff against this repo's rules — **no hardcoding (intent), DRY, explicit error handling, async-first, env-var-driven config**. Below threshold → raise `LintRejected` → `create_review_pr` returns `None` → existing CLI-approval fall-through. This is genuinely new surface: SemanticGuardian covers deterministic *patterns*; this covers *semantic intent* the regex/AST gate cannot.

**Signature change (the enforcer bite):**
```python
async def create_review_pr(self, *, ..., sandbox_token: SandboxExecutionToken,
                           blast_token: BlastRadiusClearedToken, lint_token: LintClearedToken) -> PRReviewResult | None:
    if not self._chain.verify_chain([sandbox_token, blast_token, lint_token], op_id=op_id):
        return None    # refuse — cannot reach `gh pr create`
```
Env: `JARVIS_A1_PR_LINTER_ENABLED` (default false). When `JARVIS_A1_TOKEN_ENFORCER_ENABLED=false`, tokens default `None` and the verify is skipped (legacy parity).

---

## 7. Phase 2 — Hybrid-Cloud sandbox routing (the adaptive edge case)

When the Docker pre-flight finds no local daemon, the op is flagged `REQUIRES_CLOUD_EXECUTION` and the Sandbox Exec Lock runs **on the GCP node** (native container support), not locally.

**Reuse:** the proven failover mesh — ADC IAM bridge (`gcp_compute_rest.py`), REST `instances.insert`, /32 ephemeral firewall, Reachability Racer, dead-man teardown.
**New (Phase 2 only):** `remote_sandbox_executor.py` — a thin service on the node that accepts the candidate, runs `run_in_container` there, and **streams the exit code + iso-fingerprint back** over the existing event-channel; the local `DAGProofChain` mints the `SandboxExecutionToken` from the streamed L4 result (the secret never leaves the local process — the node returns *evidence*, the local chain mints the *token*).

**Phase 1 deliberately stops at the flag** (terminate + emit `REQUIRES_CLOUD_EXECUTION`). The first A1 ignition runs the local driver with Docker Desktop up, so the local container path mints the token. This keeps the heaviest subsystem off the critical path to the first autonomous PR. YAGNI until the local proof lands.

---

## 8. Branch reconciliation (no duplication)

- **`feat/a1-disable-file-isolation`** (unmerged) — pins file-isolation OFF via launch manifest so autonomous writes land durably in `repo_root` (`written=True`), *"the last `fsm_classify_to_applied` blocker."* **Required** for A1 to reach `applied`. Reconcile into the A1 launch manifest, not re-implement.
- **`feat/isomorphic-sandbox-enhancements`** (unmerged) — faithful DW-mock (dynamic `/v1` schema, 4-stage batch API, SSE). Fold into `synthetic_adversary.py` / the local driver fidelity.

---

## 9. Reuse map (existing → role; zero duplication)

| Need | Existing asset | Net-new |
|---|---|---|
| Hardened L4 container exec | `container_sandbox.run_in_container` | call it on the *candidate* (was inert) |
| Isomorphic path/env parity | `IsomorphicEnv`, `WorktreeManager` | compose in the lock |
| Crypto pre-op tree hash | `WorkspaceCheckpointManager` (`git stash create` SHA) | post-restore SHA-equality assertion |
| SHA-256 primitive | `state_drift`, `change_engine`, `verify_gate` | — |
| Reverse-dep test graph | `call_graph_blast`, `blast_radius_adapter` | run-the-set + fail-closed guard |
| Cryo-DLQ entry | `intake_dlq.append_dlq(reason=…)` | new reasons |
| Rollback restore | `verify_gate.rollback_files`, `WorkspaceCheckpointManager.restore_checkpoint` | — |
| LLM diff critique helpers | `self_critique.{build_critique_prompt,parse_critique_json,collect_op_diff}` | **blocking** variant + token |
| Cheapest structured LLM call | `DoublewordProvider.prompt_only(json_object)` | — |
| PR creation | `OrangePRReviewer.create_review_pr` | token-gated signature |
| Local A1 E2E driver | `scripts/isomorphic_a1_local.py` | ignition runbook |
| A1 verdict | `scripts/a1_graduation_auditor.py` (`A1_DISPATCH_PROVEN`) | — |

---

## 10. Env flags (all default-OFF → byte-identical rollback)

`JARVIS_A1_TOKEN_ENFORCER_ENABLED`, `JARVIS_A1_SANDBOX_LOCK_ENABLED`, `JARVIS_A1_BLAST_RADIUS_ENABLED`, `JARVIS_A1_PR_LINTER_ENABLED` — each independently gated; composes under the existing `JARVIS_RUNTIME_SANDBOX_ENABLED`. Armed only for the A1 soak. Registered in `FlagRegistry`.

---

## 11. Testing strategy

- **Token enforcer:** forgery rejected (bad HMAC), replay rejected (mutated `state_binding`), chain tamper rejected (reordered/omitted), `create_review_pr` un-callable without tokens.
- **Gate ①:** non-zero container exit → `SandboxLockFailed` + zero writes; no-Docker → `RequiresCloudExecution` (no process fallback); pass → valid chained token.
- **Gate ②:** out-of-scope failure → tree-SHA restore + `restored==pre_op` assert + DLQ; graph failure → fail-closed + DLQ; pass → token chains to ①.
- **Gate ③:** rule violation → `LintRejected` → `create_review_pr` returns `None`; pass → token chains to ②.
- **OFF parity:** every flag off → byte-identical (0 behavioral delta) regression.
- **E2E:** `isomorphic_a1_local.py` drives the full chain locally to `A1_DISPATCH_PROVEN` ($0/minutes), then one cloud confirm.

---

## 12. Non-goals / YAGNI

- No `git reset --hard` in the apply path (rejected: destroys concurrent work; tree-SHA restore is the correct primitive).
- No marker-curated or full-suite regression set (rejected: hardcoded or budget-blowing).
- No process-isolation fallback for the sandbox (rejected: L4→L3 downgrade violates zero-trust).
- Phase 2 hybrid-cloud executor is built **after** the local A1 proof, not before.
- No new model names; all provider routing via existing policy.

---

## 13. Ignition runbook (endgame)

1. Land Phase 1 (Triad + Enforcer) behind the four flags; reconcile the two branches.
2. `python3 scripts/isomorphic_a1_local.py --mode container` with Docker Desktop up → drive to `A1_DISPATCH_PROVEN` locally.
3. Capture failure telemetry on any non-proven (`failure_telemetry.capture_failure_telemetry`); fix; repeat at $0/minutes.
4. One confirming cloud soak (`--max-wall-seconds 2400 --headless`) → `A1_DISPATCH_PROVEN` live → **first autonomous PR** → A1 gate passes → EXECUTION grade moves off C+.

---

## Addendum (2026-06-29) — FSM-routing correction: the PR-path unification

The §2 "single mandatory path" assumed a linear op flow (generate → sandbox → apply → blast → lint → PR). The whole-branch review found the real Ouroboros FSM **branches by risk tier**: auto-apply ops (SAFE_AUTO/NOTIFY_APPLY) run Gate ①+② → auto-**commit** (no PR); Orange ops (APPROVAL_REQUIRED) hit `create_review_pr` (Gate ③ + enforcer) on a branch that runs *before* APPLY — so no single op assembled all three tokens, and an armed enforcer would refuse every autonomous PR.

**Resolution (Tasks 12, 13a, 13b, 14):**
- **Branch-bound tokens** (`dag_capability_token`): the HMAC binds a `branch_context`; `verify_chain` rejects a chain whose tokens were minted in different contexts (anti-injection from concurrent workspaces).
- **`autonomous_pr_pipeline.run_pr_gate_pipeline`**: on the autonomous-PR path, materializes the candidate in an **isolated git worktree** (`ouroboros/a1-validate/<op_id>`), runs Gate ① (container exec) + Gate ② (blast radius) **there** (real tree untouched), mints sandbox+blast bound to the worktree branch, cleans up always. `create_review_pr` then mints lint, verifies the full chain, and asserts the tokens were minted in the expected worktree context.
- **Hardening**: Docker pre-flight gated on `JARVIS_A1_TOKEN_ENFORCER_ENABLED` (true byte-identical OFF); Gate ② strict catch-all → tree-SHA rollback on any crash; tuning flags registered.

**Net:** one autonomous op now survives the container, clears the blast radius, and passes the linter before its PR — the spec's single mandatory path, realized against the real FSM. All gates remain default-OFF.

**Graduation blocker (tracked):** 4 non-autonomous `create_review_pr` callers (strategy_simulator, genesis_proposal, graduation_pr_proposer, bridge_adapters) don't pass tokens → refuse under enforcer-ON; reconcile (token-wire or CLI-approval exemption) before flipping `JARVIS_A1_TOKEN_ENFORCER_ENABLED` default-ON. Live container A1 soak is operator-run.
