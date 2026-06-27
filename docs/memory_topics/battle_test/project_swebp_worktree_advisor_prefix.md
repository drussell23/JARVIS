---
title: Project Swebp Worktree Advisor Prefix
modules: []
status: historical
source: project_swebp_worktree_advisor_prefix.md
---

Step 6 P2 $2 live-fire (session bt-2026-05-17-002318) produced NO valid rubric signal because `JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH` / `JARVIS_SWE_BENCH_PRO_REPO_CACHE_PATH` were pointed at `$TMPDIR`.

`operation_advisor.resolve_envelope_repo_root` (operation_advisor.py:583,600) anchors its allowed-prefix on the JARVIS **repo root**. Worktrees under `$TMPDIR` are rejected ("outside N allowed prefix(es)") → the benchmark `repo_root` is dropped → the model edits the JARVIS repo instead of the benchmark worktree → Advisor BLOCKs (blast radius) + Iron Gate blocks auto-commit. Defenses hold; the experiment is destroyed.

**Why:** the TMPDIR runbook (memory v3.7 notes) is for *sandbox-restricted* environments where `.git/config` writes under repo root are blocked. It is NOT for sandbox-OFF runs — there it actively breaks evaluation by escaping the advisor anchor.

**How to apply:** For sandbox-OFF SWE-Bench-Pro runs, do NOT override `WORKTREE_BASE_PATH`/`REPO_CACHE_PATH`. Let them default under the repo root (advisor-allowed by design; `WorktreeManager.reap_orphans` keeps it clean). Only use the TMPDIR knobs when the sandbox is ON and blocking repo-root `.git` writes — and then expect the advisor-prefix conflict and resolve it deliberately, never by widening the advisor allowlist to `/tmp` (weakens a security guard). The preflight gate shipped in 27ef2efbca worked correctly in this run (verdict=proceed). See [[project-v3-7-phase-2-harness-inject]].
