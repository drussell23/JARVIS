"""`total_claims == 0` had four meanings and one spelling.

The MetaSensor's dormancy alarm was mounted on 2026-08-08 and fired
immediately:

    p1  VERIFICATION LOOP IS NOT EXERCISING — 71/100 (71%) of recent
    postmortems have total_claims=0 ... Check Priority A claim-capture
    wiring at every PLAN exit.

The claim-capture wiring was working at 100%. Measured on the live ledger:
every one of 2,014 routed ops with a postmortem had captured claims. What the
alarm was actually counting was ops that never got that far — 3,333 of its
5,644 empty records terminated at CLASSIFY, before PLAN exists to capture
anything. For those, zero is the correct answer.

`list_recent_postmortems` deliberately pools two record kinds:

    verification_postmortem   written at COMPLETE, claims evaluated
    terminal_postmortem       written at EVERY non-COMPLETE termination

A `terminal_postmortem` is by definition an op that did not complete. Pooling
them into one rate produced an alarm stuck at ~71% forever, naming the wrong
subsystem. An alarm that cannot be cleared is an alarm that gets ignored, and
this one had been ignorable since the day it was written — it had no caller.

THE ROOT CAUSE IS THAT THE RECORD COULD NOT SAY WHY IT WAS EMPTY
----------------------------------------------------------------
The enclosing ledger record carries `kind` and `phase`. `list_recent_
postmortems` parsed both and dropped them one line later, so a CLASSIFY
termination and a COMPLETE-with-no-claims arrived at the detector identical.
Neither an operator nor a detector could tell "nothing was claimable" from
"capture is broken".

Provenance is now stamped by the reader from the record it already parses —
no writer change, no schema migration, and every historical record acquires
it on read. Same discipline `advisor_locality` established for blast radius:
a measurement carries where it came from, and what cannot be measured is
`unknown` rather than assumed.

WHAT THE FIX EXPOSED
--------------------
With the denominator corrected the empty rate on the live window falls to 22%
and the alarm goes quiet. The signal that had been hiding behind it does not:
of 18,414 claims recorded at COMPLETE, 13,911 (76%) returned
INSUFFICIENT_EVIDENCE, and across 151,612 claims all-time **not one has ever
returned FAILED**. Three universal default claims account for it in exactly
equal counts. A must_hold claim that can never be evaluated does not block,
so the loop reports rising coverage while deciding nothing — which is what
"the verification loop is not exercising" always meant.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.ouroboros.governance.phase_cost import (  # noqa: E402
    CANONICAL_PHASE_ORDER,
)
from backend.core.ouroboros.governance.verification.postmortem import (  # noqa: E402
    _CLAIM_CAPTURE_PHASE,
    VERIFICATION_POSTMORTEM_SCHEMA_VERSION,
    VerificationPostmortem,
    list_recent_postmortems,
)


def _pm(**kw) -> VerificationPostmortem:
    base = dict(op_id="op-1", session_id="s")
    base.update(kw)
    return VerificationPostmortem(**base)


# ---------------------------------------------------------------------------
# the provenance itself
# ---------------------------------------------------------------------------

def test_claims_present_is_always_evaluated() -> None:
    """A record with claims needs no phase reasoning."""
    got = _pm(total_claims=3, terminated_at_phase="CLASSIFY")
    assert got.claims_provenance == "evaluated"
    assert got.claims_were_applicable is True


@pytest.mark.parametrize("phase", ["CLASSIFY", "ROUTE", "CONTEXT_EXPANSION"])
def test_stopping_before_plan_is_not_applicable(phase) -> None:
    """THE 3,333 records. An op that never reached PLAN could not have
    claimed, so its zero is correct and must not enter the rate."""
    got = _pm(total_claims=0, terminated_at_phase=phase)
    assert got.claims_provenance == "not_applicable"
    assert got.claims_were_applicable is False


@pytest.mark.parametrize("phase", ["GENERATE", "GATE", "APPLY", "COMPLETE",
                                   "POSTMORTEM"])
def test_stopping_after_plan_with_no_claims_is_a_finding(phase) -> None:
    """States the fact and asserts no cause. PLAN may have been skipped by
    policy for a trivial op, or capture may have failed. Both deserve a look;
    neither is assumed here."""
    got = _pm(total_claims=0, terminated_at_phase=phase)
    assert got.claims_provenance == "none_recorded"
    assert got.claims_were_applicable is True


def test_an_unstamped_record_is_unknown_and_counted_neither_way() -> None:
    """A postmortem built in memory, or read by something that does not know
    where the op stopped, must not be guessed at in either direction."""
    got = _pm(total_claims=0)
    assert got.claims_provenance == "unknown"
    assert got.claims_were_applicable is False


def test_an_unrecognised_phase_is_unknown_not_optimistic() -> None:
    got = _pm(total_claims=0, terminated_at_phase="TELEPORT")
    assert got.claims_provenance == "unknown"


def test_phase_matching_is_case_and_space_insensitive() -> None:
    assert _pm(total_claims=0,
               terminated_at_phase=" classify ").claims_provenance == \
        "not_applicable"


def test_the_capture_phase_is_in_the_canonical_order() -> None:
    """The whole derivation hangs on this name existing in the FSM's own
    ordering. If PLAN is ever renamed, every record silently becomes
    `unknown` — visible as a dead alarm, but only if someone is looking."""
    assert _CLAIM_CAPTURE_PHASE in CANONICAL_PHASE_ORDER


def test_plan_itself_counts_as_having_reached_capture() -> None:
    """Claims are captured at PLAN *exit*, so an op that terminated AT plan
    got there. The live ledger has 9 such records and none is empty."""
    assert _pm(total_claims=0,
               terminated_at_phase="PLAN").claims_provenance == "none_recorded"


# ---------------------------------------------------------------------------
# the reader, which is where the information was being dropped
# ---------------------------------------------------------------------------

@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """A real per-session ledger, written in the on-disk shape."""
    session = "provenance-test"
    root = tmp_path / "determinism" / session
    root.mkdir(parents=True)
    monkeypatch.setenv("JARVIS_PROJECT_ROOT", str(tmp_path.parent))
    return root / "decisions.jsonl", session


def _write(path: Path, rows) -> None:
    """Write the on-disk shape exactly, schema stamp included.

    `from_dict` refuses a payload whose `schema_version` does not match, and
    it is right to: a reader that accepts an unversioned blob will one day
    accept a differently-shaped one and report whatever the defaults are. The
    first draft of this helper omitted the stamp and three tests failed —
    correctly.
    """
    with path.open("w", encoding="utf-8") as fh:
        for kind, phase, payload in rows:
            body = {"schema_version": VERIFICATION_POSTMORTEM_SCHEMA_VERSION}
            body.update(payload)
            fh.write(json.dumps({
                "kind": kind, "phase": phase, "op_id": payload["op_id"],
                "output_repr": json.dumps(body),
            }) + "\n")


def test_the_reader_stamps_kind_and_phase(monkeypatch, tmp_path) -> None:
    """The regression. Both values were parsed and discarded one line later,
    which is the entire reason the alarm could not tell its populations
    apart."""
    from backend.core.ouroboros.governance.verification import postmortem as pmod

    path = tmp_path / "decisions.jsonl"
    _write(path, [
        ("terminal_postmortem", "CLASSIFY",
         {"op_id": "op-a", "session_id": "s", "total_claims": 0}),
        ("verification_postmortem", "COMPLETE",
         {"op_id": "op-b", "session_id": "s", "total_claims": 2}),
    ])
    monkeypatch.setattr(pmod, "_ledger_path_for_session", lambda *_a, **_k: path)

    rows = list_recent_postmortems(limit=10)
    assert len(rows) == 2
    by = {r.op_id: r for r in rows}
    assert by["op-a"].record_kind == "terminal_postmortem"
    assert by["op-a"].terminated_at_phase == "CLASSIFY"
    assert by["op-a"].claims_provenance == "not_applicable"
    assert by["op-b"].record_kind == "verification_postmortem"
    assert by["op-b"].claims_provenance == "evaluated"


def test_historical_records_acquire_provenance_without_migration(
        monkeypatch, tmp_path) -> None:
    """The payloads on disk predate these fields and are never rewritten.
    Stamping from the ENCLOSING record is what makes 8,288 existing records
    readable rather than requiring a migration nobody would run."""
    from backend.core.ouroboros.governance.verification import postmortem as pmod

    path = tmp_path / "decisions.jsonl"
    # Exactly the historical shape: no provenance keys in the payload.
    _write(path, [("terminal_postmortem", "CLASSIFY",
                   {"op_id": "old", "session_id": "s", "total_claims": 0})])
    monkeypatch.setattr(pmod, "_ledger_path_for_session", lambda *_a, **_k: path)
    got = list_recent_postmortems(limit=1)[0]
    assert got.claims_provenance == "not_applicable"


def test_from_dict_without_the_new_keys_still_parses() -> None:
    """Back-compat in the other direction: an old payload must not raise."""
    got = VerificationPostmortem.from_dict({
        "schema_version": VERIFICATION_POSTMORTEM_SCHEMA_VERSION,
        "op_id": "x", "session_id": "s", "total_claims": 0,
    })
    assert got is not None
    assert got.record_kind == ""
    assert got.claims_provenance == "unknown"


# ---------------------------------------------------------------------------
# the detector — the reason any of this matters
# ---------------------------------------------------------------------------

def test_a_window_of_pre_plan_terminations_does_not_fire(monkeypatch) -> None:
    """THE false alarm, reproduced and killed.

    100 ops that all died at CLASSIFY. Claim capture is perfect; there was
    simply nothing to capture. The old detector reported 100% and blamed
    PLAN.
    """
    from backend.core.ouroboros.governance.intake.sensors import meta_sensor

    rows = tuple(
        _pm(op_id=f"op-{i}", total_claims=0, terminated_at_phase="CLASSIFY")
        for i in range(100)
    )
    monkeypatch.setattr(meta_sensor, "empty_postmortem_window", lambda: 100)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification.list_recent_postmortems",
        lambda **_k: rows,
    )
    assert meta_sensor._evaluate_empty_postmortem_rate() is None


def test_a_genuine_capture_failure_still_fires(monkeypatch) -> None:
    """The other half. Silencing the false alarm must not silence the true
    one — 100 ops that reached COMPLETE and captured nothing is exactly the
    defect the detector was written for."""
    from backend.core.ouroboros.governance.intake.sensors import meta_sensor

    rows = tuple(
        _pm(op_id=f"op-{i}", total_claims=0, terminated_at_phase="COMPLETE")
        for i in range(100)
    )
    monkeypatch.setattr(meta_sensor, "empty_postmortem_window", lambda: 100)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification.list_recent_postmortems",
        lambda **_k: rows,
    )
    got = meta_sensor._evaluate_empty_postmortem_rate()
    assert got is not None
    assert got.severity == "p1"
    ev = dict(got.evidence)
    assert ev["total_count"] == 100
    assert ev["excluded_not_applicable"] == 0


def test_the_excluded_population_is_reported_not_hidden(monkeypatch) -> None:
    """A rate that silently drops two thirds of its input is a rate nobody
    should trust. The finding says how many were excluded and why."""
    from backend.core.ouroboros.governance.intake.sensors import meta_sensor

    rows = tuple(
        [_pm(op_id=f"n-{i}", total_claims=0, terminated_at_phase="CLASSIFY")
         for i in range(60)]
        + [_pm(op_id=f"c-{i}", total_claims=0, terminated_at_phase="COMPLETE")
           for i in range(40)]
    )
    monkeypatch.setattr(meta_sensor, "empty_postmortem_window", lambda: 100)
    monkeypatch.setattr(meta_sensor, "empty_postmortem_min_records", lambda: 20)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification.list_recent_postmortems",
        lambda **_k: rows,
    )
    got = meta_sensor._evaluate_empty_postmortem_rate()
    assert got is not None
    ev = dict(got.evidence)
    assert ev["total_count"] == 40 and ev["excluded_not_applicable"] == 60
    assert ev["records_read"] == 100
    assert "excluded" in got.summary


def test_too_few_applicable_ops_is_silence_not_an_all_clear(
        monkeypatch) -> None:
    """min_records now applies to the APPLICABLE population. A window in
    which almost nothing routed cannot support a claim-capture verdict."""
    from backend.core.ouroboros.governance.intake.sensors import meta_sensor

    rows = tuple(
        [_pm(op_id=f"n-{i}", total_claims=0, terminated_at_phase="CLASSIFY")
         for i in range(99)]
        + [_pm(op_id="c", total_claims=0, terminated_at_phase="COMPLETE")]
    )
    monkeypatch.setattr(meta_sensor, "empty_postmortem_window", lambda: 100)
    monkeypatch.setattr(meta_sensor, "empty_postmortem_min_records", lambda: 20)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification.list_recent_postmortems",
        lambda **_k: rows,
    )
    assert meta_sensor._evaluate_empty_postmortem_rate() is None


def test_unknown_provenance_is_excluded_from_both_sides(monkeypatch) -> None:
    """Never counted as healthy, never counted as broken."""
    from backend.core.ouroboros.governance.intake.sensors import meta_sensor

    rows = tuple(_pm(op_id=f"u-{i}", total_claims=0) for i in range(100))
    monkeypatch.setattr(meta_sensor, "empty_postmortem_window", lambda: 100)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification.list_recent_postmortems",
        lambda **_k: rows,
    )
    assert meta_sensor._evaluate_empty_postmortem_rate() is None


# ---------------------------------------------------------------------------
# the detector for what was hiding behind the false alarm
# ---------------------------------------------------------------------------

def test_claims_that_cannot_be_judged_fire(monkeypatch) -> None:
    """The real signal: 76% of claims at COMPLETE return INSUFFICIENT and not
    one has ever returned FAILED."""
    from backend.core.ouroboros.governance.intake.sensors import meta_sensor

    rows = tuple(
        _pm(op_id=f"op-{i}", total_claims=4, insufficient_count=3,
            terminated_at_phase="COMPLETE")
        for i in range(30)
    )
    monkeypatch.setattr(meta_sensor, "empty_postmortem_window", lambda: 100)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification.list_recent_postmortems",
        lambda **_k: rows,
    )
    got = meta_sensor._evaluate_unjudgeable_claim_rate()
    assert got is not None and got.severity == "p1"
    ev = dict(got.evidence)
    assert ev["total_claims"] == 120 and ev["insufficient_count"] == 90
    assert 0.74 < ev["rate"] < 0.76


def test_evaluator_errors_count_as_undecided(monkeypatch) -> None:
    """A claim whose evaluator raised is no more settled than one with
    missing evidence. Counting only INSUFFICIENT would let a broken
    evaluator read as a healthy loop."""
    from backend.core.ouroboros.governance.intake.sensors import meta_sensor

    rows = tuple(
        _pm(op_id=f"op-{i}", total_claims=4, error_count=3,
            terminated_at_phase="COMPLETE")
        for i in range(30)
    )
    monkeypatch.setattr(meta_sensor, "empty_postmortem_window", lambda: 100)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification.list_recent_postmortems",
        lambda **_k: rows,
    )
    got = meta_sensor._evaluate_unjudgeable_claim_rate()
    assert got is not None
    assert dict(got.evidence)["error_count"] == 90


def test_a_healthy_loop_is_quiet(monkeypatch) -> None:
    from backend.core.ouroboros.governance.intake.sensors import meta_sensor

    rows = tuple(
        _pm(op_id=f"op-{i}", total_claims=4, insufficient_count=0,
            terminated_at_phase="COMPLETE")
        for i in range(30)
    )
    monkeypatch.setattr(meta_sensor, "empty_postmortem_window", lambda: 100)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification.list_recent_postmortems",
        lambda **_k: rows,
    )
    assert meta_sensor._evaluate_unjudgeable_claim_rate() is None


def test_too_few_claims_is_silence(monkeypatch) -> None:
    from backend.core.ouroboros.governance.intake.sensors import meta_sensor

    rows = (_pm(op_id="op-1", total_claims=2, insufficient_count=2,
                terminated_at_phase="COMPLETE"),)
    monkeypatch.setattr(meta_sensor, "empty_postmortem_window", lambda: 100)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification.list_recent_postmortems",
        lambda **_k: rows,
    )
    assert meta_sensor._evaluate_unjudgeable_claim_rate() is None


def test_both_detectors_are_registered() -> None:
    """Registration is what makes an evaluate() function a sensor. A detector
    written and not registered is the same defect as a module merged and not
    imported — which is how this whole arc started."""
    from backend.core.ouroboros.governance.intake.sensors.meta_sensor import (
        list_dormancy_detectors,
    )
    kinds = {d.detector_kind for d in list_dormancy_detectors()}
    assert {"empty_postmortem_rate", "unjudgeable_claim_rate"} <= kinds


def test_neither_detector_raises_when_the_ledger_is_unreadable(
        monkeypatch) -> None:
    """Both are called from a sensor whose contract is NEVER raises."""
    from backend.core.ouroboros.governance.intake.sensors import meta_sensor

    def _boom(**_k):
        raise OSError("ledger on fire")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification.list_recent_postmortems",
        _boom,
    )
    assert meta_sensor._evaluate_empty_postmortem_rate() is None
    assert meta_sensor._evaluate_unjudgeable_claim_rate() is None


def test_the_thresholds_are_configurable_and_bounded(monkeypatch) -> None:
    from backend.core.ouroboros.governance.intake.sensors import meta_sensor

    monkeypatch.setenv("JARVIS_META_UNJUDGEABLE_CLAIM_THRESHOLD", "0.9")
    assert meta_sensor.unjudgeable_claim_threshold() == 0.9
    monkeypatch.setenv("JARVIS_META_UNJUDGEABLE_CLAIM_THRESHOLD", "12")
    assert meta_sensor.unjudgeable_claim_threshold() == 1.0
    monkeypatch.setenv("JARVIS_META_UNJUDGEABLE_CLAIM_THRESHOLD", "nonsense")
    assert meta_sensor.unjudgeable_claim_threshold() == 0.5
    monkeypatch.setenv("JARVIS_META_UNJUDGEABLE_MIN_CLAIMS", "0")
    assert meta_sensor.unjudgeable_min_claims() == 1
