#!/usr/bin/env bash
# =============================================================================
# pack_sovereign_release.sh — Slice 139: The Sovereign Artifact Packager
# Bundles the JARVIS-AI-Agent repo into a lean, deployable .tar.gz for migration
# to the industrial Linux host.
#
#   EXCLUDES the bulk + the dangerous: .venv, .git, __pycache__, *.pyc,
#            .pytest_cache, node_modules, .claude/worktrees, AND the 600M+ of
#            regenerable .jarvis caches/ledgers.
#   EXCLUDES secrets: .env is NEVER baked into the artifact (transferred
#            out-of-band by migrate_to_host.sh).
#   PRESERVES the load-bearing .jarvis crypto + signatures + evidence/memory via
#            an explicit allowlist (Ed25519 pubkey + salt + meta + signed roadmap
#            + the tamper-evident evidence chain + episodic memory + warm vector
#            index) so the organism wakes up authorized and remembering.
#
# Usage:  ./scripts/pack_sovereign_release.sh [output.tgz]
# Output: dist/jarvis-sovereign-<gitsha>.tgz  (default)
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
SHA="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
OUT="${1:-$REPO_ROOT/dist/jarvis-sovereign-$SHA.tgz}"
mkdir -p "$(dirname "$OUT")"

log() { printf '\033[36m[pack]\033[0m %s\n' "$*"; }

command -v rsync >/dev/null 2>&1 || { echo "[pack] FATAL: rsync required"; exit 1; }

# The ONLY .jarvis entries that travel — crypto, signatures, evidence, memory,
# and (Slice 205) the load-bearing OPERATIONAL-STATE ledgers. Everything else
# under .jarvis is regenerable local state and is left behind.
#
# Slice 205 — state portability: without the operational ledgers below, a
# migration to a new host would leave the evolutionary history behind and
# regenerate it from zero (the "wiped history" failure the cluster ask was
# really about). Carrying them on the EXISTING offline migration path is the
# correct mechanism — no live cluster, no hot-swap. Chronos records the
# cross-host boundary honestly (new image_id = supervised migration →
# total_operational chains, unsupervised_interval resets — no laundering).
_JARVIS_ALLOWLIST=(
  # crypto + signatures + dissertation + memory (pre-205)
  layer4_operator.pub
  layer4_key.salt
  layer4_key.meta.json
  roadmap.signed.yaml
  roadmap.draft.yaml
  dissertation_evidence.jsonl
  episodic_memory.jsonl
  semantic_index.npz
  # operational-state ledgers (Slice 205) — evolutionary history travels
  observability_registry.bin    # Slice 193 — hedge/registry counters
  chronos_coherence.json        # Slice 204 — uptime continuity chain
  m10_graduation_state.json     # Slice 197 — autonomous graduation state
  bandit_router_state.json      # Slice 201 — learned model posteriors
  # single-use markers — so the new host doesn't re-fire / re-propose
  genesis_proposal.done         # Slice 200 — milestone PR single-use sentinel
  .strategy_proposal_marker     # Slice 203 — strategic-proposal dedup marker
)

STAGE_PARENT="$(mktemp -d)"
STAGE="$STAGE_PARENT/jarvis-sovereign"
trap 'rm -rf "$STAGE_PARENT"' EXIT
mkdir -p "$STAGE"

log "Staging clean tree (excluding .venv / .git / .env / caches / .jarvis bulk)…"
rsync -a \
  --exclude='.venv' \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  --exclude='.mypy_cache' \
  --exclude='node_modules' \
  --exclude='.claude/worktrees' \
  --exclude='dist' \
  --exclude='.jarvis' \
  `# Slice 191 — mirror the .dockerignore bulk excludes so the payload is LEAN. These are` \
  `# build artifacts + model weights + logs (NOT source); the remote host rebuilds deps and` \
  `# compiled extensions for its OWN arch via docker compose build, so Mac arm64 binaries` \
  `# must not travel anyway. Without these the rsync drags ~20GB of untracked bulk.` \
  --exclude='.ouroboros' \
  --exclude='.cache' \
  --exclude='model_checkpoints' \
  --exclude='logs' \
  --exclude='venv' \
  --exclude='target' \
  --exclude='.build' \
  --exclude='*.so' \
  --exclude='*.dylib' \
  --exclude='*.a' \
  --exclude='*.rlib' \
  --exclude='*.rmeta' \
  --exclude='*.pt' \
  --exclude='*.pth' \
  --exclude='*.onnx' \
  --exclude='*.safetensors' \
  --exclude='*.gguf' \
  --exclude='*.mlmodel' \
  --exclude='*.mlmodelc' \
  --exclude='*.log' \
  ./ "$STAGE/"

log "Preserving load-bearing .jarvis crypto + signatures + evidence (allowlist)…"
mkdir -p "$STAGE/.jarvis"
_preserved=0
for f in "${_JARVIS_ALLOWLIST[@]}"; do
  if [ -f "$REPO_ROOT/.jarvis/$f" ]; then
    cp -p "$REPO_ROOT/.jarvis/$f" "$STAGE/.jarvis/$f"
    log "  + .jarvis/$f"
    _preserved=$((_preserved + 1))
  fi
done
[ "$_preserved" -gt 0 ] || log "WARNING: no .jarvis crypto/sig files found — provision + sign before packaging."

# Hard guarantee: no secret ever rode along.
if [ -f "$STAGE/.env" ]; then echo "[pack] FATAL: .env leaked into stage"; exit 2; fi

log "Compressing → $OUT"
tar czf "$OUT" -C "$STAGE_PARENT" "jarvis-sovereign"
SIZE="$(du -h "$OUT" | cut -f1)"
log "═══════════════════════════════════════════════════════════════"
log "ARTIFACT READY: $OUT ($SIZE, git=$SHA)"
log "  .jarvis crypto/sig/evidence preserved: $_preserved file(s)"
log "  .env + .venv + .git + caches: EXCLUDED"
log "  Next: ./scripts/migrate_to_host.sh user@host:/opt/jarvis $OUT"
log "═══════════════════════════════════════════════════════════════"
