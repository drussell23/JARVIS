---
title: Project Cursor Agent Git Ban Arc
modules: [scripts/verify_oca.py]
status: merged
source: project_cursor_agent_git_ban_arc.md
---

**`.cursor/rules` Agent git-write ban — CLOSED 2026-05-19 (PR #42980 merged → main 9a4baff258).** NEW arc, not OCA Slice N (OCA Slices 1→4 + git_index_guard + persistent_master + harness_sovereignty_pin also CLOSED — do not reopen). Shipped: `.cursor/rules/no-agent-git-write.mdc` (alwaysApply:true) + `cursor_rule_guard.py` (auto-discovered un-deletable AST pin: RED if rule deleted/empty/inactive/gutted) + `agent_fingerprint.py` (ADAPTIVE detector: flexible integrity-trailer regex + weighted multi-signal LLM-prose scorer, env-tunable JARVIS_AGENT_FINGERPRINT_THRESHOLD, composes auto_committer O+V signature to EXCLUDE sanctioned commits — no hardcoded fingerprint) wired forensics-only into existing commit_authority_archive BYPASS_SUSPECTED (+commit_message in post-commit detail). L2 OCA gate untouched/not duplicated. 33 spine + 135 regression green. Then-next: SWE-Bench-Pro soak (below).

Root problem: Cursor background Agents autonomously `git add/commit` on the operator main checkout (empirically rogue `d802b15a5a`, `8fcf55cfab` — verbose-LLM-prose + `[integrity-verified:` trailer). OCA already *refuses* them (`denied_sovereignty`) but the Agent still *attempts* + contaminates. Missing layer = prevention + structural durability of that prevention.

Grounding (verified): `.cursor/` holds only `debug.log` — no `.cursor/rules/`, no `.cursorrules`, no root AGENTS.md → greenfield. Compose-not-rebuild targets: `commit_authority_archive.BYPASS_SUSPECTED` (+ SSE), `ledger_sovereignty`/`denied_sovereignty` (hard gate, CLOSED), `meta.shipped_code_invariants` auto-discovered AST pins, `user_preference_memory` FORBIDDEN_PATH.

3-layer model: L1 Prevention (NEW — `.cursor/rules/*.mdc`), L2 Hard gate (SHIPPED OCA — compose only), L3 Forensics (SHIPPED archive — compose only). Advisory rule is necessary-not-sufficient; acceptable because L2 hard-refuses; arc's no-shortcut contribution = make the advisory structurally un-deletable + wired to forensics.

Slices: **S1** `.cursor/rules/no-agent-git-write.mdc` (`alwaysApply:true`; ban Agent add/commit/push/reset/checkout/stash/rm --cached on operator main; mandate worktree isolation; point at commit-authority daemon/CLI ritual) **+ auto-discovered `register_shipped_invariants` pin** (rule exists / non-empty / load-bearing tokens present; green-on-real + red-on-deleted/gutted). **S2** enrich existing BYPASS_SUSPECTED detail with `agent_git_write_attempt` tag on Cursor-Agent fingerprint match (pure detail enrichment, default-OFF §33.1) + extend `scripts/verify_oca.py` with read-only rule-presence check.

Non-goals: no new git hook (L2 exists), no process-killing Cursor Agents, no OCA/sovereignty/presence change. PR discipline: one branch `arc/cursor-agent-git-ban`, ~15-20 spine tests.

**Then-next focus (operator-stated): SWE-Bench-Pro to test Ouroboros+Venom.** Per [[project-v3-7-phase-f-report-card]] the SWE-Bench-Pro infra (Phases A→F) is CLOSED + soak-ready → that pivot is a graduation-SOAK/RUN effort (cherry-pick instances → `parallel_evaluate` → report card → resolved/unresolved verdict), NOT a build. Scope the soak runbook when called. Honor [[feedback-no-preresult-euphoria]].
