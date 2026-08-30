"""Operator-declared repair targets: signed intent outranks inference.

`resolve_target_symbols` had two ways to find a repair target — a stack trace
(evidence) and goal-text keyword scoring (a guess) — and no way to be TOLD one.
So a roadmap goal could DECLARE its target and still be resolved by keyword
matching, which is what happened in soak bt-2026-08-28-115654: a goal naming
`_should_use_lean_prompt` resolved to `_read_with_truncation` at confidence
0.50, because the only signal reaching the resolver was prose and the prose
said "truncation" more often than it said the symbol's name.

The declaration was not missing. `_build_signing_payload` signs the whole
`goals` list, so `target_symbol` was covered by the HMAC — and then dropped by
`RoadmapGoal`, which had no such field. Signed intent was being discarded one
layer below the signature that protected it.

Consequence, on the goal that motivated this: the op was scoped to
providers.py entire (10,923 lines, ~126,600 tokens) against a 32,768-token
local model — a 3.9x overrun that makes truncation arithmetic, not luck. The
declared symbol is 75 lines, ~750 tokens: a 169x reduction.
"""
from __future__ import annotations

import textwrap

import pytest

from backend.core.ouroboros.governance.target_symbol_resolver import (
    METHOD_DECLARED,
    METHOD_GOAL_KEYWORD,
    METHOD_STACK_TRACE,
    resolve_target_symbols,
)

# Deliberately keyword-hostile: the prose is dense with words matching the
# WRONG symbol, so a passing result cannot be keyword scoring getting lucky.
_SOURCE = textwrap.dedent('''
    def _read_with_truncation(path):
        """Truncation truncation truncation."""
        return path

    def _should_use_lean_prompt(ctx):
        """The symbol the operator actually declared."""
        return bool(ctx)

    class Helper:
        def truncate_helper(self):
            return 1
''')

_HOSTILE_GOAL = "fix the truncation truncation truncation behaviour"


def test_declared_symbol_beats_keyword_inference():
    """The regression: a declaration must not lose to prose."""
    inferred = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py", goal=_HOSTILE_GOAL,
    )
    declared = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py", goal=_HOSTILE_GOAL,
        declared_symbols=("_should_use_lean_prompt",),
    )

    assert declared.method == METHOD_DECLARED
    assert "_should_use_lean_prompt" in declared.primary
    # And it genuinely changed the outcome — otherwise this test proves nothing.
    assert inferred.primary != declared.primary


def test_declared_symbol_is_confidence_one():
    """A declaration is a fact, like a stack trace — not a score.

    It must not be filterable by a confidence floor an operator raised to
    suppress weak keyword guesses.
    """
    res = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py", goal=_HOSTILE_GOAL,
        declared_symbols=("_should_use_lean_prompt",),
        min_confidence=0.99,
    )

    assert res.confidence == 1.0
    assert res.method == METHOD_DECLARED


def test_a_declaration_naming_nothing_here_degrades_to_inference():
    """A typo must not fabricate a target.

    Matching is against the file's OWN symbol index, so a name absent from
    this file resolves nothing and the inference passes still run — the
    failure mode of a wrong declaration is "no worse than before", never
    "confidently wrong".
    """
    res = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py", goal=_HOSTILE_GOAL,
        declared_symbols=("_no_such_symbol_anywhere",),
    )

    assert res.method != METHOD_DECLARED
    assert "_no_such_symbol_anywhere" not in res.primary


def test_declaration_does_not_absorb_unrelated_stack_frames():
    """A declared target must not be widened by frames it did not ask for.

    `if primaries:` after the stack-trace pass would otherwise relabel a
    DECLARED result as STACK_TRACE with zero frames parsed — a resolution
    reporting a provenance it does not have.
    """
    frames = ['File "providers.py", line 2, in _read_with_truncation']
    res = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py", goal=_HOSTILE_GOAL,
        traceback_frames=frames,
        declared_symbols=("_should_use_lean_prompt",),
    )

    assert res.method == METHOD_DECLARED
    assert "_read_with_truncation" not in res.primary


def test_no_declaration_leaves_the_cascade_untouched():
    """Every op without a roadmap claim — nearly all of them — is unchanged."""
    res = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py",
        traceback_frames=['File "providers.py", line 2, in _read_with_truncation'],
    )

    assert res.method == METHOD_STACK_TRACE
    assert res.confidence == 1.0


@pytest.mark.parametrize("declared", [(), None, ("",), ("   ",)])
def test_empty_declarations_are_inert(declared):
    """Blank/absent declarations must not short-circuit inference."""
    res = resolve_target_symbols(
        source=_SOURCE, file_path="providers.py", goal="lean prompt",
        declared_symbols=declared,
    )

    assert res.method in (METHOD_GOAL_KEYWORD, "unresolved")


# ---------------------------------------------------------------------------
# Reader: the field must survive parsing, in either spelling
# ---------------------------------------------------------------------------


def _goal_from(entry: dict):
    from backend.core.ouroboros.governance.roadmap_reader import (
        _parse_goal_entry,
    )
    return _parse_goal_entry(entry)


@pytest.mark.parametrize("key", ["target_symbol", "target_symbols"])
def test_reader_accepts_both_spellings(key):
    """An operator writing ONE symbol should not have to know the field is
    plural. A schema that punishes the singular is one people get wrong
    silently — and silence is what this whole defect was made of."""
    goal = _goal_from({
        "id": "g1", "title": "t", "description": "d",
        "target_files": ["backend/x.py"],
        key: "_should_use_lean_prompt",
    })

    assert goal is not None
    assert goal.target_symbols == ("_should_use_lean_prompt",)


def test_reader_defaults_to_empty_for_older_roadmaps():
    """A roadmap written before this field parses and behaves as before."""
    goal = _goal_from({
        "id": "g1", "title": "t", "description": "d",
        "target_files": ["backend/x.py"],
    })

    assert goal is not None
    assert goal.target_symbols == ()


# ---------------------------------------------------------------------------
# The TRUST BOUNDARY itself.
#
# `_declared_symbols_for` is what turns a signed roadmap into a confidence-1.0
# instruction, and it shipped with NO test of its own: the suite above proves
# the resolver honours a declaration, and the reader parses one, but nothing
# proved WHERE a declaration is allowed to come from. That gap is how the
# function came to gate on `doc is None` alone.
#
# `_verified_roadmap()` RETURNS THE DOCUMENT REGARDLESS OF VERDICT — it reports
# verification, it does not enforce it. So `doc is not None` says only "a file
# was parsed", never "an operator signed it". These tests pin BOTH properties,
# the same pair `delegated_provenance` demands of the same call.
# ---------------------------------------------------------------------------


class _Verdict:
    def __init__(self, value):
        self.value = value


class _Goal:
    goal_id = "g1"
    target_files = ("backend/core/ouroboros/governance/providers.py",)
    target_symbols = ("_should_use_lean_prompt",)


class _Doc:
    goals = (_Goal(),)

    def __init__(self, signature_valid):
        self.signature_valid = signature_valid


class _Ctx:
    evidence = {"provenance": {"goal_id": "g1"}}


_PATH = "backend/core/ouroboros/governance/providers.py"


def _declared_with(monkeypatch, verdict_value, signature_valid):
    from backend.core.ouroboros.governance import delegated_provenance
    from backend.core.ouroboros.governance.candidate_generator import (
        _declared_symbols_for,
    )
    monkeypatch.setattr(
        delegated_provenance, "_verified_roadmap",
        lambda: (_Verdict(verdict_value), _Doc(signature_valid)),
    )
    return _declared_symbols_for(_Ctx(), _PATH)


def test_a_validly_signed_roadmap_confers_the_declared_target(monkeypatch):
    """The positive control. Without this the refusals below could pass for
    the trivial reason that the function never returns anything at all."""
    assert _declared_with(monkeypatch, "valid", True) == (
        "_should_use_lean_prompt",
    )


@pytest.mark.parametrize(
    "verdict_value",
    ["invalid_signature", "tampered", "missing", "invalid_format", "expired"],
)
def test_an_unverified_roadmap_confers_nothing(monkeypatch, verdict_value):
    """Every non-valid verdict must refuse. A tampered or unsigned roadmap
    that still named a target would hand an attacker who can write
    `.jarvis/roadmap.yaml` a confidence-1.0 instruction — the exact forgery
    this function's pointer-only contract exists to prevent, and a silent
    defeat of the operator's JARVIS_ROADMAP_READER_REQUIRE_SIGNATURE."""
    assert _declared_with(monkeypatch, verdict_value, True) == ()


def test_the_cryptographic_fact_is_demanded_not_just_the_verdict(monkeypatch):
    """The reader permits an unsigned dev-mode (REQUIRE_SIGNATURE=false) that
    can report `valid` for a document carrying no signature at all. The
    verdict alone is therefore not sufficient — `delegated_provenance` demands
    both of this same call, and so must this."""
    assert _declared_with(monkeypatch, "valid", False) == ()


def test_a_declaration_cannot_reach_a_file_its_goal_does_not_cover(monkeypatch):
    """Scope is enforced twice over. Even validly signed, a goal may only
    direct the files inside its own `target_files`."""
    from backend.core.ouroboros.governance import delegated_provenance
    from backend.core.ouroboros.governance.candidate_generator import (
        _declared_symbols_for,
    )
    monkeypatch.setattr(
        delegated_provenance, "_verified_roadmap",
        lambda: (_Verdict("valid"), _Doc(True)),
    )
    assert _declared_symbols_for(_Ctx(), "backend/core/unrelated.py") == ()


def test_an_unavailable_roadmap_degrades_to_inference(monkeypatch):
    """No roadmap is not an error — it is the ordinary case for almost every
    op. It must return empty, never raise."""
    from backend.core.ouroboros.governance import delegated_provenance
    from backend.core.ouroboros.governance.candidate_generator import (
        _declared_symbols_for,
    )
    monkeypatch.setattr(
        delegated_provenance, "_verified_roadmap", lambda: (None, None),
    )
    assert _declared_symbols_for(_Ctx(), _PATH) == ()


def test_an_op_with_no_provenance_pointer_declares_nothing(monkeypatch):
    """The op contributes only a POINTER. An op without one gets inference,
    and a context cannot smuggle a symbol list in by another route."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _declared_symbols_for,
    )

    class _Bare:
        evidence = {}

    class _Forged:
        # A fabricated field naming a target directly, with no goal_id.
        evidence = {"target_symbols": ["_should_use_lean_prompt"]}

    assert _declared_symbols_for(_Bare(), _PATH) == ()
    assert _declared_symbols_for(_Forged(), _PATH) == ()

# ---------------------------------------------------------------------------
# The CARRIER.
#
# Every test above hands `_declared_symbols_for` an object with a `.evidence`
# dict. No dispatch path produces one. `roadmap_reader` attaches the claim to
# the ENVELOPE and the dispatch path snapshots it onto
# `ctx.intake_evidence_json` -- a JSON STRING -- so the reader always saw
# nothing in production and silently degraded to inference.
#
# Measured, soak bt-2026-08-30-145744: the signed goal reached the resolver
# and came back `goal_keyword (conf=1.00)`. The right symbol, found by scoring
# prose, while the declaration naming it exactly sat unread one attribute
# away. These tests fix the suite's own blind spot: they drive the shape
# production actually builds.
# ---------------------------------------------------------------------------
import json as _json

from backend.core.ouroboros.governance.candidate_generator import (
    _provenance_claim_from_ctx,
)

_CLAIM = {
    "schema_version": "delegated_provenance.v1",
    "kind": "roadmap_reader",
    "goal_id": "g1",
}


class _ProdCtx:
    """What the dispatch path actually hands the generator."""
    def __init__(self, claim=_CLAIM):
        self.intake_evidence_json = _json.dumps({"provenance": claim})


class _EnvelopeCtx:
    """Envelope-shaped: a live evidence mapping, no JSON snapshot."""
    def __init__(self, claim=_CLAIM):
        self.evidence = {"provenance": claim}


def test_the_production_json_carrier_is_read():
    """The shape that ships. This is the one that was broken."""
    assert _provenance_claim_from_ctx(_ProdCtx()) == _CLAIM


def test_the_envelope_mapping_still_works():
    """The fallback must keep serving callers holding an envelope."""
    assert _provenance_claim_from_ctx(_EnvelopeCtx()) == _CLAIM


def test_declared_symbols_resolve_from_the_production_shape(monkeypatch):
    """End to end on the real carrier: a context with ONLY
    `intake_evidence_json` must yield the operator's declared symbol."""
    from backend.core.ouroboros.governance import delegated_provenance
    from backend.core.ouroboros.governance.candidate_generator import (
        _declared_symbols_for,
    )

    class _V:
        value = "valid"

    class _G:
        goal_id = "g1"
        target_files = ("backend/x.py",)
        target_symbols = ("_should_use_lean_prompt",)

    class _D:
        signature_valid = True
        goals = (_G(),)

    monkeypatch.setattr(
        delegated_provenance, "_verified_roadmap", lambda: (_V(), _D())
    )
    assert _declared_symbols_for(_ProdCtx(), "backend/x.py") == (
        "_should_use_lean_prompt",
    )


@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"provenance": 7}'])
def test_a_malformed_carrier_never_raises(raw):
    """A truncated or wrong-typed snapshot degrades to inference, never a
    crash -- the broad except in the caller would have hidden a raise here."""
    class _Bad:
        intake_evidence_json = raw

    assert _provenance_claim_from_ctx(_Bad()) is None


def test_a_context_with_neither_carrier_yields_nothing():
    """The ordinary case for almost every op."""
    class _Bare:
        pass

    assert _provenance_claim_from_ctx(_Bare()) is None


def test_the_json_carrier_takes_precedence():
    """When both exist the production snapshot wins -- it is the one the
    dispatch path authored for THIS op."""
    class _Both:
        intake_evidence_json = _json.dumps(
            {"provenance": {"kind": "roadmap_reader", "goal_id": "from-json"}}
        )
        evidence = {"provenance": {"kind": "roadmap_reader",
                                   "goal_id": "from-mapping"}}

    assert _provenance_claim_from_ctx(_Both())["goal_id"] == "from-json"


def test_the_reader_uses_the_same_attribute_the_swarm_path_does():
    """DRY pin: one parsing idiom for one field. If a future edit invents a
    second way to reach intake evidence, this fails."""
    import io as _io

    import backend.core.ouroboros.governance.candidate_generator as M

    src = _io.open(M.__file__, encoding="utf-8").read()
    assert src.count('getattr(context, "intake_evidence_json", "")') >= 2
