"""A candidate may change what it declared, and nothing else.

The other half of the declared-symbol contract. `symbols_unchanged_in_candidate`
refuses a candidate that changed NOTHING it declared; this refuses one that
changed something it did NOT.

The case that forced it is real and is pinned below verbatim: `7f8c686ce0` made
its requested change (log before degrading in two broad `except` blocks) and,
unannounced, deleted the module's `__all__`, de-indented a docstring
continuation line, and churned quote style — while its own message said "Keep
behaviour otherwise identical". Every test passed, because nothing asserts
`__all__`.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import declared_symbols as DS


# The real file, before and after, reduced to the parts that matter.
_BEFORE = '''"""Module docstring.

    A continuation line, correctly indented.
    """
from __future__ import annotations

import json


def render_jarviskit_frame(seq, event_type, payload):
    """Render. NEVER raises."""
    try:
        body = json.dumps(payload, separators=(",", ":"))
    except Exception:
        body = "{}"
    return body


def eventstream_frame_to_jarviskit(raw_frame):
    """Convert.

    device stream passes those through untouched). NEVER raises."""
    try:
        return raw_frame
    except Exception:
        return None


__all__ = [
    "render_jarviskit_frame", "eventstream_frame_to_jarviskit",
]
'''

# What the model actually produced: the asked-for logging, plus collateral.
_AFTER_WITH_COLLATERAL = '''"""Module docstring.

    A continuation line, correctly indented.
    """
from __future__ import annotations

import json
import logging


def render_jarviskit_frame(seq, event_type, payload):
    """Render. NEVER raises."""
    try:
        body = json.dumps(payload, separators=(',', ':'))
    except Exception:
        logging.exception("Failed to serialize payload")
        body = "{}"
    return body


def eventstream_frame_to_jarviskit(raw_frame):
    """Convert.

device stream passes those through untouched). NEVER raises."""
    try:
        return raw_frame
    except Exception:
        logging.exception("Failed to process EventStream frame")
        return None
'''

# The same change, kept inside its declared scope.
_AFTER_SURGICAL = '''"""Module docstring.

    A continuation line, correctly indented.
    """
from __future__ import annotations

import json
import logging


def render_jarviskit_frame(seq, event_type, payload):
    """Render. NEVER raises."""
    try:
        body = json.dumps(payload, separators=(",", ":"))
    except Exception:
        logging.exception("Failed to serialize payload")
        body = "{}"
    return body


def eventstream_frame_to_jarviskit(raw_frame):
    """Convert.

    device stream passes those through untouched). NEVER raises."""
    try:
        return raw_frame
    except Exception:
        return None


__all__ = [
    "render_jarviskit_frame", "eventstream_frame_to_jarviskit",
]
'''


def _candidate(content: str) -> dict:
    """The shape the orchestrator actually hands the contract.

    Deliberately built through the same accessor the production path uses
    (`_candidate_contents`): an invented shape here would make every test in
    this file pass against a validator that never sees a candidate at all —
    which is exactly what happened on the first run.
    """
    return {"files": [{"full_content": content}]}


@pytest.fixture(autouse=True)
def _contract_on(monkeypatch):
    monkeypatch.setenv("JARVIS_DECLARED_SYMBOL_CONTRACT_ENABLED", "true")
    yield


# --------------------------------------------------------------------------
# The defect, pinned
# --------------------------------------------------------------------------

def test_the_real_collateral_damage_is_caught():
    """THE regression: a green test suite and a deleted public surface."""
    violations = DS.out_of_scope_changes(
        ["render_jarviskit_frame"],
        _candidate(_AFTER_WITH_COLLATERAL),
        _BEFORE,
    )
    assert violations, "the whole-file re-emission passed the scope check"
    joined = " ".join(violations)
    assert "<module scope>" in joined, f"the deleted __all__ was missed: {violations}"
    assert "eventstream_frame_to_jarviskit (modified)" in joined, (
        f"the mangled docstring in an undeclared symbol was missed: {violations}"
    )


def test_the_same_change_kept_in_scope_passes():
    """The asked-for change is not what was wrong with it."""
    assert DS.out_of_scope_changes(
        ["render_jarviskit_frame"], _candidate(_AFTER_SURGICAL), _BEFORE,
    ) == ()


def test_an_import_the_change_requires_is_allowed():
    """A line-level 'nothing outside the target range' rule would reject the
    good half of the work: this change genuinely needed `import logging` at
    module level."""
    assert "import" not in " ".join(DS.out_of_scope_changes(
        ["render_jarviskit_frame"], _candidate(_AFTER_SURGICAL), _BEFORE,
    ))


def test_quote_churn_alone_is_not_a_violation():
    """Cosmetic and unobservable. Flagging it would train the lane to fear
    re-emitting a file correctly."""
    after = _AFTER_SURGICAL.replace(
        'separators=(",", ":")', "separators=(',', ':')",
    )
    assert DS.out_of_scope_changes(
        ["render_jarviskit_frame"], _candidate(after), _BEFORE,
    ) == ()


# --------------------------------------------------------------------------
# What must stay allowed
# --------------------------------------------------------------------------

def test_adding_a_symbol_is_allowed():
    """A change routinely needs a new helper, and test synthesis is nothing
    but addition."""
    after = _AFTER_SURGICAL + "\n\ndef _new_helper():\n    return 1\n"
    assert DS.out_of_scope_changes(
        ["render_jarviskit_frame"], _candidate(after), _BEFORE,
    ) == ()


def test_a_declared_symbol_may_change_freely():
    after = _AFTER_SURGICAL.replace(
        'logging.exception("Failed to serialize payload")',
        'logging.exception("totally different text")',
    )
    assert DS.out_of_scope_changes(
        ["render_jarviskit_frame"], _candidate(after), _BEFORE,
    ) == ()


def test_a_new_file_has_no_scope_to_exceed():
    assert DS.out_of_scope_changes(["x"], _candidate("def x():\n    pass\n"), None) == ()
    assert DS.out_of_scope_changes(["x"], _candidate("def x():\n    pass\n"), "") == ()


def test_no_declaration_means_no_scope_check():
    """The contract's floor is elsewhere: an op with no declared symbols is
    refused before it reaches here."""
    assert DS.out_of_scope_changes(
        [], _candidate(_AFTER_WITH_COLLATERAL), _BEFORE,
    ) == ()


def test_the_contract_flag_still_gates_it(monkeypatch):
    monkeypatch.setenv("JARVIS_DECLARED_SYMBOL_CONTRACT_ENABLED", "false")
    assert DS.out_of_scope_changes(
        ["render_jarviskit_frame"], _candidate(_AFTER_WITH_COLLATERAL), _BEFORE,
    ) == ()


# --------------------------------------------------------------------------
# Failure direction
# --------------------------------------------------------------------------

def test_unparsable_candidate_is_not_a_scope_violation():
    """Syntax is someone else's gate; reporting it here would misattribute."""
    assert DS.out_of_scope_changes(
        ["render_jarviskit_frame"], _candidate("def ("), _BEFORE,
    ) == ()


def test_never_raises_on_junk():
    for bad in (None, {}, {"file_contents": None}, {"file_contents": {"a": None}}):
        assert DS.out_of_scope_changes(["x"], bad or {}, _BEFORE) == ()


def test_enforcement_is_off_until_armed(monkeypatch):
    monkeypatch.delenv("JARVIS_SURGICAL_SCOPE_ENFORCE", raising=False)
    assert DS.scope_enforced() is False
    monkeypatch.setenv("JARVIS_SURGICAL_SCOPE_ENFORCE", "true")
    assert DS.scope_enforced() is True


def test_the_feedback_names_the_scope_and_the_breach():
    msg = DS.scope_feedback(
        ("<module scope> (module-level statements changed)",),
        ["render_jarviskit_frame"],
    )
    assert "render_jarviskit_frame" in msg
    assert "__all__" in msg
    assert "import" in msg, "the retry instruction must not forbid a needed import"


def test_the_parsers_are_not_duplicated():
    """One AST reader for both halves of the contract, or the two disagree."""
    import inspect

    src = inspect.getsource(DS.out_of_scope_changes)
    assert "_node_dump" in src and "defined_names" in src
