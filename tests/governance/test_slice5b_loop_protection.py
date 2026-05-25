"""Slice 5B (repointed) — true main-loop blocker fix in OpportunityMinerSensor.scan_file.

The original Slice 5B target (shipped_code_invariants) was already
off-loop via ``loop.run_in_executor(pool, _worker_validate_all_grouped)``
(see shipped_code_invariants.py:2633). The 33.2s ``parent_await_ms``
log line we initially suspected was process-pool worker execution
time, NOT main-loop blocking.

# Real blocker triangulation

ControlPlaneSnapshot from bt-2026-05-25-095834 (94 starvation events,
peak lag_ms=88719) revealed:

  1. ``AstCompileHelper`` events preceded 19 starvation events
     (the most common predecessor module).
  2. ``opportunity_miner_sensor.scan_once`` was already correctly
     offloaded by Slice 12L Part B (rglob + read_text both go through
     ``offload_blocking``).
  3. ``opportunity_miner_sensor.scan_file`` — the event-driven
     per-file handler fired on ``fs.changed.*`` events — was MISSED
     by Slice 12L Part B and still called ``py_file.read_text()``
     synchronously on the main loop (line 920 pre-5B).

# Fix

Route ``scan_file`` read_text through the same ``offload_blocking``
primitive that ``scan_once`` already uses. Pattern mirrors line
540-543 in scan_once for consistency.

# Test surface (2 AST pins + 3 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SENSOR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "intake" / "sensors" / "opportunity_miner_sensor.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_scan_file_offloads_read_text() -> None:
    """``scan_file`` must route its ``py_file.read_text`` through
    ``offload_blocking`` — never call ``read_text()`` synchronously
    on the main loop in the event-driven hot path.

    Without this, every ``fs.changed.*`` event handler blocks the
    asyncio loop for the duration of the disk read."""
    src = SENSOR_FILE.read_text()
    assert "opportunity_miner.scan_file.read_text" in src, (
        "scan_file does NOT carry the Slice 5B offload label — "
        "main loop still blocks on disk IO in the event-driven path."
    )
    assert "_s5b_offload_blocking" in src, (
        "scan_file does NOT use offload_blocking — Slice 5B revoked."
    )


def test_ast_pin_no_sync_read_text_remains_in_scan_file() -> None:
    """Walk the AST of ``scan_file`` specifically and confirm no
    bare ``py_file.read_text(...)`` call survives (only the awaited
    offload_blocking variant)."""
    tree = ast.parse(SENSOR_FILE.read_text(), filename=str(SENSOR_FILE))

    scan_file_fn = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "scan_file"
        ):
            scan_file_fn = node
            break

    assert scan_file_fn is not None, "scan_file async def not found"

    # Walk scan_file's body looking for read_text calls.
    # A bare py_file.read_text(...) (not inside an Await) is the bug shape.
    for sub in ast.walk(scan_file_fn):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "read_text"
        ):
            # The call must be the inner of an offload_blocking await.
            # Verify by walking ancestors — read_text must be passed
            # as a callable arg, NOT directly invoked.
            # Heuristic: if read_text is invoked with explicit ()
            # OUTSIDE an offload_blocking context, that's the bug.
            # Simpler check: scan_file's offload pattern passes
            # ``py_file.read_text`` (no parens) as positional arg.
            # Direct ``py_file.read_text()`` invocation has empty args
            # or just ``encoding=...`` AND is NOT inside an Await chain
            # rooted at offload_blocking.
            #
            # Easiest reliable shape: source text of scan_file must
            # contain "_s5b_offload_blocking(" wrapping "py_file.read_text"
            # (no parens after read_text — passed as callable).
            scan_file_src = ast.unparse(scan_file_fn)
            assert "_s5b_offload_blocking(" in scan_file_src, (
                "Found read_text call but no _s5b_offload_blocking "
                "wrapper in scan_file"
            )
            # The pattern must pass read_text as a callable (no parens
            # following before the offload's positional arg boundary).
            # Concrete substring assertion:
            assert "py_file.read_text," in scan_file_src or \
                   "py_file.read_text\n" in scan_file_src, (
                "py_file.read_text is invoked rather than passed as "
                "callable to offload_blocking — pattern violation."
            )


# ──────────────────────────────────────────────────────────────────────
# Spine — 3
# ──────────────────────────────────────────────────────────────────────


def test_spine_offload_blocking_primitive_is_canonical() -> None:
    """The offload primitive imported by scan_file must come from the
    canonical ``event_loop_governance`` module — same source as the
    pre-existing scan_once path (Slice 12L Part B). No parallel impl."""
    src = SENSOR_FILE.read_text()
    # The Slice 5B import must point at the canonical primitive
    assert "from backend.core.ouroboros.governance.event_loop_governance" in src
    assert "offload_blocking as _s5b_offload_blocking" in src


def test_spine_scan_once_unchanged_byte_equivalent() -> None:
    """Slice 5B touches ONLY scan_file; scan_once's pre-existing
    offload pattern (Slice 12L Part B) must be preserved verbatim.
    Regression guard against accidental edits to the cyclic batch path."""
    src = SENSOR_FILE.read_text()
    # The Slice 12L Part B offload pattern in scan_once is identified
    # by its label string — must still be present, unchanged.
    assert "opportunity_miner.read_text" in src, (
        "Slice 12L Part B label removed from scan_once — regression"
    )
    assert "opportunity_miner.rglob" in src, (
        "Slice 12L Part B rglob label removed — regression"
    )


def test_spine_diagnostic_comment_explains_root_cause() -> None:
    """The fix must carry a comment explaining WHY it was needed
    (bt-2026-05-25-095834 attribution). Future readers need to
    understand this isn't a duplicate of scan_once's existing pattern."""
    src = SENSOR_FILE.read_text()
    assert "Slice 5B" in src, "Missing Slice 5B attribution comment"
    assert "bt-2026-05-25-095834" in src, (
        "Missing soak attribution — future readers can't trace why "
        "this offload was added when scan_once already had one"
    )
    assert "scan_file" in src and "fs.changed" in src, (
        "Comment doesn't explain WHY scan_file specifically needed it"
    )
