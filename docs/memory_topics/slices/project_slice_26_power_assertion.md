---
title: Mechanism
modules: [backend/core/ouroboros/governance/power_supervisor.py]
status: historical
source: project_slice_26_power_assertion.md
---

PR #59082 squash-merged 2026-05-26 at `74c5f9cd44`. Branch `ouroboros/slice-26-hardware-power-assertion`. Closes v19 (`bt-2026-05-27-003843`) host-sleep wedge: operator's Mac suspended 6 min after SWE-Bench injection, LoopDeadman correctly killed the loop but no dispatch ever ran.

# Mechanism

`backend/core/ouroboros/governance/power_supervisor.py`:
- `async def assert_power_lock(*, parent_pid=None) -> Optional[PowerAssertion]`
- darwin: `asyncio.create_subprocess_exec("/usr/bin/caffeinate", "-w", str(pid), stdout=DEVNULL, stderr=DEVNULL)`
- Kernel auto-releases when parent exits via `-w` flag — no orphan, no Python cleanup
- Returns frozen `PowerAssertion(platform, parent_pid, subprocess_pid, binary)` handle

# Platform + master flag

`JARVIS_POWER_ASSERTION_ENABLED`:
- default-TRUE on darwin (load-bearing default — failure mode == no-Slice-26 baseline)
- default-FALSE on linux/win32 (no native primitive)
- explicit on/off always honored

# Integration ordering (AST-pinned)

In `GLS._build_components` BEFORE Slice 25B preflight:
```
1. DW provider constructed
2. PromotionLedger trusted-seed loaded
3. Slice 26 power assertion (NEW)
4. Slice 25B preflight (10s probe — now sleep-protected)
5. BackgroundAgentPool.start
```

# §5 attestation (AST-pinned verbatim)

`[PowerSupervisor] Active process-linked host sleep assertion established via IOKit/Caffeinate for PID: <N>.`

# Defensive shape

- Binary missing → skip + WARNING
- Spawn exception → swallowed + WARNING (NEVER raises into caller)
- Non-darwin → skip + INFO

# Verification

10 tests (3 AST pins + 7 spine). 255/255 regression (exceeds operator's 245 target). Live smoke verified real caffeinate subprocess via ps.

# v20 launch verification

v20 (bt-2026-05-27-011121, PID 15466) at boot:
- Slice 26 fired: `[PowerSupervisor] Active process-linked host sleep assertion established via IOKit/Caffeinate for PID: 15466.`
- ps confirms: `15713 15466 ... /usr/bin/caffeinate -w 15466`
- Slice 25B preflight probing 3 models (Qwen-4B already evicted from v19 persistence — `account_not_entitled` origin survived reboot)

# Architectural milestone

**v20 is the first soak in this entire arc where the engine has full external vision (Slice 25B) + hardware lifecycle control (Slice 26) + no Claude (Slice 19a) + multi-model fleet activation (Slice 23) + healing matrix (Slices 20B/20C/20D + Phase 3) + clean failure modes (Slice 21) + IMMEDIATE→STANDARD demotion (Slice 22) + structural sentinel fields (Slice 24).** Every layer of indirection between Qwen-397B and the SWE-Bench-Pro graduation bar has been removed.

Whether 397B can carry the op to RESOLVED is the empirical question v20 finally gets to answer cleanly.

Related: [[project_slice_25b_preflight_boot_wiring]] (sibling boot hook), [[project_slice_25_preflight_probe]] (substrate Slice 25B persists for Slice 26 to benefit from), [[feedback_no_preresult_euphoria]] (Slice 26 ships infrastructure; v20 RESOLVED is the capability bar).
