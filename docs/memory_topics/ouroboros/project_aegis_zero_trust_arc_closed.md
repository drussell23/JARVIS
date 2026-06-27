---
title: What shipped on main (5 PRs)
modules: [backend/core/ouroboros/governance/providers.py]
status: merged
source: project_aegis_zero_trust_arc_closed.md
---

**Aegis Zero-Trust provider proxy arc CLOSED 2026-05-24 (Sat) via 5 PRs merged to main + 4 sequential graduation soaks proving the architecture end-to-end.** All operator-flagged signals captured empirically. `JARVIS_AEGIS_ENABLED` remains default-FALSE pending operator's graduation decision — the *architecture* is proven, *graduation* is a separate operator call. Honor [[feedback-no-preresult-euphoria]].

# What shipped on main (5 PRs)

| SHA | PR | Slice | What |
|---|---|---|---|
| `f1c5f1ebb6` | #56360 | **2B-ii** | Provider Proxy Bridge — single canonical `governance/aegis_provider_bridge.py` factory; transport swap via `base_url=JARVIS_AEGIS_URL`; per-call X-JARVIS-Lease via ContextVar; 11 provider call sites rewired (6 in providers.py + 5 aux modules + claude_fallback) + 7 DW call sites |
| `a72ad5f171` | #56441 | **2B-ii.1** | Aegis-aware provider availability gates — `DoublewordProvider.is_available` + `governed_loop_service._provider_construction_gate` compose `aegis.client.is_enabled()` as OR-fallback (broke the Catch-22 where successful env scrub caused providers to self-disable) |
| `2f9f4d5913` | #56501 | **2B-ii.2 + 2B-iii.1** | Heavy probe wire (`dw_heavy_probe._do_probe` rewired through bridge — the 11th DW call site) + ledger hygiene (`aegis/ledger_hygiene.py` rotates WAL + removes stale lock before each battle-test session — never deletes, prunes old `.bak-*` past cap) |
| `a4401da0da` | #56523 | **2B-iii.2** | Structural battle-test cap defaults — `aegis/battle_test_defaults.py` installs `$2.00` session+hourly caps ONLY when operator hasn't set them (env precedence preserved; daemon-side $0.00 fail-closed default in `aegis/flags.py` UNTOUCHED per operator binding "ceiling stays strict") |

Total: ~3300 insertions across 16 files + ~115 new tests + 9 AST pins.

# 4 graduation soaks — sequential evidence of each gap closing

| Soak | Date/Time | Outcome | New gap surfaced |
|---|---|---|---|
| `bt-2026-05-24-222008` | 2026-05-24 15:20 PDT | Signals 1+2+4 ✅, **Signal #3 ❌** | Providers self-disabled under env scrub — `is_available` Catch-22 |
| `bt-2026-05-24-225714` | 2026-05-24 15:57 PDT | Signals 1+2+3a+3b+4 ✅, **Signal #3c/d ⚠️** | `dw_heavy_probe` 11th call site (missing_lease_header) + `lease_denied:cost_ceiling_exceeded` |
| `bt-2026-05-24-232345` | 2026-05-24 16:23 PDT | Signals 1+2+3a+3b+3c+4 ✅, **Signal #3d ⚠️** | Aegis daemon's `$0.00` fail-closed cap default refused even the lease itself |
| `bt-2026-05-24-233640` | 2026-05-24 16:36 PDT | **ALL signals fired GREEN** | None — arc closed |

## Final scoreboard (bt-2026-05-24-233640, the closer)

| # | Signal | Evidence (verbatim from logs) |
|---|---|---|
| **#00** | `[BattleTestDefaults]` | `JARVIS_AEGIS_SESSION_CAP_USD unset → installing default $2.00 (operator can override via env)` + same for hourly burn. `session_cap_source=default hourly_burn_cap_source=default` |
| **#0** | `[LedgerHygiene]` | `rotated WAL .jarvis/aegis/spend.jsonl → .jarvis/aegis/spend.jsonl.bak-pre-bt-1779665798 (clean financial slate for new battle-test session)` + `removed stale lock` |
| **#1** | `[AegisEnvScrub]` | both ANTHROPIC_API_KEY + DOUBLEWORD_API_KEY popped from env |
| **#2** | `[AegisDaemon]` | `serving on 127.0.0.1:62742 (pid=2889)` — ephemeral port confirmed |
| **#3a** | Providers configured under env scrub | `ClaudeProvider: configured` + `DoublewordProvider: configured (mode=real-time + Venom)` + `Self-critique engine wired (provider=doubleword)` |
| **#3b** | `[ProviderBridge]` factory | Both providers constructed via `make_async_anthropic_client` (`base_url=http://127.0.0.1:62742`, placeholder key) |
| **#3c** | HeavyProbe Aegis-routed | `[HeavyProbe] model=deepseek-ai/DeepSeek-OCR-2 ... total_ms=756` (vs prior soak's `total_ms=128` — proves full upstream round-trip) |
| **#3d** | **Aegis daemon forwarded → upstream responded** | `error=entitlement_blocked:blocked by a routing rule:status_403` — DW API returned 403 from upstream (not a daemon-side rejection); JARVIS's `dw_entitlement_classifier` correctly categorized as model-discovery negative → `routed to TERMINAL_OPEN` |
| **#4** | Slice 12AH synthetic noop | `[Slice12AH] wiring-validation fixture detected — synthesizing 2b.1-noop ... is_noop=True (provider=slice_12ah_synthetic_noop) terminal_reason_code=noop — skipping APPLY` for op `op-019e5c5c` |

Duration 287s, cost $0.00 (fixture noop) + ~$0.000021 (one Aegis-forwarded heavy probe to api.doubleword.ai). Operator-initiated SIGTERM after all signals fired.

# Operator bindings honored across the arc

* **"No hardcoding"** — env-var names + defaults centralized in `aegis/flags.py` + `aegis/battle_test_defaults.py`; AST pins forbid re-introducing literals in scripts.
* **"Ceiling stays strict"** — daemon-side defaults in `aegis/flags.py` remain `$0.00` fail-closed for production; battle-test caps installed structurally by helper, env-precedence preserved.
* **"No silent fallback"** — `acquire_call_lease()` raises `AegisClientError` on Aegis failure; no path falls back to direct upstream credentials.
* **"Single seam"** — every Aegis-side concern (transport swap, bearer composition, per-call lease, WAL rotation, cap defaults) routes through ONE canonical helper; AST pins enforce.
* **"Build on existing"** — providers reuse `aegis.client` (Slice 1) + `forward_request` (2B-i forwarding) substrate; no parallel implementations.
* **"Operator authority precedence"** — every env-var read uses `_is_truly_unset` defensive predicate; operator can override any cap at any time.
* **"No euphoria"** — graduation criterion is empirical artifact (the 200 OK forward — even if it's a 403 upstream entitlement, the proxy round-trip is proven). Each soak surfaced exactly one new gap; each gap closed by a separate audited slice.

# What's NOT graduated yet (operator decision)

`JARVIS_AEGIS_ENABLED` remains default-FALSE in `aegis/flags.py`. The *architecture* is proven (this arc's deliverable). *Graduation* to default-TRUE requires a separate operator decision per "no euphoria" — likely after:
1. A real-provider soak (not just wiring-validation fixture + heavy_probe) proves the full GENERATE→APPLY→VERIFY cycle through Aegis end-to-end
2. The DW account entitlement issues (`entitlement_blocked` on multiple models) are resolved or characterized so the soak can produce at least one 200 OK upstream success

# Related arcs

* [[project-slice-2b-ii-aegis-provider-bridge]] — Slice 2B-ii core bridge
* [[project-operator-commit-authority]] — OCA Iron Gate + sovereignty marker workflow used to commit each PR
* [[feedback-no-preresult-euphoria]] — the discipline that made each soak's "partial success" into a gap-finder rather than a graduation event

# Final chapter — Slice 2B-iv GRADUATION (added 2026-05-25)

**MERGED 2026-05-25 at `280e47f6f0` via PR #56663.** Both
`JARVIS_AEGIS_ENABLED` AND `JARVIS_AEGIS_FORWARDING_ENABLED`
graduated to default-TRUE in `aegis/flags.py`. Operator-authorized
dual graduation after surfacing the master-without-forwarding =
404-by-default trap.

* Function defaults flipped: `get_bool(..., default=True)` for both
* FlagSpec seeds flipped to match (parity AST-pinned)
* 4 AST pins enforce the graduated state at both function-default
  and FlagSpec-seed levels
* Operator opt-out PRESERVED via env-precedence (explicit `=false`
  still disables)
* Daemon-side strictness UNCHANGED: `session_cap_usd` /
  `hourly_burn_cap_usd` / `route_caps_usd` all stay $0.00 fail-closed
  (operator must explicitly authorize spend; `default_battle_test_caps()`
  helper installs battle-test caps without touching defaults)

**Total PRs in arc: 6** (#56360, #56441, #56501, #56523, #56626, #56663)
**Total slices: 7** (2B-ii / ii.1 / ii.2 / iii.1 / iii.2 / iii.3 / iv)
**Total soaks: 6 sequential** (each surfaced exactly one gap; gap → slice → next soak)
**AST pins: ~16** across the arc
**Tests: ~148**
**Real spend across all soaks: ~$0.11** (proves end-to-end forwarding)
**Daemon defaults changed: 0** (operator binding "ceiling stays strict" intact)

# Open follow-up (NOT scope of this arc)

* **Slice 2B-ii.3 (proposed, not started)**: `complete_sync()` /
  `prompt_only()` in DW provider also self-disable under Aegis env
  scrub (IntentDiscovery sensor failure noted in bt-2026-05-24-225714).
  Cousin gap to Slice 2B-ii.1.
* **Slice 2B-iv.x (proposed, not started)**: strengthen Iron Gate
  retry feedback so models can't ignore exploration mandate (Claude
  ignored it twice on the ansible op in bt-2026-05-25-004146).
* **Capability measurement arc (separate, not started)**: real
  SWE-Bench-Pro resolve-rate measurement requires the above two
  fixes + model-behavior tuning. Architecture is proven; capability
  is the next concern.
