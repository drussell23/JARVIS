---
title: Before
modules: [tests/test_smoke.py, backend/core/ouroboros/governance/swe_bench_pro/per_problem_harness.py, tests/governance/test_swe_bench_pro_per_problem_harness.py, docs/architecture/OUROBOROS_VENOM_PRD.md]
status: historical
source: project_v3_7_clone_template_bypass.md
---

May 12 2026 — `git clone --template=` substrate fix shipped on branch `ouroboros/swe-bench-pro/clone-template-bypass`.

## Root problem solved at source

After the loader enumeration union fix landed (commit `27fc2290e9`), re-running the focused validator with REAL `prepare_problem` (no stub) surfaced a new failure mode:

```
fatal: cannot copy '/opt/homebrew/opt/git/share/git-core/templates/hooks/commit-msg.sample'
       to '/Users/djrussell23/Doc[...]'
```

git was copying template hooks from the global templates directory into every fresh clone's `.git/hooks/`. Two problems:

1. **Wrong for the use case**: SWE-Bench-Pro clones are benchmark eval substrates, not contributor checkouts. The `commit-msg.sample`, `pre-commit.sample`, etc. hooks are user-facing automation that has no place in a benchmark workflow.
2. **Breaks in restricted environments**: anywhere the global templates dir is non-writable (sandboxes, locked-down developer machines, etc.), the clone fails outright.

## The fix is a single-line structural addition

```python
# Before
["clone", "--filter=blob:none", repo_url, str(target)]

# After
[
    "clone",
    "--filter=blob:none",
    "--template=",  # AST-pinned: benchmark cleanliness
    repo_url,
    str(target),
]
```

`--template=` (empty string) disables template-hook copying. Same shape + intent as the existing `--filter=blob:none` flag (which is also there for benchmark cleanliness — partial clone for speed/size).

## Composition discipline

- **No new helpers**: single-line change to existing args list in `_ensure_repo_cached`
- **No new behavior surfaces**: the flag is a configuration choice for the existing clone path
- **No env var**: this is the RIGHT behavior for SWE-Bench-Pro clones unconditionally; an env knob would imply "sometimes you want template hooks in benchmark clones" which is never true
- **AST pin enforces non-drift**: `test_ast_pin_clone_invocation_disables_template_hooks` walks the `_ensure_repo_cached` AST + asserts `--template=` appears in the unparsed args list. Diagnostic cites both the sandbox failure mode and the benchmark-cleanliness rationale.

## Validator confirmation

Re-ran the focused validator with REAL `prepare_problem`:

```
verdict: injected
elapsed: 0.97s
envelopes: 1
envelope.source: swe_bench_pro
envelope.target_files: ('tests/test_smoke.py',)
envelope.evidence.repo_root: $TMPDIR/swebp-validator-real/worktrees/jarvis__harness-smoke-001
worktree exists: True
worktree contents: ['.git', 'README', 'tests']
test_patch applied: def test_smoke_noop():
    """Trivially-passing wiring-validation test."""
    assert True
```

End-to-end real-git proof:
- ✅ Real `git clone` of `octocat/Hello-World` (filter=blob:none + no template hooks)
- ✅ Real `git worktree add -b swebp/jarvis__harness-smoke-001 ... 7fd1a60b...` (real base commit)
- ✅ Real `git apply --index` of test_patch (file `tests/test_smoke.py` actually written)
- ✅ Real `build_evaluation_envelope` with `target_files` parsed from the patch
- ✅ `verdict=INJECTED` total elapsed 0.97 sec

## Operator runbook addition (restricted environments)

The sandbox in which this PR was developed blocks writes to `.git/config` paths anywhere under JARVIS-AI-Agent (deny-within-allow pattern matches nested git configs). For operators in similarly restricted environments, the EXISTING env knobs already solve this:

```bash
export JARVIS_SWE_BENCH_PRO_REPO_CACHE_PATH="$TMPDIR/swebp-cache"
export JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH="$TMPDIR/swebp-worktrees"
```

No code change needed — these knobs already existed (they're documented in `harness_inject.py`'s register_flags). PRD §40.7.10-template now documents them for the restricted-env case.

## Files

- `backend/core/ouroboros/governance/swe_bench_pro/per_problem_harness.py` — `--template=` added to `_ensure_repo_cached` clone args + inline rationale comment
- `tests/governance/test_swe_bench_pro_per_problem_harness.py` — `test_ast_pin_clone_invocation_disables_template_hooks` pin
- `docs/architecture/OUROBOROS_VENOM_PRD.md` — §40.7.10-template closure paragraph + operator-runbook addition

## What's next

**Live harness run** with the substrate fix in place. Then stage 2 (real HF benchmark cherry-pick) if the live run lands clean.
