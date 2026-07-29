"""A reason nobody gave must never be quoted as something someone said.

The Iron Gate returned a bool, and the decision parser took the first token
and discarded the line. So a rejection arrived downstream with no reason, and
three call sites supplied one:

    serpent_flow.py           "rejected via Iron Gate"
    serpent_flow.py           "plan rejected via Plan Gate"
    inline_approval_renderer  "inline reject"     (+ a TODO for "Slice 4")

Those strings did not stay in a log. Each became a FEEDBACK memory whose own
content reads "It encodes the user's stated reason for blocking the operation
and should be treated as a binding constraint", rendered into every later
generation prompt under "They represent the user's explicit preferences ...
Honour them without asking".

So the organism was told to obey, as the operator's explicit wish, a sentence
the codebase had written about itself. Confident and content-free at once —
the same defect class as a synthetic blast radius presented as measured.
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from backend.core.ouroboros.governance.inline_approval import (
    InlineApprovalChoice,
    OperatorDecision,
    ReasonProvenance,
    normalize_reason,
    parse_decision,
    parse_gate_answer,
    synthetic_decision,
)
from backend.core.ouroboros.governance.user_preference_memory import (
    MemoryType,
    UserPreferenceStore,
)


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as td:
        yield UserPreferenceStore(pathlib.Path(td))


def _reject(store, **kw):
    base = dict(op_id="op-1", description="loosen the risk floor",
                target_files=["gate.py"], reason="a reason")
    base.update(kw)
    return store.record_approval_rejection(**base)


class TestOnlyWhatAHumanSaid:
    def test_a_code_constant_never_becomes_a_constraint(self, store):
        """THE regression, in the exact shape it shipped in."""
        for constant in ("rejected via Iron Gate",
                         "plan rejected via Plan Gate", "inline reject"):
            assert _reject(store, reason=constant,
                           provenance="unstated") is None
        assert store.find_by_type(MemoryType.FEEDBACK) == []

    def test_the_operators_own_words_DO(self, store):
        mem = _reject(store, reason="it widens the permission gate",
                      provenance="stated")
        assert mem is not None
        assert mem.why == "it widens the permission gate"

    def test_no_human_present_is_never_a_preference(self, store):
        assert _reject(store, reason="headless: no tty",
                       provenance="synthetic") is None

    def test_an_UNDECLARED_caller_is_refused(self, store):
        """Strict on purpose. A caller that has not thought about
        provenance has not witnessed a human speak, and defaulting to the
        attributable value is exactly how this bug happened. A lost memory
        is recoverable; a fabricated constraint injected into every prompt
        is not."""
        assert _reject(store) is None

    def test_it_gates_on_PROVENANCE_not_on_recognising_strings(self, store):
        """A deny-list of the three literals would rot on the fourth, and
        cannot distinguish "rejected via Iron Gate" (a constant) from
        "rejected because the Iron Gate is right" (a sentence)."""
        assert _reject(store, reason="rejected via Iron Gate",
                       provenance="stated") is not None
        assert _reject(store, op_id="op-2", description="d2",
                       reason="this reads exactly like a person wrote it",
                       provenance="unstated") is None


class TestTheWordsSurviveTheParser:
    @pytest.mark.parametrize(("typed", "choice", "reason"), [
        ("n", "REJECT", ""),
        ("n it widens the permission gate", "REJECT",
         "it widens the permission gate"),
        ("no, because the fixture asserts the bug", "REJECT",
         "the fixture asserts the bug"),
        ("reject since this touches the audit file", "REJECT",
         "this touches the audit file"),
        ("y", "APPROVE", ""),
    ])
    def test_the_line_is_not_reduced_to_a_verb(self, typed, choice, reason):
        d = parse_decision(typed)
        assert d.choice.name == choice
        assert d.reason == reason
        assert d.is_stated is bool(reason)

    def test_a_comma_no_longer_costs_the_rejection(self):
        """`no, because ...` scored WAIT purely on the comma, silently
        turning an explicit rejection into a deferral."""
        assert parse_decision("no, because it is wrong").choice is (
            InlineApprovalChoice.REJECT)

    def test_a_legitimate_wait_keeps_its_words(self):
        """`w` and `defer` MEAN wait. Asking "does this map to WAIT"
        instead of "is this a verb" made them indistinguishable from
        garbage, and dropped the reason."""
        d = parse_decision("w defer to the PR path")
        assert d.choice is InlineApprovalChoice.WAIT
        assert d.reason == "defer to the PR path"

    def test_garbage_attaches_its_words_to_NOTHING(self):
        """Binding a reason to a choice the operator did not make is how a
        typo becomes a stored preference."""
        d = parse_decision("wat this is not a verb")
        assert d.choice is InlineApprovalChoice.WAIT
        assert d.reason == ""

    def test_an_accidental_paste_is_not_a_stated_reason(self):
        """`y\\nrm -rf /` is the shape the classifier was hardened against.
        Quoting the second line as the operator's reason would be the same
        fabrication wearing a different costume."""
        assert parse_decision("y\nrm -rf /").reason == ""
        assert parse_decision("n the real reason\nand a paste").reason == (
            "the real reason")

    def test_punctuation_is_not_an_explanation(self):
        for junk in ("n ...", "n !!!", "n ---"):
            assert parse_decision(junk).is_stated is False

    def test_a_reason_is_bounded(self):
        assert len(normalize_reason("word " * 5000)) <= 400


class TestTheDefaultBelongsToThePrompt:
    def test_enter_means_what_the_PROMPT_promised(self):
        """`[Y/n]` promises Enter = approve; the inline prompt's
        `[y]es/[n]o/.../[w]ait` promises Enter = wait. Baking either into
        the parser silently breaks the other surface."""
        assert parse_gate_answer(
            "", empty_means=InlineApprovalChoice.APPROVE
        ).choice is InlineApprovalChoice.APPROVE
        assert parse_gate_answer(
            "", empty_means=InlineApprovalChoice.WAIT
        ).choice is InlineApprovalChoice.WAIT

    def test_accepting_a_default_explains_nothing(self):
        for default in (InlineApprovalChoice.APPROVE, InlineApprovalChoice.WAIT):
            assert parse_gate_answer("", empty_means=default).is_stated is False


class TestTheGateItself:
    def test_the_iron_gate_relays_rather_than_invents(self):
        from backend.core.ouroboros.battle_test.serpent_flow import (
            _gate_decision, _reject_args,
        )

        class _Flow:
            def __init__(self, d): self._last_gate_decision = d

        spoke = _Flow(_gate_decision("n it widens the permission gate"))
        assert _reject_args(spoke, "rejected via Iron Gate") == (
            "it widens the permission gate", "stated")

        silent = _Flow(_gate_decision("n"))
        # The fallback still travels — the audit log deserves to say WHICH
        # gate refused. What changed is that it cannot be attributed.
        assert _reject_args(silent, "rejected via Iron Gate") == (
            "rejected via Iron Gate", "unstated")

    def test_ctrl_d_is_a_rejection_and_not_an_explanation(self):
        from backend.core.ouroboros.battle_test.serpent_flow import (
            _reject_args, _wordless_reject,
        )

        class _Flow:
            _last_gate_decision = _wordless_reject()

        assert _reject_args(_Flow(), "rejected via Iron Gate")[1] == "unstated"

    def test_headless_auto_approve_is_synthetic(self):
        from backend.core.ouroboros.battle_test.serpent_flow import (
            _reject_args, _synthetic_gate_decision,
        )

        class _Flow:
            _last_gate_decision = _synthetic_gate_decision(
                approved=True, detail="headless: no tty")

        assert _reject_args(_Flow(), "x")[1] == "synthetic"

    def test_a_literal_reason_must_declare_its_provenance(self):
        """Structural, so the class cannot come back through a fourth call
        site. Any `reject(..., "<literal>")` is by definition a reason no
        human typed, and must say so.

        Checked as an AST rather than by grep: a regex over the source
        both false-positives on the correct `*_reject_args(...)` spread
        and cannot see a keyword argument on the following line."""
        import ast
        offenders = []
        for path in pathlib.Path("backend/core/ouroboros").rglob("*.py"):
            try:
                tree = ast.parse(path.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if getattr(node.func, "attr", None) != "reject":
                    continue
                if len(node.args) < 3:
                    continue
                reason = node.args[2]
                if not (isinstance(reason, ast.Constant)
                        and isinstance(reason.value, str)):
                    continue          # computed / relayed — fine
                declared = (len(node.args) >= 4
                            or any(k.arg == "provenance" for k in node.keywords))
                if not declared:
                    offenders.append(
                        f"{path}:{node.lineno} reject(..., {reason.value!r})")
        assert not offenders, (
            "a hardcoded rejection reason with no provenance:\n"
            + "\n".join(offenders))


class TestNeverRaises:
    @pytest.mark.parametrize("call", [
        lambda: parse_decision(None),                  # type: ignore[arg-type]
        lambda: parse_decision(12345),                 # type: ignore[arg-type]
        lambda: normalize_reason(object()),
        lambda: parse_gate_answer(None, empty_means=InlineApprovalChoice.WAIT),
        lambda: synthetic_decision(InlineApprovalChoice.WAIT, None),
    ])
    def test_junk_degrades(self, call):
        assert call() is not None

    def test_with_reason_refuses_to_launder_emptiness(self):
        """Skipping the follow-up prompt IS the answer, and must not be
        upgraded to a stated reason."""
        d = OperatorDecision(choice=InlineApprovalChoice.REJECT)
        assert d.with_reason("   ").provenance is ReasonProvenance.UNSTATED
        assert d.with_reason("it is wrong").is_stated is True
