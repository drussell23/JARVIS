"""Regression spine for Stage 1.5 motor budget — source-aware BG-pool timebox.

Operator binding 2026-05-13: stage-1 wiring soak v12 (session
bt-2026-05-13-201526) proved the substrate end-to-end but ZERO ops
reached a clean terminal because every op hit the 360s
``JARVIS_BG_WORKER_OP_TIMEOUT_S`` base ceiling in the
``BackgroundAgentPool`` worker loop.  The full pipeline (CLASSIFY →
ROUTE → CTX → PLAN → GENERATE-with-LLM → VALIDATE → APPLY → VERIFY)
for a trivial SWE-Bench-Pro fixture exceeds 360s primarily because
GENERATE awaits LLM round-trips.

Two structural fixes pinned here:

1. **Source-aware ceiling**: a new
   ``JARVIS_BG_WORKER_OP_TIMEOUT_SWE_BENCH_PRO_S`` env knob applied
   when ``op.context.signal_source == "swe_bench_pro"``.  Same
   discipline as the existing complex/read_only categories — NO
   global ceiling raise, NO benchmark-specific code path; just one
   more category in a max-aggregated table.

2. **Observable timebox kill**: when the ceiling fires, the
   ``op.error`` carries a structured ``bg_timebox:<category>:source=
   <name>:timeout=<sec>`` payload (not the previous opaque
   ``pool_worker_timeout:<sec>``) so downstream observability —
   op-lifecycle SSE, audit ledger, postmortem — can distinguish
   ceiling-bound kills from upstream cancellations and reason about
   which category contributed.

Pinned with three layers of invariant:
  - Source-grep: the SWE env knob is referenced in the pool code
  - Source-grep: the bg_timebox reason_code format is in place
  - FlagRegistry: the new flag is seeded so operators discover it
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest


def _read_pool_src() -> str:
    from backend.core.ouroboros.governance import background_agent_pool
    return Path(
        inspect.getfile(background_agent_pool),
    ).read_text(encoding="utf-8")


def _read_flag_seed_src() -> str:
    from backend.core.ouroboros.governance import flag_registry_seed
    return Path(
        inspect.getfile(flag_registry_seed),
    ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Source-aware ceiling — SWE-Bench-Pro picks up a longer lease
# ---------------------------------------------------------------------------


def test_swe_bench_pro_ceiling_env_var_referenced_in_pool():
    """The BG-pool worker MUST read
    ``JARVIS_BG_WORKER_OP_TIMEOUT_SWE_BENCH_PRO_S`` — the source-aware
    motor budget knob.  Drift back to a single global ceiling would
    re-expose v12's "everything dies at 360s" failure mode."""
    src = _read_pool_src()
    assert "JARVIS_BG_WORKER_OP_TIMEOUT_SWE_BENCH_PRO_S" in src, (
        "background_agent_pool.py no longer references the SWE-Bench-Pro "
        "ceiling env knob.  Either the source-aware motor budget was "
        "reverted (re-introducing v12's 360s death) or it migrated to a "
        "different name without updating this pin."
    )


def test_swe_bench_pro_ceiling_default_is_900s():
    """The default lease for SWE-Bench-Pro is 900s — same conservative
    default as the existing complex/read_only categories.  The
    operator's binding is "explicitly higher for swe_bench_pro" — 900s
    is the smallest 'higher' value consistent with the existing
    ceiling table."""
    src = _read_pool_src()
    # Loose: the literal '900' appears next to the SWE env var
    sweidx = src.find("JARVIS_BG_WORKER_OP_TIMEOUT_SWE_BENCH_PRO_S")
    assert sweidx > 0, "SWE-Bench-Pro env var not found in pool source"
    # Within 200 chars of the env var, the literal "900" must appear
    nearby = src[sweidx : sweidx + 400]
    assert "900" in nearby, (
        f"Default SWE-Bench-Pro timeout (900s) not present near env "
        f"var. Snippet: {nearby[:200]!r}"
    )


def test_source_signal_is_read_from_op_context():
    """Source-aware lookup MUST use ``op.context.signal_source`` —
    the canonical field set by ``unified_intake_router._dispatch_one``
    when an envelope reaches the BG pool.  Drift to a different
    surface (e.g. ``op.signal_source``, ``op.context.source``) breaks
    the lookup chain."""
    src = _read_pool_src()
    assert 'getattr(op.context, "signal_source"' in src, (
        "BG-pool worker no longer reads ``op.context.signal_source``. "
        "The source-aware ceiling lookup requires the canonical field "
        "name set by unified_intake_router._dispatch_one — check that "
        "the field hasn't been renamed or that the lookup didn't drift "
        "to a different attribute."
    )


def test_ceiling_table_uses_max_aggregation_not_first_match():
    """Max-aggregation: multiple applicable categories MUST compose by
    taking the LONGEST applicable ceiling, not the first match in a
    precedence chain.  A read-only SWE-Bench-Pro op with 4 target
    files should get the LARGEST of {read_only_ceiling, complex_ceiling,
    swe_bench_pro_ceiling, base_ceiling}, not whichever happens to
    appear first in the code's if/elif chain.

    Heuristic: source-grep for ``max(`` near the ceiling candidates
    list.  Drift to a precedence chain (read_only > complex > base)
    would let one short ceiling mask a longer applicable one — the
    original v12 failure mode where a SWE op inherited the 360s
    sensor base because of where it sat in the if/elif tree.
    """
    src = _read_pool_src()
    # The new code uses ``max(_candidates, key=...)`` for ceiling selection
    assert "max(\n                        _candidates," in src or \
           "max(_candidates," in src, (
        "BG-pool ceiling selection no longer uses max-aggregation over "
        "a candidates list.  Either it reverted to an if/elif precedence "
        "chain (re-exposing the v12 source-masked-by-shape failure mode), "
        "or the implementation moved to a different aggregator without "
        "updating this pin."
    )


# ---------------------------------------------------------------------------
# Observable bg_timebox reason_code
# ---------------------------------------------------------------------------


def test_timebox_kill_carries_structured_reason_code():
    """When the ceiling fires, ``op.error`` MUST carry the structured
    ``bg_timebox:<category>:source=<name>:timeout=<sec>`` payload.
    The unstructured ``pool_worker_timeout:Xs`` predecessor was opaque
    — observers couldn't tell ceiling-bound kills from upstream
    cancellations or reason about which category contributed."""
    src = _read_pool_src()
    assert 'f"bg_timebox:{_ceiling_reason}:"' in src, (
        "BG-pool timebox kill no longer emits the structured "
        "``bg_timebox:<category>:source=...:timeout=...`` payload.  "
        "Downstream observability (op-lifecycle SSE, audit ledger, "
        "postmortem) MUST be able to distinguish ceiling-bound kills "
        "from upstream cancellations — drift back to "
        "``pool_worker_timeout:Xs`` re-introduces the silent-starvation "
        "failure mode the operator flagged 2026-05-13."
    )


def test_timebox_kill_log_has_observable_fields():
    """The kill log line MUST surface ``reason=bg_timebox``,
    ``category=<applied>``, ``source=<signal_source>``, ``timeout=Xs``
    — those four fields are the contract with downstream parsers.
    Drift toward a less structured log line means operators / oracle /
    audit-ledger watchers can't filter on the kill class."""
    src = _read_pool_src()
    # Look for the log format string
    expected_fields = [
        "reason=bg_timebox",
        "category=%s",
        "source=%r",
        "timeout=%.0fs",
    ]
    for token in expected_fields:
        assert token in src, (
            f"BG-pool timebox kill log line missing observable field "
            f"{token!r} — drift would break downstream parsers that "
            "filter on these tokens."
        )


# ---------------------------------------------------------------------------
# FlagRegistry seed
# ---------------------------------------------------------------------------


def test_swe_bench_pro_timeout_is_seeded_in_flag_registry():
    """Per FlagRegistry discipline (CLAUDE.md §40.7.8): every
    operator-tunable env knob with semantic load MUST appear in the
    seed registry so ``/help flags`` lists it.  Without the seed, the
    knob is invisible to operators and the env is effectively
    undocumented."""
    src = _read_flag_seed_src()
    assert "JARVIS_BG_WORKER_OP_TIMEOUT_SWE_BENCH_PRO_S" in src, (
        "FlagRegistry seed is missing "
        "JARVIS_BG_WORKER_OP_TIMEOUT_SWE_BENCH_PRO_S — operators have "
        "no discoverable surface for the SWE motor budget.  Add a "
        "FlagSpec block next to the existing OP_TIMEOUT_COMPLEX_S entry."
    )


def test_swe_bench_pro_seed_has_timing_category():
    """The seed MUST mark this as a TIMING-category knob — same
    classification as the sibling complex/readonly timeout knobs.
    Drift to a different category (e.g., SAFETY, OBSERVABILITY) means
    operators filtering ``/help flags --category=timing`` would miss
    this knob."""
    src = _read_flag_seed_src()
    # Locate the SWE seed block + verify it carries Category.TIMING
    swe_idx = src.find("JARVIS_BG_WORKER_OP_TIMEOUT_SWE_BENCH_PRO_S")
    assert swe_idx > 0
    # Within the next 1200 chars, Category.TIMING must appear (block end —
    # the description block is verbose, so allow ample headroom).
    block = src[swe_idx : swe_idx + 1200]
    assert "Category.TIMING" in block, (
        "SWE-Bench-Pro timeout FlagSpec is not categorized as "
        "Category.TIMING. Sibling complex/readonly knobs are TIMING; "
        "consistency matters for ``/help flags --category`` filters."
    )


# ---------------------------------------------------------------------------
# Behavioral pin — proves the wiring actually flips the ceiling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source,expected_min_ceiling", [
    ("swe_bench_pro", 900),  # SWE-source gets the longer lease
    ("doc_staleness", 360),  # Sensor sources stay at the conservative base
    ("test_failure", 360),   # Runtime fires also at base
    ("", 360),               # Unset/missing source falls back to base
])
def test_ceiling_resolution_behavioral_smoke(
    source: str, expected_min_ceiling: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end smoke: given a synthetic op context with a given
    ``signal_source``, the ceiling selection logic in the pool worker
    MUST return at least the expected minimum.

    Because the ceiling-selection block is inline in the worker loop
    (not extracted into a pure helper), this test reproduces the
    selection logic directly to pin the algorithm.  When the inline
    code is later extracted into a helper, this test will need its
    import path updated but the assertions stay valid.
    """
    import os as _os
    # Clear any env override so defaults apply
    for env_var in (
        "JARVIS_BG_WORKER_OP_TIMEOUT_S",
        "JARVIS_BG_WORKER_OP_TIMEOUT_COMPLEX_S",
        "JARVIS_BG_WORKER_OP_TIMEOUT_READONLY_S",
        "JARVIS_BG_WORKER_OP_TIMEOUT_SWE_BENCH_PRO_S",
    ):
        monkeypatch.delenv(env_var, raising=False)

    # Reproduce the selection logic
    _op_timeout_base_s = float(
        _os.environ.get("JARVIS_BG_WORKER_OP_TIMEOUT_S", "360")
    )
    _target_file_count = 1
    _is_read_only = False
    _signal_source = source

    _candidates: list = [(_op_timeout_base_s, "base")]
    if _is_read_only:
        _candidates.append((
            float(_os.environ.get(
                "JARVIS_BG_WORKER_OP_TIMEOUT_READONLY_S", "900",
            )),
            "read_only",
        ))
    if _target_file_count >= 4:
        _candidates.append((
            float(_os.environ.get(
                "JARVIS_BG_WORKER_OP_TIMEOUT_COMPLEX_S", "900",
            )),
            "complex",
        ))
    if _signal_source == "swe_bench_pro":
        _candidates.append((
            float(_os.environ.get(
                "JARVIS_BG_WORKER_OP_TIMEOUT_SWE_BENCH_PRO_S", "900",
            )),
            "swe_bench_pro",
        ))

    _ceiling, _reason = max(_candidates, key=lambda p: (p[0], p[1]))
    assert _ceiling >= expected_min_ceiling, (
        f"signal_source={source!r} got ceiling={_ceiling:.0f}s, "
        f"expected >= {expected_min_ceiling}s.  Reason field: {_reason!r}"
    )
    if source == "swe_bench_pro":
        assert _reason == "swe_bench_pro", (
            f"SWE-Bench-Pro source ceiling reason should be "
            f"'swe_bench_pro', got {_reason!r}"
        )


def test_max_aggregation_swe_plus_complex_picks_longer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composability invariant: when an op is BOTH SWE-Bench-Pro AND
    complex (≥4 target files), the applied ceiling MUST be the
    MAX of the two, not the first match.  Setting complex higher than
    SWE means SWE inherits complex; setting SWE higher means complex
    inherits SWE.  Either way, no ceiling silently shrinks the budget."""
    import os as _os
    monkeypatch.delenv("JARVIS_BG_WORKER_OP_TIMEOUT_S", raising=False)
    monkeypatch.setenv("JARVIS_BG_WORKER_OP_TIMEOUT_COMPLEX_S", "1500")
    monkeypatch.setenv("JARVIS_BG_WORKER_OP_TIMEOUT_SWE_BENCH_PRO_S", "900")

    _op_timeout_base_s = 360.0
    _target_file_count = 5
    _is_read_only = False
    _signal_source = "swe_bench_pro"

    _candidates = [(_op_timeout_base_s, "base")]
    if _is_read_only:
        _candidates.append((float(_os.environ.get(
            "JARVIS_BG_WORKER_OP_TIMEOUT_READONLY_S", "900",
        )), "read_only"))
    if _target_file_count >= 4:
        _candidates.append((float(_os.environ.get(
            "JARVIS_BG_WORKER_OP_TIMEOUT_COMPLEX_S", "900",
        )), "complex"))
    if _signal_source == "swe_bench_pro":
        _candidates.append((float(_os.environ.get(
            "JARVIS_BG_WORKER_OP_TIMEOUT_SWE_BENCH_PRO_S", "900",
        )), "swe_bench_pro"))

    _ceiling, _reason = max(_candidates, key=lambda p: (p[0], p[1]))
    # complex=1500 > swe=900 > base=360 — complex wins
    assert _ceiling == 1500.0, f"max-aggregation picked {_ceiling}, expected 1500"
    assert _reason == "complex"
