#!/usr/bin/env bash
# Slice 121 — The Adversarial Volume & Concurrency-Hardening Matrix.
#
# (Authorized as "The Hyper-Accelerated Temporal Simulation Matrix"; renamed for
# honesty.) Fans the Slice-115 Blue/Red cage siege across N concurrent producers
# and proves the tamper-evident evidence chain stays mathematically unbroken
# under heavy write pressure — yielding two defensible statistics:
#   • adversarial VOLUME/BREADTH (escape rate over a large mutated sample), and
#   • CONCURRENCY CORRECTNESS (the hash chain holds under load).
#
# IT DOES NOT COMPRESS THE 12-18 MONTH EVIDENCE CLOCK. Parallelism buys
# throughput, never calendar duration. This run COMPLEMENTS the T5 soak; it is
# NOT a substitute for it. The harness refuses to emit any "months simulated"
# attestation by design.
#
# Usage:  ./scripts/launch_simulation_matrix.sh --concurrency 50
set -euo pipefail
cd "$(dirname "$0")/.."

CONCURRENCY=8
while [ $# -gt 0 ]; do
  case "$1" in
    --concurrency) CONCURRENCY="${2:-8}"; shift 2 ;;
    --concurrency=*) CONCURRENCY="${1#*=}"; shift ;;
    *) shift ;;
  esac
done

export JARVIS_TEMPORAL_MATRIX_ENABLED=1
export JARVIS_TEMPORAL_MATRIX_CONCURRENCY="$CONCURRENCY"
export JARVIS_RED_BLUE_MATRIX_ENABLED=1   # the siege surfaces compose Slice 115

log() { printf '\033[36m[sim-matrix]\033[0m %s\n' "$*"; }

log "Adversarial Volume & Concurrency-Hardening Matrix — concurrency=$CONCURRENCY"
printf '\033[33m'
cat <<'BANNER'
  ┌────────────────────────────────────────────────────────────────────┐
  │  HONESTY INVARIANT: this measures adversarial VOLUME + chain         │
  │  integrity under concurrency. It is NOT time-compression. It does    │
  │  NOT advance or substitute for the 12-18 month T5 wall-clock soak.   │
  └────────────────────────────────────────────────────────────────────┘
BANNER
printf '\033[0m'

exec python3 -m backend.core.ouroboros.governance.temporal_matrix --concurrency "$CONCURRENCY"
