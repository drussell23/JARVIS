"""Regression spine for AutoCommit graduation evidence — Phase 2.

The gate proves "AutoCommitter actually fired on a Yellow-tier op during
each counted clean soak". Everything here is about whether that proof is
HONEST: what it claims, what it refuses to claim, and what it must never
quietly convert one into the other.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import auto_commit_graduation_gate as g
from backend.core.ouroboros.governance import (
    autocommit_evidence_attribution as attr,
)
from backend.core.ouroboros.governance import unified_graduation_dashboard as d
from tests.source_probe import code_of

OV = "Ouroboros+Venom"
YEL = "Risk: NOTIFY_APPLY"


def _classify(body: str):
    return g.classify_commit_body(body, ov_marker=OV, yellow_marker=YEL)


def _commit(h, epoch, *, session="", yellow=True, ov=True):
    body = "fix(x): y\n\n"
    if ov:
        body += f"{OV} — Autonomous Self-Development Engine\n"
    if yellow:
        body += f"{YEL}\n"
    trailer = attr.session_trailer_line(session)
    if trailer:
        body += trailer + "\n"
    return (h, float(epoch), body)


# -- the anchor ------------------------------------------------------------


def test_the_writer_and_reader_spell_the_anchor_once():
    """Two spellings of the trailer is a silent verification failure."""
    assert attr.extract_session_id(
        attr.session_trailer_line("soak-7") + "\n") == "soak-7"


def test_an_absent_session_writes_no_trailer_rather_than_a_placeholder():
    """`Session: unknown` would match nothing and read, later, as a
    session that no longer exists."""
    assert attr.session_trailer_line("") == ""
    assert attr.session_trailer_line("   ") == ""


def test_the_committer_stamps_the_trailer():
    """Without it there is no clock-independent anchor at all."""
    from backend.core.ouroboros.governance import auto_committer

    code = code_of(auto_committer, "_build_commit_message")
    assert "session_trailer_line" in code


# -- clock skew: identity beats timestamps ---------------------------------


def test_identity_attribution_ignores_the_clock_entirely():
    """The Body and the Engine are different machines with different clocks.

    A commit stamped hours outside its soak's window still belongs to that
    soak if it says so.
    """
    commits = [_commit("a1", 999_999_999, session="soak-1")]
    a = attr.attribute("soak-1", commits, window=(0.0, 1.0),
                       classify=_classify)
    assert a.provenance is attr.Provenance.SESSION_TRAILER
    assert a.yellow_hashes == ("a1",)
    assert a.counts_as_evidence


def test_a_claimed_commit_is_never_stolen_by_another_soaks_window():
    """Mixing identity and time would let skew pull in a claimed commit."""
    commits = [_commit("a1", 50, session="soak-OTHER")]
    a = attr.attribute("soak-1", commits, window=(0.0, 100.0),
                       classify=_classify)
    assert a.provenance is attr.Provenance.ABSENT
    assert not a.counts_as_evidence


def test_the_window_survives_only_as_a_labelled_inference():
    """Pre-trailer history still counts — but says how it was established."""
    commits = [_commit("a1", 50, session="")]
    a = attr.attribute("soak-1", commits, window=(0.0, 100.0),
                       classify=_classify)
    assert a.provenance is attr.Provenance.TIME_WINDOW
    assert a.counts_as_evidence
    assert not a.provenance.is_proof
    assert "not clock-independent proof" in a.reason


def test_proof_and_inference_are_distinguishable_on_the_surface():
    """A number that cannot be proven must not look like one that can."""
    assert attr.Provenance.SESSION_TRAILER.is_proof
    assert attr.Provenance.SQUASH_RECOVERED.is_proof
    assert not attr.Provenance.TIME_WINDOW.is_proof
    for p in attr.Provenance:
        assert attr.PROVENANCE_REASON[p]


# -- squash: unreadable is not absent --------------------------------------


def test_a_squash_that_keeps_the_body_is_just_identity_again():
    """`git merge --squash` and GitHub both concatenate bodies by default,
    so the ordinary case needs no recovery at all."""
    squashed = ("s1", 500.0,
                f"squash\n\n{OV}\n{YEL}\n{attr.session_trailer_line('soak-1')}\n")
    a = attr.attribute("soak-1", [squashed], window=(0.0, 100.0),
                       classify=_classify)
    assert a.provenance is attr.Provenance.SESSION_TRAILER


def test_evidence_only_in_the_reflog_is_recovered_not_lost():
    branch = [_commit("z9", 10, session="other")]
    reflog = [_commit("a1", 50, session="soak-1")]
    absent = attr.attribute("soak-1", branch, window=(0.0, 100.0),
                            classify=_classify)
    assert absent.provenance is attr.Provenance.ABSENT
    out = attr.resolve_squash(absent, reflog, branch, window=(0.0, 100.0),
                              classify=_classify)
    assert out.provenance is attr.Provenance.SQUASH_RECOVERED
    assert out.counts_as_evidence


def test_a_proven_rewrite_with_no_recoverable_evidence_is_LOST_not_ABSENT():
    """The operator squashed a PR; their soak count must not reset."""
    branch = []
    reflog = [_commit("gone1", 50, session="", ov=False, yellow=False)]
    absent = attr.attribute("soak-1", branch, window=(0.0, 100.0),
                            classify=_classify)
    out = attr.resolve_squash(absent, reflog, branch, window=(0.0, 100.0),
                              classify=_classify)
    assert out.provenance is attr.Provenance.SQUASH_LOST
    assert out.provenance.is_unverifiable
    assert not out.counts_as_evidence, "a gate must not pass on unread data"


def test_squash_lost_is_earned_by_a_visible_rewrite_never_by_absence():
    """Otherwise any quiet soak launders itself into an excused one."""
    branch = [_commit("b1", 50, session="", ov=False, yellow=False)]
    reflog = list(branch)          # nothing rewritten away
    absent = attr.attribute("soak-1", [], window=(0.0, 100.0),
                            classify=_classify)
    out = attr.resolve_squash(absent, reflog, branch, window=(0.0, 100.0),
                              classify=_classify)
    assert out.provenance is attr.Provenance.ABSENT


def test_an_unreadable_reflog_is_not_evidence_of_a_squash():
    """"we could not look" must never be recorded as "we looked and it
    was gone" — different claims, and only one is about the history."""
    absent = attr.attribute("soak-1", [], window=(0.0, 100.0),
                            classify=_classify)
    out = attr.resolve_squash(absent, None, [], window=(0.0, 100.0),
                              classify=_classify)
    assert out.provenance is attr.Provenance.ABSENT


def test_recovery_never_raises_on_a_malformed_reflog():
    absent = attr.attribute("soak-1", [], window=None, classify=_classify)
    assert attr.resolve_squash(absent, [], [], window=None,
                               classify=_classify).provenance is (
        attr.Provenance.ABSENT)


# -- the three-state accounting --------------------------------------------


def test_the_third_state_exists_at_all():
    """Two states force a lie about a squashed soak."""
    assert hasattr(g.AutoCommitGraduationReport, "soaks_unverifiable")
    assert g.AutoCommitEvidenceVerdict.EVIDENCE_LOST_TO_SQUASH


def test_unverifiable_is_subtracted_from_missing_not_added_to_it():
    """The arithmetic that protects the operator's soak count."""
    code = code_of(g, "evaluate_graduation_evidence")
    assert "len(attributions) - with_ev - unverifiable" in code


def test_squash_loss_does_not_render_as_a_failure_on_the_board():
    """EVIDENCE_FAILED would accuse a clean soak of having gone wrong."""
    assert d._AUTOCOMMIT_VERDICT_MAP["evidence_lost_to_squash"] is (
        d.UnifiedGraduationVerdict.EVIDENCE_GATHERING)


# -- structural WHY, never a bare "locked" ---------------------------------


def test_every_blocker_has_an_operator_sentence():
    for b in g.GraduationBlocker:
        assert g.BLOCKER_REASON[b], b


def test_the_two_named_reasons_are_distinguishable():
    """The mandate's own examples."""
    soaks = g.BLOCKER_REASON[g.GraduationBlocker.INSUFFICIENT_CLEAN_SOAKS]
    sig = g.BLOCKER_REASON[g.GraduationBlocker.MISSING_YELLOW_TIER_SIGNATURE]
    assert "clean soaks" in soaks.lower()
    assert "signature" in sig.lower()
    assert soaks != sig


def test_ready_carries_no_blocker_and_everything_else_carries_one():
    V = g.AutoCommitEvidenceVerdict
    assert g._blockers_for(V.READY, 0, 0) == ()
    for missing, unver in ((1, 0), (0, 1), (1, 1), (0, 0)):
        assert g._blockers_for(V.EVIDENCE_INSUFFICIENT, missing, unver), (
            missing, unver)


def test_the_stated_reason_is_derived_from_the_counts():
    """Or a surface says "insufficient soaks" while the report says squash."""
    B = g.GraduationBlocker
    assert g._blockers_for(g.AutoCommitEvidenceVerdict.EVIDENCE_INSUFFICIENT,
                           2, 0) == (B.MISSING_YELLOW_TIER_SIGNATURE,)
    assert g._blockers_for(
        g.AutoCommitEvidenceVerdict.EVIDENCE_LOST_TO_SQUASH, 0, 2) == (
        B.VERIFICATION_LOST_VIA_SQUASH,)


async def test_a_disabled_gate_says_how_to_enable_it(monkeypatch):
    monkeypatch.delenv("JARVIS_AUTOCOMMIT_GRADUATION_GATE_ENABLED",
                       raising=False)
    rep = await g.evaluate_graduation_evidence()
    assert rep.verdict is g.AutoCommitEvidenceVerdict.MASTER_OFF
    assert rep.blockers == (g.GraduationBlocker.GATE_DISABLED,)
    assert "JARVIS_AUTOCOMMIT_GRADUATION_GATE_ENABLED" in (
        rep.blocker_reasons[0])


# -- the binding: DI, no globals -------------------------------------------


class _Report:
    class _V:
        value = "evidence_insufficient"
    verdict = _V()
    ledger_clean_count, ledger_required = 3, 3
    soaks_with_evidence, soaks_missing_evidence, soaks_unverifiable = 2, 1, 0
    blocker_reasons = ("Missing git cryptographic signature — one or more "
                       "counted clean soaks produced no O+V commit",)
    is_ready = False
    per_soak_evidence = ()


async def test_the_dashboard_takes_the_gate_by_injection():
    async def _gate():
        return _Report()

    snap = await d.aggregate_dashboard_async(autocommit_gate=_gate)
    row = next(r for r in snap.rows if r.name == d.AUTOCOMMIT_ROW_NAME)
    assert row.verdict is d.UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT
    assert "cryptographic signature" in row.diagnostic


async def test_the_row_never_renders_a_bare_locked_state():
    async def _gate():
        return _Report()

    snap = await d.aggregate_dashboard_async(autocommit_gate=_gate)
    row = next(r for r in snap.rows if r.name == d.AUTOCOMMIT_ROW_NAME)
    assert "locked" not in row.diagnostic.lower()
    assert "clean=" in row.diagnostic and "unverifiable=" in row.diagnostic


async def test_injection_stores_nothing_and_overrides_nothing():
    """Zero globals: a second caller is unaffected by the first."""
    async def _a():
        return _Report()

    class _Other(_Report):
        class _V:
            value = "ready"
        verdict = _V()

    async def _b():
        return _Other()

    first = await d.aggregate_dashboard_async(autocommit_gate=_a)
    second = await d.aggregate_dashboard_async(autocommit_gate=_b)
    third = await d.aggregate_dashboard_async(autocommit_gate=_a)
    pick = lambda s: next(  # noqa: E731
        r for r in s.rows if r.name == d.AUTOCOMMIT_ROW_NAME).raw_verdict
    assert (pick(first), pick(second), pick(third)) == (
        "evidence_insufficient", "ready", "evidence_insufficient")


async def test_an_exploding_gate_never_takes_down_the_board():
    async def _boom():
        raise RuntimeError("git exploded")

    snap = await d.aggregate_dashboard_async(autocommit_gate=_boom)
    row = next(r for r in snap.rows if r.name == d.AUTOCOMMIT_ROW_NAME)
    assert "gate_error:RuntimeError" in row.diagnostic
    assert len(snap.rows) > 1, "the other gates still reported"


async def test_the_sync_dashboard_is_composed_not_duplicated():
    code = code_of(d, "aggregate_dashboard_async")
    assert "aggregate_dashboard(" in code
    assert "_CONTRACT_ADAPTERS" not in code


async def test_the_verb_surfaces_the_gate():
    """A gate an operator cannot ask about is the unmounted class again."""
    from backend.core.ouroboros.battle_test import repl_dispatch_registry as rd

    out = await rd.try_dispatch("/graduation autocommit")
    assert out is not None and out.matched
    assert "AutoCommitter" in out.text


async def test_the_verb_renders_the_structural_reason():
    from backend.core.ouroboros.governance.graduation_repl import (
        _render_autocommit,
    )

    async def _gate():
        return _Report()

    res = await _render_autocommit(gate=_gate)
    assert res.ok
    assert "cryptographic signature" in res.text
    assert "unverifiable" in res.text


# -- composition discipline ------------------------------------------------


def test_the_gate_composes_the_generic_ledger_and_does_not_reimplement_it():
    code = code_of(g)
    assert "GraduationLedger" in code
    assert "_read_all" in code
    assert "json.loads" not in code, "the ledger JSONL is re-parsed here"


def test_attribution_holds_no_opinion_about_what_an_ov_commit_is():
    """The gate owns that definition; this owns membership only."""
    code = code_of(attr)
    assert "Risk: NOTIFY_APPLY" not in code
    assert "ov_marker" not in code
