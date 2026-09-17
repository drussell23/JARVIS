"""Capabilities must be redeemed, and a rejected hunk gets one repair.

Thirteen defects in one session shared one shape: a capability built, tested,
armed — and nothing consulted it. Every one passed its unit tests, because the
defect was in the seam and seams have no owner. Attestation gives them one.

And the measured model constraint: 43-56% of the 30B's diffs do not apply, 26
of 28 rejections being mid-hunk "context diverges after N matching line(s)" —
the change is right, its packaging drifted.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import capability_assurance as CA
from backend.core.ouroboros.governance import diff_healer as DH
from backend.core.ouroboros.governance.providers import _apply_unified_diff


@pytest.fixture(autouse=True)
def _clean():
    CA.clear_attestations_for_tests()
    yield
    CA.clear_attestations_for_tests()


# --------------------------------------------------------------------------
# Phase 1 — attestation
# --------------------------------------------------------------------------

def test_a_declared_capability_that_is_never_used_is_caught():
    """THE generalisation: the diff schema was armed, capability-resolved and
    requested 14/14 while the prompt asked for whole content."""
    CA.declare_capability("op1", "diff_schema", "lean builder emitted 2b.1-diff")
    v = CA.attest_execution("op1")
    assert v.ok is False
    assert "CapabilityAttestationDrift" in v.reason
    assert "diff_schema" in v.reason


def test_redeeming_clears_it():
    CA.declare_capability("op1", "diff_schema")
    CA.redeem_capability("op1", "diff_schema", "unified_diff returned")
    assert CA.attest_execution("op1").ok is True


def test_an_op_that_declared_nothing_passes():
    assert CA.attest_execution("never-seen").ok is True


def test_revocation_is_a_legitimate_exit_not_a_failure():
    """A file-deletion goal has no AST to scope; a multi-file op cannot emit a
    single-file diff. Without this, attestation deadlocks honest work."""
    CA.declare_capability("op2", "scope_validator")
    CA.revoke_capability("op2", "scope_validator", "deletion goal: no AST to scope")
    assert CA.attest_execution("op2").ok is True
    chain = CA.attestation_snapshot("op2")
    assert chain["scope_validator"]["state"] == "revoked"
    assert "deletion goal" in chain["scope_validator"]["reason"]


def test_the_revocation_reason_is_preserved(caplog):
    """An unbroken chain is what separates a legitimate branch from a
    capability quietly going missing."""
    with caplog.at_level("INFO"):
        CA.declare_capability("op3", "diff_schema")
        CA.revoke_capability("op3", "diff_schema", "multi-file op")
    assert any("CapabilityRevocationEvent" in r.getMessage() for r in caplog.records)


def test_enforcement_is_off_by_default():
    """A new fatal path spanning every capability is exactly the change that
    should not be armed before a soak shows what it would refuse."""
    CA.declare_capability("op4", "diff_schema")
    v = CA.attest_execution("op4")
    assert v.severity == CA.RECOVERABLE
    assert v.is_fatal is False


def test_enforcement_when_explicitly_armed():
    CA.declare_capability("op5", "diff_schema")
    v = CA.attest_execution("op5", enforced=True)
    assert v.severity == CA.FATAL
    assert v.is_fatal is True


def test_use_without_declaration_is_recorded_too():
    """The same seam defect seen from the other end: the USE is wired and the
    declaration is not."""
    CA.redeem_capability("op6", "diff_schema")
    assert CA.attestation_snapshot("op6")["diff_schema"]["state"] == "undeclared_use"


def test_nothing_here_is_cryptographic():
    """The threat is a forgotten wire, not an adversary. Signing internal
    process state would be ceremony with a key to manage."""
    import inspect

    src = inspect.getsource(CA.declare_capability)
    src += inspect.getsource(CA.revoke_capability)
    for suspect in ("hmac", "sha256", "signature", "secret"):
        assert suspect not in src.lower()


def test_attestation_never_raises():
    for bad in (None, "", 0):
        CA.declare_capability(bad, "x")
        CA.redeem_capability(bad, "x")
        CA.revoke_capability(bad, "x", "y")
        assert CA.attest_execution(bad) is not None


# --------------------------------------------------------------------------
# Phase 2 — the healer
# --------------------------------------------------------------------------

_SOURCE = '''import json


def build(port, hostname):
    return {
        "port": port,
        "hostname": hostname,
    }
'''

# The model's hunk: right change, wrong context (indentation dropped) — the
# exact shape of 26 of 28 observed rejections.
_BAD = (
    "@@ -5,3 +5,4 @@\n"
    '"port": port,\n'
    '+        "scheme": scheme,\n'
    '"hostname": hostname,\n'
)
_GOOD = (
    "@@ -5,3 +5,4 @@\n"
    '         "port": port,\n'
    '+        "scheme": scheme,\n'
    '         "hostname": hostname,\n'
)


def test_a_heal_recovers_the_change():
    async def _ask(prompt, deadline):
        assert "REJECTION:" in prompt
        assert "THE DIFF YOU PRODUCED" in prompt
        return "```diff\n" + _GOOD + "```"

    out = asyncio.run(DH.heal_rejection(
        DH.HealableRejection("m.py", _BAD, "context diverges after 1", "c1"),
        _SOURCE, ask=_ask, apply_fn=_apply_unified_diff, radius=12,
    ))
    assert out is not None
    assert '"scheme": scheme,' in out


def test_a_heal_that_alters_the_CHANGE_is_discarded():
    """A healer that may edit +/- lines is not a healer, it is a second
    generator with none of the first one's governance."""
    async def _ask(prompt, deadline):
        return _GOOD.replace('"scheme": scheme,', '"evil": True,')

    out = asyncio.run(DH.heal_rejection(
        DH.HealableRejection("m.py", _BAD, "context diverges", "c1"),
        _SOURCE, ask=_ask, apply_fn=_apply_unified_diff, radius=12,
    ))
    assert out is None


def test_intent_is_compared_on_the_change_lines_only():
    assert DH.intent_preserved(_BAD, _GOOD) is True
    assert DH.intent_preserved(_BAD, _GOOD.replace("scheme", "other")) is False


def test_a_reply_with_no_diff_heals_nothing():
    async def _ask(prompt, deadline):
        return "I could not determine the correct context."

    out = asyncio.run(DH.heal_rejection(
        DH.HealableRejection("m.py", _BAD, "x", "c1"),
        _SOURCE, ask=_ask, apply_fn=_apply_unified_diff, radius=12,
    ))
    assert out is None


def test_a_model_error_is_just_no_heal():
    async def _ask(prompt, deadline):
        raise RuntimeError("local lane down")

    out = asyncio.run(DH.heal_rejection(
        DH.HealableRejection("m.py", _BAD, "x", "c1"),
        _SOURCE, ask=_ask, apply_fn=_apply_unified_diff, radius=12,
    ))
    assert out is None


def test_the_prompt_shows_the_file_as_it_actually_reads():
    """A numbered region is the difference between 'copy these lines' and
    'remember this file' — the latter being what it gets wrong."""
    p = DH.build_alignment_prompt(
        DH.HealableRejection("m.py", _BAD, "file line 6 is X", "c1"),
        _SOURCE, radius=12,
    )
    assert '"hostname": hostname,' in p
    assert "VERBATIM" in p


def test_fences_and_bare_replies_both_parse():
    assert DH.extract_healed_diff("```diff\n" + _GOOD + "```").startswith("@@")
    assert DH.extract_healed_diff(_GOOD).startswith("@@")
    assert DH.extract_healed_diff("no diff here") == ""


def test_the_healer_can_be_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_DIFF_HEALER_ENABLED", "false")

    async def _ask(prompt, deadline):
        raise AssertionError("must not be called when disabled")

    out = asyncio.run(DH.heal_rejection(
        DH.HealableRejection("m.py", _BAD, "x", "c1"),
        _SOURCE, ask=_ask, apply_fn=_apply_unified_diff, radius=12,
    ))
    assert out is None
