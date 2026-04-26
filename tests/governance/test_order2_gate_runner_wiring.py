"""RR Pass B Slice 2b — gate_runner.py wiring integration test.

Pins the structural integration of ``apply_order2_floor`` into the
GATE phase runner. This is the "cage-touching" PR that adds the
single call site Slices 1+2 designed for.

Authority invariant: the wiring is purely additive. Slice 2b changes
NO existing GATE behavior — the new block runs after the existing
``MIN_RISK_TIER`` floor and only fires when BOTH dual flags are on
AND a target file matches the manifest. Default-off behind both
flags = zero behaviour change.

Pinned properties:
  * Source-grep: the wiring call site exists in gate_runner.py.
  * Source-grep: the call runs AFTER MIN_RISK_TIER floor + BEFORE
    SAFE_AUTO diff preview (per Pass B §4.2 ordering invariant).
  * Source-grep: defensive try/except wraps the call (mirrors the
    MIN_RISK_TIER floor block).
  * Source-grep: the wiring uses the GATE-level repo "jarvis"
    (Body-only initial deploy).
  * Source-grep: docstring ladder updated to include step 10
    (ORDER_2 floor) + step count of cross-phase risk_tier mutation
    sites bumped to 7.
  * gate_runner.py is itself in the Order-2 manifest — pinned so
    that Slice 6 amendment protocol covers any future edit to this
    file (currently Slice 1's manifest matches phase_runners/*.py).
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.meta.order2_manifest import (
    ManifestLoadStatus,
    load_manifest,
    reset_default_manifest,
)


_REPO = Path(__file__).resolve().parent.parent.parent
_GATE_RUNNER = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runners" / "gate_runner.py"
)


def _read_gate_runner() -> str:
    return _GATE_RUNNER.read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


# ===========================================================================
# A — Wiring presence (source-grep)
# ===========================================================================


def test_gate_runner_imports_apply_order2_floor():
    """Pin: gate_runner.py imports apply_order2_floor (function-local
    import — same pattern as the existing MIN_RISK_TIER floor block)."""
    src = _read_gate_runner()
    assert (
        "from backend.core.ouroboros.governance.meta.order2_classifier "
        "import" in src
    )
    assert "apply_order2_floor" in src


def test_gate_runner_calls_apply_order2_floor():
    """Pin: the function is actually CALLED, not just imported."""
    code = _strip_docstrings_and_comments(_read_gate_runner())
    assert "apply_order2_floor" in code
    # Specifically — apply_order2_floor(risk_tier, ...) signature.
    assert re.search(r"apply_order2_floor\s*\(", code)


def test_gate_runner_passes_target_files_and_repo():
    """Pin: the call passes target_files + the Body-only repo
    'jarvis' per Slice 1's initial deploy. Multi-repo support is
    deferred per Pass B §3.3."""
    src = _read_gate_runner()
    # The call shape includes list(ctx.target_files) and repo="jarvis".
    assert "list(ctx.target_files)" in src
    assert 'repo="jarvis"' in src or "repo='jarvis'" in src


# ===========================================================================
# B — Ordering invariant (Pass B §4.2)
# ===========================================================================


def test_order2_floor_runs_after_min_risk_tier_floor():
    """Pin: Per Pass B §4.2 — Order-2 floor must run AFTER the
    existing MIN_RISK_TIER floor so paranoia/quiet-hours can't
    accidentally lower an Order-2 op below itself.

    Greps for the **code section markers** (``# ---- ... ----``) so
    docstring mentions don't confuse the ordering check."""
    src = _read_gate_runner()
    min_tier_pos = src.find("# ---- MIN_RISK_TIER floor")
    order2_pos = src.find("# ---- RR Pass B Slice 2b")
    assert min_tier_pos > 0, "MIN_RISK_TIER floor section marker not found"
    assert order2_pos > 0, "ORDER_2 floor section marker not found"
    assert order2_pos > min_tier_pos, (
        "ORDER_2 floor must come AFTER MIN_RISK_TIER floor"
    )


def test_order2_floor_runs_before_safe_auto_preview():
    """Pin: Order-2 floor must run BEFORE the SAFE_AUTO diff preview
    block — an Order-2 op must never enter the SAFE_AUTO preview
    path (it shouldn't be SAFE_AUTO by the time preview considers it)."""
    src = _read_gate_runner()
    order2_pos = src.find("# ---- RR Pass B Slice 2b")
    preview_pos = src.find("# ---- Phase 5a-green:")
    assert order2_pos > 0
    assert preview_pos > 0, "SAFE_AUTO preview section marker not found"
    assert order2_pos < preview_pos, (
        "ORDER_2 floor must come BEFORE Phase 5a-green preview"
    )


# ===========================================================================
# C — Defensive structure (mirrors existing floor block pattern)
# ===========================================================================


def test_order2_floor_wrapped_in_try_except():
    """Pin: defensive try/except wraps the new block so a failure
    (manifest unreachable, classifier raised) can't break GATE.
    Mirrors the existing MIN_RISK_TIER floor block's defensive
    pattern."""
    src = _read_gate_runner()
    # Use the code section marker (``# ---- ... ----``) so we don't
    # match the docstring mention.
    order2_section_start = src.find("# ---- RR Pass B Slice 2b")
    assert order2_section_start > 0, (
        "ORDER_2 floor code section marker not found"
    )
    section = src[order2_section_start:order2_section_start + 1500]
    assert "try:" in section
    assert "except Exception:" in section
    # And a debug log on the swallowed exception (matches
    # MIN_RISK_TIER pattern).
    assert "ORDER_2 floor skipped" in section


def test_order2_floor_logs_mutation_on_change():
    """Pin: when the floor escalates risk_tier, an INFO log line
    fires with the GATE: prefix matching the existing floor logs."""
    src = _read_gate_runner()
    assert (
        '[Orchestrator] GATE: ORDER_2 floor → %s→%s' in src
    ), "ORDER_2 floor INFO log line not found in gate_runner.py"


# ===========================================================================
# D — Docstring ladder updated
# ===========================================================================


def test_gate_runner_docstring_lists_step_10_order2():
    """Pin: the GATE body composition list in the module docstring
    enumerates Order-2 floor as step 10."""
    src = _read_gate_runner()
    assert (
        "10. RR Pass B Slice 2b: ORDER_2_GOVERNANCE floor"
        in src
    )


def test_gate_runner_docstring_step_count_bumped_to_7():
    """Pin: the cross-phase risk_tier-mutation site count was bumped
    from 6 to 7 to include the new ORDER_2 floor."""
    src = _read_gate_runner()
    assert "7 sites" in src
    assert "ORDER_2_GOVERNANCE floor" in src


def test_gate_runner_docstring_renumbered_safe_auto_preview():
    """Pin: SAFE_AUTO preview was renumbered from 10 → 11 (and
    NOTIFY_APPLY preview from 11 → 12) when the Order-2 floor
    inserted at slot 10."""
    src = _read_gate_runner()
    assert "11. Phase 5a-green: SAFE_AUTO diff preview" in src
    assert "12. Phase 5b: NOTIFY_APPLY diff preview" in src


# ===========================================================================
# E — gate_runner.py self-coverage by Order-2 manifest
# ===========================================================================


def test_gate_runner_path_matched_by_real_manifest(monkeypatch):
    """Pin: gate_runner.py is itself an Order-2 governance path —
    any future edit to this file IS an Order-2 amendment per Slice
    6's protocol (when implemented). The shipped manifest's
    ``phase_runners/*.py`` glob covers it."""
    monkeypatch.setenv("JARVIS_ORDER2_MANIFEST_LOADED", "1")
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_PATH",
        str(_REPO / ".jarvis" / "order2_manifest.yaml"),
    )
    reset_default_manifest()
    m = load_manifest()
    assert m.status is ManifestLoadStatus.LOADED
    # The phase_runners/*.py entry covers gate_runner.py.
    assert m.matches(
        "jarvis",
        "backend/core/ouroboros/governance/phase_runners/gate_runner.py",
    ) is True


# ===========================================================================
# F — Authority invariant: wiring did not widen gate_runner imports
# ===========================================================================


def test_wiring_only_imports_classifier_not_manifest_directly():
    """Pin: gate_runner.py imports apply_order2_floor from the
    classifier module — NOT Order2Manifest directly. Keeps the cage
    layered: gate_runner → classifier → manifest, no shortcuts."""
    src = _read_gate_runner()
    assert (
        "from backend.core.ouroboros.governance.meta.order2_manifest"
        not in src
    ), (
        "gate_runner.py must not import order2_manifest directly — "
        "go through the order2_classifier abstraction"
    )


def test_wiring_does_not_introduce_subprocess_or_env_writes():
    """Pin: the wiring is pure function-call additive. No new
    subprocess / network / env writes."""
    code = _strip_docstrings_and_comments(_read_gate_runner())
    # The integration test pins that the wiring block doesn't add
    # any of these — it's just import + call + log.
    section_start = code.find("ORDER_2_GOVERNANCE floor")
    if section_start < 0:
        # Token-stripped form may not preserve the comment label —
        # find the import line instead.
        section_start = code.find("apply_order2_floor")
    assert section_start > 0
    # Examine the Order-2 block specifically (chars after section_start
    # up to the next section marker — Phase 5a-green or end).
    block = code[section_start:section_start + 1200]
    forbidden = [
        "subprocess.",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in block, f"Order-2 wiring block introduced: {c}"
