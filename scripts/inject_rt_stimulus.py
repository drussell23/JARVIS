#!/usr/bin/env python3
"""Slice 27 — sterile real-time-route stimulus injector.

Purpose: empirically prove the Tier-1 (Claude) real-time routing during a
verification soak WITHOUT touching the core state machine or mutating any
production source. It drops ONE synthetic failing test file into the
TestWatcher's watched directory; the existing sense→route→dispatch chain
does the rest:

    TestWatcher (2-consecutive-poll debounce, fs.changed fast path)
      → TestFailureSensor  (source="test_failure", urgency="high")
      → UrgencyRouter      (high_urgency_immediate_source:test_failure → IMMEDIATE)
      → CandidateGenerator (IMMEDIATE = Claude direct; and per the sealed
                            topology, STANDARD/COMPLEX ALSO cascade_to_claude —
                            so the Claude proof survives even the headless
                            sensor-demotion edge case)

Sterility properties:
  * ZERO production-source mutation — one new test file, marker-tagged.
  * The synthetic test imports a REAL source module (event_loop_governance)
    so Slice 6 test→source attribution resolves via direct_import — no
    AttributionUnresolved edge. Even a full pipeline run stays contained:
    workspace promotion is default-OFF (quarantine workspace) and test-only
    mutations floor at NOTIFY/APPROVAL tiers.
  * --revert deletes the file ONLY if it carries the injection marker.
  * Refuses to overwrite an existing file at the target path.

Usage:
    python3 scripts/inject_rt_stimulus.py --inject   # drop the failing test
    python3 scripts/inject_rt_stimulus.py --status   # is a probe present?
    python3 scripts/inject_rt_stimulus.py --revert   # remove it (marker-checked)

Greppable proof lines in the soak debug.log after injection:
    "high_urgency_immediate_source:test_failure"   (route stamp)
    "IMMEDIATE route: Claude direct"               (dispatch)
    "cascade_to_claude"                            (topology block, STANDARD/COMPLEX)
    "[ClaudeProvider]"                             (actual Anthropic call)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

MARKER = (
    "RT-STIMULUS-PROBE (Slice 27) — synthetic failing test injected by "
    "scripts/inject_rt_stimulus.py; removable via --revert"
)

PROBE_REL = Path("tests") / "test_slice27_rt_stimulus_probe.py"

PROBE_BODY = f'''"""{MARKER}.

Deliberately-failing test: proves the TestFailure→IMMEDIATE→Claude routing
chain live. Imports a REAL source module so Slice 6 attribution resolves
via direct_import. The assertion is impossible by contract
(the sentence_transformer estimate is a positive MB count), so the red is
deterministic.

Target-choice constraint (learned live, bt-2026-07-15-223446): the imported
module must sit OUTSIDE the risk engine's self-mod sentinels (anything
matching "ouroboros/governance/", kernel or security surfaces) — an
attribution that lands inside the cage is BLOCKED pre-GENERATE with
reason self_modification_unsanctioned_source (the immune system working
as designed), which kills the dispatch proof.
"""
from backend.core.proactive_resource_guard import COMPONENT_MEMORY_ESTIMATES


def test_rt_stimulus_probe_deliberate_failure():
    # {MARKER}
    assert COMPONENT_MEMORY_ESTIMATES.get("sentence_transformer") == -1, (
        "RT stimulus probe: deliberate failure to exercise the "
        "TestFailure -> IMMEDIATE -> Claude dispatch chain"
    )
'''


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def inject(root: Path) -> int:
    target = root / PROBE_REL
    if target.exists():
        print(f"REFUSED: {target} already exists — revert first", file=sys.stderr)
        return 2
    target.write_text(PROBE_BODY, encoding="utf-8")
    print(f"INJECTED: {target}")
    print("NOTE: TestWatcher debounce requires the failure to be seen on two")
    print("consecutive polls before the signal fires; the fs.changed fast path")
    print("usually triggers the first scoped poll within seconds.")
    return 0


def revert(root: Path) -> int:
    target = root / PROBE_REL
    if not target.exists():
        print(f"CLEAN: {target} not present")
        return 0
    content = target.read_text(encoding="utf-8", errors="replace")
    if MARKER not in content:
        print(
            f"REFUSED: {target} exists but lacks the injection marker — "
            "not ours to delete",
            file=sys.stderr,
        )
        return 2
    target.unlink()
    print(f"REVERTED: {target} removed")
    return 0


def status(root: Path) -> int:
    target = root / PROBE_REL
    if not target.exists():
        print("status: absent")
        return 0
    marked = MARKER in target.read_text(encoding="utf-8", errors="replace")
    print(f"status: present (marker={'yes' if marked else 'NO — foreign file!'})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(__doc__ or "RT stimulus injector").splitlines()[0],
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--inject", action="store_true")
    g.add_argument("--revert", action="store_true")
    g.add_argument("--status", action="store_true")
    ap.add_argument(
        "--repo-root", type=Path, default=None,
        help="Repo root (default: parent of scripts/)",
    )
    args = ap.parse_args()
    root = args.repo_root or _repo_root()
    if args.inject:
        return inject(root)
    if args.revert:
        return revert(root)
    return status(root)


if __name__ == "__main__":
    raise SystemExit(main())
