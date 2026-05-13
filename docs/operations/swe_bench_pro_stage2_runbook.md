# SWE-Bench-Pro Stage 2 Runbook — HuggingFace dataset dry-run

**Owner:** Derek J. Russell
**Status:** Stage 2 prep (non-blocking on Stage 1.6 graduation soak)
**Cost ceiling:** Operator-bound, ~$1.00 per 1–3 instance dry-run
**Created:** 2026-05-13

---

## What this is

A controlled cherry-pick of 1–3 real SWE-Bench-Pro instances from the
HuggingFace dataset, exercising the full Phase A→F SWE-Bench-Pro
pipeline against canonical benchmark traffic.  This is **wiring
validation**, not the graduation soak — the rubric criterion for full
masters default-TRUE remains "≥1 RESOLVED known-good + ≥1 UNRESOLVED
known-hard" (set in `memory/project_v3_7_phase_2_harness_inject.md`).

This runbook does **not** replace the Stage 1.6 Slice 3 motor-arc
graduation soak.  Stage 1.6's Bar A is about BG-slot release; Stage 2
is about benchmark substrate correctness.  Orthogonal.

## Pre-flight checklist

- [ ] `huggingface-cli login` (or `HF_TOKEN` env var) — the dataset is
      gated; without auth the loader returns `None` and the harness
      hook emits `verdict=skipped_no_problems`.
- [ ] `pip install datasets huggingface_hub` (loader uses
      `datasets.load_dataset` at line 533 of `dataset_loader.py`).
- [ ] Verify `backend/.env` has cost-cap floor ≥ $1.00 (sandbox).
- [ ] Confirm `tests/fixtures/swe_bench_pro/problems.jsonl` is NOT
      pointed-to by `JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH` — for
      Stage 2 we want the HF source, not the trivial fixture.

## Instance picks (canonical SWE-Bench-Pro examples)

Three instances spanning easy/medium/hard difficulty.  Operator may
substitute via `JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS`:

| instance_id | difficulty | rationale |
|---|---|---|
| (TBD by operator) | easy | quick smoke — proves prepare→generate→apply path |
| (TBD by operator) | medium | exercises Iron Gate retry + Venom tool rounds |
| (TBD by operator) | hard | rubric sanity — should fail correctly with cheat-detect on |

Reasoning for letting the operator pick: instance choice depends on
which repos they have HF auth + clone access to, which evolves with
benchmark releases.  The harness substrate (Phase A→F) handles
arbitrary instance_ids uniformly — there is no code path that needs
to know "which 3" we picked.

## Launch script (template)

Create `/tmp/claude-501/stage2-hf-dry-run.sh`:

```bash
#!/bin/bash
set -e
cd ~/Documents/repos/JARVIS-AI-Agent
set -a
source backend/.env
set +a

# === HF dataset source (NOT the local fixture) ===
unset JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH
export JARVIS_SWE_BENCH_PRO_HF_DATASET="princeton-nlp/SWE-Bench-Pro"  # or operator's choice
export JARVIS_SWE_BENCH_PRO_HF_SPLIT="test"

# === Inject specific instance IDs (cherry-pick) ===
export JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS="instance-1,instance-2,instance-3"

# === Substrate masters ===
export JARVIS_SWE_BENCH_PRO_ENABLED=true
export JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED=true
export JARVIS_SWE_BENCH_PRO_RESULT_PERSISTENCE_ENABLED=true

# === Cache + worktree paths ===
export JARVIS_SWE_BENCH_PRO_REPO_CACHE_PATH="$TMPDIR/swebp-cache-stage2"
export JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH="$TMPDIR/swebp-worktrees-stage2"
export JARVIS_ADVISOR_WORKTREE_ROOT_ALLOWLIST="$TMPDIR/swebp-worktrees-stage2"

# === Optional: include Stage 1.6 park substrate if graduated ===
# export JARVIS_BG_PARK_ENABLED=true

rm -rf "$TMPDIR/swebp-cache-stage2" "$TMPDIR/swebp-worktrees-stage2" 2>/dev/null || true

echo "=== Stage 2 dry-run launching ==="
caffeinate -dimsu python3 scripts/ouroboros_battle_test.py \
    --cost-cap 1.00 \
    --idle-timeout 300 \
    --max-wall-seconds 3600 \
    --headless \
    -v
```

## Proof bundle (Stage 2-specific)

After completion, run:

```bash
# Reuse v14-proof-bundle.sh signal-grep shape, then add:
SESSION=$(ls -1dt .ouroboros/sessions/bt-* | head -1)
cat "$SESSION/summary.json" | jq '.ops[] | select(.signal_source == "swe_bench_pro")'
```

Expected for **wiring validation** (NOT rubric sanity):

- ≥1 SWE-Bench-Pro envelope ingested per instance_id
- ≥1 ProblemSpec successfully loaded from HF
- ≥1 worktree prepared at the base_commit
- ≥1 GENERATE → APPLY → VERIFY phase progression
- ≥1 EvaluationResult recorded in
  `.jarvis/swe_bench_pro/results.jsonl`
- ≥1 ScoringResult (PASS/PARTIAL/FAIL) per problem (Phase C)

## Failure-mode triage (Stage 2-specific)

| Symptom | Most likely cause | First check |
|---|---|---|
| `verdict=skipped_no_problems` | HF auth missing or instance_id CSV empty | `huggingface-cli whoami` |
| Phase B.1 `PrepareOutcome.CLONE_FAILED` | base_commit gone from upstream repo | `git ls-remote <repo> <base_commit>` |
| Phase C `SCORE_REJECT_TEST_MODS` | candidate touched test files | inspect captured_patch — rubric integrity |
| `EvaluationOutcome.TERMINAL_TIMEOUT` | 1800s eval timeout hit | check JARVIS_SWE_BENCH_PRO_EVAL_TIMEOUT_S |
| All ops `INJECTED` but 0 RESOLVED | Phase 2 substrate green but model can't solve | rubric-correct — expected for hard instances |

## Cost discipline

Stage 2 stays under operator's $1.00 cap.  Each instance is
approximately:

- 1× ProblemSpec load: free (HF API)
- 1× clone + worktree prepare: free (local git)
- 1× Ouroboros pipeline (CLASSIFY → … → COMPLETE):
  - DW Tier 0: ~$0.005–0.030 (BACKGROUND route, 397B input + 16K out)
  - Claude fallback (if DW fails): ~$0.05–0.30 (Sonnet inputs)
- 1× scoring run: free (local pytest)

Worst case 3 instances × ~$0.30 = ~$0.90 — under the cap.

## What this runbook DOES NOT do

- ❌ Flip masters to default-TRUE (that requires the rubric criterion:
  ≥1 RESOLVED + ≥1 UNRESOLVED known-hard, not just "wiring works").
- ❌ Replace Stage 1.6 Slice 3 graduation evidence.  Park substrate
  graduation is orthogonal.
- ❌ Generate competitive SWE-Bench-Pro leaderboard submissions.

## Cross-references

- Stage 1.6 graduation: `memory/project_stage_1_6_park_spike.md`
- SWE-Bench-Pro arc closure: `memory/project_v3_7_phase_f_report_card.md`
- Harness inject substrate: `memory/project_v3_7_phase_2_harness_inject.md`
- v14 proof-bundle: `/tmp/claude-501/v14-proof-bundle.sh` (Stage 1.6
  pattern — adapt for Stage 2-specific signals as noted above)
