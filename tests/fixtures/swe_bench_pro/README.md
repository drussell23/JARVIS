# SWE-Bench-Pro test fixtures

This directory holds **wiring-validation fixtures** for the
SWE-Bench-Pro arc (Phases A → F). These are NOT real benchmark
problems — they exist so the harness boot hook
(`backend/core/ouroboros/governance/swe_bench_pro/harness_inject.py`)
has at least one cached ProblemSpec to lift through Phase A loader
→ B.1 prepare → B.2.1 envelope → canonical intake at boot time.

## Files

### `problems.jsonl`

One ProblemSpec per line, JSON-encoded. The Phase A dataset_loader
reads this when `JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH` points
here.

The shipped record `jarvis__harness-smoke-001` targets a stable
public upstream repo (`octocat/Hello-World`) at its canonical first
commit. The `test_patch` adds a trivially-passing `test_smoke_noop`
so even a no-op model scores PASS — this fixture validates the
harness wiring, **NOT** the model's solving ability.

## Usage

### Local smoke (no LLM tokens; CI-safe)

```python
import os
os.environ["JARVIS_SWE_BENCH_PRO_ENABLED"] = "true"
os.environ["JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH"] = (
    "tests/fixtures/swe_bench_pro/problems.jsonl"
)
# spine tests stub prepare_problem so no git clone happens
```

### Live harness run (real O+V, real git clone, real tokens)

```bash
export JARVIS_SWE_BENCH_PRO_ENABLED=true
export JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED=true
export JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH=tests/fixtures/swe_bench_pro/problems.jsonl
export JARVIS_SWE_BENCH_PRO_INJECT_COUNT=1

# Recommended: also enable result persistence + lifecycle SSE for full observability
export JARVIS_SWE_BENCH_PRO_RESULT_PERSISTENCE_ENABLED=true
export JARVIS_OP_LIFECYCLE_SSE_ENABLED=true

python3 scripts/ouroboros_battle_test.py --cost-cap 0.50 --idle-timeout 1800 -v
```

**Cost ceiling**: the operator-bound budget for the first live run
is $2.00 max for 5–10 problems. This fixture's single trivially-
passing problem should cost ~$0.01–0.10 depending on whether the
model over-engineers.

### Real upstream benchmark problems

For the actual SWE-Bench-Pro benchmark (1865 problems across 41
repos), configure the upstream HuggingFace fetch path:

```bash
export JARVIS_SWE_BENCH_PRO_HF_DATASET=princeton-nlp/SWE-bench-Pro  # or equivalent
export HUGGINGFACE_HUB_TOKEN=<your-token>
# Optionally cherry-pick problems first to keep costs bounded
export JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS=astropy__astropy-12345,sympy__sympy-67890
```

See PRD §40.7.10-arc "Soak-readiness checklist" for the full
operator runbook.

## Spine tests

Regression tests at
`tests/governance/test_swe_bench_pro_harness_inject.py` exercise
all 5 `SWEBenchProInjectionVerdict` outcomes via stubs — no network,
no git, no LLM tokens. CI runs these on every commit.
