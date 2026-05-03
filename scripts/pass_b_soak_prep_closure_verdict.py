#!/usr/bin/env python3
"""Empirical-closure verdict for Pass B soak preparation infrastructure
(Tier 3 #7 follow-up Arc 3).

This arc does NOT graduate META_PHASE_RUNNER + REPLAY_EXECUTOR -- it
ships the assertion infrastructure operators run during the W2(5)
3-clean-session arc + the operator-facing playbook.

Four primary contracts:

  C1 -- pass_b_soak_assertion.py exists, is executable as a script,
        and has the 5 named clean-bar criteria visible in source.
  C2 -- The assertion script correctly identifies a CLEAN session
        (synthetic fixture: stub session with clean termination).
  C3 -- The assertion script correctly identifies a NOT-CLEAN
        session (synthetic fixture: stub session with abnormal
        termination).
  C4 -- The operator-facing playbook is present in memory at
        memory/project_pass_b_soak_playbook.md and references the
        assertion script + the 5 criteria.

Exit codes:
    0 = all four primary contracts PASSED
    1 = at least one primary contract FAILED
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


REPO_ROOT = Path(__file__).resolve().parents[1]
ASSERTION_SCRIPT = REPO_ROOT / "scripts" / "pass_b_soak_assertion.py"
PLAYBOOK = (
    Path.home()
    / ".claude" / "projects"
    / "-Users-djrussell23-Documents-repos-JARVIS-AI-Agent"
    / "memory" / "project_pass_b_soak_playbook.md"
)


@dataclass(frozen=True)
class ContractVerdict:
    name: str
    passed: bool
    evidence: str
    details: Dict[str, object] = field(default_factory=dict)


def _eval_assertion_script_present() -> ContractVerdict:
    if not ASSERTION_SCRIPT.is_file():
        return ContractVerdict(
            name="C1 pass_b_soak_assertion.py present + 5 criteria visible",
            passed=False,
            evidence=f"missing: {ASSERTION_SCRIPT}",
        )
    src = ASSERTION_SCRIPT.read_text(encoding="utf-8")
    expected_criteria = ("CB1", "CB2", "CB3", "CB4", "CB5")
    missing = [c for c in expected_criteria if c not in src]
    return ContractVerdict(
        name="C1 pass_b_soak_assertion.py present + 5 criteria visible",
        passed=not missing,
        evidence=(
            f"size_bytes={ASSERTION_SCRIPT.stat().st_size} "
            f"criteria_found={len(expected_criteria) - len(missing)}/"
            f"{len(expected_criteria)}"
            + (f" missing={missing}" if missing else "")
        ),
    )


def _build_synthetic_session(
    parent: Path, session_id: str, summary: dict, debug_log: str = "",
) -> Path:
    sd = parent / ".ouroboros" / "sessions" / session_id
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "summary.json").write_text(
        json.dumps(summary), encoding="utf-8",
    )
    (sd / "debug.log").write_text(debug_log, encoding="utf-8")
    return sd


def _eval_clean_session_detection() -> ContractVerdict:
    """Build a synthetic session that satisfies all 5 criteria
    + run the assertion script against it. Expect exit 0."""
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)
        _build_synthetic_session(
            parent,
            session_id="bt-2026-05-03-999900",
            summary={
                "session_outcome": "complete",
                "stop_reason": "idle_timeout",
                "cost_total": 0.10,
                "duration_s": 600.0,
                "schema_version": 2,
            },
            debug_log=(
                # No Pass B exception lines, no replay_executor
                # invocations, no auth lines needed.
                "[Orchestrator] op=foo phase=verify passed\n"
                "[StdlibSelfHealthOracle] tick complete\n"
            ),
        )
        # Run assertion script with cwd=parent so it picks up our
        # synthetic .ouroboros/sessions/ tree.
        result = subprocess.run(
            [sys.executable, str(ASSERTION_SCRIPT)],
            cwd=str(parent),
            capture_output=True, text=True, timeout=30,
        )
        passed = result.returncode == 0
        return ContractVerdict(
            name="C2 Clean session correctly identified (exit 0)",
            passed=passed,
            evidence=(
                f"exit_code={result.returncode} "
                f"stdout_tail={result.stdout.splitlines()[-1] if result.stdout.splitlines() else ''!r}"
            ),
        )


def _eval_not_clean_session_detection() -> ContractVerdict:
    """Build a synthetic session with abnormal termination + run
    assertion. Expect exit 1 (NOT CLEAN)."""
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)
        _build_synthetic_session(
            parent,
            session_id="bt-2026-05-03-999901",
            summary={
                "session_outcome": "incomplete_kill",
                "stop_reason": "sigkill",
                "cost_total": 0.05,
                "duration_s": 60.0,
                "schema_version": 2,
            },
            debug_log="[Orchestrator] minimal log\n",
        )
        result = subprocess.run(
            [sys.executable, str(ASSERTION_SCRIPT)],
            cwd=str(parent),
            capture_output=True, text=True, timeout=30,
        )
        passed = result.returncode == 1
        return ContractVerdict(
            name="C3 NOT-CLEAN session correctly identified (exit 1)",
            passed=passed,
            evidence=(
                f"exit_code={result.returncode} "
                f"stdout_contains_NOT_CLEAN="
                f"{'NOT CLEAN' in result.stdout}"
            ),
        )


def _eval_playbook_present() -> ContractVerdict:
    if not PLAYBOOK.is_file():
        return ContractVerdict(
            name="C4 Operator-facing soak playbook present",
            passed=False,
            evidence=f"missing: {PLAYBOOK}",
        )
    text = PLAYBOOK.read_text(encoding="utf-8")
    expected_markers = (
        "pass_b_soak_assertion.py",
        "META_PHASE_RUNNER",
        "REPLAY_EXECUTOR",
        "3-clean-session",
        "CB1", "CB2", "CB3", "CB4", "CB5",
    )
    missing = [m for m in expected_markers if m not in text]
    return ContractVerdict(
        name="C4 Operator-facing soak playbook present",
        passed=not missing,
        evidence=(
            f"playbook_size={PLAYBOOK.stat().st_size} "
            f"markers_found={len(expected_markers) - len(missing)}/"
            f"{len(expected_markers)}"
            + (f" missing={missing}" if missing else "")
        ),
    )


def main() -> int:
    print("Empirical-closure verdict for Pass B Soak Prep (Arc 3)")
    print(f"  repo_root: {REPO_ROOT}")
    print()
    primary = [
        _eval_assertion_script_present(),
        _eval_clean_session_detection(),
        _eval_not_clean_session_detection(),
        _eval_playbook_present(),
    ]
    for v in primary:
        mark = "PASS" if v.passed else "FAIL"
        print(f"  [{mark}] {v.name}")
        print(f"         {v.evidence}")
    print()
    if all(v.passed for v in primary):
        print("VERDICT: Arc 3 (Pass B Soak Prep) EMPIRICALLY CLOSED "
              "-- all four primary contracts PASSED. Operators have "
              "the assertion infrastructure + playbook ready for the "
              "W2(5) 3-clean-session graduation arc.")
        return 0
    print("VERDICT: at least one primary contract FAILED -- "
          "Arc 3 not yet empirically closed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
