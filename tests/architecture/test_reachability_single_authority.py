"""Architecture pin: a switch that is ON must be reachable.

``JARVIS_META_SENSOR_ENABLED`` defaults to true. The PRD records it as
"graduated default-true (§25 Priority B)". ``meta_sensor.py`` is on disk, has
tests, and has **zero import statements anywhere in the tree**. The sensor
whose job is to notice that O+V has stopped learning is itself the thing
nobody noticed.

It is not alone. A full board scan on 2026-08-08 found 37 switches in that
state, and they are not scattered — 12 are the adaptive immune system
(``governance/adaptation/``), 6 are closed-loop verification and replay
(``governance/verification/``), 3 are temporal observability. Those are
precisely the tiers the PRD grades A, A− and A+.

So the defect is not any one dark module. It is that **this repository
measures merges and reports them as capability.** A merge is evidence that
code exists. Only the import graph is evidence that it runs, and until today
nothing consulted it.

WHAT THIS PIN ASSERTS
---------------------
Two things, both narrow:

    MISSING   a registry names a `source_file` that is not on disk.
              Zero tolerance, no waivers — it is unambiguously wrong.

    DARK      a SWITCH resolves ON, its module is present, and nothing in
              production imports it. Waivable with a reason.

WHY SWITCHES AND NOT KNOBS
--------------------------
The board draws this distinction itself and it is load-bearing here. A switch
turns something on; a knob tunes something already on. Of the 131 dark flags,
94 are knobs — and a dark knob is, in the board's own words, "the same finding
as its module, repeated once per dial". Twelve ``ADAPTATION_*`` thresholds
would crowd out every switch in the report and make the failure unreadable,
which is how a check becomes something people route around.

The 37 switches name all 61 dark modules' worth of signal without the noise.
Fix a switch's module and its knobs light up with it.

WHY THE IMPORT GRAPH AND NOT A GREP
-----------------------------------
A grep for ``meta_sensor`` finds mentions in ``flag_registry_seed.py`` (a
``source_file=`` string) and ``shipped_code_invariants.py`` (an invariant
target). Neither is a caller. Worse, ``curiosity_engine`` exists three times —
``topology/``, ``governance/``, and ``governance/adaptation/`` — so a textual
search reports eight callers for a name whose adaptation-tier module has none,
and the dark one hides behind its own name. That is the same shape as the
three functions named LPC in the voice arc.

``ProgressBoard`` resolves real ``import`` edges over an AST, counts lazy
in-function imports (this codebase is full of them), excludes test importers
(a module imported only by its tests is inert in production), and knows about
entry points and dynamically-discovered registries. This pin consults it
rather than reimplementing any of it.

WHY THE WAIVERS CANNOT ROT
--------------------------
Same contract as ``test_env_default_single_authority``: a waiver for a flag
that is no longer dark FAILS. Wiring a module forces deleting its excuse, so
the list can only shrink, and it is the wiring backlog rather than a place to
hide.

COST
----
A cold scan is ~157 s; the fingerprint that decides whether to re-scan is
~1.2 s. ``cached_read`` keys on the tree, the ``JARVIS_*`` environment, the
scan roots and the board's own source, so an unchanged checkout pays the
1.2 s and a changed one pays honestly. A pin nobody runs because it is slow
catches nothing — that reasoning is the ``_resolutions()`` docstring's, and it
applies with 130x more force here.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List

import pytest

_REPO = Path(__file__).resolve().parents[2]

from backend.core.ouroboros.battle_test.progress_board import (  # noqa: E402
    DARK,
    MISSING,
    BoardReading,
    FeatureRow,
    ProgressBoard,
    scan_budget_s,
)

# ---------------------------------------------------------------------------
# the waiver list — this IS the wiring backlog
# ---------------------------------------------------------------------------
#
# Every entry is a switch that is ON and imported by nothing. None was
# introduced by the work that added this file; all 37 are the state of `main`
# on 2026-08-08, recorded so the pin goes red for the 38th rather than
# starting life red and being disabled.
#
# The order below is the order they should be wired, and the reason text says
# what the module is FOR — because "it was already broken" is not a reason,
# and the text is the review.

_WAIVED: Dict[str, str] = {
    # ---- Tier 1: closed-loop verification -------------------------------
    # The feedback edge. These six are why O+V cannot learn across ops, and
    # they must be wired BEFORE the adaptive tier below — Pass C's tighteners
    # consume this signal, so adapting first would adapt on noise.
    "JARVIS_META_SENSOR_ENABLED": (
        "TIER 1. The degenerate-loop alarm: fires when postmortems go empty, "
        "i.e. when the organism has stopped learning. Zero import statements "
        "in the entire tree, tests included. Wiring this one first is what "
        "would let O+V find the rest of this list without a human."
    ),
    "JARVIS_POSTMORTEM_INJECTION_ENABLED": (
        "TIER 1. Injects prior postmortems into the generation prompt. "
        "Without it every operation reasons from a blank slate about failures "
        "the system has already survived."
    ),
    "JARVIS_ADVISORY_PLAUSIBILITY_CHECK_ENABLED": (
        "TIER 1. postmortem_recall_consumer — scores an advisory against "
        "recalled failure classes before it is trusted."
    ),
    "JARVIS_POSTMORTEM_RECURRENCE_BOOST_ENABLED": (
        "TIER 1. Second switch on postmortem_recall_consumer: raises priority "
        "for a failure class that keeps recurring. Same module as the row "
        "above; both light up together."
    ),
    "JARVIS_CONFIDENCE_ROUTE_ROUTING_ENABLED": (
        "TIER 1. Routes on the model's own confidence history rather than on "
        "urgency alone. The PRD grades 'Confidence-Aware Execution CLOSED "
        "2026-04-29'; the import graph disagrees."
    ),
    "JARVIS_CAUSALITY_REPLAY_FROM_RECORD_ENABLED": (
        "TIER 1. Deterministic replay of a recorded decision — the "
        "time-travel debugging the roadmap asks for, already written. "
        "Imported by scripts/ouroboros_battle_test.py alone, so it is "
        "reachable in a soak and dark in `ov`."
    ),
    "JARVIS_REPLAY_HOOK_ENABLED": (
        "TIER 1. The orchestrator-side hook replay_from_record needs to "
        "capture from. Dark for the same reason and fixed by the same change."
    ),
    "JARVIS_CIGW_COLLECTOR_ENABLED": (
        "TIER 1. gradient_collector — gathers the per-op signal the "
        "convergence metrics are computed from."
    ),

    # ---- Tier 2: the adaptive immune system (Pass C) ---------------------
    # Twelve modules that tighten the cage as the shell expands. The PRD
    # grades this tier A+ 'self-tightening immunity'. Nothing imports any of
    # it, so the cage is exactly as tight as the day it was written.
    "JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED": (
        "TIER 2. semantic_guardian_miner — mines new SemanticGuardian "
        "patterns from observed failures. Without it the 10 AST/regex "
        "patterns are a fixed list, not an immune system."
    ),
    "JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED": (
        "TIER 2. exploration_floor_tightener — raises the Iron Gate's "
        "exploration floor where shallow exploration has correlated with "
        "failure."
    ),
    "JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED": (
        "TIER 2. risk_tier_extender — escalates a change class to a stricter "
        "tier after it has misbehaved."
    ),
    "JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED": (
        "TIER 2. per_order_mutation_budget — bounds mutations per "
        "Reverse-Russian-Doll Order. The structural ceiling on Order-2 "
        "self-modification, currently unenforced."
    ),
    "JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED": (
        "TIER 2. category_weight_rebalancer — rebalances exploration-ledger "
        "category weights from observed correlation."
    ),
    "JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED": (
        "TIER 2. stale_pattern_detector — sunsets guardian patterns that "
        "have stopped matching, so the cage does not accrete dead rules."
    ),
    "JARVIS_CONVERGENCE_GOVERNOR_ENABLED": (
        "TIER 2. convergence_governor — the bound on recursive "
        "self-improvement. Mathematically bounded RSI is the stated goal and "
        "the module that bounds it is imported by nothing."
    ),
    "JARVIS_CURIOSITY_ENGINE_ENABLED": (
        "TIER 2. adaptation/curiosity_engine — generates hypotheses from "
        "clustered gaps. NOTE: two other modules are also named "
        "curiosity_engine (topology/, governance/) and both ARE imported, "
        "which is why a text search says this name has callers."
    ),
    "JARVIS_CURIOSITY_SCHEDULER_ENABLED": (
        "TIER 2. curiosity_scheduler — the rate limiter that keeps the "
        "curiosity primitive from becoming an infinite loop. Wiring the "
        "engine above without this one would be the unsafe order."
    ),
    "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED": (
        "TIER 2. hypothesis_probe_bridge — turns a hypothesis into a bounded "
        "environment probe. The safe-ambiguity-resolution primitive."
    ),
    "JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED": (
        "TIER 2. anthropic_venom_evidence_prober — the production prober "
        "behind the bridge above."
    ),
    "JARVIS_ADAPT_REPL_ENABLED": (
        "TIER 2. meta_governor — the operator's surface onto the whole "
        "adaptation tier. Dark, so there is no way to inspect an adaptive "
        "decision even if one were made."
    ),

    # ---- Tier 3: temporal observability + Order-2 -------------------------
    "JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED": (
        "TIER 3. multi_op_renderer — the synchronized multi-op timeline. "
        "Imported by the soak harness only. It also has nowhere to draw: the "
        "cockpit has no live region, which is the separate renderer defect."
    ),
    "JARVIS_PHASE8_IDE_OBSERVABILITY_ENABLED": (
        "TIER 3. observability/ide_routes — the Phase 8 GET surface. Distinct "
        "from ide_observability.py, which IS live; this is the temporal one."
    ),
    "JARVIS_POST_MERGE_AUDITOR_ENABLED": (
        "TIER 3. post_merge_auditor — re-checks a merged change after an "
        "interval. Directly relevant to this pin's own finding: it is the "
        "module that would have caught 'merged is not reachable'."
    ),
    "JARVIS_METRICS_SUITE_ENABLED": (
        "TIER 3. metrics_repl_dispatcher — the `/metrics` verb family the "
        "PRD cites as answering session-completion rate."
    ),
    "JARVIS_ORDER2_REPL_ENABLED": (
        "TIER 3. order2_repl_dispatcher — the operator surface onto Order-2 "
        "self-modification."
    ),
    "JARVIS_META_PHASE_RUNNER_ENABLED": (
        "TIER 3. meta_phase_runner — the Order-2 phase runner, i.e. the loop "
        "applied to itself. Dark, which is the honest reason Order-2 is a "
        "horizon and not a state."
    ),
    "JARVIS_M10_GRADUATION_CONTRACT_ENABLED": (
        "TIER 3. m10_arch_proposer_graduation_contract — the contract an "
        "architectural proposal must satisfy to graduate."
    ),
    "JARVIS_PRESSURE_CONVERGENCE_PROVER_ENABLED": (
        "TIER 3. pressure_convergence_prover — proves a pressure signal "
        "reaches steady state rather than oscillating."
    ),

    # ---- Tier 4: surfaces and adjuncts ------------------------------------
    "JARVIS_REMOTE_STATUS_ENABLED": (
        "TIER 4. remote_status — the external status endpoint. Its "
        "governance-mode reporter was corrected on 2026-08-07 (PR #70441); "
        "the module it lives in is still imported by nothing."
    ),
    "JARVIS_IDE_POLICY_ROUTER_ENABLED": (
        "TIER 4. ide_policy_router — the write-side IDE surface (approve / "
        "reject from the editor). Read-only IDE observability is live; this "
        "is its counterpart."
    ),
    "JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED": (
        "TIER 4. inline_prompt_gate_http — HTTP transport for ask_human, so "
        "a clarification can be answered from somewhere other than the REPL."
    ),
    "JARVIS_COMMAND_BUS_BRIDGE_ENABLED": (
        "TIER 4. autonomy_command_bus_bridge — bridges the autonomy command "
        "bus into governance."
    ),
    "JARVIS_ERROR_CLASSIFIER_ENABLED": (
        "TIER 4. error_classifier — shared failure-class taxonomy. Several "
        "subsystems classify errors independently in its absence."
    ),
    "JARVIS_SKILL_OBSERVER_ENABLED": (
        "TIER 4. skill_observer — records which capabilities an op actually "
        "used."
    ),
    "JARVIS_PERSONA_GOVERNOR_ENABLED": (
        "TIER 4. persona_governor — bounds the contrarian/adversarial "
        "reviewer persona so an internal opponent cannot monopolise a session."
    ),
    "JARVIS_MEMORY_REPUTATION_BIAS_ENABLED": (
        "TIER 4. perception_profile — biases attention by per-file "
        "reputation. Also carries a conflicting-default waiver in "
        "test_env_default_single_authority; wiring it should resolve both."
    ),

    # ---- Outside O+V ------------------------------------------------------
    "JARVIS_TCC_SENTINEL_ENABLED": (
        "OUTSIDE O+V. system_control/tcc_sentinel — watches macOS TCC grants "
        "(microphone, screen recording, accessibility) and notices when a "
        "permission is revoked. Relevant to the voice and ghost-touch paths, "
        "where a revoked grant currently surfaces as zeros rather than as an "
        "error. The only dark switch outside the O+V surface."
    ),
}


# ---------------------------------------------------------------------------
# the reading — taken once, shared by every test in the module
# ---------------------------------------------------------------------------

#: ``pytest.ini`` sets a 120 s global timeout and a COLD board scan is ~162 s,
#: so under the default this pin dies mid-scan on any checkout whose cache is
#: cold — which in CI is every checkout. A check that reliably times out is a
#: check that reliably reports nothing.
#:
#: The number comes from :func:`scan_budget_s`, which is the board's own claim
#: about its own cost, because it is needed here AND in
#: ``tests/battle_test/test_progress_board_relative.py`` and a cost written
#: down twice drifts. Two answers were rejected before declaring it: parsing
#: fewer files trades a slow correct answer for a fast wrong one (a module's
#: importers can live anywhere), and parsing in parallel does not work —
#: ``ast.parse`` holds the GIL so threads do not help, and
#: ``ProcessPoolExecutor`` cannot start under a restricted sandbox
#: (``SC_SEM_NSEMS_MAX`` unreadable; measured, not assumed). Adding a
#: multiprocessing dependency to an instrument contractually forbidden from
#: raising, to buy back a cost the cache already removes, is a bad trade.
#:
#: Applied at MODULE level because the reading is built in a fixture and a
#: fixture carries no timeout of its own — pytest-timeout honours marks on
#: TESTS. Marking the fixture would have looked right and done nothing, which
#: is the shape of defect this whole file exists to catch.
pytestmark = pytest.mark.timeout(scan_budget_s())


@pytest.fixture(scope="module")
def reading() -> BoardReading:
    """One board reading for the whole module.

    Function scope would pay the fingerprint walk per test. Module scope pays
    it once, and because `cached_read` is keyed on the tree rather than on
    process lifetime, a second pytest run in an unchanged checkout pays it
    once more and nothing else.
    """
    got = ProgressBoard(repo_root=_REPO).cached_read()
    if got.degraded:
        pytest.skip(f"board reading degraded: {got.degraded}")
    return got


def _dark_switches(reading: BoardReading) -> List[FeatureRow]:
    return [r for r in reading.rows
            if r.state == DARK and r.kind == "switch"]


def _report(rows: List[FeatureRow], headline: str) -> str:
    out = ["", headline, ""]
    for row in sorted(rows, key=lambda r: (r.module, r.flag)):
        out.append(f"  {row.flag}")
        out.append(f"      {row.module}")
        if row.reason:
            out.append(f"      {row.reason}")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# the pin
# ---------------------------------------------------------------------------

def test_no_enabled_switch_is_unreachable(reading: BoardReading) -> None:
    """The invariant. A switch that is ON and imported by nothing is a claim
    about what runs that the import graph contradicts."""
    unwaived = [r for r in _dark_switches(reading) if r.flag not in _WAIVED]
    if not unwaived:
        return

    raise AssertionError(
        _report(
            unwaived,
            "A switch resolves ON and NOTHING in production imports its "
            "module.\nMerged is not reachable.",
        )
        + "\nGive the module a caller on the default path. If it is "
        "deliberately\ndormant, default its switch OFF — an off flag is an "
        "honest one. If it is\nreached in a way the import graph cannot see, "
        "waive it in _WAIVED with\nthat reason."
    )


def test_no_registry_entry_names_a_file_that_is_gone(
        reading: BoardReading) -> None:
    """MISSING has no waivers and starts at zero.

    A registry pointing at a deleted file is unambiguously wrong: nothing can
    be reached through it, and the entry will outlive anyone's memory of what
    it described.
    """
    gone = [r for r in reading.rows if r.state == MISSING]
    assert not gone, _report(
        gone, "A registry names a source_file that is not on disk.",
    )


def test_every_waiver_still_excuses_a_dark_switch(
        reading: BoardReading) -> None:
    """Self-cleaning. Wiring a module must also require deleting its excuse.

    This is what stops the list becoming a graveyard, and it is why the list
    can be read as a backlog: an entry here is a to-do that removes itself.
    """
    live = {r.flag for r in _dark_switches(reading)}
    stale = sorted(set(_WAIVED) - live)
    assert not stale, (
        "\nThese switches are no longer dark — delete their waivers:\n  "
        + "\n  ".join(stale)
        + "\n\n(If one went dark-to-OFF rather than dark-to-wired, that is "
        "also\nprogress: an off flag is honest. Delete the waiver either way.)"
    )


def test_every_waiver_carries_a_reason() -> None:
    """'Because it failed' is not a reason. The text is the review."""
    thin = sorted(f for f, why in _WAIVED.items() if len(why.strip()) < 60)
    assert not thin, f"waivers without a substantive reason: {thin}"


def test_the_backlog_is_shrinking_not_growing(reading: BoardReading) -> None:
    """The waiver list must not be a place new debt accumulates.

    Bounded by its own recorded size. Adding a 38th waiver requires lowering
    this ceiling first, which is a deliberate act with a number attached
    rather than a line quietly appended to a dictionary.
    """
    ceiling = len(_WAIVED)
    dark = _dark_switches(reading)
    assert len(dark) <= ceiling, _report(
        [r for r in dark if r.flag not in _WAIVED],
        f"{len(dark)} dark switches against a ceiling of {ceiling}.",
    )


# ---------------------------------------------------------------------------
# the pin's teeth, driven through the real assertion
# ---------------------------------------------------------------------------
#
# Everything above is green today, which is exactly what a check that has
# stopped measuring also looks like. These drive the SHIPPING assertion
# functions with a doctored reading and require them to raise.
#
# They call the test functions rather than reimplementing their conditions:
# a copy of the logic would keep passing after the original was weakened,
# which is the failure this file is named for.


def _reading_with(*rows: FeatureRow) -> BoardReading:
    return BoardReading(rows=list(rows), scanned_files=len(rows))


_UNWAIVED_DARK_SWITCH = FeatureRow(
    flag="JARVIS_A_BRAND_NEW_FEATURE_ENABLED",
    state=DARK, module="backend/core/ouroboros/governance/newthing.py",
    category="governance", enabled=True, importers=0,
    reason="nothing imports it",
)


def test_the_pin_fails_when_a_new_dark_switch_appears() -> None:
    """THE proof that the gate has teeth.

    A switch defaulting ON, present on disk, imported by nothing, and absent
    from _WAIVED must fail — otherwise the 38th instance of this defect lands
    green and the file is decoration.
    """
    with pytest.raises(AssertionError) as caught:
        test_no_enabled_switch_is_unreachable(
            _reading_with(_UNWAIVED_DARK_SWITCH))
    # The message has to name the flag AND its module: a failure that says
    # only "1 dark switch" sends the reader back to a 162-second scan to find
    # out which.
    assert "JARVIS_A_BRAND_NEW_FEATURE_ENABLED" in str(caught.value)
    assert "newthing.py" in str(caught.value)


def test_a_dark_KNOB_alone_does_not_fail_the_pin() -> None:
    """The other half of the switch/knob split. 94 of the 131 dark flags are
    knobs; failing on those would bury the 37 switches and train everyone to
    waive."""
    knob = FeatureRow(
        flag="JARVIS_A_BRAND_NEW_THRESHOLD_S", state=DARK,
        module="backend/core/ouroboros/governance/newthing.py",
        category="governance", enabled=None, importers=0, value="30",
    )
    assert knob.kind == "knob"
    test_no_enabled_switch_is_unreachable(_reading_with(knob))


def test_the_pin_fails_on_a_registry_entry_pointing_at_nothing() -> None:
    gone = FeatureRow(
        flag="JARVIS_DELETED_FEATURE_ENABLED", state=MISSING,
        module="backend/core/ouroboros/governance/deleted.py",
        enabled=True, reason="reading file vanished mid-scan",
    )
    with pytest.raises(AssertionError, match="deleted.py"):
        test_no_registry_entry_names_a_file_that_is_gone(_reading_with(gone))


def test_the_self_cleaning_check_fails_on_a_waiver_that_outlived_its_defect(
) -> None:
    """Wiring a module must force deleting its excuse. Proven by handing the
    checker a reading in which NOTHING is dark: every one of the 37 waivers is
    then stale, and it must say so."""
    with pytest.raises(AssertionError) as caught:
        test_every_waiver_still_excuses_a_dark_switch(_reading_with())
    assert "JARVIS_META_SENSOR_ENABLED" in str(caught.value)


def test_the_ceiling_fails_when_the_backlog_grows() -> None:
    """The waiver list must not become a place new debt accumulates."""
    rows = [
        FeatureRow(flag=f, state=DARK, module=f"m{i}.py", enabled=True)
        for i, f in enumerate(_WAIVED)
    ] + [_UNWAIVED_DARK_SWITCH]
    with pytest.raises(AssertionError):
        test_the_backlog_is_shrinking_not_growing(_reading_with(*rows))


def test_a_thin_reason_is_rejected(monkeypatch) -> None:
    """'Because it failed' is not a reason, and the check that says so must
    itself be shown to fire."""
    monkeypatch.setitem(_WAIVED, "JARVIS_LAZY_EXCUSE_ENABLED", "broken")
    with pytest.raises(AssertionError, match="JARVIS_LAZY_EXCUSE_ENABLED"):
        test_every_waiver_carries_a_reason()


# ---------------------------------------------------------------------------
# the detector itself, because a blind detector passes everything
# ---------------------------------------------------------------------------
#
# Every check above consults ProgressBoard. If the board silently stopped
# reporting DARK, all five would go green and this file would become the
# fourth surface in this repository to report a state it did not measure.
#
# These build real trees on disk and scan them with the real board. No fakes:
# a fake mirroring the board's current behaviour would mirror its bugs too.

def _tree(tmp_path: Path, files: Dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    """Scan ONLY the synthetic tree, and never touch the repo's cache."""
    monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ROOTS", ".")
    monkeypatch.setenv(
        "JARVIS_PROGRESS_BOARD_CACHE_PATH", str(tmp_path / "cache.json"))
    return tmp_path


def test_an_unimported_enabled_switch_is_reported_dark(isolated) -> None:
    """THE regression. If this stops failing, the pin above stops meaning
    anything."""
    _tree(isolated, {
        "pkg/__init__.py": "",
        "pkg/lonely.py":
            'import os\n'
            'def on():\n'
            '    return os.environ.get("JARVIS_LONELY_ENABLED", "true")\n',
    })
    rows = {r.flag: r for r in ProgressBoard(repo_root=isolated).read().rows}
    assert "JARVIS_LONELY_ENABLED" in rows, "the flag was not discovered at all"
    assert rows["JARVIS_LONELY_ENABLED"].state == DARK
    assert rows["JARVIS_LONELY_ENABLED"].kind == "switch"


def test_one_production_importer_is_enough_to_be_live(isolated) -> None:
    """The other half. A detector that called everything dark would also make
    the pin useless, in the direction that trains people to waive."""
    _tree(isolated, {
        "pkg/__init__.py": "",
        "pkg/wired.py":
            'import os\n'
            'def on():\n'
            '    return os.environ.get("JARVIS_WIRED_ENABLED", "true")\n',
        "pkg/caller.py": "from pkg.wired import on\n",
    })
    rows = {r.flag: r for r in ProgressBoard(repo_root=isolated).read().rows}
    assert rows["JARVIS_WIRED_ENABLED"].state != DARK


def test_a_test_importer_does_not_launder_a_dark_module(isolated) -> None:
    """The failure mode this whole file exists for.

    Every dark module in _WAIVED has tests, and those tests pass. If a test
    importer counted, all 37 would read LIVE and the audit that found them
    would have found nothing.
    """
    _tree(isolated, {
        "pkg/__init__.py": "",
        "pkg/tested.py":
            'import os\n'
            'def on():\n'
            '    return os.environ.get("JARVIS_TESTED_ENABLED", "true")\n',
        "tests/test_tested.py": "from pkg.tested import on\n",
    })
    rows = {r.flag: r for r in ProgressBoard(repo_root=isolated).read().rows}
    assert rows["JARVIS_TESTED_ENABLED"].state == DARK, (
        "a test importer laundered a dark module into a live one"
    )


def test_a_flag_switched_off_is_off_and_not_dark(isolated, monkeypatch) -> None:
    """`off` and `dark` are different findings and only one is actionable.

    This also proves the environment is an INPUT to a reading, which is why
    the cache key must cover it.
    """
    _tree(isolated, {
        "pkg/__init__.py": "",
        "pkg/dormant.py":
            'import os\n'
            'def on():\n'
            '    return os.environ.get("JARVIS_DORMANT_ENABLED", "true")\n',
    })
    monkeypatch.setenv("JARVIS_DORMANT_ENABLED", "0")
    rows = {r.flag: r for r in ProgressBoard(repo_root=isolated).read().rows}
    assert rows["JARVIS_DORMANT_ENABLED"].state != DARK


def test_a_lazy_in_function_import_counts_as_reachable(isolated) -> None:
    """This codebase imports inside functions constantly. Counting only
    module-level imports would report most of the cockpit dark and bury the
    37 real findings under hundreds of false ones."""
    _tree(isolated, {
        "pkg/__init__.py": "",
        "pkg/lazy.py":
            'import os\n'
            'def on():\n'
            '    return os.environ.get("JARVIS_LAZY_ENABLED", "true")\n',
        "pkg/caller.py":
            "def use():\n"
            "    from pkg.lazy import on\n"
            "    return on()\n",
    })
    rows = {r.flag: r for r in ProgressBoard(repo_root=isolated).read().rows}
    assert rows["JARVIS_LAZY_ENABLED"].state != DARK


# ---------------------------------------------------------------------------
# the cache, because a cache that lies is worse than a slow check
# ---------------------------------------------------------------------------

def test_a_hit_returns_exactly_what_the_scan_returned(isolated) -> None:
    """Byte-equal rows, and the second call says it was a recollection."""
    _tree(isolated, {
        "pkg/__init__.py": "",
        "pkg/a.py": 'import os\nX = os.environ.get("JARVIS_A_ENABLED", "true")\n',
        "pkg/b.py": "from pkg.a import X\n",
    })
    board = ProgressBoard(repo_root=isolated)
    first = board.cached_read()
    second = board.cached_read()
    assert first.from_cache is False
    assert second.from_cache is True
    assert [r.to_dict() for r in first.rows] == [r.to_dict() for r in second.rows]
    assert first.fingerprint == second.fingerprint


def test_editing_a_file_invalidates_the_reading(isolated) -> None:
    """The property the whole design rests on. A stale board is worse than no
    board — it reports a state it did not measure, which is the defect this
    pin exists to catch."""
    _tree(isolated, {
        "pkg/__init__.py": "",
        "pkg/c.py": 'import os\nX = os.environ.get("JARVIS_C_ENABLED", "true")\n',
    })
    board = ProgressBoard(repo_root=isolated)
    before = board.cached_read()
    assert {r.flag: r.state for r in before.rows}["JARVIS_C_ENABLED"] == DARK

    caller = isolated / "pkg" / "caller.py"
    caller.write_text("from pkg.c import X\n", encoding="utf-8")
    # mtime_ns has nanosecond resolution but a filesystem may not; a new file
    # also changes the file SET, so this is decisive either way.
    after = board.cached_read()
    assert after.fingerprint != before.fingerprint
    assert after.from_cache is False
    assert {r.flag: r.state for r in after.rows}["JARVIS_C_ENABLED"] != DARK


def test_changing_a_jarvis_variable_invalidates_the_reading(
        isolated, monkeypatch) -> None:
    """An identical tree read under a different flag environment is a
    different reading. Keying on the tree alone would let one operator's
    session poison another's with the more comfortable of the two answers."""
    _tree(isolated, {
        "pkg/__init__.py": "",
        "pkg/d.py": 'import os\nX = os.environ.get("JARVIS_D_ENABLED", "true")\n',
    })
    board = ProgressBoard(repo_root=isolated)
    before = board.cached_read()
    assert {r.flag: r.state for r in before.rows}["JARVIS_D_ENABLED"] == DARK

    monkeypatch.setenv("JARVIS_D_ENABLED", "0")
    after = board.cached_read()
    assert after.fingerprint != before.fingerprint
    assert {r.flag: r.state for r in after.rows}["JARVIS_D_ENABLED"] != DARK


def test_a_corrupt_cache_is_rescanned_not_trusted(isolated) -> None:
    """Never raises, never serves garbage. A cache is a performance artefact
    and must not be able to break the reader."""
    _tree(isolated, {
        "pkg/__init__.py": "",
        "pkg/e.py": 'import os\nX = os.environ.get("JARVIS_E_ENABLED", "true")\n',
    })
    board = ProgressBoard(repo_root=isolated)
    board.cached_read()
    Path(os.environ["JARVIS_PROGRESS_BOARD_CACHE_PATH"]).write_text(
        "{not json at all", encoding="utf-8")
    again = board.cached_read()
    assert again.from_cache is False
    assert {r.flag: r.state for r in again.rows}["JARVIS_E_ENABLED"] == DARK


def test_a_cache_from_another_schema_is_ignored(isolated) -> None:
    """Both guards are load-bearing. A build whose ROW SHAPE changed can have
    a fingerprint that still matches, because the tree did not move across the
    upgrade."""
    import json as _json

    _tree(isolated, {
        "pkg/__init__.py": "",
        "pkg/f.py": 'import os\nX = os.environ.get("JARVIS_F_ENABLED", "true")\n',
    })
    board = ProgressBoard(repo_root=isolated)
    board.cached_read()
    path = Path(os.environ["JARVIS_PROGRESS_BOARD_CACHE_PATH"])
    payload = _json.loads(path.read_text(encoding="utf-8"))
    payload["schema"] = "progress_board.cache.v0"
    path.write_text(_json.dumps(payload), encoding="utf-8")
    assert board.cached_read().from_cache is False


def test_the_cache_can_be_turned_off_and_agrees_when_it_is(
        isolated, monkeypatch) -> None:
    """The escape hatch, and the proof it is not needed: cached and uncached
    readings of the same tree must be identical."""
    _tree(isolated, {
        "pkg/__init__.py": "",
        "pkg/g.py": 'import os\nX = os.environ.get("JARVIS_G_ENABLED", "true")\n',
        "pkg/h.py": "from pkg.g import X\n",
    })
    board = ProgressBoard(repo_root=isolated)
    warmed = board.cached_read()
    assert board.cached_read().from_cache is True

    monkeypatch.setenv("JARVIS_PROGRESS_BOARD_CACHE", "0")
    direct = board.cached_read()
    assert direct.from_cache is False
    assert [r.to_dict() for r in direct.rows] == [r.to_dict() for r in warmed.rows]


def test_a_degraded_reading_is_never_stored(isolated, monkeypatch) -> None:
    """A degraded reading describes a transient failure, not the tree. Storing
    one would serve that failure back for as long as nothing moves."""
    _tree(isolated, {"pkg/__init__.py": ""})
    monkeypatch.setenv("JARVIS_PROGRESS_BOARD_ENABLED", "0")
    board = ProgressBoard(repo_root=isolated)
    assert board.cached_read().degraded == "disabled"
    assert not Path(os.environ["JARVIS_PROGRESS_BOARD_CACHE_PATH"]).exists()


def test_a_row_survives_the_round_trip_intact() -> None:
    """`kind` and `is_actionable` are derived, so restoring the eight stored
    fields restores the behaviour the pin reads."""
    row = FeatureRow(
        flag="JARVIS_X_ENABLED", state=DARK, module="pkg/x.py",
        category="governance", enabled=True, importers=0,
        reason="nothing imports it", value="true",
    )
    back = FeatureRow.from_dict(row.to_dict())
    assert back == row
    assert back.kind == row.kind == "switch"
    assert back.is_actionable is True


def test_the_cache_cannot_perturb_its_own_key(isolated) -> None:
    """`.jarvis/` is excluded from the scan. If it were not, writing the cache
    would change the tree, so the next fingerprint would differ and the cache
    would never hit — a slow, silent no-op that no test would have caught."""
    from backend.core.ouroboros.battle_test.progress_board import (
        _EXCLUDE_DIRS, cache_path,
    )
    assert ".jarvis" in _EXCLUDE_DIRS
    # And the default location is in fact inside it.
    os.environ.pop("JARVIS_PROGRESS_BOARD_CACHE_PATH", None)
    try:
        assert ".jarvis" in cache_path(isolated).parts
    finally:
        os.environ["JARVIS_PROGRESS_BOARD_CACHE_PATH"] = str(
            isolated / "cache.json")


# ---------------------------------------------------------------------------
# the findings this pin was built from, as regressions
# ---------------------------------------------------------------------------

def test_meta_sensor_is_still_the_headline(reading: BoardReading) -> None:
    """The single most quotable row. When this fails, it is either because
    meta_sensor was wired — delete this test and its waiver — or because the
    board stopped seeing it, which is far more serious."""
    rows = {r.flag: r for r in reading.rows}
    got = rows.get("JARVIS_META_SENSOR_ENABLED")
    assert got is not None, "the flag vanished from the board entirely"
    assert got.state in (DARK, "live"), got.state
    if got.state != DARK:
        pytest.fail(
            "meta_sensor is reachable now — delete this test and its waiver, "
            "and lower the ceiling in test_the_backlog_is_shrinking_not_growing"
        )


def test_the_dark_tiers_are_still_where_the_audit_found_them(
        reading: BoardReading) -> None:
    """Shape, not count. The finding was never '37 flags' — it was that the
    dark set clusters in adaptation, verification and observability, which are
    the tiers graded A. If that clustering dissolves, the roadmap's grades
    need re-deriving from whatever is true then."""
    dark = {r.module for r in _dark_switches(reading)}
    for tier in ("governance/adaptation/",
                 "governance/verification/",
                 "governance/observability/"):
        assert any(tier in m for m in dark), (
            f"no dark switch remains under {tier} — if this tier is fully "
            "wired, that is the headline and this assertion should be "
            "narrowed to the tiers that are still dark"
        )
