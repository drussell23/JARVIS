---
title: Project Slice123 Isolation Quarantine
modules: [backend/core/ouroboros/governance/boot_recovery_quarantine.py, scripts/ouroboros_battle_test.py, tests/architecture/test_slice123_quarantine.py, backend/core/ouroboros/governance/governed_loop_service.py]
status: merged
source: project_slice123_isolation_quarantine.md
---

**Slice 123 — Production Isolation & Hygiene Matrix. MERGED PR #69326 (`ac8ac50775`), PRD §51.11.23, v3.14.** Born from the FIRST armed T5 launch's live telemetry: Layer-4 verified flawlessly (`valid/ok(ed25519)`, floor holding) but the boot (a) replayed **81 provenance-less recovery ops** (all → `manual_intervention_required`, starving fresh Layer-4 work) and (b) froze ~2.5min on the in-process Oracle load. Both closed by composing existing mechanism.

**Phase 1** `backend/core/ouroboros/governance/boot_recovery_quarantine.py` (gated `JARVIS_BOOT_RECOVERY_QUARANTINE_ENABLED`, §33.1 default-FALSE): when a recovery op trips `boot_recovery_missing_provenance` (at `governed_loop_service.py` ~5431), its raw payload is sequestered to `.jarvis/quarantine/{ts}_{op}.json` + marked `quarantined`. ADDITIVE (escalation/postmortem untouched — auditability), BEST-EFFORT (never breaks recovery; unwritable dir→None proven), path-traversal-safe filenames, non-JSONable→repr. GLS wiring try/except-isolated.

**Phase 2 VERIFY-FIRST overturned "rewrite the Oracle load":** process isolation was ALREADY SHIPPED (Slices 112/113 `oracle_ipc.AsyncOracleProxy` separate-GIL worker + `oracle_adapter.make_oracle_adapter` → `IsolatedOracleAdapter` when `JARVIS_ORACLE_PROCESS_ISOLATION_ENABLED` set, already called by harness). Boot froze only because the flag was unset. Slice 123 ENABLES it (no rewrite = no duplication); 18 Slice-112/113 tests confirm sound.

**Phase 3** `--production-soak` flag in `scripts/ouroboros_battle_test.py` arg layer: overrides cost-cap=25/idle=0/wall=0 + `setdefault`s isolation+quarantine env; explicit `--cost-cap`/`--idle-timeout`/`--max-wall-seconds` still win (sys.argv check). Plus `launch_shadow_soak.sh` Karen-voice flags now env-overridable (`JARVIS_KAREN_VOICE_ENABLED=0` silences — operator found Karen annoying).

25 tests (`tests/architecture/test_slice123_quarantine.py` 7 + composed isolation 18). **Clean ignition command:** `JARVIS_LAYER4_ROADMAP_ENABLED=1 JARVIS_SOVEREIGN_KEYS_ENABLED=1 JARVIS_KAREN_VOICE_ENABLED=0 ./scripts/launch_shadow_soak.sh --production-soak --layer4-autonomous`.

**OPERATIONAL STATE (2026-06-06):** Operator (Zero-Order Doll) provisioned real Ed25519 key (pubkey `NTYtgCUcY40Msi8od75WxHq+m2QEJ7mFqAMH1G/3LWQ=`, in `.jarvis/`, gitignored), signed a 365-day roadmap (6 safe scopes, $25, depth 3), dry-check PASSED (`valid/ok(ed25519)`, floor holds). First armed soak (bt-2026-06-07-001117) was SIGKILLed (wedged in Oracle freeze) — that telemetry birthed Slice 123. **NOT yet relaunched clean.** Karen silenced (killed afplay + stopped the soak). NOTE: my failed smoke test wrote throwaway-passphrase key cruft to REPO ROOT (`layer4_key.salt/.meta.json/.pub`) — REMOVED; cause was empty `JARVIS_SOVEREIGN_KEY_DIR` → `Path("")`=cwd. Local+remote main synced to `ac8ac50775`. See [[project_slice122_sovereign_keys]], [[project_slice120_layer4_authority]]
