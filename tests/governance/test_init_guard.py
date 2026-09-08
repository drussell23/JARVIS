"""Nothing may derive a decision from configuration that has not loaded.

The concurrency clamp was correct, tested, and inert: it read
`JARVIS_LOCAL_PRIME_ENABLED` during boot before `.env` loaded, got "unset",
resolved the lane as CLOUD, and handed a single GPU a six-worker pool. Every
test passed. The only evidence was one line in a session log.

The defect was not in the clamp. It was a missing ORDER, and nothing in the
system could notice. These tests pin the noticing.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.init_guard import (
    ConfigHydrationFault,
    assert_ready,
    faults,
    is_hydrated,
    mark_hydrated,
    require_hydrated,
    reset_for_tests,
    validate_required,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_for_tests()
    yield
    reset_for_tests()


# --------------------------------------------------------------------------
# The ordering contract
# --------------------------------------------------------------------------

def test_nothing_is_hydrated_until_declared():
    assert is_hydrated() is False


def test_marking_unlocks_and_is_idempotent():
    mark_hydrated()
    mark_hydrated()
    assert is_hydrated() is True
    assert require_hydrated("x") is True


def test_an_early_read_is_refused_and_recorded():
    assert require_hydrated("local_lane_capacity") is False
    recorded = faults()
    assert len(recorded) == 1
    assert recorded[0].component == "local_lane_capacity"
    assert "before load_env_once" in recorded[0].reason


def test_the_fault_renders_for_the_operator():
    require_hydrated("capacity")
    assert "ConfigHydrationFault" in faults()[0].render()


def test_reads_after_hydration_record_nothing():
    mark_hydrated()
    require_hydrated("a")
    require_hydrated("b")
    assert faults() == ()


# --------------------------------------------------------------------------
# The distinction that matters: unset vs deliberately false
# --------------------------------------------------------------------------

def test_unset_and_false_are_not_the_same_question(monkeypatch):
    """`os.environ.get` cannot tell "the operator chose false" from "nobody
    has loaded the file". Those demand opposite responses, and conflating them
    is how a default-shaped value became a decision nobody made."""
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "false")
    # Deliberately false, but STILL unhydrated → the guard refuses to decide.
    assert require_hydrated("local_lane_capacity") is False
    mark_hydrated()
    # Hydrated: now the operator's "false" is honoured as a real answer.
    assert require_hydrated("local_lane_capacity") is True


# --------------------------------------------------------------------------
# Required values are a DIFFERENT fault from bad ordering
# --------------------------------------------------------------------------

def test_missing_required_config_is_reported(monkeypatch):
    monkeypatch.delenv("JARVIS_TEST_ONLY_SECRET", raising=False)
    missing = validate_required(["JARVIS_TEST_ONLY_SECRET"], component="sentinel")
    assert missing == ("JARVIS_TEST_ONLY_SECRET",)
    assert faults()[0].component == "sentinel"


def test_a_blank_value_counts_as_missing(monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_ONLY_SECRET", "   ")
    assert validate_required(["JARVIS_TEST_ONLY_SECRET"]) == ("JARVIS_TEST_ONLY_SECRET",)


def test_a_present_value_is_not_missing(monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_ONLY_SECRET", "s3cret")
    assert validate_required(["JARVIS_TEST_ONLY_SECRET"]) == ()


# --------------------------------------------------------------------------
# assert_ready is the ONE fatal entry point
# --------------------------------------------------------------------------

def test_assert_ready_refuses_an_unhydrated_boot():
    with pytest.raises(ConfigHydrationFault):
        assert_ready(component="boot")


def test_assert_ready_refuses_a_boot_missing_required_config(monkeypatch):
    monkeypatch.delenv("JARVIS_TEST_ONLY_SECRET", raising=False)
    mark_hydrated()
    with pytest.raises(ConfigHydrationFault) as exc:
        assert_ready(required=["JARVIS_TEST_ONLY_SECRET"], component="sentinel")
    assert "JARVIS_TEST_ONLY_SECRET" in str(exc.value)


def test_assert_ready_passes_a_sound_boot(monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_ONLY_SECRET", "s3cret")
    mark_hydrated()
    assert_ready(required=["JARVIS_TEST_ONLY_SECRET"], component="sentinel")


def test_the_query_path_never_raises():
    """Only assert_ready raises; a capacity resolver must always get an answer
    it can act on."""
    assert require_hydrated("x") is False
    assert validate_required(["NOPE_NOT_SET"]) == ("NOPE_NOT_SET",)


# --------------------------------------------------------------------------
# The seams: the guard must actually be consulted
# --------------------------------------------------------------------------

def test_capacity_fails_safe_to_the_smallest_lane_when_unhydrated(monkeypatch):
    """The exact regression: unhydrated must NOT resolve to the cloud default."""
    from backend.core.ouroboros.governance.autonomy.local_lane_capacity import (
        resolve_primary_concurrency,
    )

    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "false")   # would say "cloud"
    cap = resolve_primary_concurrency(cloud_default=6)
    assert cap.concurrency == 1, "an unhydrated boot was handed a six-worker pool"
    assert cap.basis == "unhydrated_fail_safe"


def test_capacity_answers_normally_once_hydrated(monkeypatch):
    from backend.core.ouroboros.governance.autonomy.local_lane_capacity import (
        resolve_primary_concurrency,
    )

    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "false")
    mark_hydrated()
    cap = resolve_primary_concurrency(cloud_default=6)
    assert cap.concurrency == 6 and cap.basis == "cloud_lane"


def test_the_boot_seam_marks_hydration_after_loading_env():
    """One marker, one caller, and it must come AFTER the loader."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "scripts" / "ouroboros_battle_test.py"
    text = src.read_text(encoding="utf-8")
    assert "mark_hydrated" in text, "nothing ever declares the environment loaded"
    assert text.index("_early_load_env()") < text.index("_mark_env_hydrated()")
    # ...and the envelope must be composed only after that.
    assert text.index("_mark_env_hydrated()") < text.index("_hydrate_envelope(")
