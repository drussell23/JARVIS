# Sovereign Command Node — Design Spec (Biometric Mission Control)

> **Arc.** The backend is a complete self-healing, self-improving sovereign organism. The bottleneck is now the *operator interface*: you cannot manage an AGI through terminal stdout + raw JSON. The Sovereign Command Node is a dynamic web UI/UX that visualizes the live organism and provides a **Zero-Trust, voice-biometric-gated** one-click authorization surface for CRITICAL_ELEVATION cross-repo PRs.
> **Date:** 2026-06-23. **Status:** design for review (no implementation until approved).
> **PRIMARY LAW (inherited, non-negotiable):** the Command Node is an *operator surface*, NOT a new authority. It can never weaken a backend law. The **Immutable Orange Protocol still holds underneath** — no UI action, even a valid biometric, can make a Mind/Nerves PR auto-merge. The biometric gate only authorizes the *operator-approval step* of a CRITICAL_ELEVATION; the backend quarantine remains the source of truth.

---

## 0. Reuse Map (verified — do NOT reinvent)
- **Read-feed:** `ide_observability_stream.py` (SSE `/observability/stream`, typed events, replay, heartbeat, drop-oldest backpressure) + `ide_observability.py` (`GET /observability/{health,tasks,tasks/{op_id}}`, loopback, rate-limited, authority-free). The Command Node is a new SSE/GET *consumer*; we ADD event types, not a new transport.
- **Blast-radius data:** `oracle.compute_blast_radius(node_id, max_depth) -> BlastRadiusReport` with `directly_affected` + `transitively_affected: Set[NodeID]{repo,file,name,type}` + `to_dict()`. The graph is a direct render of this.
- **Voice biometric:** `backend/voice_unlock/voice_biometric_intelligence.py` — `EcapaFacade` (ECAPA-TDNN embeddings), `VBIConfig` (Bayesian auth threshold 0.85, anti-spoof physics/VTL, drift), `voice_biometric_cache.py`. The master voice-print enrollment exists. The middleware REUSES this verification pipeline — it does NOT build a parallel one.
- **Cross-repo governance:** `critical_elevation.py` (the CRITICAL_ELEVATION state + the Immutable Orange floor), `cross_repo_trust_ledger.py`, `OrangePRReviewer`. The Command Node's authorize-action calls the EXISTING approval path; it does not bypass it.
- **Audit substrate:** `adaptation/graduation_ledger.py` durable-JSONL append pattern (reuse for the immutable audit ledger).

## 1. Goals / Non-Goals
**Goals.** (G1) A dynamic web dashboard (Next.js/React) that visualizes the live organism: the active DAG, the real-time Ouroboros FSM state, and an **interactive Body↔Mind↔Nerves blast-radius graph**. (G2) A **Zero-Trust voice-biometric write-path** for CRITICAL_ELEVATION approval — challenge/response, ECAPA-TDNN verification, anti-spoof, mathematically-fresh anti-replay. (G3) An **Immutable Audit Ledger** — every authorization (pass AND fail) cryptographically hashed + appended, binding voice-print → AST mutation. (G4) Reuse the SSE feed + the voice pipeline + the governance path; the UI is a consumer, never a new authority.
**Non-Goals.** NOT a bypass of any backend law (Immutable Orange, fail-CLOSED, the trust ledger all stand). NOT a replacement for the read-only `/observability` layer (that stays authority-free; the write-path is a SEPARATE, biometric-gated service). NOT password/token auth (explicitly rejected). NOT auto-merge of Mind/Nerves (structurally impossible regardless of the UI).

## 2. Architecture
```
┌──────────────────────── Sovereign Command Node (Next.js/React web app) ────────────────────────┐
│  READ (live):  SSE client ── /observability/stream ──► [DAGCanvas] [FSMStateStream]             │
│                GET /observability/* ───────────────────► [BlastRadiusGraph (React Flow/D3)]      │
│                                                          [ElevationQueue]                        │
│  WRITE (biometric-gated):  [ElevationApprovalModal] ──► challenge → mic capture → POST audio     │
│                            [AuditLedgerView] ◄── GET /audit (read-only projection)               │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
        │ SSE/GET (read, reuse)                          │ POST /authorize-elevation (NEW write-path)
        ▼                                                ▼
  ide_observability_stream (existing)        Biometric Auth Middleware (NEW, this spec)
   + ADDITIVE event types (§3)                 challenge-issuer → ECAPA verify (reuse EcapaFacade)
                                               → anti-spoof + freshness → CALL the existing
                                               CRITICAL_ELEVATION approval path → Immutable Audit Ledger
```
Two backends, deliberately split by risk: the **read feed stays the existing authority-free observability layer**; the **write-path is a new, isolated, biometric-gated service** that is the ONLY thing able to act, and even it only calls the existing approval path (it cannot bypass the quarantine).

## 3. The Read Path — Live Visualization
- **Additive SSE event types** (extend `ide_observability_stream`, backward-compatible): `fsm_phase_changed` (the 11-phase Ouroboros state), `cross_repo_elevation_pending` (a CRITICAL_ELEVATION PR awaiting auth, with its blast-radius + PR ref), `dag_node_updated`, `sovereign_yield` (FRACTURE/QUARANTINE/RECOVERED). These ride the existing broker (bounded, replay, heartbeat) — no new transport.
- **`DAGCanvas`** — the active execution DAG (the L3 work-units / the op graph), live node-state coloring (pending/running/applied/fractured) from the SSE stream.
- **`FSMStateStream`** — the real-time Ouroboros phase ribbon (CLASSIFY→…→COMPLETE) with per-op provider/route/risk-tier badges; replaces watching `debug.log`.
- **`BlastRadiusGraph` (the operator constraint):** an interactive React Flow (or D3-force) node graph rendered directly from `BlastRadiusReport.to_dict()`. The mutated symbol at the center; edges to every `directly_affected` + `transitively_affected` dependent; **nodes color-coded by repo** (Body=blue / Mind=amber / Nerves=violet) so the cross-boundary blast is visually obvious. Click a node → its source excerpt. When the AI proposes a cross-repo PR, the operator *explores the Body↔Nerves dependency map in the browser* before authorizing. Read-only, authority-free.

## 4. The Write Path — Cryptographic Biometric Edge-Gate
`backend/core/ouroboros/governance/command_node/biometric_auth_middleware.py` (NEW). `POST /authorize-elevation` REJECTS all passwords/static tokens. The flow:

1. **Challenge issue** (`GET /elevation/{pr_id}/challenge`): the server mints a **single-use, TTL-bounded challenge**: `{nonce: random-256-bit, phrase: randomized time-based challenge phrase, pr_id, blast_radius_hash, issued_at, ttl_s}`. The nonce is stored server-side (pending, unconsumed). The phrase is randomized per request (the operator must speak THIS phrase — a static enrollment recording can't answer it).
2. **Capture** (UI): the `ElevationApprovalModal` displays the challenge phrase + the blast-radius graph for the PR; captures an audio payload of the operator speaking the phrase.
3. **Verify** (`POST /authorize-elevation`, payload = `{pr_id, nonce, audio}`):
   - **Freshness / anti-replay (mathematical):** the nonce must exist, be unconsumed, and be within TTL → else REJECT. The nonce is **consumed atomically** on use (a replay of the same audio+nonce fails — nonce already spent). The audio is bound to THIS nonce + pr_id (the response only authorizes the PR the challenge was issued for).
   - **Biometric (reuse `EcapaFacade`):** extract the ECAPA-TDNN embedding from the audio; cosine-score against the master voice-print; require `>= VBI_BAYESIAN_AUTH_THRESHOLD` (0.85). **Anti-spoof (reuse):** the existing physics/VTL anti-spoof + drift checks must pass (defeats a recording/replay-of-audio attack at the signal level too).
   - **(Optional) phrase-match:** ASR-verify the spoken content == the challenge phrase (binds *what was said* to the fresh nonce — defeats replaying old audio of the right voice).
   - **Decision:** biometric-match AND anti-spoof-pass AND fresh-nonce → **authorize**: call the EXISTING CRITICAL_ELEVATION approval path (`critical_elevation` / `OrangePRReviewer` merge-approve) for that PR. Else → REJECT.
   - **THE LAW HOLDS:** the approval path itself enforces the Immutable Orange floor — if `pr.target_repo in {prime, reactor}`, the merge is STILL refused (the biometric only lifts the *operator hard-halt* for Body cross-repo PRs; it can NEVER make a Mind/Nerves PR merge). The biometric is necessary, not sufficient — the backend laws compose on top.
4. **Transport:** the write-path is served over TLS only; the audio never persists (processed in-memory, only its hash is retained for the audit ledger). Zero-Trust: NO session bypasses re-auth — every approval requires a fresh challenge + fresh biometric. There is no "stay logged in" for a governance write.

## 5. The Immutable Audit Ledger
`command_node/biometric_audit_ledger.py` (NEW, reuses the GraduationLedger durable-JSONL append pattern). On EVERY authorization attempt (success AND failure), append an immutable record:
`{ts, pr_id, target_repo, ast_mutation_id, blast_radius_hash, challenge_nonce, voiceprint_id, ecapa_score, antispoof_verdict, freshness_ok, decision: AUTHORIZED|REJECTED, audio_sha256, record_hash}`.
- **Cryptographic chaining:** `record_hash = sha256(prev_record_hash + this_record_payload)` — a hash-chained ledger; tampering with any past record breaks the chain (verifiable). Append-only at `.jarvis/command_node_audit.jsonl`.
- **The binding:** an immutable, verifiable record of exactly *which voice-print authorized which AST mutation* — the `audio_sha256` + `ecapa_score` + `ast_mutation_id` + `record_hash` chain. Surfaced read-only in the `AuditLedgerView`.
- Composes with the existing GraduationLedger (the trust-ledger graduation) — the audit ledger is the *who-authorized* record; the trust ledger is the *earned-trust* record.

## 6. UI Component Architecture (Next.js/React)
```
app/
  page.tsx                      # mission-control layout (3-region: DAG | FSM stream | blast/queue)
  components/
    DAGCanvas.tsx               # live op/work-unit DAG (SSE-driven)
    FSMStateStream.tsx          # 11-phase Ouroboros ribbon + badges
    BlastRadiusGraph.tsx        # React Flow node graph from BlastRadiusReport.to_dict()
    ElevationQueue.tsx          # pending CRITICAL_ELEVATION PRs (from cross_repo_elevation_pending)
    ElevationApprovalModal.tsx  # challenge phrase + blast graph + mic capture + verdict
    AuditLedgerView.tsx         # read-only hash-chained audit ledger
  hooks/
    useSovereignStream.ts       # SSE client (Last-Event-ID replay, exp-backoff reconnect, bounded buffer)
    useBlastRadius.ts           # GET /observability + blast-radius fetch
    useBiometricAuth.ts         # challenge → capture → POST → verdict
  lib/api.ts                    # typed client for the read GETs + the write POST
```
- **Stack:** Next.js (App Router) + React + TypeScript + React Flow (graph) + Web Audio API (mic capture) + a typed SSE client (mirror the IDE extensions' native-fetch SSE parser + exp-backoff+jitter reconnect).
- **Boot-safe:** the dashboard degrades gracefully if the backend is down (poll fallback, the IDE-extension pattern). The approval modal is unreachable unless an elevation is pending.

## 7. Cross-cutting / Invariants
- **The UI is never an authority.** Every write goes through the existing CRITICAL_ELEVATION approval path; the Immutable Orange floor + the trust ledger + fail-CLOSED all compose underneath and CANNOT be weakened by the UI. A valid biometric on a Mind/Nerves PR still cannot merge it.
- **Zero-Trust write:** no static credential; every approval = a fresh server-issued challenge + a fresh live biometric + anti-spoof + a consumed single-use nonce. Replay is mathematically defeated (nonce + freshness + anti-spoof + phrase-binding).
- **Read/write split by risk:** the read feed reuses the authority-free observability layer (low risk, can be remote); the write-path is an isolated biometric-gated service (TLS, no persistence of audio, hash-chained audit).
- **Fail-CLOSED:** any error in the biometric/freshness/anti-spoof chain → REJECT (never authorize on uncertainty). A missing voice-print or a down ECAPA pipeline → no authorization possible (the CLI approval path remains the fallback, as today).
- **Default-OFF + gated:** the write-path service is gated (`JARVIS_COMMAND_NODE_AUTH_ENABLED`, default false); the dashboard read-only mode works without it.
- **Reuse-first:** SSE feed, EcapaFacade + VBIConfig + anti-spoof, compute_blast_radius, the CRITICAL_ELEVATION path, the GraduationLedger pattern. New code = the React app + the biometric middleware (challenge/freshness/verify-orchestration) + the hash-chained audit ledger.

## 8. Phasing
1. **Phase 1 — Read-only dashboard:** the Next.js app + `useSovereignStream` (SSE consumer) + DAGCanvas + FSMStateStream + BlastRadiusGraph + the additive SSE event types. $0, no write-path, no new authority. Delivers the visualization value immediately.
2. **Phase 2 — Biometric write-path:** the `biometric_auth_middleware` (challenge issuer + freshness/anti-replay + EcapaFacade verify + anti-spoof) + the ElevationApprovalModal + the existing-approval-path wiring (Immutable Orange composed underneath) + the hash-chained audit ledger + the AuditLedgerView. Security review mandatory (it's a governance write-path).
3. **Phase 3 — Hardening:** TLS/transport, the audit-chain verifier tool, the phrase-match ASR binding, remote-access auth envelope.

## 9. Open Decisions (for operator review)
1. **Phrase-match ASR binding** (§4.3): include the optional ASR "did they say the challenge phrase" check (stronger anti-replay) vs ECAPA-embedding + anti-spoof + nonce alone? *Spec recommends including it — it's the strongest freshness binding.*
2. **Read-feed remoteness:** the dashboard read path remote-accessible (so you watch from anywhere) while the write-path stays TLS+biometric — confirm the read feed can leave loopback (it's authority-free, low-risk) or must it also stay local?
3. **Audit ledger location:** local `.jarvis/command_node_audit.jsonl` hash-chain (simple, tamper-evident) vs also mirroring to the Trinity bus/GCS for off-box immutability. *Spec assumes local hash-chain first.*
4. **Stack confirm:** Next.js + React Flow + Web Audio — or a preference (SvelteKit, plain React+D3)?
