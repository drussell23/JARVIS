---
title: Project V2 87 Layer5 Modality Ledger
modules: [backend/core/ouroboros/governance/dw_discovery_runner.py]
status: historical
source: project_v2_87_layer5_modality_ledger.md
---

May 10 2026: closes Layer 5 of the cadence-connectivity arc. Built on v2.86's 4 structural fixes.

**Discovery sequence**:

After v2.86 shipped, soak verification produced 24 ops with **18 (75%) blanket-blocked by topology** despite all 4 v2.86 fixes. Investigation traced the actual cause to a different layer.

**Sentinel state observation** (`.jarvis/topology_sentinel_current.json`):
- 14 endpoints tracked, grouped by state:
  - 5 CLOSED (healthy) including `Qwen/Qwen3.5-397B-A17B-FP8` (streak=2.0)
  - 7 OPEN (transient breaker)
  - 2 TERMINAL_OPEN (permanent ban):
    - `allenai/olmOCR-2-7B-1025-FP8` — `403 Real-time access to 'allenai/olmOCR-2-7B-1025-FP8' is blocked by a routing rule`
    - `lightonai/LightOnOCR-2-1B-bbox-soup` — `RuntimeError:background_dw_timeout:180s`

**Critical finding**: sentinel dispatch report from failing run was `[lightonai/LightOnOCR-2-1B-bbox-soup:skipped_terminal_open, allenai/olmOCR-2-7B-1025-FP8:skipped_terminal_open, google/gemma-4-31B-it:failed:live_transport]`. **TERMINAL_OPEN models were leading the BG candidate list**, exhausted before reaching Qwen3.5-397B.

Direct classifier audit confirmed:
- WITHOUT modality_ledger: `BG = ['lightonai/...', 'allenai/...', 'openai/gpt-oss-20b']` (TERMINAL_OPEN models leading)
- WITH modality_ledger: `BG = ['openai/gpt-oss-20b', 'google/gemma-4-31B-it', 'Qwen/Qwen3.5-397B-A17B-FP8']` (clean list)

**Root cause**: `dw_discovery_runner._get_or_create_modality_ledger` at lines 592-611 conflated:
- (a) Loading the disk ledger of pre-classified verdicts (cheap pure-I/O, always safe)
- (b) Probing new models live via HTTP (expensive, requires opt-in)

Both gated by `JARVIS_DW_MODALITY_VERIFICATION_ENABLED` (default-FALSE). With the flag off, even though `.jarvis/dw_modality_ledger.json` had 22 pre-existing verdicts (14 CHAT_CAPABLE including Qwen3.5-397B + 8 UNKNOWN), the singleton returned None → classifier received `modality_ledger=None` → produced legacy BG list with TERMINAL_OPEN models at top.

**Structural fix** at `dw_discovery_runner.py:592-616`:
- Separated the two concerns. Ledger loading is unconditional — only short-circuited by explicit `JARVIS_DW_MODALITY_LEDGER_DISABLE=1` escape hatch for diagnostic isolation
- Probing remains gated by the existing `modality_verification_enabled()` check at the canonical call site (Step 1.5 lines 202-205) — that gate is unchanged, expensive HTTP still requires opt-in
- Operator-override discipline preserved
- Authority asymmetry preserved (no parallel ledger machinery, composes existing `ModalityLedger` substrate)

**Verification**:
- Direct repro: `_get_or_create_modality_ledger()` now returns populated ledger with `has_record('Qwen/Qwen3.5-397B-A17B-FP8') == True`
- 149 governance modality+discovery+catalog_classifier tests pass — no regression
- Substrate-level fix; the next soak should see clean BG candidate ordering with Qwen3.5-397B at #3 instead of `lightonai/olmOCR` TERMINAL_OPEN models

**What v2.87 does NOT close** (Layer 6, deferred to v2.88):

When the battle-test subprocess exits via wall-clock cap, the harness's terminal history row writes `outcome=infra session=unknown`. The session-detection anchor (`_read_most_recent_session` mtime-sorts and looks for `summary.json` in the matched dir) finds the right dir but the inner `summary.json` isn't reliably written.

Wave 3 v2.79 added "partial-shutdown insurance: atexit fallback AND a sync signal-handler write so every session dir ends up with a v1.1a-parseable summary.json — even when SIGTERM arrives mid-cleanup or the async finally can't complete." That insurance should cover this but apparently isn't firing in the wall-clock-cap exit path.

Multi-hour investigation required:
- atexit ordering (does atexit even fire when wall-clock cap raises SystemExit?)
- signal handler write path (which signal does the cap actually deliver?)
- battle-test's own exit hook chain (does it cleanly shut down before the harness's atexit runs?)
- session-detection anchor's interaction with mtime under concurrent writes

**Operator binding 2026-05-10 satisfied verbatim**: solved root problem directly (modality ledger consultation IS the load-bearing fact; probing is a separate concern), no workarounds (we did NOT just flip the verification flag because that would also enable expensive probes), no shortcuts (148 tests verify), composes existing canonical `ModalityLedger.load()` (zero parallel state), preserves operator-override discipline.

**Files modified**:
- `backend/core/ouroboros/governance/dw_discovery_runner.py` — `_get_or_create_modality_ledger` separated concerns (lines 592-616)

**Test impact**: 149 governance modality+discovery+catalog_classifier tests pass with the fix.

**Cadence arc status**:
- v2.86 closed Layers 1-4 (env loading, --once delegation, sentinel flag default, socksio dep)
- v2.87 closes Layer 5 (modality ledger always loads)
- v2.88+ pending Layer 6 (0-cost session linkage)

**NEXT**: Layer 6 — fresh-eyes investigation of atexit + signal handler write paths under wall-clock-cap exit.
