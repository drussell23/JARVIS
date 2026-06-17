# Sovereign Cross-Host Migration — Architecture Design Document

> **Status:** DRAFT for operator review (brainstorm output; not yet a plan).
> **Author:** O+V architecture pass, 2026-06-16.
> **Goal:** Securely transport J.A.R.M.A.T.R.I.X. off the local 16 GB constraint boundary to a
> provisioned cloud host with the ambient RAM/accelerator headroom the Phase-9 Capstone needs —
> **without re-running the 29k-file cold index** and **without exposing secrets**.

---

## 1. Context & framing

The Oracle persistence arc concluded: SQLite is the canonical, memory-armored brain (default-ON).
The remaining blocker to the Phase-9 Capstone is **environmental** — the local host has ~3 GB free,
no `.env` in the worktree, and no accelerators. This ADD designs the protocol to move the organism
to a host that does.

**Crucial finding (drives the whole design): ~80% of this already exists.** The Slice 137–141
deployment matrix is production-ready. This ADD **extends three seams**; it does not rebuild the
pipeline. Every new component is justified against "why can't an existing one do this?"

### Existing infrastructure we build on (do NOT duplicate)
| Capability | Existing asset | Reused as-is? |
|---|---|---|
| Workstation→host orchestration | `scripts/migrate_to_host.sh` (pack→scp→ssh provision→launch) | Extended (new flags) |
| Lean artifact packaging | `scripts/pack_sovereign_release.sh` (rsync allowlist; `.env` hard-excluded; weights excluded) | Extended (brain allowlist) |
| Host IaC bootstrap | `deploy/provision_host.sh` (py3.11, venv, deps, perms) | Extended (accelerator step) |
| Crypto arm + systemd launch | `scripts/arm_and_launch.sh` + `deploy/*.service.template` | Reused as-is |
| Asymmetric operator key | `governance/sovereign_keys.py` (Ed25519, scrypt passphrase, never stored) | Reused (+ derive enc key) |
| Signed-roadmap authority | `governance/layer4_roadmap_authority.py` | Reused as-is |
| Credential allowlist | `scripts/launch_shadow_soak.sh` (`DOUBLEWORD_API_KEY`/`ANTHROPIC_API_KEY`/`HF_TOKEN`/`HUGGINGFACE_TOKEN`, bash-native parse, no `source .env`) | Reused as-is |
| State backup daemon | `governance/state_persistence_daemon.py` (rsync/s3/git of `.jarvis/`) | Reused as-is |
| GPU-capable image | `docker/Dockerfile.gcp-inference` (CUDA/CPU autodetect, PyTorch/Transformers/llama-cpp) | Reused as base |

### Provider & host-sizing (operator-confirmed: DoubleWord is primary)
**DoubleWord (DW 397B) is the primary LLM provider (Tier 0), with Claude as Tier-1 fallback** — both
are **remote APIs** (`api.doubleword.ai`, `api.anthropic.com`). The decisive consequence for this
design: **the migration host is an FSM *orchestration* host, not an LLM-serving host.** Generation
happens remotely; the host only runs the governance loop + the Oracle brain + two small local models.

- **The host is RAM-bound, not GPU-bound.** The reason to migrate is the **~5 GB Oracle graph** that
  doesn't fit the local 3 GB-free boundary — *that* is the headroom we need. A modest **16–32 GB VM
  with network egress to `api.doubleword.ai`** satisfies the core migration. No LLM-serving
  accelerator is required for generation.
- **Critical env-envelope payload:** `DOUBLEWORD_API_KEY` (Tier 0) + `ANTHROPIC_API_KEY` (Tier-1
  fallback). `HF_TOKEN`/`HUGGINGFACE_TOKEN` are only needed if §5's local models pull weights from HF.
- **Cloud target:** host can be **GCP** (assumed; J-Prime is GCP, the inference/Cloud-Run images
  exist) or any VM with DW network reach — the transport/brain layers are provider-agnostic (`user@host`
  SSH, as `migrate_to_host.sh` already is). Only §5's *optional* accelerator step is provider-specific,
  isolated behind a profile file.

## 2. Goals / Non-goals
**Goals:** (1) secrets encrypted at rest AND in transit, decryptable only by the operator passphrase;
(2) ship the initialized SQLite brain so the host warm-boots instead of cold-indexing; (3) a declared
accelerator-allocation map for Tiny Prime + Voice Biometrics, with a model-weight staging pipeline.
**Non-goals:** multi-node/HA clustering; autoscaling; rewriting the migration pipeline; shipping model
weights inside the lean artifact (they stage separately — §4). YAGNI on anything beyond a single
provisioned host.

---

## 3. Pillar 1 — Secure Environment Transport

### Problem with the status quo
`migrate_to_host.sh` `scp`s `.env` **out-of-band** — encrypted in transit by SSH, but it lands as
**plaintext at rest** on both ends and exists as a plaintext file during the transfer window. The
mandate is a cryptographic protocol with no plaintext exposure and no repo commit.

### Design: passphrase-sealed env envelope (reuses `sovereign_keys` primitives)
A new tiny module **`governance/env_envelope.py`** with two operations, built on the SAME crypto the
operator already uses (`hashlib.scrypt` + the `cryptography` dep that `provision_host.sh` already
verifies):

- **`seal`** (local): `key = scrypt(operator_passphrase, salt)` → `env.sealed = AESGCM(key).encrypt(.env)`.
  Writes `.jarvis/env.sealed` (ciphertext + nonce + the existing `layer4_key.salt`). The plaintext
  `.env` never leaves the workstation.
- **`unseal`** (host): prompts the same passphrase → re-derives the key from the shipped salt →
  decrypts to `.env` at mode `600`, then immediately `shred`s any temp.

Because the envelope is ciphertext, it can ride **inside the lean artifact** safely (no separate
out-of-band channel, no plaintext window) — the artifact already hard-excludes plaintext `.env`, and
`env.sealed` is added to the `pack_sovereign_release.sh` allowlist. The operator passphrase is the
single secret and is **never transported** (host operator types it, exactly like `arm_and_launch.sh`
already does for signing).

- **Key reuse, not new key management:** the salt is the existing `.jarvis/layer4_key.salt`; the
  passphrase is the existing operator passphrase. One secret, one mental model.
- **Alternative considered:** `age`/`sops` (X25519). Rejected for v1 — adds a binary dependency and a
  second key to manage; the scrypt+AESGCM path reuses what's already provisioned. (Noted as a future
  option for multi-operator teams.)
- **Integration:** `migrate_to_host.sh` gains `--sealed-env` (default when `env.sealed` exists): skip
  the out-of-band scp entirely; `provision_host.sh` calls `unseal` before the sanity-import step.

---

## 4. Pillar 2 — SQLite Brain Migration

### Problem
The lean artifact deliberately **excludes** the 600 MB+ `.jarvis` caches. But `oracle.db` is now the
**canonical brain**, and re-deriving it on the host means a 29k-file cold index — the exact
RAM-heavy operation we're migrating to escape. We must ship the initialized brain, portably and
verifiably.

### Design: opt-in, checkpointed, integrity-verified brain payload
- **Consistency before packing:** a new `scripts/checkpoint_brain.sh` (or a `--checkpoint` mode on the
  provider) runs `PRAGMA wal_checkpoint(TRUNCATE)` so the `-wal`/`-shm` fold into a single consistent
  `oracle.db`. The transient `-wal`/`-shm` are excluded from the artifact.
- **Allowlist extension (opt-in):** `pack_sovereign_release.sh --with-brain` adds
  `~/.jarvis/oracle/oracle.db` (and optionally `~/.jarvis/oracle/chroma/` for the semantic layer —
  `semantic_index.npz` is already allowlisted). Default packing stays lean; `--with-brain` is chosen
  when you want to skip the host cold index. `migrate_to_host.sh` forwards `--with-brain`.
- **Integrity on arrival — already built:** `AioSqliteProvider` already runs the corruption ladder
  (`PRAGMA quick_check` → quarantine → cold-index fresh) on load. So a torn/corrupt shipped brain is
  **self-healing**: the host quarantines it and cold-indexes (degraded but safe). No new verify code —
  the persistence layer's existing armor covers transport corruption.
- **Drift is free to tolerate:** the `file_hashes` table means a slightly-stale shipped brain just
  **incrementally tops up** the changed files on first boot via the memory-armored hot path. The
  shipped brain need not be byte-current; it only needs to avoid the full cold index.
- **Size reality:** ~265 MB for `backend/` (214k nodes); a full 3-repo brain is ~0.5–1 GB. That is the
  payload cost of skipping the cold index — acceptable and opt-in. A checksum (`sha256`) is recorded in
  the artifact manifest and verified post-extract before load.

### Why not just run the state-vault daemon's restore?
The `state_persistence_daemon` backs up `.jarvis/` continuously (rsync/s3/git) — that's the *ongoing*
durability channel and pairs perfectly here: after migration, point the daemon at the cloud vault so
the brain keeps replicating. But the **initial** seeding needs the brain *in the artifact* (or a
one-shot vault pull) so first boot is warm. The ADD uses artifact-seeding for the initial move and the
daemon for steady-state — each tool for its job, no duplication.

---

## 5. Pillar 3 — Component Staging for Tiny Prime (GPU/TPU) — OPTIONAL, off the critical path

### Scope note (DW-primary)
Because **generation is remote via DW (Tier 0)**, this host serves **no large LLM**. The only locally
accelerated workloads are two *small* models: **Tiny Prime** (the upcoming compact intent classifier —
today `intent_classifier.py` routes to remote J-Prime) and the **Voice Biometric** stack (ECAPA
192-dim, onnxruntime, `backend/voice_unlock/`). Both fit a **single modest GPU (e.g. L4)** or run on
**CPU** with the profile's `fallback`. **No A100/TPU is required**, and this pillar is **deferrable** —
the core migration (Pillars 1–2 + a RAM-adequate host) stands alone and unblocks the Phase-9 Capstone
without it. Build §5 only when the Tiny Prime / voice workloads actually land.

### Problem
There is currently **no GPU/TPU allocation** anywhere (only macOS Metal via `metal_accelerator.rs`).
Model weights are **excluded** from the lean artifact (`.onnx`/`.pt`/`.so`/`.dylib`), so Tiny Prime and
the ECAPA stack need a declarative device map + a weight-staging path when they do land.

### Design: a declarative accelerator profile + a weight-staging step
1. **`deploy/accelerator_profile.yaml`** (NEW — the single source of truth, no hardcoding). Declares
   per-component device requirements + weight sources, e.g.:
   ```yaml
   provider: gcp
   vm: { machine_type: g2-standard-8, accelerator: { type: nvidia-l4, count: 1 } }   # or tpu: v5e-1
   components:
     tiny_prime:        { device: cuda, fallback: cpu, weights: { source: hf,  repo: <org>/tiny-prime } }
     voice_biometric:   { device: cuda, fallback: cpu, weights: { source: gcs, uri: gs://<bucket>/ecapa } }
     oracle_embeddings: { device: cpu }   # fastembed/onnx — CPU is fine
   ```
2. **Provisioning extension:** `provision_host.sh` reads the profile and (GCP) installs the NVIDIA/CUDA
   driver stack (or configures the TPU runtime) only when `accelerator` is declared — CPU-only hosts
   skip it. The serving image is the existing **`docker/Dockerfile.gcp-inference`** (already CUDA/CPU
   autodetect); we add `onnxruntime-gpu` for the ECAPA CUDA execution provider.
3. **Weight staging (NEW `scripts/stage_models.sh`):** runs on the host AFTER `unseal` (needs
   `HF_TOKEN`/cloud creds from the now-decrypted `.env`). Pulls each component's weights from its
   declared source (`huggingface-cli download` via the allowlisted `HF_TOKEN`, or `gsutil` from GCS)
   into a host model dir, verifies `sha256`, and never bakes weights into the artifact or the image.
4. **Device binding:** components use the standard `torch.cuda.is_available()` / onnxruntime EP
   detection with the profile's `fallback` (so a GPU-less host degrades to CPU rather than crashing —
   the same graceful-degradation discipline as the Memory Armor). Tiny Prime serves like J-Prime (a
   local URL the `intent_classifier` async path targets, or in-process on the device); the voice
   service (`jarvis-voice-unlock.service`) pins the ECAPA model to its declared device.

### Why a profile file (not flags or hardcoded device strings)
Accelerators differ per host (L4 vs A100 vs TPU vs CPU-only). A declarative profile keeps the
provisioning, the container device-pinning, and the weight-staging all reading **one** authority —
swapping hosts or providers is editing YAML, not code. This mirrors the codebase's env-driven,
no-hardcoding convention.

---

## 6. End-to-end flow

```
LOCAL (workstation)
  1. env_envelope seal           → .jarvis/env.sealed         (plaintext .env never leaves)
  2. checkpoint_brain            → PRAGMA wal_checkpoint(TRUNCATE) on oracle.db
  3. pack_sovereign_release.sh --with-brain
       → dist/jarvis-sovereign-<sha>.tgz  (code + signed roadmap + crypto + env.sealed
                                            + oracle.db + accelerator_profile.yaml + manifest[sha256])
  4. migrate_to_host.sh user@host --sealed-env --with-brain --launch

TRANSPORT
  5. scp artifact over SSH        (no plaintext secrets anywhere in transit)

HOST (provisioned cloud)
  6. extract + verify manifest sha256 (artifact + brain integrity)
  7. provision_host.sh            → py3.11/venv/deps  + (if profile.accelerator) CUDA/TPU stack
  8. env_envelope unseal          → prompt passphrase → .env (mode 600)
  9. stage_models.sh              → fetch Tiny Prime + ECAPA weights (HF_TOKEN/GCS), verify sha256
 10. arm_and_launch.sh            → verify/sign roadmap + install reboot-surviving systemd units
 11. FIRST BOOT: Oracle loads shipped oracle.db (quick_check OK → warm, ~seconds);
                 file_hashes incrementally tops up any drift — NO 29k cold index.
 12. state_persistence_daemon     → repoint backup at the cloud vault for steady-state durability
```

## 7. Security / threat model
- **Operator passphrase** is the only secret; never stored, never transported (host operator types it).
- **`.env`** exists only as ciphertext outside the workstation; AESGCM is authenticated (tamper-evident).
- **Roadmap** stays Ed25519-signed + the un-signable floor (`layer4_roadmap_authority`) holds on the host.
- **Brain** is integrity-checked (`quick_check`) + sha256-manifested; corruption → quarantine + cold index.
- **Weights** fetched with allowlisted creds post-unseal; never in the repo, artifact, or image.
- **Repo hygiene:** `pack` keeps its hard `.env`-exclusion guarantee; `env.sealed` is ciphertext, safe to ship.

## 8. Failure modes & rollback
| Failure | Behavior |
|---|---|
| Wrong passphrase at unseal | AESGCM auth fails → abort provisioning (no partial .env) |
| Corrupt/torn brain in transit | `quick_check` fails → quarantine → cold-index fresh (self-heal, degraded) |
| GPU absent on host | profile `fallback: cpu` → degraded serving, no crash |
| Weight fetch fails | `stage_models.sh` non-zero → abort launch with a clear error (don't boot half-armed) |
| Stale shipped brain | `file_hashes` incremental top-up on first boot (no full reindex) |
| Whole migration bad | host is additive; archive/rollback by pointing systemd back / re-running with a prior artifact |

## 9. What is genuinely NEW vs reused
**New (small, focused):** `governance/env_envelope.py` (seal/unseal), `scripts/checkpoint_brain.sh`,
`scripts/stage_models.sh`, `deploy/accelerator_profile.yaml`, and **flags** on existing scripts
(`pack --with-brain`, `migrate --sealed-env --with-brain`, a CUDA/TPU step in `provision_host.sh`).
**Reused unchanged:** the entire migrate→pack→provision→arm pipeline, Ed25519 keys, roadmap authority,
credential allowlist, state-vault daemon, `Dockerfile.gcp-inference`, systemd templates.

## 10. Proposed implementation decomposition (each = its own spec→plan→build)
1. **Env Envelope** — `env_envelope.py` (seal/unseal, scrypt+AESGCM, reuse salt) + `pack`/`migrate`/
   `provision` wiring + tests. Independently shippable; smallest, highest-security-value.
2. **Brain Portability** — `checkpoint_brain` (WAL truncate) + `pack --with-brain` + manifest sha256 +
   post-extract verify-before-load. Leans entirely on the existing corruption ladder for safety.
3. **Accelerator Staging** — `accelerator_profile.yaml` + `provision_host.sh` CUDA/TPU step +
   `stage_models.sh` + device-binding/fallback for Tiny Prime + ECAPA. Largest; provider-specific.

> These three are independent and ordered by value+risk. **Slices 1 → 2 unblock the Phase-9 Capstone
> on their own** (env + brain + RAM-adequate host, DW remote for generation). **Slice 3 is optional /
> deferred** until the Tiny Prime + voice workloads actually land. Each goes through the normal
> spec → writing-plans → build cycle.

## 11. Open questions for the operator
**Settled (operator-confirmed):** DW is the primary provider ⇒ host is RAM-bound orchestration (no
LLM-serving GPU); critical secret is `DOUBLEWORD_API_KEY` (+ `ANTHROPIC_API_KEY` Tier-1). Pillar 3 is
optional/deferrable.

Remaining:
1. **Host sizing/provider** — a 16–32 GB VM with egress to `api.doubleword.ai`. GCP assumed; confirm,
   or name AWS/Azure/VPS (transport+brain are provider-agnostic; only optional §5 cares).
2. **Brain shipment** — ship `oracle.db` in the artifact (`--with-brain`, ~0.5–1 GB) vs seed it via a
   one-shot state-vault pull on the host? (Artifact is simpler; vault-pull keeps the artifact lean.)
3. *(Deferred until Tiny Prime/voice land)* accelerator type (L4 vs CPU) + weight source (HF vs GCS).
