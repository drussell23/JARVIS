"""Ticket #4 Gap Closure — Live-fire proof of TestRunner streaming.

Mirrors the pattern of scripts/attach_livefire.py (Multi-Modal ingest
live proof) and /tmp/claude/general_battle_matrix.py (GENERAL driver
live battle test).

Runs a real pytest invocation through TestRunner under the Slice 4
graduated defaults, captures:

  * Observed [TestRunner] streaming ... INFO log lines (grep-stable
    contract documented in project_ticket_4_testrunner_streaming.md)
  * Per-event callback payloads (the programmatic consumer contract)
  * TestResult structural fields (proves the end-to-end structural
    contract under real-session conditions, not just unit tests)

This is the live-fire proof that Slice 4 graduation works outside the
test harness — operators see per-test events in their actual debug
stream, matching the rigor bar applied to GENERAL / Multi-Modal /
Vision Sensor closures.

Writes a session artifact under .ouroboros/sessions/livefire-*/ with
the captured logs + structured JSON summary.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import textwrap
import time
from io import StringIO
from pathlib import Path

REPO_ROOT = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")
sys.path.insert(0, str(REPO_ROOT))

# CRITICAL: do NOT set JARVIS_TEST_RUNNER_STREAMING_ENABLED — we want
# to prove the GRADUATED DEFAULT (from Slice 4) actually activates
# streaming. If we force it here, we don't learn anything about the
# default-on behavior.
for key in (
    "JARVIS_TEST_RUNNER_STREAMING_ENABLED",
    "JARVIS_TOOL_MONITOR_ENABLED",
):
    os.environ.pop(key, None)

from backend.core.ouroboros.governance.test_runner import (
    TestRunner,
    _streaming_enabled,
)
from backend.core.ouroboros.governance.monitor_tool import (
    monitor_enabled,
)


# --- Capture both log lines and event-callback payloads --------------------


captured_events: list = []
log_capture_stream = StringIO()

# Attach a capture handler to the test_runner logger. Preserves whatever
# handlers are already attached (for stdout visibility).
runner_logger = logging.getLogger(
    "backend.core.ouroboros.governance.test_runner"
)
runner_logger.setLevel(logging.INFO)
stream_handler = logging.StreamHandler(log_capture_stream)
stream_handler.setLevel(logging.INFO)
stream_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s %(message)s",
))
runner_logger.addHandler(stream_handler)

# Also print to real stdout so the operator running the script sees
# progress live — this IS the point of streaming.
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.setFormatter(logging.Formatter(
    "%(asctime)s [LIVEFIRE] %(message)s", datefmt="%H:%M:%S",
))
runner_logger.addHandler(stdout_handler)


def _capture_event(event: dict) -> None:
    """Programmatic consumer — mirrors what a SerpentFlow subscriber
    would do in production."""
    captured_events.append(event)


# --- Build a real pytest fixture in a tmp dir ------------------------------


def _write_fixture(root: Path) -> Path:
    tests_dir = root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    # Mix of pass/fail/error to exercise all four event kinds.
    tf = tests_dir / "test_livefire.py"
    tf.write_text(textwrap.dedent('''
        import time

        def test_alpha_passes():
            """First test — should PASS."""
            time.sleep(0.05)
            assert 1 + 1 == 2

        def test_beta_passes():
            """Second test — should PASS."""
            time.sleep(0.05)
            assert "jarvis".upper() == "JARVIS"

        def test_gamma_fails():
            """Third test — deliberate FAIL for event-stream observability."""
            time.sleep(0.05)
            assert False, "deliberate failure for livefire"

        def test_delta_skipped():
            """Fourth test — SKIPPED."""
            import pytest
            pytest.skip("livefire skip marker")

        def test_epsilon_passes():
            """Fifth test — should PASS (proving run-everything semantics
            since early-exit defaults off)."""
            assert sorted([3, 1, 2]) == [1, 2, 3]
    ''').lstrip())
    return tf


# --- Main ----------------------------------------------------------------


async def main() -> int:
    print("=" * 78)
    print("Ticket #4 — Live-fire proof of graduated streaming defaults")
    print("=" * 78)
    print()

    # Step 1: pre-flight — prove the graduated defaults are active.
    print("[pre-flight] Graduated defaults:")
    print(f"  JARVIS_TEST_RUNNER_STREAMING_ENABLED (unset) -> "
          f"_streaming_enabled() = {_streaming_enabled()}")
    print(f"  JARVIS_TOOL_MONITOR_ENABLED          (unset) -> "
          f"monitor_enabled() = {monitor_enabled()}")
    assert _streaming_enabled() is True, "streaming must be on-by-default"
    assert monitor_enabled() is True, "monitor must be on-by-default"
    print("  ✓ Both graduated defaults active (no env override needed)")
    print()

    # Step 2: build the fixture.
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="testrunner_livefire_"))
    test_file = _write_fixture(tmp)
    print(f"[fixture] Wrote {test_file.relative_to(tmp)} in {tmp}")
    print()

    # Step 3: run TestRunner with the event callback. This is the
    # end-to-end path an operator would see after Slice 4 graduation.
    print("[live] Invoking TestRunner with streaming default + "
          "event_callback subscriber...")
    print("-" * 78)
    t0 = time.monotonic()
    runner = TestRunner(
        repo_root=tmp, timeout=60.0, event_callback=_capture_event,
    )
    result = await runner.run(test_files=(test_file,))
    elapsed = time.monotonic() - t0
    print("-" * 78)
    print()

    # Step 4: report results.
    print(f"[result] TestRunner returned in {elapsed:.2f}s")
    print(f"  passed:          {result.passed}")
    print(f"  total:           {result.total}")
    print(f"  failed:          {result.failed}")
    print(f"  failed_tests:    {list(result.failed_tests)}")
    print(f"  flake_suspected: {result.flake_suspected}")
    print()

    # Step 5: event-callback capture.
    print(f"[events] Captured {len(captured_events)} programmatic events "
          f"via event_callback:")
    for e in captured_events:
        print(f"  - {e['kind']:16s} node={e['node_id']} sequence={e['sequence']}")
    print()

    # Step 6: grep the captured log lines for the documented
    # [TestRunner] streaming ... contract.
    log_text = log_capture_stream.getvalue()
    streaming_lines = [
        line for line in log_text.splitlines()
        if "[TestRunner] streaming" in line
    ]
    print(f"[log-grep] Captured {len(streaming_lines)} "
          f"'[TestRunner] streaming' log lines:")
    for line in streaming_lines:
        # Strip timestamp prefix for readability.
        trimmed = line.split("INFO ", 1)[-1] if "INFO " in line else line
        print(f"  | {trimmed}")
    print()

    # Step 7: write a session artifact — mirrors the .ouroboros/sessions
    # pattern used by ouroboros_battle_test.py.
    session_id = f"livefire-{int(time.time())}"
    session_dir = REPO_ROOT / ".ouroboros" / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "session_id": session_id,
        "purpose": "Ticket #4 live-fire proof of streaming graduation",
        "commits": {
            "closing": "405a808873",
            "closure_marker": "ad781ea6c7",
        },
        "pre_flight": {
            "streaming_enabled": _streaming_enabled(),
            "monitor_enabled": monitor_enabled(),
            "env_overrides": False,
        },
        "test_result": {
            "passed": result.passed,
            "total": result.total,
            "failed": result.failed,
            "failed_tests": list(result.failed_tests),
            "duration_s": elapsed,
        },
        "events_captured": len(captured_events),
        "event_kinds_seen": sorted({e["kind"] for e in captured_events}),
        "events": captured_events,
        "streaming_log_lines_captured": len(streaming_lines),
        "streaming_log_lines": streaming_lines,
    }
    (session_dir / "summary.json").write_text(
        json.dumps(artifact, indent=2, default=str),
    )
    (session_dir / "debug.log").write_text(log_text)
    print(f"[artifact] Session written to "
          f"{session_dir.relative_to(REPO_ROOT)}/")
    print(f"  summary.json ({len(json.dumps(artifact))} bytes)")
    print(f"  debug.log    ({len(log_text)} bytes)")
    print()

    # Step 8: verdict.
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    checks = [
        ("Streaming default is active without env override",
         _streaming_enabled() is True),
        ("Monitor default is active without env override",
         monitor_enabled() is True),
        ("Structural TestResult populated",
         result.total > 0),
        ("At least one failed test detected",
         result.failed >= 1),
        ("Event callback fired ≥ 1 time",
         len(captured_events) >= 1),
        ("Per-test events observed (test_passed OR test_failed kind)",
         any(e["kind"] in ("test_passed", "test_failed")
             for e in captured_events)),
        ("'[TestRunner] streaming' log lines captured",
         len(streaming_lines) >= 1),
        ("'completed total=' summary line present",
         any("completed total=" in line for line in streaming_lines)),
    ]
    all_pass = True
    for label, ok in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {label}")
        if not ok:
            all_pass = False
    print()
    if all_pass:
        print("LIVE-FIRE PROOF: PASS. Gap #4 Reading-A closure "
              "empirically verified against real subprocesses + "
              "real log pipeline. Operators on a fresh install now "
              "see the streaming events that the unit tests pinned.")
        return 0
    else:
        print("LIVE-FIRE PROOF: FAIL. Some checks did not pass — "
              "inspect the captured session artifact.")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
