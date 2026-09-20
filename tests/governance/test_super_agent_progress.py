"""A ReAct loop that repeats itself must stop, not spend its budget.

The premise this corrects: 36 `StitchCollisionError` events read like
inter-agent collisions over a shared file. They are not. The stitch
pre-compiler trial-grafts a node into the whole file IN MEMORY and rejects
it if the file stops parsing -- a stray bracket, one agent, no contention. A
scope lock cannot prevent that.

Measured, and it splits cleanly in two:

  _check_api_keys_or_die    36 fractures at turn 1 -> 25 CONVERGED at turn 2
  _sibling_candidate_count  11 incidents, fractures at turns 1,2,3,4,5 with
                            the identical error, then agent_unconverged

The first is the mechanism working: caught in memory, never written,
self-corrected one turn later. The second is five model calls to produce one
answer five times. `max_turns` bounded that waste but could not see it --
the loop cannot tell refinement from repetition.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import agentic_super_agent as sa


@pytest.fixture(autouse=True)
def _fresh_detector():
    sa._PROGRESS_DETECTOR = None
    yield
    sa._PROGRESS_DETECTOR = None


ERR = "StitchCollisionError: empty node — return the complete definition"


def test_first_occurrence_is_a_refinement_not_a_cycle():
    """One correction is the loop doing its job; severing there would
    break the case that converges at turn 2."""
    assert sa._observe_refinement("sym", ERR) is False


def test_same_error_twice_severs():
    sa._observe_refinement("sym", ERR)
    assert sa._observe_refinement("sym", ERR) is True


def test_a_changing_error_keeps_refining():
    """Different errors are progress: the agent is being corrected on
    something new each turn, which is what the feedback loop is for."""
    assert sa._observe_refinement("sym", "error one") is False
    assert sa._observe_refinement("sym", "error two") is False
    assert sa._observe_refinement("sym", "error three") is False


def test_agents_do_not_share_a_series():
    """Two agents hitting the same error independently are not one agent
    repeating itself."""
    assert sa._observe_refinement("alpha", ERR) is False
    assert sa._observe_refinement("beta", ERR) is False


def test_release_resets_the_series():
    sa._observe_refinement("sym", ERR)
    sa._release_refinement("sym")
    assert sa._observe_refinement("sym", ERR) is False


def test_empty_signature_is_not_a_repeat():
    """An absent error is not evidence of repetition."""
    assert sa._observe_refinement("sym", "") is False
    assert sa._observe_refinement("sym", "") is False


def test_detector_fault_never_ends_a_working_agent(monkeypatch):
    """A telemetry fault must not sever an agent that is still converging."""
    class _Boom:
        def observe(self, *_a, **_k):
            raise RuntimeError("detector down")

    sa._PROGRESS_DETECTOR = _Boom()
    assert sa._observe_refinement("sym", ERR) is False


def test_release_is_safe_before_any_observation():
    sa._release_refinement("never-seen")


def test_key_is_namespaced():
    """The detector is shared with GENERATE retries and the micro-fix
    governor; an unnamespaced key would collide across subsystems."""
    assert sa._no_progress_key("sym").startswith("superagent::")


def test_cycle_detection_is_not_reimplemented():
    """DRY: ForwardProgressDetector already owns consecutive-hash detection
    for the GENERATE retry loop and the micro-fix governor.

    Asserted on the OBJECT actually used, not on the source text -- a
    substring check would pass on a comment naming the class while a
    hand-rolled counter did the work underneath.
    """
    from backend.core.ouroboros.governance.forward_progress import (
        ForwardProgressDetector,
    )

    sa._observe_refinement("dry-check", "some error")
    assert isinstance(sa._PROGRESS_DETECTOR, ForwardProgressDetector)


def test_severance_is_reachable_from_the_loop():
    """The helper must actually be CALLED -- a progress detector nothing
    consults is the defect it was built to fix.

    Asserted against the parsed module rather than its text: a substring
    check passes on a comment mentioning the name and fails on a rename
    that keeps the behaviour, which is the pin class ci/string_pin_ratchet
    exists to stop. (It caught this test.)
    """
    from pathlib import Path

    from tests.support.ast_contract import calls_to, parse_module

    module = parse_module(Path(sa.__file__))
    assert calls_to(module, "_observe_refinement"), (
        "the turn loop never consults the progress detector"
    )
    assert calls_to(module, "_release_refinement"), (
        "a severed series is never released, so the detector leaks entries"
    )
