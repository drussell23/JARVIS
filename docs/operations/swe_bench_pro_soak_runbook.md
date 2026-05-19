# SWE-Bench-Pro Soak Runbook — putting Ouroboros+Venom to the test

**This is a RUN/soak, not a build.** The `swe_bench_pro/` package
(Phases A→F) is CLOSED & soak-ready. No new modules pre-soak. A
soak-surfaced bug is a *separate triage arc* (precedent:
loader-enumeration-union / clone-template-bypass). Operator approves
spend per phase. No pre-result euphoria: methodology-validation ≠
capability measurement; claims move only on the rendered
`report_card` + `results.jsonl` + session `debug.log`.

Driver: `scripts/swe_bench_pro_soak.sh` (composes
`scripts/ouroboros_battle_test.py` — it does **not** reimplement the
cost/wall/idle/headless caps; it passes them).

---

## Pre-soak operator checklist (run before ANY spend)

1. `git fetch`; on `main@9a4baff258` or later; **clean index** (no
   "7856 staged deletions" illusion — if seen, `git reset` first;
   see `operator_commit_authority.md`).
2. OCA presence+grant active for this branch:
   `python3 -m backend.core.ouroboros.governance.commit_authority_cli grant --channel ide --branch "$(git branch --show-current)" --minutes 120`
   (or daemon `refresh`). **Cursor background Agents STOPPED on the
   repo root** (cursor-agent-git-ban rule is advisory only).
3. Phase-3 only: `huggingface-cli whoami` succeeds OR `HF_TOKEN`
   exported (HF dataset access for real instance picks).
4. Phase-3 only: `unset JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH`
   (real run reads the HF source, not the wiring fixture).

---

## Phase 0 — rails (no spend)

Restricted-env knobs are **mandatory** (sandbox blocks `.git/config`
under the repo root): cache + worktree base point at `$TMPDIR`.
Caps are **mandatory** (retry storms defeat `--idle-timeout`, so a
hard wall-clock ceiling is required):

| Cap | Value | Why |
|---|---|---|
| `--cost-cap` | `2.00` | operator-bound USD ceiling |
| `--max-wall-seconds` | `2400` | hard wall ceiling (retry-storm-proof) |
| `--idle-timeout` | `1800` | per-op liveness |
| `--headless` | (set) | agent-conducted; no interactive REPL |

Masters stay default-FALSE globally; the soak bundle is **single
session, never persisted**. Only the flags listed per phase are set.

## Phase 1 — wiring dry-run (~$0.01–0.10, operator runs locally)

```
bash scripts/swe_bench_pro_soak.sh phase1
```

Runs the checked-in fixture `jarvis__harness-smoke-001`
(octocat/Hello-World, trivially-passing `test_patch`) end-to-end:
loader → prepare → envelope → intake → evaluator → scorer →
result_store → report_card.

**Proof of wiring (the gate to proceed):**
- `.jarvis/swe_bench_pro/results.jsonl` has a row with
  `envelope.source == "swe_bench_pro"` and a terminal outcome.
- session `debug.log` (`.ouroboros/sessions/<id>/`) contains
  `swe_bench_pro` injection lines.

**STOP and report. Do not proceed to Phase 3 without explicit
"Phase 3 go" + confirmed instance ids.**

## Phase 2 — selection (no spend)

`geometric_sampler` (composed, not reinvented) proposes stratified
**known-good** (small, single-file-ish → expected RESOLVED) and
**known-hard** (multi-file/architectural → expected UNRESOLVED)
candidates from HF metadata. 5–10 ids presented with rationale
(file count / patch size / repo). **Operator picks the final set.**
No instance ids are hardcoded in repo code or this runbook — the
rubric sanity floor requires *both poles*, but the specific ids are
operator-selected from the dataset.

## Phase 3 — soak (ONLY after "Phase 3 go" + confirmed ids)

```
SWEBP_INSTANCE_IDS="<id1>,<id2>,..." \
  bash scripts/swe_bench_pro_soak.sh phase3 --confirm-spend
```

The script **refuses** to run Phase 3 without both `SWEBP_INSTANCE_IDS`
and `--confirm-spend` (anti-accidental-spend). Default **Path B**
(harness-inject + full autonomous GENERATE→APPLY→VERIFY — the truer
"put O+V to the test"); `SWEBP_PATH=A` selects the cheaper
`parallel_evaluate` rubric-only path. `SCORE_REJECT_TEST_MODS=true`
(cheat-detection on) + R2 rubric soak profile (serial / high-urgency
/ sensor-throttled / adaptive). One session, $2.00 cap, **no auto
re-spend**.

## Phase 4 — verdict ($0)

Source of truth: `.jarvis/swe_bench_pro/results.jsonl` (Phase D
store) + session `debug.log`. **`summary.json` has a known
`attempted` counter bug — do not trust it alone.** Render
`report_card.build_report_card(store)` → Markdown/JSON:
per-instance ScoreOutcome + EvaluationOutcome + pass_rate (excludes
SKIPPED).

- **Provider EXHAUSTION / timeout → INCONCLUSIVE in the report
  card, NEVER FAIL.** Infra failure ≠ capability failure (no
  euphoria in either direction).

## Phase 5 — graduation ($0)

Rubric sanity floor **MET iff: ≥1 known-good → RESOLVED AND ≥1
known-hard → UNRESOLVED**. Floor MET validates rubric+wiring, **not
capability** (capability = resolve-rate over a representative
sample — a separate, larger, later effort). Document the evidence
row for PRD §41.6. **Masters stay default-FALSE; the soak never
auto-graduates a flag** — flipping `JARVIS_SWE_BENCH_PRO_ENABLED`
toward default-TRUE is a separate, operator-authorized graduation
PR.

---

## Non-goals

No new `swe_bench_pro` modules pre-soak. No duplication of
`battle_test` caps (composed via flags). No conflation with OCA /
sovereignty / git-index (CLOSED — untouched unless soak triage
finds a regression). No Phase-3 run without operator spend
approval. No capability claim from a 5–10-problem rubric-floor run.
