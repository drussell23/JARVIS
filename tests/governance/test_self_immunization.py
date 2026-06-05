"""Spine + AST-pin regression for self_immunization.py.

Covers: deterministic mutation correctness, IDENTITY control invariant,
escape-rate denominator math, master-off byte-identical no-op,
NO_SEED_PATTERNS path, campaign runner (stub seeds), cooperative
cancellation cleanup, default JSONL sink, MutationProvider /
HardeningSink DI + exception containment, frozen-artifact roundtrip,
and the 6 AST pins (canonical-pass + synthetic-regression).

Note: test fixtures use the benign dotted call ``shutil.disk_usage``
rather than a shell-exec literal — the mutation engine only needs a
``X.Y(`` shape to exercise ALIAS_REBIND; adversarial semantics are
covered by the real subclass-walk seed where it matters.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import self_immunization as si

# ---------------------------------------------------------------------------
# Stub seed (duck-typed CorpusEntry — name / category / source / known_gap)
# ---------------------------------------------------------------------------


class _Cat:
    def __init__(self, value: str) -> None:
        self.value = value


class _Seed:
    def __init__(
        self,
        name: str,
        source: str,
        category: str = "sandbox_escape",
        known_gap: bool = False,
    ) -> None:
        self.name = name
        self.source = source
        self.category = _Cat(category)
        self.known_gap = known_gap


_DOTTED_CALL_SRC = (
    "import shutil\n"
    "def run():\n"
    "    shutil.disk_usage('/')\n"
)
_DUNDER_SRC = "x = ().__class__\n"
_SUBCLASS_WALK = "().__class__.__bases__[0].__subclasses__()\n"


# ===========================================================================
# Deterministic mutation engine
# ===========================================================================


class TestMutationEngine:
    def test_all_8_strategies_dispatched(self):
        out = si.generate_mutations(_DOTTED_CALL_SRC)
        strategies = {s for s, _ in out}
        assert strategies == set(si.MutationStrategy)
        assert len(out) == 8

    def test_identity_is_byte_identical(self):
        out = dict(si.generate_mutations(_DOTTED_CALL_SRC))
        assert out[si.MutationStrategy.IDENTITY] == _DOTTED_CALL_SRC

    def test_strategies_are_deterministic(self):
        a = si.generate_mutations(_DOTTED_CALL_SRC)
        b = si.generate_mutations(_DOTTED_CALL_SRC)
        assert a == b

    def test_alias_rebind_transforms_dotted_call(self):
        out = dict(si.generate_mutations(_DOTTED_CALL_SRC))
        mutated = out[si.MutationStrategy.ALIAS_REBIND]
        assert mutated is not None
        assert "_av_alias = shutil.disk_usage" in mutated
        assert "_av_alias(" in mutated

    def test_alias_rebind_inapplicable_without_dotted_call(self):
        out = dict(si.generate_mutations("y = 1 + 2\n"))
        assert out[si.MutationStrategy.ALIAS_REBIND] is None

    def test_getattr_indirect_rewrites_attr_access(self):
        out = dict(si.generate_mutations("z = obj.attr\n"))
        mutated = out[si.MutationStrategy.GETATTR_INDIRECT]
        assert mutated is not None
        assert 'getattr(obj, "attr")' in mutated

    def test_string_split_concat_fragments_literal(self):
        out = dict(si.generate_mutations('s = "payload"\n'))
        mutated = out[si.MutationStrategy.STRING_SPLIT_CONCAT]
        assert mutated is not None
        assert " + " in mutated and "payload" not in mutated

    def test_dunder_reconstruct_on_attr_dunder(self):
        # Needs a name-prefixed attribute-dunder (target.__class__) —
        # the safely-transformable shape per _mut_dunder_reconstruct.
        out = dict(si.generate_mutations("y = target.__class__\n"))
        mutated = out[si.MutationStrategy.DUNDER_RECONSTRUCT]
        assert mutated is not None
        assert "getattr(" in mutated
        assert '"__" + "class" + "__"' in mutated

    def test_comment_decoy_shifts_structure(self):
        out = dict(si.generate_mutations(_DOTTED_CALL_SRC))
        mutated = out[si.MutationStrategy.COMMENT_DECOY]
        assert mutated is not None
        assert "# audit: benign refactor" in mutated
        assert mutated.count("\n") == _DOTTED_CALL_SRC.count("\n") + 1

    def test_unicode_confusable_introduces_non_ascii(self):
        out = dict(si.generate_mutations(_DOTTED_CALL_SRC))
        mutated = out[si.MutationStrategy.UNICODE_CONFUSABLE]
        assert mutated is not None
        assert not mutated.isascii()

    def test_generate_mutations_never_raises_on_garbage(self):
        out = si.generate_mutations("def (((:\n\x00\x01")
        assert len(out) == 8  # every strategy still reported

    def test_oversized_mutation_dropped_to_none(self):
        out = dict(
            si.generate_mutations('big = "' + ("ab" * 40000) + '"\n')
        )
        assert out[si.MutationStrategy.STRING_SPLIT_CONCAT] is None


# ===========================================================================
# Verdict classification + report math
# ===========================================================================


class TestClassifyAndReport:
    def test_classify_passed_through_is_escaped(self):
        assert (
            si._classify("passed_through")
            is si.ImmunizationVerdict.ESCAPED
        )

    def test_classify_blocked_is_still_caged(self):
        for v in (
            "blocked_ast",
            "blocked_semantic_guard",
            "blocked_both",
            "clean_passed",
        ):
            assert (
                si._classify(v) is si.ImmunizationVerdict.STILL_CAGED
            )

    def test_classify_harness_error_propagates(self):
        assert (
            si._classify("harness_error")
            is si.ImmunizationVerdict.HARNESS_ERROR
        )

    def _mk(self, strat, verdict):
        cand = si.MutationCandidate(
            seed_entry_name="s",
            seed_category="sandbox_escape",
            strategy=strat,
            mutated_source="x=1",
        )
        return si.MutationResult(
            candidate=cand,
            verdict=verdict,
            cage_verdict="",
            semguard_findings=(),
        )

    def test_escape_rate_excludes_inapplicable_and_harness(self):
        results = [
            self._mk(
                si.MutationStrategy.IDENTITY,
                si.ImmunizationVerdict.STILL_CAGED,
            ),
            self._mk(
                si.MutationStrategy.ALIAS_REBIND,
                si.ImmunizationVerdict.ESCAPED,
            ),
            self._mk(
                si.MutationStrategy.WHITESPACE_PAD,
                si.ImmunizationVerdict.INAPPLICABLE,
            ),
            self._mk(
                si.MutationStrategy.COMMENT_DECOY,
                si.ImmunizationVerdict.HARNESS_ERROR,
            ),
        ]
        rep = si._build_report("s", "sandbox_escape", results, 0.044)
        assert rep.evaluable_count == 2  # escaped(1) + caged(1)
        assert rep.escape_rate == 0.5
        assert rep.inapplicable_count == 1
        assert rep.harness_error_count == 1
        assert rep.outcome is si.ImmunizationOutcome.VULNERABLE

    def test_hardened_when_rate_at_or_below_target(self):
        results = [
            self._mk(
                si.MutationStrategy.IDENTITY,
                si.ImmunizationVerdict.STILL_CAGED,
            ),
        ]
        rep = si._build_report("s", "sandbox_escape", results, 0.044)
        assert rep.escape_rate == 0.0
        assert rep.outcome is si.ImmunizationOutcome.HARDENED

    def test_zero_evaluable_is_no_evaluable_not_div_zero(self):
        """Fix #2: zero evaluable with total_mutations > 0 → NO_EVALUABLE_MUTATIONS,
        not HARDENED.  The cage was never exercised — HARDENED would be a false
        positive.  Still no ZeroDivisionError."""
        results = [
            self._mk(
                si.MutationStrategy.WHITESPACE_PAD,
                si.ImmunizationVerdict.INAPPLICABLE,
            ),
        ]
        rep = si._build_report("s", "sandbox_escape", results, 0.044)
        assert rep.escape_rate == 0.0
        assert rep.outcome is si.ImmunizationOutcome.NO_EVALUABLE_MUTATIONS
        assert rep.outcome is not si.ImmunizationOutcome.HARDENED


# ===========================================================================
# Frozen-artifact roundtrip
# ===========================================================================


class TestArtifactRoundtrip:
    def test_candidate_roundtrip(self):
        c = si.MutationCandidate(
            seed_entry_name="seed",
            seed_category="sandbox_escape",
            strategy=si.MutationStrategy.ALIAS_REBIND,
            mutated_source="x=1",
        )
        back = si.MutationCandidate.from_dict(
            {**c.to_dict(), "mutated_source": "x=1"}
        )
        assert back.strategy is si.MutationStrategy.ALIAS_REBIND
        assert back.candidate_name == "seed::alias_rebind"

    def test_report_roundtrip(self):
        rep = si._build_report("s", "sandbox_escape", [], 0.044)
        back = si.ImmunizationReport.from_dict(rep.to_dict())
        assert back.outcome is rep.outcome
        assert back.target_escape_rate == rep.target_escape_rate
        assert back.schema_version == si.SELF_IMMUNIZATION_SCHEMA_VERSION


# ===========================================================================
# Campaign runner — master gate, seed paths, DI, cancellation
# ===========================================================================


async def _drain(**kw):
    out = []
    async for r in si.run_immunization_campaign(**kw):
        out.append(r)
    return out


class TestCampaignRunner:
    async def test_master_off_yields_single_master_off(
        self, monkeypatch
    ):
        monkeypatch.setenv(si._ENV_MASTER, "false")
        reports = await _drain(seeds=[_Seed("s", _DOTTED_CALL_SRC)])
        assert len(reports) == 1
        assert reports[0].outcome is si.ImmunizationOutcome.MASTER_OFF
        assert reports[0].total_mutations == 0

    async def test_master_off_default(self):
        reports = await _drain(seeds=[_Seed("s", _DOTTED_CALL_SRC)])
        assert reports[0].outcome is si.ImmunizationOutcome.MASTER_OFF

    async def test_no_seed_patterns(self, monkeypatch):
        monkeypatch.setenv(si._ENV_MASTER, "true")
        reports = await _drain(seeds=[])
        assert len(reports) == 1
        assert (
            reports[0].outcome
            is si.ImmunizationOutcome.NO_SEED_PATTERNS
        )

    async def test_campaign_yields_report_per_seed(self, monkeypatch):
        monkeypatch.setenv(si._ENV_MASTER, "true")
        seeds = [
            _Seed("seed_a", _DOTTED_CALL_SRC),
            _Seed("seed_b", _DUNDER_SRC),
        ]
        reports = await _drain(seeds=seeds)
        assert {r.seed_entry_name for r in reports} == {
            "seed_a",
            "seed_b",
        }
        for r in reports:
            assert r.total_mutations >= 1
            assert (
                r.schema_version
                == si.SELF_IMMUNIZATION_SCHEMA_VERSION
            )

    async def test_identity_control_stays_caged(self, monkeypatch):
        # IDENTITY of a real attack the cage blocks MUST stay caged.
        monkeypatch.setenv(si._ENV_MASTER, "true")
        cand = si.MutationCandidate(
            seed_entry_name="subclass_walk",
            seed_category="sandbox_escape",
            strategy=si.MutationStrategy.IDENTITY,
            mutated_source=_SUBCLASS_WALK,
        )
        res = si._evaluate_candidate(cand)
        assert res.verdict is not si.ImmunizationVerdict.ESCAPED

    async def test_mutation_provider_errors_swallowed(
        self, monkeypatch
    ):
        monkeypatch.setenv(si._ENV_MASTER, "true")

        class _BoomProvider:
            def mutate(self, seed_source, *, n):
                raise RuntimeError("provider boom")

        reports = await _drain(
            seeds=[_Seed("s", _DOTTED_CALL_SRC)],
            mutation_provider=_BoomProvider(),
        )
        assert len(reports) == 1
        assert reports[0].total_mutations >= 1  # deterministic ran

    async def test_custom_hardening_sink_receives_escapes(
        self, monkeypatch
    ):
        monkeypatch.setenv(si._ENV_MASTER, "true")
        captured = []

        class _CaptureSink:
            async def record_escape(self, result):
                captured.append(result)
                return True

        # A clean-control source PASSES_THROUGH the cage → _classify
        # maps that to ESCAPED for our measurement → sink fires.
        seed = _Seed(
            "benign", "x = 1 + 1\n", category="clean_control"
        )
        await _drain(seeds=[seed], hardening_sink=_CaptureSink())
        assert all(
            r.verdict is si.ImmunizationVerdict.ESCAPED
            for r in captured
        )

    async def test_sink_exception_does_not_abort(self, monkeypatch):
        monkeypatch.setenv(si._ENV_MASTER, "true")

        class _BoomSink:
            async def record_escape(self, result):
                raise RuntimeError("sink boom")

        seed = _Seed("benign", "y = 2\n", category="clean_control")
        reports = await _drain(
            seeds=[seed], hardening_sink=_BoomSink()
        )
        assert len(reports) == 1  # completed despite sink boom

    async def test_cancellation_cleans_up(self, monkeypatch):
        monkeypatch.setenv(si._ENV_MASTER, "true")
        seeds = [_Seed(f"s{i}", _DOTTED_CALL_SRC) for i in range(8)]
        gen = si.run_immunization_campaign(seeds=seeds)
        first = await gen.__anext__()
        assert first.seed_entry_name.startswith("s")
        await gen.aclose()  # must cancel in-flight without raising


# ===========================================================================
# Default JSONL sink
# ===========================================================================


class TestLedgerSink:
    async def test_default_sink_appends_jsonl(
        self, monkeypatch, tmp_path: Path
    ):
        ledger = tmp_path / "imm.jsonl"
        monkeypatch.setenv(si._ENV_LEDGER_PATH, str(ledger))
        sink = si._LedgerHardeningSink()
        cand = si.MutationCandidate(
            seed_entry_name="s",
            seed_category="sandbox_escape",
            strategy=si.MutationStrategy.ALIAS_REBIND,
            mutated_source="x=1",
        )
        res = si.MutationResult(
            candidate=cand,
            verdict=si.ImmunizationVerdict.ESCAPED,
            cage_verdict="passed_through",
            semguard_findings=(),
        )
        ok = await sink.record_escape(res)
        assert ok is True
        assert ledger.exists()
        content = ledger.read_text()
        assert '"kind": "escape"' in content
        assert '"verdict": "escaped"' in content


# ===========================================================================
# summarize_campaign aggregate
# ===========================================================================


class TestSummarize:
    async def test_master_off_summary(self, monkeypatch):
        monkeypatch.setenv(si._ENV_MASTER, "false")
        s = await si.summarize_campaign(
            seeds=[_Seed("s", _DOTTED_CALL_SRC)]
        )
        assert s["outcome"] == "master_off"
        assert s["meets_parity_gate"] is False

    async def test_summary_reports_parity_gate(self, monkeypatch):
        monkeypatch.setenv(si._ENV_MASTER, "true")
        s = await si.summarize_campaign(
            seeds=[_Seed("subclass_walk", _SUBCLASS_WALK)]
        )
        assert "overall_escape_rate" in s
        assert s["target_escape_rate"] == pytest.approx(0.044)
        assert isinstance(s["meets_parity_gate"], bool)
        assert "vulnerable_seeds" in s


# ===========================================================================
# Env readers
# ===========================================================================


class TestEnvReaders:
    def test_mutations_per_pattern_clamped(self, monkeypatch):
        monkeypatch.setenv(si._ENV_MUTATIONS_PER_PATTERN, "5000")
        assert (
            si._mutations_per_pattern()
            == si._MAX_MUTATIONS_PER_PATTERN
        )
        monkeypatch.setenv(si._ENV_MUTATIONS_PER_PATTERN, "0")
        assert (
            si._mutations_per_pattern()
            == si._DEFAULT_MUTATIONS_PER_PATTERN
        )
        monkeypatch.setenv(si._ENV_MUTATIONS_PER_PATTERN, "garbage")
        assert (
            si._mutations_per_pattern()
            == si._DEFAULT_MUTATIONS_PER_PATTERN
        )

    def test_target_escape_rate_clamped(self, monkeypatch):
        monkeypatch.setenv(si._ENV_TARGET_ESCAPE_RATE, "1.5")
        assert (
            si._target_escape_rate()
            == si._DEFAULT_TARGET_ESCAPE_RATE
        )
        monkeypatch.setenv(si._ENV_TARGET_ESCAPE_RATE, "-0.1")
        assert (
            si._target_escape_rate()
            == si._DEFAULT_TARGET_ESCAPE_RATE
        )
        monkeypatch.setenv(si._ENV_TARGET_ESCAPE_RATE, "0.02")
        assert si._target_escape_rate() == pytest.approx(0.02)

    def test_concurrency_clamped(self, monkeypatch):
        monkeypatch.setenv(si._ENV_CONCURRENCY, "999")
        assert si._concurrency() == 64
        monkeypatch.setenv(si._ENV_CONCURRENCY, "0")
        assert si._concurrency() == si._DEFAULT_CONCURRENCY


# ===========================================================================
# AST pins — canonical pass
# ===========================================================================


@pytest.fixture
def canonical_src_tree():
    path = Path(si.__file__)
    src = path.read_text(encoding="utf-8")
    return src, ast.parse(src)


@pytest.fixture
def pins():
    return si.register_shipped_invariants()


class TestAstPinsCanonicalPass:
    def test_6_pins_registered(self, pins):
        assert len(pins) == 6
        assert {p.invariant_name for p in pins} == {
            "self_immunization_strategy_taxonomy_closed",
            "self_immunization_verdict_taxonomy_closed",
            "self_immunization_outcome_taxonomy_closed",
            "self_immunization_authority_asymmetry",
            "self_immunization_composes_canonical_cage",
            "self_immunization_master_default_false",
        }

    def test_all_pins_pass_on_canonical_source(
        self, canonical_src_tree, pins
    ):
        src, tree = canonical_src_tree
        for pin in pins:
            violations = pin.validate(tree, src)
            assert violations == (), (
                f"{pin.invariant_name} should pass canonical: "
                f"{violations}"
            )


# ===========================================================================
# AST pins — synthetic regression (each pin fires on drift)
# ===========================================================================


def _pin(pins, name):
    return next(p for p in pins if p.invariant_name == name)


class TestAstPinsSyntheticRegression:
    def test_strategy_taxonomy_fires_on_missing(self, pins):
        synthetic = (
            "import enum\n"
            "class MutationStrategy(str, enum.Enum):\n"
            "    IDENTITY = 'identity'\n"
            "    ALIAS_REBIND = 'alias_rebind'\n"
        )
        pin = _pin(
            pins, "self_immunization_strategy_taxonomy_closed"
        )
        v = pin.validate(ast.parse(synthetic), synthetic)
        assert v and "missing" in v[0]

    def test_strategy_taxonomy_fires_on_drift(self, pins):
        synthetic = (
            "import enum\n"
            "class MutationStrategy(str, enum.Enum):\n"
            "    IDENTITY = 'identity'\n"
            "    ALIAS_REBIND = 'alias_rebind'\n"
            "    STRING_SPLIT_CONCAT = 'string_split_concat'\n"
            "    DUNDER_RECONSTRUCT = 'dunder_reconstruct'\n"
            "    GETATTR_INDIRECT = 'getattr_indirect'\n"
            "    WHITESPACE_PAD = 'whitespace_pad'\n"
            "    COMMENT_DECOY = 'comment_decoy'\n"
            "    UNICODE_CONFUSABLE = 'unicode_confusable'\n"
            "    SNEAKY_EXTRA = 'sneaky_extra'\n"
        )
        pin = _pin(
            pins, "self_immunization_strategy_taxonomy_closed"
        )
        v = pin.validate(ast.parse(synthetic), synthetic)
        assert v and "drift" in v[0]

    def test_verdict_taxonomy_fires_on_missing(self, pins):
        synthetic = (
            "import enum\n"
            "class ImmunizationVerdict(str, enum.Enum):\n"
            "    ESCAPED = 'escaped'\n"
        )
        pin = _pin(
            pins, "self_immunization_verdict_taxonomy_closed"
        )
        v = pin.validate(ast.parse(synthetic), synthetic)
        assert v and "missing" in v[0]

    def test_outcome_taxonomy_fires_on_missing(self, pins):
        synthetic = (
            "import enum\n"
            "class ImmunizationOutcome(str, enum.Enum):\n"
            "    HARDENED = 'hardened'\n"
        )
        pin = _pin(
            pins, "self_immunization_outcome_taxonomy_closed"
        )
        v = pin.validate(ast.parse(synthetic), synthetic)
        assert v and "missing" in v[0]

    def test_authority_asymmetry_fires_on_forbidden_import(
        self, pins
    ):
        synthetic = (
            "from backend.core.ouroboros.governance.orchestrator "
            "import Orchestrator\n"
        )
        pin = _pin(pins, "self_immunization_authority_asymmetry")
        v = pin.validate(ast.parse(synthetic), synthetic)
        assert v and "forbidden import" in v[0]

    def test_composes_cage_fires_when_evaluate_entry_absent(
        self, pins
    ):
        synthetic = "x = 1\n"
        pin = _pin(
            pins, "self_immunization_composes_canonical_cage"
        )
        v = pin.validate(ast.parse(synthetic), synthetic)
        assert v and "evaluate_entry" in v[0]

    def test_master_default_false_fires_on_truthy_default(
        self, pins
    ):
        synthetic = (
            "import os\n"
            "def master_enabled():\n"
            "    raw = os.environ.get('X', 'true')"
            ".strip().lower()\n"
            "    return raw in ('true',)\n"
        )
        pin = _pin(
            pins, "self_immunization_master_default_false"
        )
        v = pin.validate(ast.parse(synthetic), synthetic)
        assert v and "truthy" in v[0]

    def test_master_default_false_passes_on_empty_default(
        self, pins
    ):
        synthetic = (
            "import os\n"
            "def master_enabled():\n"
            "    raw = os.environ.get('X', '')"
            ".strip().lower()\n"
            "    return raw in ('true',)\n"
        )
        pin = _pin(
            pins, "self_immunization_master_default_false"
        )
        assert pin.validate(ast.parse(synthetic), synthetic) == ()


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


class TestFlagSeeds:
    def test_register_flags_seeds_all_five(self):
        # Slice 93 added 2 (mutation budget + corpus cache path): 5 → 7.
        # Slice 95d added 4 (batching enabled + max-calls-per-seed +
        # escape-capture enabled + escape-capture path): 7 → 11.
        from backend.core.ouroboros.governance.flag_registry import (
            FlagRegistry,
        )

        reg = FlagRegistry()
        assert si.register_flags(reg) == 11

    def test_master_flag_seeded_default_false(self):
        from backend.core.ouroboros.governance.flag_registry import (
            FlagRegistry,
        )

        reg = FlagRegistry()
        si.register_flags(reg)
        spec = reg.get_spec(si._ENV_MASTER)
        assert spec is not None
        assert spec.default is False
