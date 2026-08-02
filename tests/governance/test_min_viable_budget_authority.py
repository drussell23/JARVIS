"""One floor for "is there enough budget to try the fallback".

There were two, answering the same question with different numbers:

    OUROBOROS_MIN_VIABLE_FALLBACK_S      10s   candidate_generator:630
    JARVIS_ADMISSION_MIN_VIABLE_CALL_S   25s   admission_gate

The admission gate sheds first and is strictly tighter, so at default
settings the 10s constant was dead as a decision boundary — the tighter
authority silently becoming the real budget for reasons no log explains.

It was not merely redundant. The gate is disableable
(`JARVIS_ADMISSION_GATE_ENABLED=0`), and with it off the 10s floor came back
to life, so the effective minimum moved by 15s depending on a flag that is
nominally about something else. That is the shape of bug this codebase keeps
finding: a fact computed in one place and a second opinion about it kept
somewhere that nobody reconciles.

The gate's number is authoritative because its rationale is the reasoned one
— 25s is where a single Venom tool round with no thinking budget can land,
and below that we admit ops that time out at the API layer instead of at the
gate, defeating the gate's purpose. Its clamp floor is 10.0, exactly the old
default, so the legacy value survives as the gate's own lower bound rather
than as a rival.
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.candidate_generator as cg
from backend.core.ouroboros.governance import admission_gate


class TestThereIsExactlyOneFloor:
    def test_the_retry_loop_asks_the_admission_gate(self):
        assert cg._min_viable_fallback_s() == admission_gate.min_viable_call_s()

    def test_the_rival_constant_is_gone(self):
        assert not hasattr(cg, "_MIN_VIABLE_FALLBACK_S"), (
            "the second opinion is back — two floors for one decision")

    def test_no_use_site_reads_a_second_number(self):
        """Every budget comparison in the fallback path must go through the
        one accessor. A fourth site added tomorrow that re-reads an env var
        would reintroduce exactly what this removed."""
        import inspect
        source = inspect.getsource(cg)
        assert "_min_viable_fallback_s()" in source
        # The only surviving mentions of the legacy variable are the
        # supersession notice and its docstring — never a live read into a
        # comparison.
        for line in source.split("\n"):
            if "OUROBOROS_MIN_VIABLE_FALLBACK_S" in line and "<" in line:
                pytest.fail(f"legacy env still gates a comparison: {line}")


class TestTheFloorDoesNotMoveWithAnUnRELATEDFlag:
    def test_disabling_the_gate_does_not_change_the_floor(self, monkeypatch):
        """THE drift. With the gate on, 25s decided; with it off, the retry
        loop fell back to its own 10s and the effective minimum silently
        dropped by 15s."""
        monkeypatch.setenv("JARVIS_ADMISSION_GATE_ENABLED", "0")
        assert cg._min_viable_fallback_s() == 25.0
        monkeypatch.setenv("JARVIS_ADMISSION_GATE_ENABLED", "1")
        assert cg._min_viable_fallback_s() == 25.0

    def test_it_is_read_per_call_not_bound_at_import(self, monkeypatch):
        """The gate re-reads on every dispatch so a flip hot-reverts without a
        restart. A module constant computed at import would have quietly
        opted the retry loop out of that."""
        before = cg._min_viable_fallback_s()
        monkeypatch.setenv("JARVIS_ADMISSION_MIN_VIABLE_CALL_S", "40")
        assert cg._min_viable_fallback_s() == 40.0
        monkeypatch.delenv("JARVIS_ADMISSION_MIN_VIABLE_CALL_S")
        assert cg._min_viable_fallback_s() == before

    def test_the_gate_clamp_still_governs(self, monkeypatch):
        """Tuning goes through the gate's own clamp [10, 60], so the retry
        loop cannot be handed a floor the gate would refuse."""
        monkeypatch.setenv("JARVIS_ADMISSION_MIN_VIABLE_CALL_S", "5")
        assert cg._min_viable_fallback_s() == 10.0
        monkeypatch.setenv("JARVIS_ADMISSION_MIN_VIABLE_CALL_S", "999")
        assert cg._min_viable_fallback_s() == 60.0

    def test_the_old_default_survives_as_the_clamp_floor(self, monkeypatch):
        """10s was not wrong, it was second. It remains reachable — as the
        gate's lower bound, where one authority still owns it."""
        monkeypatch.setenv("JARVIS_ADMISSION_MIN_VIABLE_CALL_S", "10")
        assert cg._min_viable_fallback_s() == 10.0


class TestTheOperatorIsTold:
    def test_a_set_legacy_var_is_announced_as_superseded(self, monkeypatch, caplog):
        """Silently ignoring an operator's setting would be the same class of
        lie this removes — a number that looks like a control and is not."""
        import logging
        monkeypatch.setenv("OUROBOROS_MIN_VIABLE_FALLBACK_S", "7")
        with caplog.at_level(logging.WARNING):
            cg._warn_legacy_min_viable_env_once()
        assert any("SUPERSEDED" in r.getMessage() for r in caplog.records)
        assert any("JARVIS_ADMISSION_MIN_VIABLE_CALL_S" in r.getMessage()
                   for r in caplog.records)

    def test_it_does_not_change_the_floor(self, monkeypatch):
        monkeypatch.setenv("OUROBOROS_MIN_VIABLE_FALLBACK_S", "7")
        assert cg._min_viable_fallback_s() == 25.0

    def test_silence_when_it_is_not_set(self, monkeypatch, caplog):
        import logging
        monkeypatch.delenv("OUROBOROS_MIN_VIABLE_FALLBACK_S", raising=False)
        with caplog.at_level(logging.WARNING):
            cg._warn_legacy_min_viable_env_once()
        assert not [r for r in caplog.records if "SUPERSEDED" in r.getMessage()]

    def test_the_notice_never_raises(self, monkeypatch):
        monkeypatch.setattr(cg, "_min_viable_fallback_s",
                            lambda: (_ for _ in ()).throw(RuntimeError()))
        cg._warn_legacy_min_viable_env_once()   # must not propagate


class TestItFailsSoft:
    def test_an_unreadable_gate_yields_the_conservative_value(self, monkeypatch):
        """Admitting a doomed call is the worse failure direction, so the
        fail-soft mirrors the gate's default rather than its floor."""
        monkeypatch.setattr(
            admission_gate, "min_viable_call_s",
            lambda: (_ for _ in ()).throw(RuntimeError("gate unavailable")))
        assert cg._min_viable_fallback_s() == cg._MIN_VIABLE_FALLBACK_FAILSOFT_S
        assert cg._MIN_VIABLE_FALLBACK_FAILSOFT_S == 25.0
