#!/usr/bin/env python3
"""Upgrade 2 (PRD §31.3) Slice 2 — Replay-determinism CLI.

Verifies the self-consistency of a session's DecisionRecord
ledger at ``.jarvis/determinism/<session-id>/decisions.jsonl``.

Examples:
    python3 scripts/replay_determinism.py --session bt-2026-04-29-074851
    python3 scripts/replay_determinism.py --session <id> --json
    python3 scripts/replay_determinism.py --session <id> --allow-disabled

Exit codes:
    0 — clean (zero drift, ≥1 record verified)
    1 — drift detected (one or more drift entries)
    2 — insufficient data (no records / file missing /
        master flag off)

This script is a **thin launcher** — all logic lives in
``backend.core.ouroboros.governance.determinism.replay_-
determinism`` so the primitive is unit-testable in isolation
without invoking subprocess. Mirrors the
``ouroboros_battle_test.py`` thin-launcher pattern.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add repo root to sys.path so the in-repo backend package
# resolves regardless of cwd. Mirrors ouroboros_battle_test.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main() -> int:
    from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
        replay_cli_main,
    )
    return replay_cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
