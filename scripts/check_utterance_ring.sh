#!/usr/bin/env bash
# The utterance recorder must keep the MOST RECENT window of audio, in order.
#
# It used to stop writing once full, and `begin()` runs when the audio ENGINE
# starts — so the buffer filled with whatever the room was doing while nobody
# was talking, and the command the operator actually spoke arrived to find no
# space left. Measured live 2026-08-05: 345KB of audio delivered, "Audio
# appears to be silent", 0.00% confidence, unlock refused.
#
# There is no XCTest target for the HUD app, so this compiles the REAL source
# file together with a harness and exercises it. Testing a copy of the logic
# would prove nothing about the file that ships.
#
# Usage:  ./scripts/check_utterance_ring.sh
# Exit:   0 all checks pass · 1 a check failed · 2 toolchain missing
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${ROOT}/JARVIS-Apple/JARVISHUD/Services/UtteranceRecorder.swift"
HARNESS="${ROOT}/scripts/swift_checks/utterance_ring/main.swift"

if ! command -v swiftc > /dev/null 2>&1; then
    echo "ERROR: swiftc not found — cannot verify the utterance ring." >&2
    exit 2
fi
[ -f "${SRC}" ] || { echo "ERROR: missing ${SRC}" >&2; exit 2; }

OUT="$(mktemp -d)"
trap 'rm -rf "${OUT}"' EXIT
swiftc -o "${OUT}/run" "${HARNESS}" "${SRC}"
"${OUT}/run"
