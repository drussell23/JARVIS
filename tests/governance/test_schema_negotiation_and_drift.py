"""The schema is negotiated from capability, and a downgrade must be loud.

Root cause of every damaged autonomous commit this repository has produced.
Soak bt-2026-09-17-184946 logged, in the same second:

    capability resolved: full_content_only -> full_content_and_diff
        (served=qwen3-coder-ov:30b)
    diff schema is armed and qwen3-coder-ov:30b is diff-capable on a
        single-file op

and sent the model a prompt saying, 108 times:

    `full_content` must be the COMPLETE file (not a diff, not a patch)

because a size gate forced full_content for any file at or under 800 lines.
`backend/api/sse_contract.py` is 144 lines. Three commits re-emitted it whole
to change three lines, and each drifted on the rest.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import capability_assurance as CA
from backend.core.ouroboros.governance import providers as P


# --------------------------------------------------------------------------
# Phase 1 — capability decides, size does not
# --------------------------------------------------------------------------

@pytest.mark.parametrize("lines", [1, 12, 144, 799, 800, 801, 50_000])
def test_a_diff_capable_model_always_gets_the_diff_schema(lines):
    """THE regression: 144 lines took the whole-file path under a 800 gate."""
    assert P.should_force_full_content(
        schema_capability="full_content_and_diff",
        target_line_count=lines, threshold_lines=800,
    ) is False


@pytest.mark.parametrize("lines", [1, 144, 5000])
def test_a_model_that_cannot_diff_still_gets_full_content(lines):
    """Capability is the authority in BOTH directions."""
    assert P.should_force_full_content(
        schema_capability="full_content_only",
        target_line_count=lines, threshold_lines=800,
    ) is True


def test_an_unknown_capability_is_conservative():
    assert P.should_force_full_content(
        schema_capability="", target_line_count=10, threshold_lines=800,
    ) is True


def test_no_line_count_heuristic_survives_in_the_decision():
    """A cost heuristic must never again decide a correctness question."""
    import inspect

    body = inspect.getsource(P.should_force_full_content).split('"""')[-1]
    assert "target_line_count" not in body
    assert "threshold_lines" not in body


def test_the_unused_parameters_are_still_accepted():
    """Call sites and their pins keep their shape; the arguments carry no
    authority."""
    import inspect

    params = inspect.signature(P.should_force_full_content).parameters
    assert {"schema_capability", "target_line_count", "threshold_lines"} <= set(params)


# --------------------------------------------------------------------------
# Phase 2 — a downgrade with no stated cause is fatal
# --------------------------------------------------------------------------

def test_the_drift_verdict_is_now_fatal_for_a_sanctioned_op():
    """It fired six times in the soak and nothing acted on it, because the size
    gate made the downgrade legitimate. With the gate gone it is a defect."""
    import inspect

    # Anchored on the VERDICT CONSTRUCTION, not the first textual mention.
    # It used to index the first occurrence of the bare name, and a later
    # comment elsewhere in the module that merely REFERRED to the verdict moved
    # the anchor and broke this test while the behaviour was untouched — a
    # source-inspection pin failing on prose.
    src = inspect.getsource(CA)
    idx = src.index('"CapabilityExecutionDrift: the diff schema is armed')
    tail = src[idx:idx + 2000]
    assert "severity=FATAL" in tail, "the silent-downgrade verdict is still advisory"


def test_the_verdict_is_still_bounded_by_enforceability():
    """A diagnostic must not become the thing that breaks unsanctioned work —
    an ambient tool call or probe reports and proceeds."""
    v = CA.CapabilityVerdict(
        False, "CapabilityExecutionDrift: x", {}, "", enforceable=False,
        severity=CA.FATAL,
    )
    assert v.is_fatal is False
    armed = CA.CapabilityVerdict(
        False, "CapabilityExecutionDrift: x", {}, "", enforceable=True,
        severity=CA.FATAL,
    )
    assert armed.is_fatal is True


def test_a_model_that_cannot_diff_is_not_drift():
    """full_content from a full_content_only model is correct, not a downgrade."""
    import inspect

    src = inspect.getsource(CA)
    assert "served model is" in src  # the non-drift branch still exists


# --------------------------------------------------------------------------
# Phase 3 — a malformed diff is rejected, never downgraded
# --------------------------------------------------------------------------

def test_a_malformed_diff_does_not_become_a_full_content_candidate():
    """The constraint has to stay tight: silently re-emitting the whole file is
    how the drift this schema prevents gets back in.

    Checked on the except-branch ONLY — the success path a few lines below
    legitimately builds a ``full_content`` candidate out of the PATCHED text,
    and a naive window over both reads as a fallback that is not there.
    """
    import inspect

    src = inspect.getsource(P)
    idx = src.index("MalformedDiffException")
    branch = src[idx:src.index("continue", idx)]
    # The property is "this branch produces NO candidate", not "the word
    # full_content is absent" — the log line in it explains that the schema is
    # not downgraded, and says so using that word.
    assert "rewritten.append" not in branch, (
        "the failure path emitted a candidate instead of rejecting"
    )
    assert "_record_malformed_diff_lesson" in branch


def test_every_candidate_failing_raises_rather_than_falling_back():
    import inspect

    src = inspect.getsource(P)
    assert "diff_apply_failed_all_candidates" in src


def test_the_malformed_diff_lesson_names_what_to_do_differently(monkeypatch):
    """Driven inside a loop on purpose: the recorder is fire-and-forget, so
    without one the coroutine is closed unawaited and records nothing — which
    is correct behaviour, and makes a loop-less assertion a test of nothing."""
    import asyncio

    captured = {}

    async def _fake(**kw):
        captured.update(kw)
        return "ok"

    import backend.core.ouroboros.governance.lesson_memory as LM

    monkeypatch.setattr(LM, "record_lesson", _fake)

    class _Ctx:
        op_id = "op-test"

    async def _drive():
        P._record_malformed_diff_lesson(
            _Ctx(), "backend/api/x.py", "hunk 1 not found",
        )
        await asyncio.sleep(0)      # let the fire-and-forget task run

    asyncio.run(_drive())
    assert captured.get("failure_class") == "malformed_diff"
    assert captured.get("phase") == "GENERATE"
    assert "VERBATIM" in captured.get("summary", "")
    assert "do not re-emit the whole file" in captured.get("summary", "")


def test_the_lesson_never_raises_without_a_loop():
    """It sits inside a synchronous candidate loop; a missing event loop must
    not surface as an exception mid-generation."""
    class _Ctx:
        op_id = "op-test"

    P._record_malformed_diff_lesson(_Ctx(), "x.py", "boom")


def test_the_lesson_never_raises_on_a_broken_ledger(monkeypatch):
    import backend.core.ouroboros.governance.lesson_memory as LM

    def _boom(**kw):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(LM, "record_lesson", _boom)

    class _Ctx:
        op_id = "op-test"

    P._record_malformed_diff_lesson(_Ctx(), "x.py", "boom")
