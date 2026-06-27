---
title: What shipped on main
modules: []
status: historical
source: project_swe_bench_pro_wiring_validation_closure.md
---

**SWE-Bench-Pro Phase-1 wiring-validation arc CLOSED as VALIDATED 2026-05-24 (Sat).** Two structural fixes landed on main and were empirically proven by two soaks; the original load-bearing wedges are closed in code; the remaining "fixture COMPLETE" gap is a newly-characterized **orthogonal** governance-pipeline budget floor, NOT a wiring/reservation/TMPDIR failure. Honor [[feedback-no-preresult-euphoria]] — résumé moves only on the specific artifact.

# What shipped on main

| SHA | PR | Slice |
|---|---|---|
| `a490c1ec0b` | #54392 | [[project-predictive-provider-resilience]]'s downstream — **Slice 12AA-fix** — `ClaudeProvider.generate` lazy `_sba_acquire` sized from new `CostGovernor.get_op_cap_usd(op_id)` accessor (cumulative cap), fallback to `_max_cost_per_op` only when CostGov unregistered/disabled |
| `c4d3e8415e` | #54539 | **Slice 12AC** — new `_swe_bench_pro_implicit_allowlist()` in `operation_advisor.py` composes `swe_bench_pro_enabled()` + `worktree_base_path()` + `repo_cache_path()` canonical accessors into the advisor allowlist when SWE-Bench-Pro master is ON (env/config-driven, AST-pinned: no `/tmp`/`/private/tmp`/`/var/tmp` literals) |
| `1e6110256` | #54588 | docs(architecture) PDF refresh (orthogonal) |

# What the soaks proved (artifact-grounded)

## bt-2026-05-24-030457 — $1.00 cap, 8m04s, $0.58 spent, stop_reason=session_exhausted (5 SBA circuit-breaker trips)

* **Slice 12AC** ✅ verified by `[SWEBenchPro] prepared problem='jarvis__harness-smoke-001' worktree='/private/var/folders/.../T/swebp_wt/jarvis__harness-smoke-001'` + `[Harness] SWE-Bench-Pro boot hook: verdict=injected` at 20:07:19; **zero** `swebp_repo_root_rejected` events in the 632KB debug.log
* **Slice 12AA-fix** ✅ verified by SBA preflight log `est=$0.5000 > effective_remaining=$0.0000 (remaining=$0.9777 other_reserved=$1.0000)` at 20:08:23 — fixture op `op-019e57f3-8d17` held a `$1.00` reservation (the CostGovernor cumulative cap, NOT the provider's `_max_cost_per_op` ≈ `$0.585`), and that reservation correctly blocked five subsequent sensor ops from consuming the fixture's runway

## bt-2026-05-24-033510 — $2.00 cap, 42m03s, $1.81 spent, stop_reason=wall_clock_cap+atexit_fallback

* **Slice 12AC** ✅ re-verified same shape; advisor scan logged `[Orchestrator] Advisor scanning per-envelope repo_root=/private/var/folders/.../T/swebp_wt/jarvis__harness-smoke-001 (legacy project_root retained as fallback)`
* **Slice 12AA-fix** ✅ re-verified with scaled reservation: `est=$0.5000 > effective_remaining=$0.0000 (remaining=$2.0000 other_reserved=$2.0000)` — reservation scaled to `$2.00` because CostGovernor cumulative cap scales with the doubled session cap, proving the helper reads live cap data (not a boot-time snapshot)
* Wall-cap fired cleanly at `tick=420 monotonic=2172s`, Ticket A1 Guard 2 wrote partial `summary.json` via `atexit_fallback`, ShutdownWatchdog `os._exit(75)` after 37.5s deadline (Oracle shutdown hang — pre-existing, orthogonal)
* LoopDeadman thread visible in `shutdown_watchdog_tombstone.txt` stack — **no 12AB false-positive observed**

# Wedges CLOSED in code

1. **bt-2026-05-23-235325** ([[project-predictive-provider-resilience]] wedge) — reservation under-sized at `_max_cost_per_op` $0.585 → fixed structurally by **Slice 12AA-fix**
2. **bt-2026-05-24-014841** — runbook-vs-advisor TMPDIR contract gap → fixture died in 5s with `swebp_repo_root_rejected` → fixed structurally by **Slice 12AC**

# What did NOT happen (honest gap statement)

* **Fixture COMPLETE — still 0** at both $1.00 and $2.00 caps
* **Rubric sanity floor** (≥1 RESOLVED + ≥1 UNRESOLVED per [[project-swe-bench-pro-soak-runbook]] §5) — UNMET
* Cannot claim wiring is "fully validated end-to-end" until ≥1 fixture reaches COMPLETE; CAN claim the two specific load-bearing wedges that previously prevented any forward motion are closed and the mechanisms behind 12AA-fix + 12AC are empirically correct

# NEW finding — Phase-1 governance-pipeline minimum-spend floor

The Ouroboros governance pipeline imposes a **real cumulative-spend floor** on **any** op, irrespective of whether the actual problem is trivial:

* **IronGate exploration-first mandate** — ≥2 `read_file`/`search_code` calls before any patch (`ExplorationInsufficientError` → GENERATE_RETRY w/ targeted feedback)
* **Venom tool loop** — each round ≈ `$0.50` per the observed `est=$0.5000` in SBA refusal logs
* **GENERATE retry headroom** — 2 attempts (`Generation attempt 2/2 failed`)
* **CostGovernor's per-op cap** — derived from `baseline × route × complexity × headroom × readonly`; for the fixture (route=complex, complexity=complex per the smoke fixture's catch-all classification) this came out to **$2.00** — exactly the session cap

**The runbook's `~$0.01–0.10` Phase-1 estimate is stale** — it assumed a near-no-op pipeline path that doesn't exist on main as of 2026-05-24. Even a `gold_patch=""` smoke fixture costs **≥$1.81** through the full pipeline before SBA preflight refuses the next chunk request (`session_remaining=$0.1902 < est=$0.5000`).

This is **NOT a slice failure** — it's an architectural insight that surfaces only now that the prior wedges are closed.

# Future slice (recommended; NOT started per operator binding)

**"Budget-aware smoke-fixture routing" / "low-cost wiring-validation mode"** — design surface:

* Likely seam: route inference (`urgency_router.py`) inspects `envelope.metadata.purpose == "wiring_validation"` + `envelope.metadata.real_benchmark == False` (both already present in the checked-in fixture) → routes to a `WIRING_VALIDATION` route class that:
  * Skips IronGate exploration mandate (justified because the fixture is structurally trivially-passing)
  * Skips Venom tool loop
  * Caps Claude tokens at a low ceiling (e.g. 512)
  * Uses route_factor = 0.1 for `CostGovernor._derive_cap` so per-op cap is `baseline × 0.1 × …` = pennies
* Compose existing surfaces — DO NOT fork the orchestrator FSM
* AST-pin that `WIRING_VALIDATION` route is ONLY taken when both `purpose=="wiring_validation"` AND `real_benchmark==False` (defense against the route being abused for real benchmark spend)
* Master flag default-FALSE; graduate only after empirical fixture COMPLETE proof on its own slice

**Do NOT alter Iron Gate / Venom / smoke fixture behavior in this arc** — that's the new slice's scope.

# Operator bindings honored this arc

* No Aegis files touched
* `ouroboros/battle-test/20260524-015042` branch preserved at `423cd0b916` (parallel Claude session's Aegis-2B-i autocommit)
* Aegis stash@{0} (`aegis-wip-preserved-for-slice-12aa-fix-2026-05-23`) intact
* No autonomous PDF commit on main checkout (operator declined to stamp sovereignty marker; PDF was eventually committed by parallel session + landed via PR #54588)
* No Path B / no real benchmark spend
* All env-driven, no hardcoded paths (AST-pinned)
* Compositional fixes — reused canonical surfaces (`get_default_cost_governor`, `swe_bench_pro_enabled`, `worktree_base_path`, `repo_cache_path`, `_parse_allowlist_env`)

# Preserved session evidence

* `bt-2026-05-24-030457` — $1.00 cap soak (debug.log 632KB, summary.json, replay.html, cost_tracker.json)
* `bt-2026-05-24-033510` — $2.00 cap soak (debug.log 3.7MB, summary.json, shutdown_watchdog_tombstone.txt)
* Both sessions' artifacts under `.ouroboros/sessions/` — the authoritative truth, NOT stdout

# Why this matters

Two **load-bearing structural fixes** are now on main, with passing unit tests + AST pins + soak-evidence. The path from "wiring broken" to "minimum-spend floor exceeds runbook estimate" is forward motion — the failure surface has been moved into plain sight where the next slice can address it cleanly, rather than masked by upstream wedges.

# How to apply

* When the next attempt at fixture COMPLETE happens, design the new "budget-aware wiring-validation route" slice FIRST (don't just raise the cap — that's a workaround, not a fix)
* Update [[project-swe-bench-pro-soak-runbook]] Phase-1 cost estimate from "$0.01–0.10" to "**~$2.00 expected via full pipeline; <$0.10 expected once budget-aware smoke route lands**"
* If the operator wants to confirm fixture COMPLETE empirically before the new slice, set `--cost-cap 3.50` or higher (escape hatch only; not the structural answer)
* When investigating future "wedge X is blocking SWE-Bench-Pro" reports, check the SBA preflight logs FIRST (`session_remaining` + `other_reserved`) — that's where this arc's structural finding lives
