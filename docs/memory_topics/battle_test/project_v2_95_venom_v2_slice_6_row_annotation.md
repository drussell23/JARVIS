---
title: Project V2 95 Venom V2 Slice 6 Row Annotation
modules: [backend/core/ouroboros/governance/permission_decision_archive.py]
status: historical
source: project_v2_95_venom_v2_slice_6_row_annotation.md
---

May 10 2026: final slice of the Venom V2 observability arc. Pure-documentation closure.

**Honest discovery during Slice 6 scoping**:

When I started Slice 6 expecting to "flip a 🟡 row to ✅", grep revealed:
1. **No `§3.6.3` section header exists** in the PRD. Earlier doc-history references (`§3.6.3 priority #4` etc.) were prose nicknames the previous entries used, not actual section pointers. There is no `### §3.6.3` or `#### §3.6.3` markdown header.
2. **"§37 Tier 2 #6"** in my earlier session-state was the Explore agent's local enumeration of the 7 Tier 2 candidate items it listed at the start of the v2.89 Slice 1 audit, **not the PRD row number**. The agent's numbering was 1=rerun-from, 2=session search, 3=op-graph, 4=per-tool confidence, 5=Operation Modes, 6=Venom V2, 7=Pattern C.
3. **The actual canonical row** for Venom V2 in the §37 Tier 2 catalog at line 5732 is **row #15**, already ✅ SHIPPED 2026-05-07 for the **policy substrate** (V1+V2+V3+V4 closure: PermissionRegistry first-DENY-wins, 4-value PermissionDecision taxonomy, 595/595 cumulative regression).

**Per the operator binding (avoid duplication; build cleanly on what already exists)**, Slice 6 is therefore:
- NOT a new row addition
- NOT a phantom §3.6.3 edit
- NOT a duplicate ✅ ship-stamp for the policy work
- IS a single forward-additive annotation appended to row #15

**The annotation** (appended after the 2026-05-07 policy-substrate ✅ line at PRD line 5732):

> **OBSERVABILITY ARC ✅ SHIPPED 2026-05-10** (v2.89→v2.94, forward-additive on the policy substrate):
> - Slice 1 substrate ring `permission_decision_archive.py` (~440 LOC, monotonic `p-N` refs, BoundedBodyStore canonical pattern, producer-bridge §33.2 at `tool_executor:1218`)
> - Slice 2 REPL `/tool_permissions {recent|by-tool|by-op|stats|help}` (auto-discovered §33.3 + `/expand p-N` 5th cross-substrate prefix)
> - Slice 3 SSE event `permission_decision_recorded` (canonical broker bridge, dual-master-flag gated)
> - Slice 4 IDE GET `/observability/tool-permissions[/by-tool/{tool_name}|/{op_id}]` (route-order AST-pinned, read-only contract enforced via snapshot-equality test)
> - Slice 5 FlagRegistry seed (`register_flags()` auto-discovered, master `JARVIS_PERMISSION_ARCHIVE_ENABLED` BOOL/SAFETY/default-FALSE + `JARVIS_PERMISSION_ARCHIVE_SIZE` INT/CAPACITY/default-50)
>
> **98 new regression tests + 23 AST pins across the 5 observability slices; 162 cumulative regression green**; registry now 361 total specs (was 359 pre-arc). Master flag stays default-FALSE per §33.1 — operator-flippable via 3-clean-soak ladder. **Completes §8 absolute-observability triad: ring (history) + REPL (operator query) + SSE (real-time push) + GET (browseable HTTP) + FlagRegistry (typed catalog).**

**No code changes in Slice 6**. The load-bearing engineering work (Slices 1-5) is already complete. Slice 6 closes the documentation loop so future readers of the row #15 catalog entry see the full picture: policy substrate (2026-05-07) + observability arc (2026-05-10).

**Cumulative Venom V2 arc state — ALL 6 SLICES SHIPPED**:
- Slice 1 (v2.89) — substrate ring + producer-bridge + tool_executor seam (31 tests + 5 AST pins)
- Slice 2 (v2.90) — REPL verb auto-discovered + /expand p-N 5th cross-substrate prefix (31 tests + 8 AST pins)
- Slice 3 (v2.91) — SSE event registration + producer-bridge to broker (11 tests + 4 AST pins)
- Slice 4 (v2.93) — IDE GET endpoints + route-order discipline (15 tests + 2 AST pins)
- Slice 5 (v2.94) — FlagRegistry seed (10 tests + 4 AST pins)
- Slice 6 (v2.95) — PRD row annotation (0 tests — pure documentation)

**Total: 98 new regression tests + 23 AST pins + 9 PRD versions** (v2.86 → v2.95 = cadence-arc Layers 1-7 + Venom V2 observability arc Slices 1-6).

**Files modified in this slice**:
- `docs/architecture/OUROBOROS_VENOM_PRD.md` — single forward-additive annotation appended to §37 Tier 2 row #15 at line 5732 + new doc-history entry at top (v2.95)

**Operator binding 2026-05-10 satisfied verbatim**:
- Solved root problem directly — the observability arc IS the missing operator-visibility layer atop the policy substrate; row #15 is the canonical row that documents Venom V2; annotating it is the canonical closure marker
- No workarounds — did NOT invent a phantom §3.6.3 section; did NOT add a duplicate "Venom V2 observability" row to the catalog; did NOT re-stamp the 2026-05-07 policy ✅
- No shortcuts — the annotation explicitly enumerates all 5 observability slices with their test counts + AST pin counts + version stamps; future readers can trace each slice to its doc-history entry
- Composes existing canonical paths — the row #15 itself (canonical Venom V2 row); the doc-history's already-existing v2.89-v2.94 entries (per-slice detail); the row's existing ✅ marker discipline
- No hardcoding — the annotation defers to canonical version stamps for forward-readers to trace
- No duplication — Slice 6 is annotation NOT redeclaration

**Why row-annotation > new-row**: had I added a new row (e.g. "#15b — Venom V2 observability arc"), it would have:
- Duplicated information already in row #15 + doc-history entries v2.89-v2.94
- Created a confusing parallel taxonomy (#15 vs #15b — operators would wonder which is canonical)
- Violated the §37 catalog's single-row-per-arc discipline (each Tier 2 row is one arc; row #15 IS the Venom V2 arc)

The forward-additive annotation discipline is the canonical move: the row stays the row; the work done on top extends the row's narrative.

**Why this matters for RSI**: future operators (and future-me) opening row #15 see the FULL Venom V2 arc — policy substrate + observability layer + flag-graduation status — in one place. Without Slice 6, a reader looking at row #15 alone would think Venom V2 stopped at the 2026-05-07 policy substrate and miss the entire observability triad. The annotation is the operator-visibility win at the documentation layer (mirrors the observability win at the runtime layer).

**THE STRUCTURAL VENOM V2 ARC IS COMPLETE.** Remaining work is operator-paced graduation: when callback registrations land in production + 3-clean-soak ladder runs, the master flag flips default-FALSE → default-TRUE; the observability surfaces (REPL/SSE/GET) become operator-active by default.

**Soak #3 status at Slice 6 close**: still in-flight under post-Layer-7 (v2.92) dual-clock watchdog. Op `op-019e1082-bc8f` in GENERATE_RETRY (L2 repair) — DW Tier 0 degraded as usual. When the soak terminates (idle_timeout / wall-clock cap / clean exit), evidence row #3 lands. If clean, `JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED` becomes the **first Phase 9 flag graduation under the fully-closed cadence arc** (Layers 1-7) AND the **first graduation under the post-Layer-7 dual-clock watchdog**.
