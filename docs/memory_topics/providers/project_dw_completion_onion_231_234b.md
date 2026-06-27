---
title: DW Autonomy Completion Arc — Slices 231–234b (banked 2026-06-12)
modules: []
status: historical
source: project_dw_completion_onion_231_234b.md
---

# DW Autonomy Completion Arc — Slices 231–234b (banked 2026-06-12)

The arc that finally moved GOAL-001 (the operator-signed roadmap goal: "eradicate
cold-start GIL starvation", target_files `semantic_index.py` + `goal_inference.py`,
NOTIFY_APPLY, blast_radius 2) from "dies at GENERATE budget starvation
(`deadline_exhausted_pre_fallback ×23`)" to "generates an APPROVED multi-file patch
on DW, passes review + gate, APPLY path resolves correctly". **The originally-reported
root cause (budget starvation) is SOLVED and live-proven.** Branch: `perf/slice-233-schema-masking`
(carries all five). NOT merged to main yet.

## The onion — five layers, each exposed by a live soak, each fixed at its real layer

| # | Blocker | Root fix | Commit |
|---|---------|----------|--------|
| 1 | IMMEDIATE route → static budget `{tier0:0, reserve:0, max_dw_wait:0}` → DW (the funded primary) gets ZERO window → `deadline_exhausted_pre_fallback ×23` | **Slice 231** Telemetry-Driven Budget Synthesizer: `urgency_router.synthesize_budget_profile` + `synthesize_generation_timeout` + `context_budget_profile` seam; `provider_availability.collect_provider_availability` reads breaker/DW-health READ-ONLY (never `should_allow_request` — Slice-162 probe trap). Master `JARVIS_BUDGET_SYNTHESIS_ENABLED` default-TRUE, OFF byte-identical. | `a301c38941` (+spec `d20f628508`) |
| 2 | DW reroute severed at 30s reflex cap after the Claude breaker tripped | Slice 231 + the EXISTING Slice 225 DW autarky (grants DW the full ~146s remaining budget on breaker-OPEN) | (composes) |
| 3 | Cold-start TIMING gap: breaker boots CLOSED → at the pre-dispatch lift decision claude looks available → lift never fires (`gen_timeout lifted=0`); breaker only trips mid-GENERATE on the first 402 | **Slice 232** cold-start funding signal: collector derives Claude-funding from the AUTHORITATIVE persisted breaker state at op #1 (reuses `claude_circuit_breaker._read_breaker_state`, TTL-aware, fail-soft), INDEPENDENT of the persist ENABLE flag; AND the economic-record WRITE (`_maybe_persist_open`/`_maybe_clear_persisted`) made flag-independent so the signal stays fresh (auto-boot-OPEN restore stays gated per §33.1). New reason `breaker_open_economic_persisted`. | `f608cb152e` |
| 4 | Convergence machinery (Slice 85 cumulative + Slice 3E nudge) FIRES but is ADVISORY — model can keep emitting read-only tools on the grace round, loop returns patch-less raw, op fails on exploration | **Slice 233** parse-gate enforcement: `tool_executor._slice233_enforcement_action` — once `_final_nudge_issued`, a PURE read-only round is structurally REJECTED (reuses `_READONLY_EXPLORATION_TOOLS`), model gets a hard denial via the proven `current_prompt`-append+continue path, bounded by `_enforcement_rounds_cap` (env `JARVIS_TOOL_LOOP_ENFORCEMENT_ROUNDS`, default 2) AND remaining budget. Valid patch (non-tool response) honored upstream at `parse_fn=None`. NOTE: NOT exercised in soaks — DW generated cleanly without wandering; machinery armed for when needed. | `46192673f2` |
| 5 | APPLY wrote to a HOST-absolute path inside the `/app` container → `[Errno 2] No such file: '/Users/.../semantic_index.py'` | **Slice 234** `harness._resolve_runtime_repo_root` (honor `JARVIS_REPO_PATH` only if valid in THIS runtime, else derive from code on-disk location, else `.git`, else FAIL LOUD) — fixed the harness `repo_path`→envelope `repo_root` source. **Slice 234b** (config): re-soak proved 234 necessary-not-sufficient because `JARVIS_REPO_PATH` is STILL the host path in the container (`env_file: .env` leaks it) and ~8 modules read it DIRECTLY; compose `environment:` now sets `JARVIS_REPO_PATH=/app` (+sibling reactor/prime), winning over env_file. | `d86ffe0143`, `98b69cbcf6` |

## Live-soak evidence the budget root cause is CLOSED (jarvis-dw-cortex-soak, attested commit)

- GOAL-001 routes `immediate (critical_urgency:roadmap)` with **`budget_profile {tier0:1.0, reserve:0, max_dw_wait:180.0}`** (was `{0,0,0}`).
- `[BudgetSynth] route=immediate claude=unavailable:breaker_open_economic_persisted dw=healthy tool_loop=True → dw_wait=180.0s` — Slice 232 persisted signal firing.
- DW primary `Primary sem release ... outcome=ok` (×9) — DW autonomously GENERATES the patch.
- `[REVIEW-SHADOW] aggregate=APPROVE files_reviewed=5 approved=5`; `GATE: NOTIFY_APPLY → APPROVAL_REQUIRED → Orange PR`.
- After Slice 234b: **`errno2=0`** — zero host-path APPLY failures. Path bug GONE.
- Tests: 74 green (231:45, 232:11, 233:11, 234:7). No new regressions (the 6 pre-existing failures — `test_snapshot_shape` S147 drift, `test_primary_budget_tier3_cap`/`test_tool_loop_budget_plan` starvation drift — fail identically with changes stashed).

## Tooling/methodology that worked (reuse next session)

- The soak that actually exercises GOAL-001 is `docker-compose.dw-cortex-soak.yml` via `soak_git_entrypoint.sh` (runs the battle test directly, no host-Docker; `COPY . /app` = local branch code). NOT the generic `launch_docker_soak.sh` (host-Docker entrypoint, crashloops in-container).
- Launch: `export GIT_COMMIT=<short> GIT_DIRTY=false JARVIS_ATTESTATION_EXPECTED_COMMIT=<short> SOAK_REQUIREMENTS=requirements-soak-oracle.txt; docker compose -f docker-compose.dw-cortex-soak.yml up -d --build`. Attestation (`JARVIS_RUNTIME_ATTESTATION_ENABLED=1`) fail-closes on UNSTAMPED/strict — pin the commit. `.dockerignore` excludes `.jarvis`/`.pyc`/`.git` so `dirty=false` is honest.
- To make the lift fire from op #1: refresh `.jarvis/claude_breaker_state.json` to fresh-OPEN (Claude IS unfunded). The container reads `.jarvis` (JARVIS_STATE_DIR unset → defaults `.jarvis`); the HOST shell has `JARVIS_STATE_DIR=.ouroboros/state` so write with explicit `path='.jarvis/claude_breaker_state.json'`.
- All docker commands need `dangerouslyDisableSandbox` (socket outside sandbox).

## LAYER 5 — NEXT SESSION'S FIRST TASK (diagnosis only; do NOT fix tired/blind)

**Symptom:** GOAL-001 now fails at GENERATE-parse, not APPLY:
`[doubleword] JSON parse failed op=... JSONDecodeError: Expecting ',' delimiter`.
The DW model emits the FULL multi-file rewrite — `semantic_index.py` as a single
~60K-char JSON `full_content` field — and the JSON is malformed. Evidence:
`error_pos=50452 raw=61303` and `error_pos=51421 raw=60394` (error MID-content at
~50K of ~60K; `raw==extracted` = no extraction-layer truncation).

**Two hypotheses with DIFFERENT root fixes — frame before any code (operator: guessing wrong = wasted slice):**
1. **max_tokens ceiling.** `doubleword_provider._DW_MAX_TOKENS=16384` (RT default + complex/heavy_code). 60K chars ≈ 15–20K tokens — right at/over the ceiling → response truncated mid-JSON → unclosed structure. Check: does the truncation point correlate with 16384 tokens? Is the JSON unterminated at the END (vs a mid-string escaping break)? `error_pos < raw` argues somewhat AGAINST pure end-truncation.
2. **JSON assembly/escaping.** The 60K `full_content` contains unescaped quotes/newlines/delimiters that break the JSON structure mid-content. "Expecting ',' delimiter" mid-content supports this.

**The deeper architectural answer (operator's steer — almost certainly NOT "raise max_tokens", a brute-force ceiling-move):** a robust system should NOT emit an entire 60K-char file as one JSON `full_content` blob. Investigate whether the EXISTING patch/diff machinery can emit CHANGES instead of whole-file rewrites — reuse what's there:
- candidate schema is `2b.1` with `(file_path, full_content)` pairs (`candidate_generator.py:~2390`).
- BUT diff-handling failure classes already exist: `diff_apply_failed`, `stale_diff`, `validate_diff` (`candidate_generator.py:1177-1185`) → some diff/hunk path may already exist to reuse rather than build new.
- Question to answer first: does the candidate schema / ChangeEngine support a diff/hunk emit mode, and can the GENERATE prompt steer DW to emit a unified diff for large existing-file edits instead of full_content? That bounds output size structurally (the real fix), independent of max_tokens.

### LAYER-5 → Slice 235 ADAPTIVE DIFF: AUDIT + FULL WIRING COMPLETE (2026-06-13), NOT YET SOAK-VALIDATED. Branch `perf/slice-235-adaptive-diff` (committed 9577d608d0 keystone + the wiring commit).
WIRED (all committed, 19 TDD tests green, slice208 + op_context replace green): keystone `should_force_full_content`/`_diff_schema_threshold_lines` (env `JARVIS_DIFF_SCHEMA_THRESHOLD_LINES`=800) + seam `resolve_diff_capability_for_model` (env `JARVIS_DW_DIFF_CAPABLE_FAMILIES`=moonshotai,deepseek-ai,zai-org; 397B EXCLUDED; reuses dw_catalog_client.parse_family) / `_max_target_line_count` / `resolve_force_full_content` — all in providers.py. Wired into doubleword_provider.py:1583 (was unconditional True) + providers.py Claude block (the computed-but-IGNORED _force_full now USED on both lean+full prompt builds). FAIL-SAFE: `op_context.force_full_content_override` (on OperationContext @881, carried by advance/replace) + orchestrator GENERATE_RETRY governor (diff_apply_failed/stale_diff/validate_diff → set override + 'emit full file' feedback → retry degrades to full_content, never crash/loop). KEY: elite DW models are CATALOG models (NOT brain_selection_policy.yaml brains) → capability via env families. NEXT: rebuild dw-cortex soak + validate the open empirical Q (do elite models clear the verbatim-diff wall the 397B couldn't; fail-safe makes it safe to test). Original audit detail below (the mapped plan, now executed):
### (audit detail) Slice 235 = ADAPTIVE DIFF (re-enable existing 2b.1-diff).
The full diff machinery ALREADY EXISTS and is dead-coded OFF. Do NOT build edits[]/a new parser (rejected — would duplicate + hit the same verbatim wall). What exists:
- `providers.py`: `_SCHEMA_VERSION_DIFF="2b.1-diff"` (:219), `_DIFF_CANDIDATE_KEYS={...,"unified_diff",...}` (:222), `_apply_unified_diff(original,diff_text)` (:1488), `validate_diff_context()`+`StaleDiffError` (:1343/1373). Full applier+validator.
- schema_capability plumbing is COMPLETE end-to-end: `brain_selector.py` per-brain cfg (`schema_capability` "full_content_only" | "full_content_and_diff"; :434/447/554 — ONE brain already has full_content_and_diff) → `governed_loop_service.py:2800` → `routing_intent.schema_capability` → provider `:4884`.
- THE LAYER-5 BUG (root): `providers.py:4884-4893` COMPUTES `_force_full` from schema_capability, then the prompt builders are passed HARDCODED `force_full_content=True` (:4912/:4926) — the computed value is IGNORED. `doubleword_provider.py:1583` forces True UNCONDITIONALLY ("the 397B can't reliably produce verbatim context lines for unified diffs"). So 2b.1-diff is off for EVERYONE → DW emits 60K-char full_content → JSONDecodeError.
SLICE 235 PLAN (clean reuse, ~4 surfaces; the riskiest = generation core, so TDD + soak carefully):
1. Pure keystone fn `should_force_full_content(schema_capability, target_line_count, threshold_lines)` → force full UNLESS (schema_capability=="full_content_and_diff" AND file_lines>threshold). Small files stay full_content (diffs add no value; blob problem only hits large files). Threshold env-tunable (`JARVIS_DIFF_SCHEMA_THRESHOLD_LINES`, ~800 default). Both providers call it.
2. providers.py: replace the hardcoded `force_full_content=True` (:4912/:4926) with the keystone (USE the computed value). doubleword_provider.py:1583: same — derive from schema_capability+size instead of unconditional True.
3. CONFIG (not code, honors "no hardcoded models"): set `schema_capability: full_content_and_diff` for the ELITE agentic brains in `brain_selection_policy.yaml` (Kimi-K2.6 / DeepSeek-V4-Pro / GLM-5.1 etc.). Verify which brains the elite pool uses.
4. FAIL-SAFE degradation governor: on `diff_apply_failed`/`StaleDiffError` (orchestrator already has an `elif "diff_apply_failed" in _err_str` at :6245 — extend it), flag the op `force_full_content=True` for the retry and fall back to full_content. Never crash.
OPEN EMPIRICAL QUESTION (the reason 2b.1-diff was disabled): can the 2026 elite agentic models (Kimi/DeepSeek-V4-Pro/GLM) reliably emit verbatim unified-diff context where the 397B couldn't? The fail-safe (#4) makes this safe to TEST in soak — if they can't, it degrades to full_content cleanly. Validate empirically.

## TWO TRACKED FOLLOW-UPS (carry forward — don't lose)

1. **gen_timeout-lift wiring (S231 lever).** `synthesize_generation_timeout` (the orchestrator 120→240s lift at `orchestrator.py:~4437`) fired **0 times** in soak — `adaptive gen budget: 120s→146s` shows it didn't apply (claude_available likely flipped True at the GENERATE-phase collect, or the lift value got overwritten by the adaptive path, or a swallowed exception). NOT the current bottleneck (DW autarky already grants ~146s and DW generates), but the S231 lever should fire as designed rather than being masked by autarky. Own slice.
2. **Canonical repo-root resolver refactor.** Promote `harness._resolve_runtime_repo_root` (S234) to a SHARED governance module and route ALL ~8 direct `os.environ.get("JARVIS_REPO_PATH")` readers through it (validate vs runtime FS, fail loud): `auto_action_router.py:1193`, `memory_crystallization.py:265`, `hybrid_teammate_executor.py:73`, `agent_memory.py:641`, `graduation/live_fire_soak.py:1479`, `github_issue_sensor.py:151`, integration.py, mcp_server.py. Makes the code context-independent of the env (234b's compose override is the deployment stopgap, not the durable fix). TDD: container/host/unresolvable. Own slice.

See [[project_slice131_cost_sovereign]].
