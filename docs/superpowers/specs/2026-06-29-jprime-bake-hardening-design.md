# J-Prime Golden-Image Bake Hardening — Design / Decision Record

**Date:** 2026-06-29
**Author:** Derek J. Russell (+ O+V)
**Status:** Implemented; remote 0.5B validation → real 32B bake in progress
**Repos touched:** `jarvis-prime` (Layer 1), `JARVIS-AI-Agent` (Layer 2)

## Problem

The J-Prime QUALITY-tier golden-image bake had been fixed four times in sequence
(backports-strip → zstd-first → ollama daemon-pull → `HOME=/root` panic), each a
real root-cause fix — but each discovered only on real metal. That sequential
"passes-bake-fails-live" discovery loop is the standing root problem, the same
class as the wider C+ diagnosis. Hardening means attacking that *class*
structurally, not adding a fifth patch.

Two latent fragilities + one architectural drift remained:

1. **Single-shot pull** — one transient network reset on a ~20 GB pull wastes the
   whole bake.
2. **Presence, not integrity** — `ollama list | grep` proves only that the
   *manifest* exists; a truncated blob layer passes it and fails at runtime.
3. **3-place hardcode drift** — `jarvis-prime-coder-32b` / `qwen2.5-coder:32b`
   were independently hardcoded in `failover_tier.py` (runtime), the packer
   template, and `CloudBuildBaker.__init__` (and a 4th: the bake CLI's argparse
   defaults). Only comments kept them in sync; a model bump in one place would
   silently bake an image the provisioner never requests.

## Design

### Layer 1 — Packer template robustness (`jarvis-prime`)
`infra/packer/jprime_gpu_golden_image.pkr.hcl`, pull provisioner:

- **Dynamic disk preflight (no magic numbers).** Resolve the *exact* byte
  footprint of `${model_label}` from its Ollama registry manifest
  (`https://${ollama_registry}/v2/<ns>/<name>/manifests/<tag>`, sum
  `[.layers[].size] + [.config.size]`), apply a ×1.5 unpack/headroom heuristic,
  and assert live `df`. The required size *adapts* to whatever the model is — no
  GB is ever hardcoded **or parameterized** (a parameterized magic number is
  still a magic number). The only constant is the 1.5 overhead ratio (intrinsic,
  not a footprint guess). The manifest probe shares the bounded retry so a
  registry blip doesn't false-fail the preflight.
- **Resumable retry + backoff** on the pull. `ollama pull` keeps already-fetched
  blobs, so a retry *resumes*. Vars `pull_max_attempts` (3), `pull_backoff_base_s`
  (20).
- **Integrity proof** via `ollama show` — dereferences the manifest + config blob;
  an incomplete/corrupt model fails here, unlike `list`. (`pull` exit-0 already
  sha256-verifies each blob; `show` is a second independent read path proving
  usability, kept alongside the `list|grep` presence check.)

### Layer 2 — Orchestration drift-kill (`JARVIS-AI-Agent`)
`failover_tier.quality_tier()` becomes the **single source of truth** — a public
accessor returning the QUALITY spec *unconditionally* (deliberately bypassing the
`quality_tier_enabled()` cost gate: that gate governs whether to *provision* a GPU
node at runtime, not what to *bake*; routing through `resolve_tier` would bake the
7B survival image when the gate is off — the drift bug itself).

- `CloudBuildBaker.__init__` derives `image_family` + `model` from
  `_quality_tier_defaults()` (→ `quality_tier()`), fail-soft to legacy literals.
  Explicit caller args still win (the validation bake path).
- `scripts/bake_gpu_golden_image.py` argparse defaults resolve from
  `quality_tier()` too, collapsing the 4th hardcode site.

Net: a bare `CloudBuildBaker()` / a default `--execute` can never manufacture the
wrong artifact, and one env var (`JARVIS_FAILOVER_QUALITY_{IMAGE,MODEL}`) moves
the provisioner *and* the baker together.

## Validation discipline
- Layer 2: pure-Python regression tests (`test_cloud_build_baker_drift.py` ×5,
  `test_failover_tier_router.py` +3) — 50/50 green incl. existing baker/IAM suites.
- Layer 1: static gates (bash `-n`, live registry byte-math dry-run, HCL `${}`
  escaping invariant scan) → then the authoritative proof: a **remote 0.5B bake
  via CloudBuildBaker** (faithful to the production path — ephemeral IAM, zone
  failover), exercising every new step on real metal before the 32B spend.

## Ship sequence
1. Layer 1 + Layer 2 edits (working tree).
2. Remote 0.5B validation bake (reads working-tree spec + baker) → green.
3. Commit + PR + merge both repos to `main`.
4. Ignite the real 32B bake via `CloudBuildBaker.bake_with_ephemeral_iam()`.

## Root causes found DURING validation (the real "passes-static-fails-live" payload)

The remote 0.5B validation surfaced two latent failures that static checks + manual
SSH had never exercised — both fixed in Layer 1:

1. **`${var.model_label}` inside a `variable` *description*** → `packer init`
   failure ("Variables may not be used here"). Interpolation is illegal in a
   variable block even in prose. Caught in ~30s via a faithful
   `hashicorp/packer:latest` Docker `init`/`validate` repro — not a slow cloud
   bake. (Lesson: the escaping scan's "`${var.` is always valid" heuristic has a
   blind spot for non-interpolatable contexts.)
2. **`set -o pipefail` under packer's default `/bin/sh` = dash** (Debian DLVM) →
   `Illegal option -o pipefail`, killing provisioner line 2 on **every** prior
   Cloud Build bake. The earlier "test6 green" was validated by manual SSH in an
   interactive *bash* shell, never through packer's dash provisioner. Fixed with
   `inline_shebang = "/bin/bash -e"` on all three provisioners (dash-fails /
   bash-works confirmed in `debian:11`; pipefail is load-bearing for the new
   pipe-heavy pull steps). This was the actual long-standing blocker.

Validation proof: build `912b7aca` SUCCESS, all 3 steps green, image READY; the new
disk-preflight ran on real metal (`need_bytes=397821516 required_x1.5=596732274
avail_bytes=122860478464`, `[disk-preflight] OK`). Reusable fast gate added to the
workflow: container `packer init`+`validate` before any remote bake.

## Non-goals / rejected
- A static/parameterized `min_free_disk_gb` — rejected as a hardcoded guess.
- Local `packer build` for the real bake — rejected; we do not bypass the
  Zero-Trust ephemeral-IAM remote path for local convenience.
