"""A capability that delegates its bookkeeping was being called dead.

Firing markers are derived SOURCE-LOCALLY: a ledger counts only if the literal
``name.jsonl`` appears in the capability's own text. That is right for a module
that opens its own file and wrong for one that delegates — and delegating is
the better design, so the observability layer was penalising exactly the code
that separates concerns.

``repair_engine`` is the case that surfaced it. L2 persists every attempt:
``repair_engine`` → ``repair_tree._archive_result`` → ``repair_tree_archive``
→ ``.jarvis/ouroboros/repair_tree.jsonl``, through the canonical flock append.
Two hops, so ``repair_engine.py`` contains no ``.jsonl`` literal, so it derived
LOG TAGS ONLY — and `firing_verdict` documents a log-only silence as
*ambiguous*: "an absent log tag may mean 'ran silently' — an observability
gap — not proven dormancy".

The liveness sensor read that ambiguity as proven death and scored
``JARVIS_L2_ENABLED`` — a ``safety`` capability, the loop that closes the
Ouroboros cycle — a HIGH-severity severance.

Measured on the live snapshot: **11 of the 12 rows scoring HIGH did so on
log-only silence.** An auditor wrong 11 times in 12 is ignored on the twelfth.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.capability_firing import (
    FiringEvidence, derive_markers, firing_verdict,
)
from backend.core.ouroboros.governance.intake.sensors.liveness_sensor import (
    severity_for,
)

_REPO = Path(__file__).resolve().parents[2]
_REPAIR = _REPO / "backend/core/ouroboros/governance/repair_engine.py"


# ---------------------------------------------------------------------------
# 1. the declaration
# ---------------------------------------------------------------------------


class TestDeclaredEvidence:
    def test_a_module_can_declare_a_ledger_it_writes_through_a_collaborator(self):
        markers = derive_markers(
            '__firing_ledgers__ = ("repair_tree",)\n'
            'logger.info("[Thing] hello")\n'
        )
        assert "repair_tree" in markers.ledgers
        assert "Thing" in markers.tags

    def test_declared_ledgers_union_with_scanned_ones(self):
        """A module may both open its own ledger and drive another's. The
        declaration ADDS; it never replaces what the scan found."""
        markers = derive_markers(
            '__firing_ledgers__ = ("repair_tree",)\n'
            'PATH = "own_history.jsonl"\n'
        )
        assert {"repair_tree", "own_history"} <= set(markers.ledgers)

    def test_a_jsonl_suffix_in_the_declaration_is_tolerated(self):
        """``("repair_tree.jsonl",)`` means the same thing to a human, so it
        must mean the same thing here — evidence is compared by STEM."""
        assert "repair_tree" in derive_markers(
            '__firing_ledgers__ = ("repair_tree.jsonl",)').ledgers

    def test_tags_can_be_declared_too(self):
        """A module that logs under a name its own text never spells — a
        delegated logger, a renamed component — has the same blind spot."""
        assert "Treefinement" in derive_markers(
            '__firing_tags__ = ("Treefinement",)').tags

    def test_declaration_is_PARSED_not_imported(self):
        """Derivation must stay pure. Importing a capability to ask whether it
        is alive would run its module body — side effects, logging config, and
        a circular dependency for anything the auditor itself imports."""
        markers = derive_markers(
            '__firing_ledgers__ = ("never_imported_module_xyz",)')
        assert "never_imported_module_xyz" in markers.ledgers

    @pytest.mark.parametrize("hostile", [
        "__firing_ledgers__ = (", "__firing_ledgers__ = ()",
        "__firing_ledgers__", "", "__firing_ledgers__ = (1, 2)",
    ])
    def test_malformed_declarations_never_raise(self, hostile):
        assert isinstance(derive_markers(hostile).ledgers, tuple)

    def test_l2_now_derives_its_real_evidence_channel(self):
        """The live file, not a fixture — the declaration has to be ON it."""
        markers = derive_markers(_REPAIR.read_text(encoding="utf-8"))
        assert "repair_tree" in markers.ledgers, (
            "repair_engine lost its __firing_ledgers__ declaration; its "
            "dormancy is unprovable again"
        )
        assert "RepairEngine" in markers.tags


# ---------------------------------------------------------------------------
# 2. what the declaration BUYS — provability, in both directions
# ---------------------------------------------------------------------------


class TestProvability:
    def test_an_inactive_declared_ledger_is_PROVEN_dormancy(self):
        """The point is not to make L2 look alive. It is to make the answer
        falsifiable: with no ledger row in-window, the verdict is SILENT on a
        ``ledger`` channel — reliable evidence-of-work absent, which is a real
        signal worth acting on."""
        verdict, hits, channels = firing_verdict(
            '__firing_ledgers__ = ("repair_tree",)\nlog("[RepairEngine] x")',
            FiringEvidence(present_markers=set(), active_ledgers=set()),
        )
        assert verdict == "SILENT"
        assert "ledger" in channels
        assert not hits

    def test_an_active_declared_ledger_flips_it_to_FIRING(self):
        verdict, hits, _ = firing_verdict(
            '__firing_ledgers__ = ("repair_tree",)',
            FiringEvidence(active_ledgers={"repair_tree"}),
        )
        assert verdict == "FIRING"
        assert "ledger:repair_tree" in hits

    def test_without_the_declaration_the_channel_is_log_only(self):
        """The state this fixes: markers derivable, but only ambiguous ones."""
        _, _, channels = firing_verdict(
            'log("[RepairEngine] x")',
            FiringEvidence(present_markers=set(), active_ledgers=set()),
        )
        assert channels == ["log"]


# ---------------------------------------------------------------------------
# 3. the consumer — a computed distinction that was being dropped
# ---------------------------------------------------------------------------


class TestSeverityHonoursProvenance:
    def test_log_only_silence_cannot_raise_an_alarm(self):
        assert severity_for("safety", "SILENT", 1.0, ledger_backed=False) == "low"

    def test_ledger_backed_silence_still_does(self):
        assert severity_for("safety", "SILENT", 1.0, ledger_backed=True) == "high"

    def test_missing_provenance_is_not_proof(self):
        assert severity_for("safety", "SILENT", 1.0) == "low"

    def test_the_distinction_already_existed_upstream(self):
        """Not a new concept — `CapabilityVerdict.ledger_backed` has been
        computed and published all along, and `capability_liveness` already
        sorts ALIVE capabilities into ``dormant`` vs ``observability_gaps`` on
        it. It was simply never consulted where it decides whether to wake
        someone."""
        from backend.core.ouroboros.governance.capability_liveness import (
            CapabilityVerdict,
        )
        v = CapabilityVerdict(flag="F", source_file="x.py", category="safety",
                              live_on=(), verdict="SEVERED",
                              firing="SILENT", firing_channels=("log",))
        assert v.ledger_backed is False
        assert "ledger_backed" in v.to_dict()

    def test_the_sensor_forwards_it(self):
        """The seam itself. Computing the distinction upstream and dropping it
        at the consumer is what let 11 of 12 HIGH findings rest on unprovable
        silence — and a grep for the field name would have passed the whole
        time, since it is present three modules away."""
        import inspect
        from backend.core.ouroboros.governance.intake.sensors import (
            liveness_sensor,
        )
        scan_src = inspect.getsource(liveness_sensor.LivenessSensor)
        assert "ledger_backed" in scan_src, (
            "the sensor stopped forwarding ledger_backed; severity is being "
            "decided on evidence it was given and threw away"
        )
