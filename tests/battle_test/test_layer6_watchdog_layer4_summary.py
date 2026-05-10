"""§Layer 6 closure (v2.88) — wall-clock watchdog Layer 4 escape hatch
must write a partial ``summary.json`` BEFORE invoking ``os._exit(75)``.

Closes the cadence-arc Layer 6 root cause diagnosed 2026-05-10:
when the asyncio cleanup path wedges (provider streams stuck after
wall-clock cap fires, etc.), the wall-clock watchdog escalates
through 4 layers ending with ``os._exit(75)``. ``os._exit`` bypasses
``atexit``, so the partial-shutdown insurance from Wave 3 v2.79
never fires → session dir contains only ``debug.log`` → the
soak harness's ``_read_most_recent_session`` finds the dir but
``summary.json`` is missing → ``session_id`` falls back to literal
``"unknown"`` → soak record gets ``outcome=infra session=unknown``
→ cadence produces unparseable evidence indefinitely.

The structural fix invokes the existing canonical
``_atexit_fallback_write`` synchronously from the watchdog thread
BEFORE ``os._exit(75)``. Sync I/O is safe from the watchdog thread
because that thread is NOT the asyncio loop that's wedged.
Composes the existing canonical fallback writer (zero parallel
logic, zero new state machinery).
"""
from __future__ import annotations

import atexit
import inspect
import json
import re
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from backend.core.ouroboros.battle_test.harness import (
    BattleTestHarness,
    HarnessConfig,
)


_HARNESS_SRC = Path(inspect.getfile(BattleTestHarness)).read_text(
    encoding="utf-8",
)


# -----------------------------------------------------------------
# AST pin: Layer 4 escape hatch invokes _atexit_fallback_write BEFORE
# os._exit(75). Bytes-pin — drift here silently regresses Layer 6.
# -----------------------------------------------------------------


def test_layer4_invokes_atexit_fallback_before_os_exit():
    """Verify the fallback call site appears AFTER 'LAYER 4
    ESCALATION' marker AND BEFORE the os._exit(75) call. The
    positional invariant is load-bearing: writing AFTER os._exit
    is unreachable; writing too early would block earlier
    escalation layers."""
    # Grep for the LAYER 4 ESCALATION block.
    # Anchor on the section-comment marker `# ── Layer 4: os._exit`
    # which is a code marker, not a log-message string. Then capture
    # through the actual `os._exit(75)` STATEMENT (not the
    # substring inside the log message above it). The minimum match
    # is large enough to span the fallback invocation.
    layer4_match = re.search(
        r"# ── Layer 4: os\._exit.*?try:\s*os\._exit\(75\)",
        _HARNESS_SRC, re.DOTALL,
    )
    assert layer4_match, (
        "LAYER 4 ESCALATION → os._exit(75) block not found"
    )
    block = layer4_match.group(0)
    # The fallback must be invoked WITHIN this block.
    assert "_atexit_fallback_write" in block, (
        "LAYER 4 escape hatch must invoke _atexit_fallback_write "
        "BEFORE os._exit(75) — without it, summary.json never "
        "gets written and soak harness records "
        "session=unknown outcome=infra"
    )
    # AND must be BEFORE the EXECUTABLE os._exit(75) call.
    # The block contains an os._exit(75) substring inside a log
    # message ABOVE the fallback (the comment "ESCALATION:
    # os._exit(75)" is a string literal). The TRAILING os._exit(75)
    # — the one that actually executes — is captured at the very
    # end of the regex match. Use the last occurrence to anchor.
    fallback_pos = block.index("_atexit_fallback_write")
    exit_pos = block.rindex("os._exit(75)")
    assert fallback_pos < exit_pos, (
        "_atexit_fallback_write must be called BEFORE the "
        "executable os._exit(75) statement — writing after the "
        "kill is unreachable"
    )


def test_layer4_fallback_uses_incomplete_kill_layer4_outcome():
    """The session_outcome stamped on the partial summary must be
    distinct enough that operators can grep it in
    `live_fire_graduation_history.jsonl` to identify Layer-4-killed
    soaks."""
    # Anchor on the section-comment marker `# ── Layer 4: os._exit`
    # which is a code marker, not a log-message string. Then capture
    # through the actual `os._exit(75)` STATEMENT (not the
    # substring inside the log message above it). The minimum match
    # is large enough to span the fallback invocation.
    layer4_match = re.search(
        r"# ── Layer 4: os\._exit.*?try:\s*os\._exit\(75\)",
        _HARNESS_SRC, re.DOTALL,
    )
    assert layer4_match
    assert "incomplete_kill_layer4" in layer4_match.group(0), (
        "Layer 4 fallback must stamp session_outcome="
        "'incomplete_kill_layer4' for operator-greppable audit "
        "(distinct from signal-driven 'incomplete_kill' from "
        "Wave 3 v2.79's Ticket B path)"
    )


def test_layer4_fallback_swallows_exception():
    """The fallback call must be wrapped in a defensive try/except
    so a fallback-write failure NEVER blocks the escape hatch from
    firing os._exit(75). Without this, a bug in the fallback path
    could turn the load-bearing escape hatch into a deadlock."""
    # Anchor on the section-comment marker `# ── Layer 4: os._exit`
    # which is a code marker, not a log-message string. Then capture
    # through the actual `os._exit(75)` STATEMENT (not the
    # substring inside the log message above it). The minimum match
    # is large enough to span the fallback invocation.
    layer4_match = re.search(
        r"# ── Layer 4: os\._exit.*?try:\s*os\._exit\(75\)",
        _HARNESS_SRC, re.DOTALL,
    )
    assert layer4_match
    block = layer4_match.group(0)
    # The fallback call must be inside a try block.
    fallback_idx = block.index("_atexit_fallback_write")
    # Search backward for `try:` and forward for `except`.
    pre_block = block[:fallback_idx]
    post_block = block[fallback_idx:]
    assert "try:" in pre_block.split("\n")[-3:][0] or "try:" in pre_block, (
        "_atexit_fallback_write call must be inside a try/except"
    )
    assert "except" in post_block, (
        "_atexit_fallback_write call must be wrapped — a fallback "
        "exception must not block the os._exit(75) escape hatch"
    )


# -----------------------------------------------------------------
# Functional integration — invoke the watchdog's Layer 4 path
# directly + assert summary.json gets written
# -----------------------------------------------------------------


@pytest.fixture
def tmp_harness(tmp_path: Path) -> Iterator[BattleTestHarness]:
    """Build a harness rooted in tmp_path. Clean up its atexit handler
    after the test."""
    session_dir = tmp_path / ".ouroboros" / "sessions" / "bt-layer6-test"
    config = HarnessConfig(
        repo_path=tmp_path,
        cost_cap_usd=0.05,
        idle_timeout_s=30.0,
        session_dir=session_dir,
    )
    harness = BattleTestHarness(config)
    yield harness
    atexit.unregister(harness._atexit_fallback_write)


def test_atexit_fallback_writes_session_id_field(tmp_harness):
    """The summary.json written by the fallback MUST include
    a ``session_id`` field — that's the load-bearing piece the
    soak harness uses to link the session in
    ``live_fire_graduation_history.jsonl``."""
    session_dir = tmp_harness._session_dir
    summary_path = session_dir / "summary.json"
    assert not summary_path.exists()

    # Manually invoke the fallback with the Layer 4 outcome marker.
    tmp_harness._atexit_fallback_write(
        session_outcome="incomplete_kill_layer4",
    )

    assert summary_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    # Load-bearing fields for soak-harness linkage.
    assert "session_id" in summary, (
        "summary.json MUST include session_id field — without it, "
        "soak harness's _read_most_recent_session linkage falls "
        "back to 'unknown' literal string and graduation evidence "
        "is unparseable"
    )
    assert summary["session_id"] == tmp_harness.session_id
    # Layer 4 outcome stamp survives.
    assert summary.get("session_outcome") == "incomplete_kill_layer4"


def test_atexit_fallback_idempotent_when_summary_already_written(
    tmp_harness,
):
    """If the clean path already wrote summary.json
    (``_summary_written = True``), the fallback is a no-op. This
    invariant matters for Layer 4: Layer 3 SIGTERM may have triggered
    atexit which already wrote a summary; Layer 4's call to
    ``_atexit_fallback_write`` must NOT clobber it."""
    session_dir = tmp_harness._session_dir
    session_dir.mkdir(parents=True, exist_ok=True)
    summary_path = session_dir / "summary.json"

    # Pre-write a clean-path summary.
    canonical = {"session_id": "pre-existing", "stop_reason": "clean_exit"}
    summary_path.write_text(json.dumps(canonical), encoding="utf-8")
    tmp_harness._summary_written = True

    # Layer 4 fires and calls the fallback.
    tmp_harness._atexit_fallback_write(
        session_outcome="incomplete_kill_layer4",
    )

    # Pre-existing summary preserved.
    re_read = json.loads(summary_path.read_text(encoding="utf-8"))
    assert re_read == canonical


# -----------------------------------------------------------------
# Provenance pin
# -----------------------------------------------------------------


def test_layer4_block_documents_layer6_closure():
    """The Layer 4 block must cite v2.88 / Layer 6 in its
    explanatory comment so future readers can find the design doc."""
    # Anchor on the section-comment marker `# ── Layer 4: os._exit`
    # which is a code marker, not a log-message string. Then capture
    # through the actual `os._exit(75)` STATEMENT (not the
    # substring inside the log message above it). The minimum match
    # is large enough to span the fallback invocation.
    layer4_match = re.search(
        r"# ── Layer 4: os\._exit.*?try:\s*os\._exit\(75\)",
        _HARNESS_SRC, re.DOTALL,
    )
    assert layer4_match
    block = layer4_match.group(0)
    assert "Layer 6" in block, (
        "Layer 4 block must cite 'Layer 6' so the design doc is "
        "discoverable"
    )
    assert "v2.88" in block, (
        "Layer 4 block must cite the version that shipped the fix"
    )
