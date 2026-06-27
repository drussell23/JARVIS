---
title: Project Slice122 Sovereign Keys
modules: [backend/core/ouroboros/governance/sovereign_keys.py, tests/architecture/test_slice122_sovereign_keys.py, backend/core/ouroboros/governance/roadmap_synthesizer.py, backend/api/observability_gateway.py]
status: merged
source: project_slice122_sovereign_keys.md
---

**Slice 122 — Sovereign Cryptographic Key Manager & Dynamic Synthesis. MERGED PR #69325 (`1bc7b70863`), PRD §51.11.22, v3.13.** The Layer-4 ignition infrastructure (zero hardcoding) + a real crypto fix to Slice 120.

**ROOT-CAUSE UPGRADE (HMAC→Ed25519):** Slice 120 verified roadmaps with symmetric HMAC — but with HMAC whoever can VERIFY can also FORGE (the loop holds the verify key → can mint its own roadmap → self-authorize, voiding §1). Slice 122 moves Layer-4 signing to **asymmetric Ed25519** (`backend/core/ouroboros/governance/sovereign_keys.py`, gated `JARVIS_SOVEREIGN_KEYS_ENABLED` §33.1 default-FALSE): operator holds PRIVATE key, loop gets ONLY public key → can verify, CANNOT forge (mathematical guarantee). `layer4_roadmap_authority.verify_signed_roadmap` got an additive `signature_alg=="ed25519"` branch routed before the HMAC fallback (HMAC path byte-identical, backward-compat).

**ZERO HARDCODING / never-stored key:** private key = `hashlib.scrypt(passphrase, salt)` (stdlib, no third-party KDF; needs explicit `maxmem` since n=2^15 hits OpenSSL's 32MiB default cap), derived live from operator passphrase, NEVER written to disk. Only salt + public key (`.jarvis/layer4_operator.pub`) + a sig-verifier persist. Test asserts passphrase bytes never appear on disk. **Single-user honesty: same OS user = no kernel air-gap; it's cryptographic+procedural (private key transient in interactive operator session), backstopped by Slice-120 un-signable floor.**

Phase 2: `roadmap_synthesizer.py` audits commit scopes+tier → unsigned authority-free `.jarvis/roadmap.draft.yaml` (SAFE scope allow-list only, recursion clamped to Slice-104 cap, ~12mo expiry). Phase 3 **REFUSED the one-click web-sign button** — `observability_gateway.py`'s OWN invariant (line 23) forbids a browser endpoint minting authority ("sovereignty leak past the Zero-Order Doll"); exposed draft READ-ONLY (`GET /api/observability/layer4-roadmap-draft`, `armed=false`) + signing stays local passphrase-gated CLI (`python3 -m backend.core.ouroboros.governance.sovereign_keys {provision,sign}`). React tab deferred (no React app in-repo). `cryptography>=42` added to requirements.txt.

10 tests (`tests/architecture/test_slice122_sovereign_keys.py`): round-trip (provision→synthesize→sign→loop verifies VALID w/ pubkey only + floor still rejects Order-2 on valid roadmap); air-gap (foreign-key→INVALID_SIGNATURE, wrong passphrase rejected, passphrase never on disk); fail-closed (tamper/no-pubkey/expired). Slice 120 regression 21/21.

**T4 NOW ARMABLE END-TO-END.** Phase-4 ignition (operator-only, NOT yet run): (1) `python3 -m backend.core.ouroboros.governance.sovereign_keys provision` (type passphrase). (2) `python3 -m backend.core.ouroboros.governance.roadmap_synthesizer` (draft). (3) review `.jarvis/roadmap.draft.yaml`. (4) `python3 -m backend.core.ouroboros.governance.sovereign_keys sign`. (5) export `JARVIS_LAYER4_ROADMAP_ENABLED=1` + `JARVIS_SOVEREIGN_KEYS_ENABLED=1` + pubkey. (6) launch soak `--layer4-autonomous`. The remaining bottleneck is purely the T5 calendar clock + the operator's actual signature. See [[project_slice120_layer4_authority]], [[project_slice121_temporal_matrix]]
