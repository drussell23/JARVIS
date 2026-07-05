"""test_brain_absolute_selfdestruct.py -- Piece D (A1 Brain mandate 4):
node-side ABSOLUTE self-destruct dead-man's switch, armed only when
``JARVIS_BRAIN_ABSOLUTE_LIFETIME_S`` is a positive int. Drives the REAL
``_brain_runtime_startup_script`` generator (pure string builder, no I/O) --
no mocking of the function under test.
"""
from __future__ import annotations

from backend.core.ouroboros.governance import brain_lifecycle as bl

_SELFDESTRUCT_MARKER = "shutdown -h now"


def test_armed_when_lifetime_env_set(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", "1800")

    script = bl._brain_runtime_startup_script()

    assert "sleep 1800" in script
    assert _SELFDESTRUCT_MARKER in script
    assert "nohup" in script
    # detached: the self-destruct line backgrounds itself with `&`
    selfdestruct_line = next(
        line for line in script.splitlines() if _SELFDESTRUCT_MARKER in line
    )
    assert selfdestruct_line.rstrip().endswith("&")
    assert "nohup" in selfdestruct_line


def test_disarmed_when_lifetime_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", raising=False)

    script = bl._brain_runtime_startup_script()

    assert _SELFDESTRUCT_MARKER not in script
    assert "JARVIS_BRAIN_ABSOLUTE_LIFETIME_S" not in script


def test_disarmed_when_lifetime_env_blank(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", "")

    script = bl._brain_runtime_startup_script()

    assert _SELFDESTRUCT_MARKER not in script


def test_disarmed_when_lifetime_env_zero(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", "0")

    script = bl._brain_runtime_startup_script()

    assert _SELFDESTRUCT_MARKER not in script


def test_disarmed_when_lifetime_env_negative(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", "-30")

    script = bl._brain_runtime_startup_script()

    assert _SELFDESTRUCT_MARKER not in script


def test_disarmed_when_lifetime_env_non_numeric_never_raises(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", "not-a-number")

    script = bl._brain_runtime_startup_script()  # must not raise

    assert _SELFDESTRUCT_MARKER not in script


def test_resolve_absolute_lifetime_s_helper_fail_soft(monkeypatch) -> None:
    """Direct unit coverage of the resolver helper (fail-soft contract)."""
    monkeypatch.setenv("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", "3600")
    assert bl._resolve_absolute_lifetime_s() == 3600

    monkeypatch.setenv("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", "0")
    assert bl._resolve_absolute_lifetime_s() == 0

    monkeypatch.setenv("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", "-1")
    assert bl._resolve_absolute_lifetime_s() == 0

    monkeypatch.setenv("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", "garbage")
    assert bl._resolve_absolute_lifetime_s() == 0

    monkeypatch.delenv("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", raising=False)
    assert bl._resolve_absolute_lifetime_s() == 0
