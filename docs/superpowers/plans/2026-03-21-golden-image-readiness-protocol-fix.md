# Golden Image Readiness Protocol Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Fix golden image boot verification deadlock and cascading system degradation by establishing J-Prime as the single source of truth for readiness.

**Architecture:** Remove APARS readiness override from J-Prime's health handler, parallelize golden image startup phases, add adaptive hysteresis for golden image deployments, and add voice fallback chain for safe_say. All changes follow the INV-3 principle: APARS is observational metadata, J-Prime owns readiness.

**Tech Stack:** Python 3, asyncio, bash (GCP startup script), macOS say command

**Spec:** docs/superpowers/specs/2026-03-21-golden-image-readiness-protocol-fix-design.md

---

## File Structure

| File | Repo | Action | Responsibility |
|------|------|--------|---------------|
| jarvis_prime/server.py | jarvis-prime | Modify lines 1407-1421 | Remove APARS readiness override |
| backend/core/gcp_vm_manager.py | JARVIS-AI-Agent | Modify lines 9529-9574 | Add ETA to update_apars() |
| backend/core/gcp_vm_manager.py | JARVIS-AI-Agent | Modify lines 9860-10242 | Parallel startup phases |
| backend/core/gcp_vm_manager.py | JARVIS-AI-Agent | Modify line 10237 | Golden image timeout (120s) |
| backend/core/gcp_vm_manager.py | JARVIS-AI-Agent | Modify lines 10603-10680 | Adaptive hysteresis |
| backend/core/supervisor/unified_voice_orchestrator.py | JARVIS-AI-Agent | Modify lines 1288-1370 | Voice fallback chain |
| tests/unit/core/test_jprime_health_inv3.py | JARVIS-AI-Agent | Create | Test APARS doesn't override readiness |
| tests/unit/core/test_golden_hysteresis.py | JARVIS-AI-Agent | Create | Test adaptive hysteresis |
| tests/unit/core/test_safe_say_voice_fallback.py | JARVIS-AI-Agent | Create | Test voice fallback chain |

---

### Task 1: Remove APARS Readiness Override in J-Prime (ROOT CAUSE FIX)

**Files:**
- Modify: /Users/djrussell23/Documents/repos/jarvis-prime/jarvis_prime/server.py:1407-1421
- Create: /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/tests/unit/core/test_jprime_health_inv3.py

- [ ] **Step 1: Write test that proves the APARS override bug and validates the fix**

Create tests/unit/core/test_jprime_health_inv3.py with tests that:
1. Simulate J-Prime get_status() with APARS override (proves bug: phase=ready but ready_for_inference=false)
2. Simulate fixed get_status() without APARS override (proves fix: phase=ready means ready_for_inference=true)
3. Verify APARS data is still attached for observability
4. Verify when J-Prime is actually loading, readiness is correctly false

- [ ] **Step 2: Run tests to verify they pass**

Run: python3 -m pytest tests/unit/core/test_jprime_health_inv3.py -v

- [ ] **Step 3: Apply the fix in J-Prime server.py**

Edit /Users/djrussell23/Documents/repos/jarvis-prime/jarvis_prime/server.py lines 1407-1421.

Replace the APARS override block with:
- Keep: result["apars"] = apars_payload (observational metadata)
- Remove: The if-blocks that override ready_for_inference and model_loaded from APARS
- Add comment: INV-3 — J-Prime is single source of truth for readiness. APARS must never override live readiness signals.

- [ ] **Step 4: Verify J-Prime tests still pass**

Run: cd /Users/djrussell23/Documents/repos/jarvis-prime && python3 -m pytest tests/ -v -x --timeout=30

- [ ] **Step 5: Commit in jarvis-prime repo**

Message: "fix(health): INV-3 — stop APARS from overriding ready_for_inference"

---

### Task 2: Add ETA to APARS Progress File

**Files:**
- Modify: /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/backend/core/gcp_vm_manager.py:9529-9574

- [ ] **Step 1: Read current update_apars() function**

Read backend/core/gcp_vm_manager.py lines 9475-9574 to confirm exact structure.

- [ ] **Step 2: Add phase duration variables and ETA calculation**

After PROCESS_EPOCH (around line 9481), add phase duration variables:
_PHASE_DUR_0=5; _PHASE_DUR_1=20; _PHASE_DUR_4=30; _PHASE_DUR_5=120; _PHASE_DUR_6=30

Inside update_apars(), add ETA calculation using a case statement on $phase that computes remaining time from current phase progress + all subsequent phases.

Add "eta_seconds": ${eta} field to the JSON heredoc template.

- [ ] **Step 3: Verify contract tests pass**

Run: python3 -m pytest tests/unit/backend/core/test_gcp_vm_startup_script_contract.py -v -x

- [ ] **Step 4: Commit**

Message: "feat(gcp): add eta_seconds to APARS progress file"

---

### Task 3: Parallel Golden Image Startup (Validation + Model Loading)

**Files:**
- Modify: /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/backend/core/gcp_vm_manager.py:9860-10242

- [ ] **Step 1: Read current phase 4-5 transition**

Read backend/core/gcp_vm_manager.py lines 9860-10230 to understand exact flow.

- [ ] **Step 2: Restructure to start J-Prime before validation**

Move the "Kill APARS stub + start J-Prime" block (currently at lines 10182-10230) to BEFORE the validation block (currently at lines 9862-9956).

After phase 1 completion (update_apars 1 100 35 "golden_ready_for_validation"):
1. Write transitioning state: update_apars 5 20 40 "transitioning_to_service" true false
2. Kill APARS health stub
3. Start J-Prime (systemd or direct — same logic as current lines 10196-10230)
4. THEN run validation (phases 4) while J-Prime loads the model in background

Remove the original "Kill APARS stub + start J-Prime" block (now moved earlier).

- [ ] **Step 3: Add validation failure guard**

After validation block, if VALIDATION_OK != true:
- Stop J-Prime (systemctl stop or pkill)
- Update APARS with validation_failed error
- Exit 1

- [ ] **Step 4: Verify startup script bash syntax**

Generate the script and check with bash -n.

- [ ] **Step 5: Commit**

Message: "perf(gcp): parallel validation + model loading on golden image"

---

### Task 4: Golden Image Timeout Profile + Adaptive Hysteresis

**Files:**
- Modify: /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/backend/core/gcp_vm_manager.py:10237,10603-10680
- Create: /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/tests/unit/core/test_golden_hysteresis.py

- [ ] **Step 1: Write test for adaptive hysteresis**

Create tests/unit/core/test_golden_hysteresis.py with tests that verify:
- golden_image deployment mode uses hysteresis=1
- non-golden deployments use default hysteresis=3
- GCP_GOLDEN_HYSTERESIS_UP env var is respected

- [ ] **Step 2: Run test**

Run: python3 -m pytest tests/unit/core/test_golden_hysteresis.py -v

- [ ] **Step 3: Change golden image timeout from 90 to 120**

Edit line 10237: change default from 90 to 120 in SERVICE_HEALTH_TIMEOUT.

- [ ] **Step 4: Add adaptive hysteresis to _poll_health_until_ready**

Around line 10640, add tracking variables:
- _detected_deployment_mode: Optional[str] = None
- _effective_hysteresis = self.config.readiness_hysteresis_up

When first APARS response with deployment_mode is received (~line 10688):
- If deployment_mode == "golden_image", set _effective_hysteresis = max(1, int(GCP_GOLDEN_HYSTERESIS_UP or "1"))
- Log the reduction

Change line 10666 to use _effective_hysteresis instead of self.config.readiness_hysteresis_up.

- [ ] **Step 5: Run existing GCP tests**

Run: python3 -m pytest tests/unit/core/test_gcp_vm_readiness_prober.py tests/unit/core/test_health_verdict.py -v -x

- [ ] **Step 6: Commit**

Message: "feat(gcp): adaptive hysteresis + golden image timeout"

---

### Task 5: safe_say Voice Fallback Chain

**Files:**
- Modify: /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/backend/core/supervisor/unified_voice_orchestrator.py:1285-1370
- Create: /Users/djrussell23/Documents/repos/JARVIS-AI-Agent/tests/unit/core/test_safe_say_voice_fallback.py

- [ ] **Step 1: Write test for voice fallback**

Create tests/unit/core/test_safe_say_voice_fallback.py with async tests that verify:
- Fallback to next available voice when primary is unavailable
- Primary used when it IS available
- Last resort returns preferred voice when none available

- [ ] **Step 2: Run test**

Run: python3 -m pytest tests/unit/core/test_safe_say_voice_fallback.py -v

- [ ] **Step 3: Add voice resolution to unified_voice_orchestrator.py**

Before safe_say() (around line 1285), add:
- _VOICE_FALLBACK_CHAIN list: ["Daniel", "Samantha", "Alex", "Fred"]
- _validated_voice: Optional[str] = None (session cache)
- async def _resolve_voice(preferred: str) -> str that:
  1. Returns cached _validated_voice if available
  2. Tests each voice via say -v <voice> "" with 5s timeout
  3. Returns first voice with exit code 0
  4. Caches result for session
  5. Falls back to preferred voice as last resort

In safe_say._do_say(), call _resolve_voice(voice) before the say subprocess and use the resolved voice.

- [ ] **Step 4: Run tests**

Run: python3 -m pytest tests/unit/core/test_safe_say_voice_fallback.py -v

- [ ] **Step 5: Commit**

Message: "fix(voice): add fallback chain when primary voice unavailable"

---

### Task 6: Integration Verification

- [ ] **Step 1: Run all new tests together**

Run: python3 -m pytest tests/unit/core/test_jprime_health_inv3.py tests/unit/core/test_golden_hysteresis.py tests/unit/core/test_safe_say_voice_fallback.py -v

- [ ] **Step 2: Run existing GCP test suite**

Run: python3 -m pytest tests/unit/core/test_gcp_vm_readiness_prober.py tests/unit/core/test_gcp_apars_early_exit.py tests/unit/core/test_ready_for_inference_null_fix.py tests/unit/core/test_health_verdict.py -v

- [ ] **Step 3: Verify startup script bash syntax**

Generate and check with bash -n.

- [ ] **Step 4: Final commit with all test files**

Verify git status in both repos shows all changes committed.
