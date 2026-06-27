---
title: Project Slice97 Cross Repo Mesh
modules: [backend/core/ouroboros/cross_repo_mesh/, tests/architecture/test_slice97_e2e_multirepo.py]
status: historical
source: project_slice97_cross_repo_mesh.md
---

**§40 #20 "cross-repo causal mirror" — distributed neural mesh BUILT (Slice 97, 2026-06-06).** The trigger-gate is met: sibling repos **EXIST** at `~/Documents/repos/jarvis-prime` (Mind, GitHub `drussell23/jarvis-prime`, an LLM-inference server) + `~/Documents/repos/reactor-core` (Soul, `drussell23/reactor-core`, a training runner). Verify-first found #20's CORE already existed: `cross_repo_causal_mirror.py` (predictions-not-requests correlation, default-FALSE) + deep substrate (`cross_repo.py` CrossRepoEventBus, `event_channel.py` inbound webhook+HMAC, `cross_repo_drift_sensor`, `event_bridge`, `aegis/lease.py` HMAC-SHA256 codec+NonceLedger). The genuinely-missing piece = the signed async EMIT + independent-VERIFY contract.

**MODEL = "PREDICTIONS, NOT REQUESTS" (load-bearing): JARVIS EMITS a signed NOTIFICATION; each consumer INDEPENDENTLY verifies + emits a LOCAL intent — NEVER executes anything JARVIS sends.** Rejected the user's original "trigger remote code" framing as a supply-chain vector.

**Slice 97 (all merged):**
- **Stage 1 (JARVIS, PR #69286)** `backend/core/ouroboros/cross_repo_mesh/`: `ripple_contract.py` (PORTABLE stdlib-only, vendorable — `RipplePayload`/`RippleKind`/`VerifyVerdict`, `sign_ripple`, `verify_ripple` first-failure-wins drop matrix: MALFORMED/BAD_SIGNATURE/WRONG_ORIGIN/EXPIRED/REPLAY; nonce registered LAST so forged ripple can't pre-burn a legit nonce; NEVER raises/executes; `intent` is a STRING never invoked) + `ripple_emitter.py` (`build_ripple` + async `emit_ripple`, §33.1 `JARVIS_CROSS_REPO_RIPPLE_EMIT_ENABLED` default-FALSE, PSK from `JARVIS_CROSS_REPO_EMIT_PSK`, immutable receipt `.jarvis/cross_repo_ripples.jsonl`). **Cross-compat PROVEN: `sign_ripple(p,K) == aegis.lease._encode_token(K, p.to_canonical_dict())` byte-identical** — composes existing crypto, no rewrite. 32 tests.
- **Stage 2 (siblings)** — `jarvis-prime#2` + `reactor-core#1` (their own repos, NO OCA/pre-commit hook → direct branch+PR): each vendored `ripple_contract.py` BYTE-IDENTICAL (diff -q) + `cross_repo_mesh/ripple_listener.py` (`handle_inbound_ripple` verifies + emits local intent JSONL, silent-drop, master `JARVIS_PRIME_RIPPLE_LISTENER_ENABLED` / `REACTOR_CORE_RIPPLE_LISTENER_ENABLED` default-FALSE, shared PSK). 9 tests each.
- **Stage 3 (JARVIS, PR #69287)** `tests/architecture/test_slice97_e2e_multirepo.py`: LIVE 3-repo handshake — fires REAL JARVIS-signed ripple → drives BOTH siblings' actual on-disk listeners (loaded via importlib under unique aliases to dodge same-named `cross_repo_mesh` collision; **cross-module enum identity → compare `.value` not `is`**) → VERIFIED+local-intent on both; tampered/replay/wrong-origin DROPPED; `__import__('os').system(...)` intent = inert data, nothing executed. SKIPS if siblings absent (`JARVIS_SIBLING_REPOS_ROOT` env + `~/Documents/repos` fallback). 36 architecture tests green.

**Adversarial crypto review (Stage 1): forgery/sig-strip/alg-confusion/replay/nonce-burn/clock-skew/50-string-fuzz/dangerous-intent ALL dropped-or-safe; constant-time `hmac.compare_digest`; no exec path; portable=stdlib-only; emit inert without flag+PSK.**

**REMAINING (operational, NOT engineering):** stand up sibling inbound HTTP listeners as live network daemons + provision shared PSK across 3 repos. Crypto correctness proven; live network op is a deploy step. All 3 masters §33.1 default-FALSE. **Session total: 16 JARVIS PRs + 2 sibling PRs.** See [[project_section40_open_items]], [[project_cc_parity_benchmark]], [[project_repo_git_pr_ops]].
