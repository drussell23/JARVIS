"""Tests for the A1 GraduationAuditor multi-session lineage-aware audit fix.

Run bt-iso-1783039338 produced an op suspended (FSM checkpoint) in a PRIOR
session, resumed in this one -- but the offline auditor (built for
single-session logs) could never prove ``fsm_classify_to_applied`` or
``lineage_verifiable`` for it: ``fsm_phase_changed`` / ``operation_terminal``
are SSE-only event types invisible to ``--log-file``-only static replay, and
the auditor had no notion of "this op's evidence spans two session dirs".

These tests cover:
  * cross-session ancestor discovery is dynamic (op_id correlation, NO
    hardcoded session names) and excludes the current + already-visited
    sessions;
  * an ancestor log's phase/hop evidence is stitched into the SAME auditor
    (fsm_classify_to_applied becomes provable from a resumed op's split
    history);
  * lineage_verifiable is HMAC-anchored via the HYDRATION-HANDSHAKE VERIFIED
    log record (fsm_checkpoint's OWN verify, reused not reimplemented);
  * edge cases: ancestor log missing, a lineage pointer cycle (bounded
    traversal, no hang), corrupted ancestor log lines (skip, keep going);
  * --replay downgrades ONLY an honest UNVERIFIABLE flag to a WARN --
    REJECTED still always fails (not a blanket lenient mode);
  * flag-family REJECT correlation is lineage-scoped the same way the
    intervention-lock already is (an unrelated op's legitimate gate
    rejection must not poison a resumed op's flag audit).

Synthetic event/log streams + tmp_path session dirs only -- no network, no
live soak.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the standalone script by path (it lives in scripts/, not a package).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "a1_graduation_auditor.py"
_spec = importlib.util.spec_from_file_location("a1_graduation_auditor", _SCRIPT)
assert _spec and _spec.loader
aud = importlib.util.module_from_spec(_spec)
sys.modules["a1_graduation_auditor"] = aud
_spec.loader.exec_module(aud)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sessions_root(tmp_path: Path) -> Path:
    d = tmp_path / ".ouroboros" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_session(root: Path, name: str, lines):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    log = d / "debug.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def _make_auditor(*, current_log_path=None, strict=True, replay=False, flags=None):
    return aud.A1GraduationAuditor(
        flags=flags if flags is not None else ["JARVIS_TEST_FLAG"],
        strict=strict,
        lineage_scoping_enabled=True,
        replay=replay,
        current_log_path=str(current_log_path) if current_log_path else None,
    )


# ===========================================================================
# 1. Cross-session ancestor discovery -- dynamic, no hardcoded paths
# ===========================================================================


def test_find_ancestor_session_logs_discovers_by_op_id_correlation(tmp_path):
    root = _sessions_root(tmp_path)
    older = _write_session(
        root, "bt-iso-1000", [
            "[A1Trace] emit goal=op-XYZ source=roadmap",
            "[A1Trace] accept goal=op-XYZ phase=CLASSIFY",
        ],
    )
    _write_session(root, "bt-iso-2000", ["unrelated content, nothing here"])
    current = _write_session(
        root, "bt-iso-3000", [
            "[A1Trace] emit goal=op-XYZ source=roadmap lineage=resumed "
            "original_emit_wall=999.0",
        ],
    )
    found = aud.find_ancestor_session_logs(str(current), "op-XYZ")
    assert found == [str(older)]


def test_find_ancestor_session_logs_excludes_current_and_future_sessions(tmp_path):
    root = _sessions_root(tmp_path)
    current = _write_session(root, "bt-iso-2000", ["[A1Trace] emit goal=op-A"])
    _write_session(root, "bt-iso-3000", ["[A1Trace] emit goal=op-A"])  # future
    found = aud.find_ancestor_session_logs(str(current), "op-A")
    assert found == []  # only a FUTURE session mentions it -- not an ancestor


def test_find_ancestor_session_logs_no_sessions_root_is_fail_soft(tmp_path):
    missing = tmp_path / "nope" / "debug.log"
    assert aud.find_ancestor_session_logs(str(missing), "op-A") == []


def test_find_ancestor_session_logs_bounded_by_visited_set(tmp_path):
    root = _sessions_root(tmp_path)
    older = _write_session(root, "bt-iso-1000", ["[A1Trace] emit goal=op-A"])
    current = _write_session(root, "bt-iso-2000", ["[A1Trace] emit goal=op-A"])
    # Simulate "already stitched" by pre-visiting the ancestor session name.
    visited = {older.parent.name}
    found = aud.find_ancestor_session_logs(str(current), "op-A", visited=visited)
    assert found == []


# ===========================================================================
# 2. Stitching splices ancestor evidence into the SAME auditor
# ===========================================================================


def test_stitching_makes_fsm_classify_to_applied_provable(tmp_path):
    """The ancestor session carries CLASSIFY; the resumed session carries the
    no-op-skip-APPLY + LEDGER_TERMINAL state=applied evidence. Neither alone
    (without the log-replay bridges + stitching) would prove the criterion."""
    root = _sessions_root(tmp_path)
    _write_session(
        root, "bt-iso-1000", [
            "[A1Trace] emit goal=op-RESUME-1 source=roadmap",
            "[A1Trace] accept goal=op-RESUME-1 phase=CLASSIFY",
        ],
    )
    current = _write_session(
        root, "bt-iso-2000", [
            "[HYDRATION-HANDSHAKE] HMAC-SHA256 VERIFIED op=op-RESUME-1 "
            "phase=CLASSIFY digest=abc123deadbeef...",
            "[A1Trace] emit goal=op-RESUME-1 source=roadmap lineage=resumed "
            "original_emit_wall=500.0",
            "[A1Trace] ingest goal=op-RESUME-1",
            "[A1Trace] dequeue goal=op-RESUME-1",
            "[A1Trace] submit goal=op-RESUME-1",
            "[A1Trace] accept goal=op-RESUME-1 phase=CLASSIFY",
            # Genuine mutation evidence (mutation-gate tightening,
            # run a1-brain-20260705-233225): a real APPLY phase heartbeat +
            # a written, NON-noop applied terminal. The old shape here (an
            # is_noop=True read_only_complete skip-APPLY) is now correctly
            # rejected by the gate — noop coverage lives in
            # test_a1_graduation_auditor.py section 10.
            "[CommProtocol] HEARTBEAT op=op-RESUME-1 seq=3 "
            "payload={'phase': 'APPLY'}",
            "[Slice74Probe] LEDGER_TERMINAL op_id=op-RESUME-1 state=applied "
            "written=True",
        ],
    )
    a = _make_auditor(current_log_path=current, flags=[])
    for line in current.read_text().splitlines():
        a.ingest_log_line(line)
    v = a.verdict()
    assert v.criteria["fsm_classify_to_applied"] is True
    assert v.criteria["lineage_verifiable"] is True
    assert str(root.parent / "sessions" / "bt-iso-1000" / "debug.log") in [
        str(Path(p)) for p in a.stitched_ancestor_logs
    ] or a.stitched_ancestor_logs  # ancestor was discovered + stitched


def test_stitching_is_recorded_in_verdict_lineage(tmp_path):
    root = _sessions_root(tmp_path)
    ancestor = _write_session(
        root, "bt-iso-1000", ["[A1Trace] accept goal=op-Q phase=CLASSIFY"],
    )
    current = _write_session(
        root, "bt-iso-2000", [
            "[A1Trace] emit goal=op-Q source=roadmap lineage=resumed "
            "original_emit_wall=1.0",
        ],
    )
    a = _make_auditor(current_log_path=current, flags=[])
    for line in current.read_text().splitlines():
        a.ingest_log_line(line)
    v = a.verdict()
    assert str(ancestor) in v.lineage["stitched_ancestor_logs"]
    assert "op-Q" in v.lineage["resumed_ops"]


# ===========================================================================
# 3. Edge case: ancestor log missing
# ===========================================================================


def test_ancestor_log_missing_lineage_unverifiable_no_crash(tmp_path):
    root = _sessions_root(tmp_path)
    current = _write_session(
        root, "bt-iso-2000", [
            "[A1Trace] emit goal=op-LONELY source=roadmap lineage=resumed "
            "original_emit_wall=1.0",
        ],
    )
    a = _make_auditor(current_log_path=current, strict=False, flags=[])
    for line in current.read_text().splitlines():
        a.ingest_log_line(line)  # must not raise
    v = a.verdict()
    assert v.criteria["lineage_verifiable"] is False
    assert v.proven is False
    assert any("no_ancestor_log" in r for r in a.lineage_stitch_failures), (
        "must record a CLEAR reason string, got %r" % (a.lineage_stitch_failures,)
    )


# ===========================================================================
# 4. Edge case: lineage pointer cycle -- bounded traversal, no hang
# ===========================================================================


def test_lineage_cycle_is_bounded_no_hang(tmp_path):
    """Two sessions each reference the SAME op_id with a lineage=resumed
    breadcrumb (a degenerate mutual-reference shape). Stitching must
    terminate (visited-set) rather than looping forever."""
    root = _sessions_root(tmp_path)
    _write_session(
        root, "bt-iso-1000", [
            "[A1Trace] emit goal=op-CYCLE source=roadmap lineage=resumed "
            "original_emit_wall=1.0",
            "[A1Trace] accept goal=op-CYCLE phase=CLASSIFY",
        ],
    )
    current = _write_session(
        root, "bt-iso-2000", [
            "[A1Trace] emit goal=op-CYCLE source=roadmap lineage=resumed "
            "original_emit_wall=1.0",
        ],
    )
    a = _make_auditor(current_log_path=current, strict=False, flags=[])
    start = time.monotonic()
    for line in current.read_text().splitlines():
        a.ingest_log_line(line)
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, "stitching must terminate quickly, not hang"
    # Bounded -- the op was only ever stitched once (visited-set worked).
    assert len(a._stitched_ops) <= 2  # noqa: SLF001 -- internal invariant check


# ===========================================================================
# 5. Edge case: corrupted ancestor log lines -- skip, keep stitching
# ===========================================================================


def test_corrupted_ancestor_lines_are_skipped_not_fatal(tmp_path):
    root = _sessions_root(tmp_path)
    ancestor_dir = root / "bt-iso-1000"
    ancestor_dir.mkdir(parents=True, exist_ok=True)
    ancestor_log = ancestor_dir / "debug.log"
    # Mix garbage bytes with a valid, parseable line.
    with open(ancestor_log, "wb") as fh:
        fh.write(b"\xff\xfe not valid utf-8 garbage \x00\x01\n")
        fh.write(b"[A1Trace] accept goal=op-CORRUPT phase=CLASSIFY\n")
    current = _write_session(
        root, "bt-iso-2000", [
            "[A1Trace] emit goal=op-CORRUPT source=roadmap lineage=resumed "
            "original_emit_wall=1.0",
        ],
    )
    a = _make_auditor(current_log_path=current, strict=False, flags=[])
    for line in current.read_text().splitlines():
        a.ingest_log_line(line)  # must not raise
    assert "CLASSIFY" in a.fsm_phases_seen, (
        "the valid line after the corrupted one must still be ingested"
    )


# ===========================================================================
# 6. lineage_verifiable is HMAC-anchored (HYDRATION-HANDSHAKE VERIFIED)
# ===========================================================================


def test_lineage_verifiable_true_via_hydration_handshake(tmp_path):
    root = _sessions_root(tmp_path)
    current = _write_session(
        root, "bt-iso-2000", [
            "[HYDRATION-HANDSHAKE] HMAC-SHA256 VERIFIED op=op-H phase=CLASSIFY "
            "digest=deadbeef01234567...",
            "[A1Trace] emit goal=op-H source=roadmap lineage=resumed "
            "original_emit_wall=1.0",
        ],
    )
    a = _make_auditor(current_log_path=current, strict=False, flags=[])
    for line in current.read_text().splitlines():
        a.ingest_log_line(line)
    v = a.verdict()
    assert v.criteria["lineage_verifiable"] is True
    assert "op-H" in v.lineage["hydration_verified_ops"]


def test_lineage_verifiable_false_when_no_handshake_and_no_checkpoint(tmp_path):
    root = _sessions_root(tmp_path)
    current = _write_session(
        root, "bt-iso-2000", [
            "[A1Trace] emit goal=op-NOHANDSHAKE source=roadmap lineage=resumed "
            "original_emit_wall=1.0",
        ],
    )
    a = _make_auditor(
        current_log_path=current, strict=False, flags=[],
    )
    a.checkpoint_base_dir = str(tmp_path / "no_such_checkpoint_dir")
    for line in current.read_text().splitlines():
        a.ingest_log_line(line)
    v = a.verdict()
    assert v.criteria["lineage_verifiable"] is False
    assert "op-NOHANDSHAKE" in v.lineage["unverifiable_resume_lineage"]


def test_verify_pending_checkpoint_reuses_fsm_checkpoint_hmac(tmp_path, monkeypatch):
    """verify_pending_checkpoint must reuse fsm_checkpoint's OWN _sign/_verify
    (not reimplement HMAC logic) -- a checkpoint signed with the real module
    verifies; a tampered payload does not."""
    monkeypatch.setenv("JARVIS_CHECKPOINT_HMAC_SECRET", "test-secret-key")
    monkeypatch.setenv("JARVIS_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    fc = aud._import_fsm_checkpoint_module()
    if fc is None:
        pytest.skip("fsm_checkpoint module not importable in this environment")
    cp = fc.FSMCheckpoint(op_id="op-CKPT-1", phase="CLASSIFY")
    path = fc.write_checkpoint(cp)
    assert path is not None
    assert aud.verify_pending_checkpoint("op-CKPT-1") is True
    # Tamper with the payload -> must fail closed.
    with open(path, "r", encoding="utf-8") as fh:
        wrapper = json.loads(fh.read())
    wrapper["payload"] = wrapper["payload"].replace("CLASSIFY", "APPLY")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(wrapper))
    assert aud.verify_pending_checkpoint("op-CKPT-1") is False


# ===========================================================================
# 7. --replay: downgrades ONLY UNVERIFIABLE, never REJECTED
# ===========================================================================


def test_replay_downgrades_unverifiable_flag_to_warn():
    a = aud.A1GraduationAuditor(
        flags=["JARVIS_TOTALLY_UNOBSERVABLE_FLAG"],
        strict=True,
        lineage_scoping_enabled=False,
        replay=True,
    )
    v = a.verdict()
    assert v.criteria["twelve_flag_audit_passed"] is True
    assert "JARVIS_TOTALLY_UNOBSERVABLE_FLAG" in v.lineage[
        "replay_downgraded_unverifiable_flags"
    ]


def test_replay_does_not_mask_rejected_flag():
    a = aud.A1GraduationAuditor(
        flags=["JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS"],
        strict=True,
        lineage_scoping_enabled=False,
        replay=True,
    )
    a.ingest_log_line("[SemanticGuard] op=op-abc findings=0")  # evaluated
    a.ingest_log_line("removed_import_still_referenced for op-abc")  # then rejected
    v = a.verdict()
    assert v.criteria["twelve_flag_audit_passed"] is False
    assert any(f["verdict"] == "rejected" for f in v.flags), (
        "--replay must NEVER mask a REJECTED flag; got flags=%r" % (v.flags,)
    )


def test_non_replay_strict_still_fails_on_unverifiable():
    """Baseline (non-replay) behavior must be UNCHANGED -- strict mode still
    fails an honest UNVERIFIABLE flag."""
    a = aud.A1GraduationAuditor(
        flags=["JARVIS_TOTALLY_UNOBSERVABLE_FLAG"],
        strict=True,
        lineage_scoping_enabled=False,
        replay=False,
    )
    v = a.verdict()
    assert v.criteria["twelve_flag_audit_passed"] is False


# ===========================================================================
# 8. Lineage-scoped flag-family REJECT correlation (mirrors run #13 fix,
#    now applied to _correlate_flag_signal -- the actual root cause of
#    twelve_flag_audit_passed=false on bt-iso-1783039338's static replay)
# ===========================================================================


def test_unrelated_op_reject_marker_does_not_poison_resumed_op_flag():
    a = aud.A1GraduationAuditor(
        flags=["JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS"],  # iron_gate
        strict=True,
        lineage_scoping_enabled=True,
        replay=False,
    )
    # Establish op-RESUMED as the audited (resumed-lineage) op.
    a.ingest_log_line(
        "[A1Trace] emit goal=op-RESUMED source=roadmap lineage=resumed "
        "original_emit_wall=1.0"
    )
    # The resumed op's OWN gate evaluated cleanly.
    a.ingest_log_line("[IronGate] tool_exploration_start op=op-RESUMED")
    # An UNRELATED op's Iron Gate legitimately rejects (working as designed).
    a.ingest_log_line(
        "Generation attempt 1/2 failed for "
        "op-019f9999-0000-1111-2222-333333333333-cau: "
        "exploration_insufficient: 0/1 exploration tool calls"
    )
    v = a.verdict()
    assert v.criteria["twelve_flag_audit_passed"] is True, (
        "an unrelated op's legitimate gate rejection must not poison the "
        "resumed op's flag audit; got flags=%r" % (v.flags,)
    )
    assert any(
        "op-019f9999-0000-1111-2222-333333333333-cau" in r
        for r in a.observed_unrelated_flag_rejects
    )


def test_no_resumed_ops_keeps_legacy_global_reject_behavior():
    """When NO resumed op is present, flag-reject correlation stays GLOBAL
    (byte-identical pre-fix behavior) -- backward compatible."""
    a = aud.A1GraduationAuditor(
        flags=["JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS"],
        strict=True,
        lineage_scoping_enabled=True,
        replay=False,
    )
    a.ingest_log_line(
        "Generation attempt 1/2 failed for op-ANY-9999: "
        "exploration_insufficient: 0/1 exploration tool calls"
    )
    v = a.verdict()
    assert v.criteria["twelve_flag_audit_passed"] is False
    rejected = [f for f in v.flags if f["verdict"] == "rejected"]
    assert rejected, "legacy global correlation must still REJECT with no resumed ops"


# ===========================================================================
# 9. Bare op-id extraction fallback (no '=' prefix)
# ===========================================================================


def test_extract_op_id_bare_token_fallback():
    line = "Generation attempt 1/2 failed for op-019f2532-95b8-7a6a-b728-02a10f8186ca-cau: boom"
    assert aud.A1GraduationAuditor._extract_op_id("", {}, line) == (
        "op-019f2532-95b8-7a6a-b728-02a10f8186ca-cau"
    )


def test_extract_op_id_prefers_equals_form_over_bare():
    line = "op=op-abc-123-456 for op-should-not-win-789-012"
    assert aud.A1GraduationAuditor._extract_op_id("", {}, line) == "op-abc-123-456"
