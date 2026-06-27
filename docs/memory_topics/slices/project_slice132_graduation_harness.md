---
title: Project Slice132 Graduation Harness
modules: [backend/core/ouroboros/governance/graduation_orchestrator.py]
status: merged
source: project_slice132_graduation_harness.md
---

**Slice 132 — The Sovereign Shadow Graduation Harness. MERGED (PR #69347, main `bd70a351ec`, 2026-06-07).** Born from operator: "an autonomous Tier-D organism does not rely on a human executing a manual graduation runbook — automate the shadow→graduate flip." Replaces the manual runbook with an executable async harness.

**`graduation_orchestrator.py` (NEW, master `JARVIS_GRADUATION_ORCHESTRATOR_ENABLED` default-FALSE):**
- `graduate(flag, *, assertion=, is_safety=, persist=, env_path=)` → runs the flag's LIVE integration assertion; on pass autonomously flips `os.environ[flag]="1"` (+ optional bounded `.env` persist) + best-effort STANDARD-tier receipt via existing `graduation_override_ledger`. `graduate_all(flags, assertion_for=, ...)` fans out. Injectable `assertion`/`is_safety` → unit-tested without funded key.
- **Default per-flag assertions:** offline-verifiable tiers run REAL composition checks (semantic cache write-through+near-match via stdlib embedder; CAI low-urgency→cheapest cascade via injected classifier); live-API tiers (prefix cache 200+tool_use, batch dispatch) honestly return `passed=False detail="funded Anthropic lane required"` → operator supplies a live assertion.

**THE RECURSION BOUND (load-bearing, the verify-first refusal):** auto-flip is allowlisted to `_COST_CANDIDATES` (the 6 Slice-131 ROUTING/TUNING flags) and **FAIL-CLOSED** — anything not on the list (unknown OR FlagRegistry `Category.SAFETY`) → `REFUSED_SAFETY` (operator only), NEVER auto-granted. "An organism that could flip its own kill-switches is not bounded, so it cannot." COMPOSES (not bypasses) the existing Tiered Authority (`autonomous_graduation_engine` + `graduation_override_ledger` which itself structurally refuses non-STANDARD tier). Honors operator env-precedence (explicit `=0` → `HELD_OPERATOR_PRECEDENCE`, never overridden). Bounded `.env` writer (`persist_flag_to_env`) REFUSES credential-shaped keys (API_KEY/TOKEN/SECRET/PASSWORD/_KEY) + touches ONLY the target flag line (credentials/other lines byte-preserved) + never logs values.

**LIVE TELEMETRY (this env, no funded key):** `JARVIS_SEMANTIC_CACHE_ENABLED` + `JARVIS_CAI_ROUTER_ENABLED` → **GRADUATED** (flipped=True); `JARVIS_BATCH_ROUTING_ENABLED` + `JARVIS_PROMPT_PREFIX_CACHE_ENABLED` → HELD (live lane required); kill-switch `JARVIS_SEMANTIC_GUARD_ENABLED` → **REFUSED_SAFETY** (not flipped) — bound proven live. 10 tests.

**Remaining:** the live-API assertions (prefix cache 200+tool_use; batch) need the operator's funded Anthropic lane to execute the real graduation (the harness runs them when a funded `assertion`/client is supplied). The P2a prefix-cache ACTIVATION (thread `stable_prefix_out` into the 4 system-composition sites) is still its own follow-on before its assertion can pass. See [[project_slice131_cost_sovereign]].
