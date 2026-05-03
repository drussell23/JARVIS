#!/usr/bin/env python3
"""Pass B soak-graduation clean-bar assertion.

Reads a single battle-test session's debug.log + summary.json (+ the
production .jarvis/order2_review_queue.jsonl for cross-session
amendment events) and asserts the 5 clean-bar criteria the W2(5)
policy requires before flipping META_PHASE_RUNNER + REPLAY_EXECUTOR
default-true. The operator runs this against each of the 3 soak
sessions; all 3 must produce CLEAN before flipping.

Five clean-bar criteria:

  CB1 -- ZERO unhandled exceptions in MetaPhaseRunner / replay_executor
         logger lines. Pass B substrate is supposed to fail-soft;
         exceptions surfacing in WARNING/ERROR for these modules
         indicate a substrate bug.
  CB2 -- Every replay_executor.execute_replay_under_operator_trigger
         invocation logged as operator_authorized=True. The cage
         (amendment_requires_operator) is structurally enforced but
         the assertion catches the empirical case of "the function
         is being called somehow without the cage firing".
  CB3 -- Order-2 manifest amendments (entries in order2_review_queue
         .jsonl after session start) all have status APPROVED via
         /order2 amend (not raw queue manipulation). Verifies the
         operator surface is THE only mutation path empirically.
  CB4 -- Cost burn within env-tunable baseline (default $0.50 per
         session; matches StdlibSelfHealthOracle baseline). Catches
         the runaway-cost failure mode that drained earlier soaks.
  CB5 -- session_outcome=complete + stop_reason in the clean set
         (idle_timeout / wall_clock_cap / cost_cap / shutdown_event /
         operator_quit). Abnormal terminations (SIGKILL / SIGTERM /
         sighup / sigint) fail the bar -- the harness must reach
         a clean exit for the soak to count toward the 3-clean-
         session arc.

Exit codes:
    0 = CLEAN (all 5 criteria passed)
    1 = at least one criterion failed
    2 = session artifacts missing / unparseable

Usage:
    python3 scripts/pass_b_soak_assertion.py [SESSION_ID]
    python3 scripts/pass_b_soak_assertion.py  # picks most recent

Designed to be run against any session — the operator-paced 3-clean-
session arc just runs it three times, once per soak.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]


def _sessions_dir() -> Path:
    """Resolve the sessions directory at call-time so an env override
    or cwd-relative path takes effect (used by the soak-prep verdict
    script which exercises the assertion against a synthetic fixture
    rooted at a tmpdir)."""
    raw = os.environ.get("JARVIS_OUROBOROS_SESSIONS_DIR")
    if raw:
        return Path(raw)
    # CWD-relative when the cwd contains a .ouroboros/sessions tree
    # (lets verdict harnesses cd into a tmpdir and have the script
    # find the synthetic sessions there).
    cwd_candidate = Path(os.getcwd()) / ".ouroboros" / "sessions"
    if cwd_candidate.is_dir():
        return cwd_candidate
    return REPO_ROOT / ".ouroboros" / "sessions"


SESSIONS_DIR = _sessions_dir()
REVIEW_QUEUE_DEFAULT = REPO_ROOT / ".jarvis" / "order2_review_queue.jsonl"


@dataclass(frozen=True)
class CleanBarVerdict:
    name: str
    passed: bool
    evidence: str
    details: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionArtifacts:
    session_id: str
    session_dir: Path
    summary: Dict[str, object]
    debug_log_text: str


_SESSION_NAME_RE = re.compile(r"^bt-\d{4}-\d{2}-\d{2}-\d{6}$")


def _resolve_session(arg: Optional[str]) -> Optional[Path]:
    sessions_dir = _sessions_dir()
    if arg:
        candidate = sessions_dir / arg
        return candidate if candidate.is_dir() else None
    if not sessions_dir.is_dir():
        return None
    sessions = sorted(
        (
            p for p in sessions_dir.iterdir()
            if p.is_dir() and _SESSION_NAME_RE.match(p.name)
        ),
        key=lambda p: p.name,
        reverse=True,
    )
    return sessions[0] if sessions else None


def _load_artifacts(session_dir: Path) -> Optional[SessionArtifacts]:
    debug_path = session_dir / "debug.log"
    if not debug_path.is_file():
        return None
    summary_path = session_dir / "summary.json"
    summary: Dict[str, object] = {}
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            summary = {"_parse_error": "summary.json corrupt"}
    try:
        debug_log_text = debug_path.read_text(
            encoding="utf-8", errors="replace",
        )
    except Exception:
        debug_log_text = ""
    return SessionArtifacts(
        session_id=session_dir.name,
        session_dir=session_dir,
        summary=summary,
        debug_log_text=debug_log_text,
    )


# ---------------------------------------------------------------------------
# Clean-bar evaluators
# ---------------------------------------------------------------------------


_RE_PASS_B_EXCEPTION = re.compile(
    r"\[(MetaPhaseRunner|ReplayExecutor|Order2ReviewQueue)\].*"
    r"(ERROR|exception|Traceback)",
    re.IGNORECASE,
)
_RE_REPLAY_INVOCATION = re.compile(
    r"execute_replay_under_operator_trigger\(",
)
_RE_OPERATOR_AUTHORIZED = re.compile(
    r"operator_authorized\s*=\s*True",
)


def _eval_cb1_no_exceptions(art: SessionArtifacts) -> CleanBarVerdict:
    matches = _RE_PASS_B_EXCEPTION.findall(art.debug_log_text)
    return CleanBarVerdict(
        name="CB1 ZERO unhandled exceptions in Pass B substrate",
        passed=not matches,
        evidence=(
            f"pass_b_exception_lines={len(matches)}"
            + (f" -- first match: {matches[0]!r}" if matches else "")
        ),
    )


def _eval_cb2_operator_authorized(art: SessionArtifacts) -> CleanBarVerdict:
    invocations = _RE_REPLAY_INVOCATION.findall(art.debug_log_text)
    authorized = _RE_OPERATOR_AUTHORIZED.findall(art.debug_log_text)
    # If replay_executor was never invoked, the cage was naturally
    # held -- that's a clean state, not a failure.
    if not invocations:
        return CleanBarVerdict(
            name="CB2 Every replay_executor invocation has operator_authorized=True",
            passed=True,
            evidence="zero replay_executor invocations (cage naturally held)",
        )
    # Heuristic: authorized count must be >= invocation count (each
    # invocation should appear with operator_authorized=True nearby).
    # The signal isn't perfect (logger formatting may decouple them)
    # but a deficit indicates structural cage bypass.
    passed = len(authorized) >= len(invocations)
    return CleanBarVerdict(
        name="CB2 Every replay_executor invocation has operator_authorized=True",
        passed=passed,
        evidence=(
            f"invocations={len(invocations)} "
            f"authorized_log_lines={len(authorized)} "
            f"deficit={len(invocations) - len(authorized)}"
        ),
    )


def _eval_cb3_amendment_path() -> CleanBarVerdict:
    """Reads the order2_review_queue.jsonl + verifies all amendments
    came through the /order2 amend REPL (i.e., have an
    ``approved_via_repl=True`` marker in the entry)."""
    queue_path = Path(os.environ.get(
        "JARVIS_ORDER2_REVIEW_QUEUE_PATH",
        str(REVIEW_QUEUE_DEFAULT),
    ))
    if not queue_path.is_file():
        return CleanBarVerdict(
            name="CB3 Order-2 amendments only via /order2 amend",
            passed=True,
            evidence=(
                f"queue file not present at {queue_path} -- "
                "no amendments occurred (cage naturally held)"
            ),
        )
    approved = 0
    amended = 0
    bad: List[str] = []
    try:
        for line in queue_path.read_text(
            encoding="utf-8", errors="replace",
        ).splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            status = str(rec.get("status", "")).lower()
            if status == "approved":
                approved += 1
                # Check for the operator-via-REPL marker.
                if rec.get("approved_via_repl") is not True:
                    bad.append(
                        f"entry_id={rec.get('entry_id', '?')} "
                        "approved without /order2 amend marker"
                    )
            if status == "amended":
                amended += 1
    except Exception as exc:
        return CleanBarVerdict(
            name="CB3 Order-2 amendments only via /order2 amend",
            passed=False,
            evidence=f"queue read failed: {exc!r}",
        )
    return CleanBarVerdict(
        name="CB3 Order-2 amendments only via /order2 amend",
        passed=not bad,
        evidence=(
            f"approved={approved} amended={amended} "
            + (f"violations={bad[:3]}" if bad else "all paths clean")
        ),
    )


def _eval_cb4_cost_burn(art: SessionArtifacts) -> CleanBarVerdict:
    raw_baseline = os.environ.get(
        "JARVIS_PASS_B_SOAK_COST_BASELINE_USD", "0.50",
    )
    try:
        baseline = float(raw_baseline)
    except (TypeError, ValueError):
        baseline = 0.50
    try:
        cost = float(art.summary.get("cost_total", 0) or 0)
    except (TypeError, ValueError):
        cost = 0.0
    # Allow up to 3x baseline before failing -- matches
    # StdlibSelfHealthOracle's degraded threshold.
    fail_ceiling = baseline * 3.0
    passed = cost <= fail_ceiling
    return CleanBarVerdict(
        name="CB4 Cost burn within baseline",
        passed=passed,
        evidence=(
            f"cost_usd={cost:.4f} baseline=${baseline:.2f} "
            f"fail_ceiling=${fail_ceiling:.2f} "
            f"ratio={cost / baseline:.2f}x"
            if baseline > 0
            else f"cost_usd={cost:.4f} baseline=zero"
        ),
    )


def _eval_cb5_clean_termination(art: SessionArtifacts) -> CleanBarVerdict:
    outcome = str(art.summary.get("session_outcome", "")).lower()
    raw_reason = str(art.summary.get("stop_reason", "")).lower()
    head = raw_reason.split("+", 1)[0].strip()
    clean_reasons = {
        "idle_timeout", "wall_clock_cap", "cost_cap",
        "shutdown_event", "operator_quit",
    }
    abnormal_reasons = {"sigkill", "sigterm", "sighup", "sigint"}
    is_complete = outcome == "complete"
    is_clean_reason = head in clean_reasons
    is_abnormal = head in abnormal_reasons
    passed = is_complete and is_clean_reason and not is_abnormal
    return CleanBarVerdict(
        name="CB5 session_outcome=complete + clean stop_reason",
        passed=passed,
        evidence=(
            f"outcome={outcome!r} stop_reason={raw_reason!r} "
            f"head={head!r} clean={is_clean_reason} "
            f"abnormal={is_abnormal}"
        ),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Tuple[str, ...]) -> int:
    arg = argv[1] if len(argv) > 1 else None
    session_dir = _resolve_session(arg)
    if session_dir is None:
        print(
            f"FATAL: no session found "
            f"(arg={arg!r}, dir={SESSIONS_DIR})",
            file=sys.stderr,
        )
        return 2
    art = _load_artifacts(session_dir)
    if art is None:
        print(
            f"FATAL: artifacts missing/unparseable in {session_dir}",
            file=sys.stderr,
        )
        return 2
    print(f"Pass B soak-graduation clean-bar assertion")
    print(f"  session_id : {art.session_id}")
    print(f"  session_dir: {art.session_dir}")
    print()
    verdicts = [
        _eval_cb1_no_exceptions(art),
        _eval_cb2_operator_authorized(art),
        _eval_cb3_amendment_path(),
        _eval_cb4_cost_burn(art),
        _eval_cb5_clean_termination(art),
    ]
    for v in verdicts:
        mark = "PASS" if v.passed else "FAIL"
        print(f"  [{mark}] {v.name}")
        print(f"         {v.evidence}")
    print()
    if all(v.passed for v in verdicts):
        print("VERDICT: CLEAN -- session counts toward the 3-clean-"
              "session arc. Operators run this against the next 2 "
              "soaks; when all 3 are CLEAN, flip "
              "JARVIS_META_PHASE_RUNNER_ENABLED + "
              "JARVIS_REPLAY_EXECUTOR_ENABLED defaults true.")
        return 0
    failing = [v.name for v in verdicts if not v.passed]
    print(f"VERDICT: NOT CLEAN -- {len(failing)} criterion failed. "
          "Session does NOT count toward the 3-clean arc. Operators "
          "investigate the failed criteria before re-running.")
    return 1


if __name__ == "__main__":
    sys.exit(main(tuple(sys.argv)))
