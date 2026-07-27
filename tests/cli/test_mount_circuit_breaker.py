"""A software crash must not be able to look like a hardware downgrade.

One fallback, two causes. Until now both printed the same line:

    ⎿ cockpit fallback → legacy view (ValueError: ...)

*Hardware* — no TTY, kill-switch, a terminal that will not report its cursor
position. Expected; nothing is wrong; stay quiet.

*Software* — the cockpit raised. Something IS wrong, and the evidence was
being truncated to 80 characters with the traceback discarded.

That collapse is how a crash hides: the operator reads a routine downgrade
notice, the parachute opens, the session continues, and the bug is never
reported. These tests pin that the two are distinguishable — and that being
loud did NOT become being fatal, because the parachute is the whole point.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from backend.core.ouroboros.battle_test.mount_breaker import (
    HARDWARE,
    SOFTWARE,
    announce,
    classify_mount_failure,
    crash_banner,
    crash_log_path,
    record_mount_crash,
)

_REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 1. classification
# --------------------------------------------------------------------------

def test_no_exception_is_a_hardware_downgrade() -> None:
    assert classify_mount_failure(None, "stdout is not a real TTY") == HARDWARE
    assert classify_mount_failure(None, "kill-switch") == HARDWARE


@pytest.mark.parametrize("exc", [
    ValueError("layout"), ImportError("missing"), RuntimeError("ipc"),
    KeyError("payload"), AttributeError("None has no .render"),
])
def test_any_exception_is_a_software_crash(exc: Exception) -> None:
    assert classify_mount_failure(exc, "whatever") == SOFTWARE


def test_classification_ignores_the_reason_prose() -> None:
    """The reason is a sentence written for an operator — it changes freely.
    Branching on it would make the classifier drift with the copy."""
    assert classify_mount_failure(None, "ValueError: looks scary") == HARDWARE
    assert classify_mount_failure(ValueError("x"), "just a TTY thing") == SOFTWARE


# --------------------------------------------------------------------------
# 2. the black box
# --------------------------------------------------------------------------

def _raised(exc_type: Any = ValueError, msg: str = "layout blew up"):
    try:
        raise exc_type(msg)
    except Exception as exc:      # noqa: BLE001 — a real traceback is the point
        return exc


def test_the_full_traceback_reaches_disk(tmp_path: Path) -> None:
    log = tmp_path / "ov-crash.log"
    assert record_mount_crash(_raised(), path=log) == log
    body = log.read_text()
    assert "ValueError: layout blew up" in body
    assert "Traceback" in body
    assert "test_mount_circuit_breaker" in body, (
        "the frames were lost — a one-line message is what this replaces"
    )


def test_crashes_append_rather_than_overwrite(tmp_path: Path) -> None:
    """An intermittent cockpit failure is a far harder bug than a constant
    one, and the earlier occurrences are what tell them apart."""
    log = tmp_path / "ov-crash.log"
    record_mount_crash(_raised(msg="first"), path=log)
    record_mount_crash(_raised(msg="second"), path=log)
    body = log.read_text()
    assert "first" in body and "second" in body


def test_a_missing_directory_is_created(tmp_path: Path) -> None:
    log = tmp_path / "deep" / "nested" / "ov-crash.log"
    assert record_mount_crash(_raised(), path=log) is not None
    assert log.exists()


def test_an_unwritable_log_does_not_escalate_the_crash(tmp_path: Path) -> None:
    """The log instruments the parachute; it must never be the thing that
    turns a survivable crash into a fatal one."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    assert record_mount_crash(_raised(), path=blocker / "ov-crash.log") is None


def test_the_log_path_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_OV_CRASH_LOG", "/tmp/somewhere/else.log")
    assert crash_log_path() == Path("/tmp/somewhere/else.log")


def test_the_default_path_is_under_dot_jarvis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_OV_CRASH_LOG", raising=False)
    assert crash_log_path().name == "ov-crash.log"
    assert ".jarvis" in str(crash_log_path())


# --------------------------------------------------------------------------
# 3. the banner
# --------------------------------------------------------------------------

def test_the_banner_names_the_fault_and_where_to_look(tmp_path: Path) -> None:
    banner = crash_banner(_raised(), tmp_path / "ov-crash.log")
    assert "FATAL MOUNT EXCEPTION" in banner
    assert "SAFE MODE" in banner
    assert "ValueError" in banner
    assert "ov-crash.log" in banner


def test_the_banner_survives_an_unwritable_log() -> None:
    assert "unwritable" in crash_banner(_raised(), None)


def test_the_banner_carries_no_markup() -> None:
    """It prints at the moment the rich surface just proved it does not work,
    so it must not depend on anything the failure may have taken with it."""
    banner = crash_banner(_raised(), Path("/tmp/x.log"))
    assert "[" not in banner.replace("[6n", "")
    assert "\x1b" not in banner


def test_an_enormous_exception_message_is_bounded() -> None:
    banner = crash_banner(_raised(msg="x" * 5000), Path("/tmp/x.log"))
    assert len(banner) < 500, "a runaway message would push the banner offscreen"


def test_a_multiline_exception_message_stays_on_one_line() -> None:
    banner = crash_banner(_raised(msg="line one\nline two"), Path("/tmp/x.log"))
    assert "line two" not in banner


# --------------------------------------------------------------------------
# 4. the seam — one path in, two behaviours out
# --------------------------------------------------------------------------

def test_a_hardware_downgrade_stays_quiet() -> None:
    out: List[str] = []
    kind, log = announce(None, "stdout is not a real TTY", emit=out.append)
    assert kind == HARDWARE
    assert log is None
    assert len(out) == 1
    assert "FATAL" not in out[0]
    assert "not a real TTY" in out[0]


def test_a_software_crash_is_loud_and_recorded(tmp_path: Path) -> None:
    """MANDATE 4(1): trips Path B, writes the log, does not kill anything."""
    out: List[str] = []
    log = tmp_path / "ov-crash.log"
    kind, written = announce(
        _raised(), "ValueError: layout blew up", emit=out.append, path=log,
    )
    assert kind == SOFTWARE
    assert written == log and log.exists()
    assert "FATAL MOUNT EXCEPTION" in "".join(out)
    assert "Traceback" in log.read_text()


def test_the_seam_never_raises_however_badly_it_is_used() -> None:
    """It runs at the exact moment something else already failed."""
    def _boom(_text: str) -> None:
        raise OSError("stdout is gone too")

    announce(_raised(), "x", emit=_boom)                 # must not raise
    announce(None, "x", emit=_boom)
    announce(_raised(), "x", emit=None)                  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 5. wiring
# --------------------------------------------------------------------------

def test_the_attach_path_routes_both_causes_through_one_seam() -> None:
    """DRY, asserted structurally: two branches printing their own message is
    exactly the arrangement that let a crash impersonate a downgrade."""
    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    assert "from backend.core.ouroboros.battle_test.mount_breaker import" in src
    assert "announce(" in src
    assert '_crash = _exc' in src, (
        "the exception is being discarded again — a truncated str() cannot be "
        "classified or logged"
    )


def test_a_software_crash_forces_append_only_output() -> None:
    """A crash mid-render leaves the terminal in an unknown state — possibly
    alt-screen, possibly raw mode, cursor anywhere. The parachute must assume
    nothing about the screen."""
    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    assert 'if _kind == "software":' in src
    assert "ui.degrade_to_append_only()" in src


def test_a_hardware_downgrade_keeps_its_colour() -> None:
    """The inverse, and it matters: a missing TTY on an otherwise healthy
    terminal is not a reason to strip every operator's output to plain text."""
    import ast

    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    tree = ast.parse(src)

    # Asserted on the AST, not on a character window. The first version of
    # this sliced 400 characters after the branch and broke the moment a
    # comment was added above the call — measuring prose length, not
    # structure, which is the same mistake a proximity pin makes.
    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if "_kind" not in ast.unparse(node.test) or "software" not in ast.unparse(node.test):
            continue
        body = "\n".join(ast.unparse(stmt) for stmt in node.body)
        assert "degrade_to_append_only" in body, (
            "the software branch no longer forces append-only output"
        )
        guarded = True
    assert guarded, "the `_kind == software` branch is gone"

    # And it appears NOWHERE else in the attach flow, so a hardware downgrade
    # on a healthy terminal keeps its colour.
    assert src.count("ui.degrade_to_append_only()") == 1, (
        "append-only is being forced outside the software-crash branch — a "
        "missing TTY is not a reason to strip every operator's output"
    )


async def test_the_breaker_does_not_re_raise() -> None:
    """Being loud is not the same as being fatal. The fallback exists so a
    cockpit bug cannot brick attach, and that judgement is unchanged."""
    out: List[str] = []
    kind, _ = announce(_raised(), "boom", emit=out.append,
                       path=Path("/dev/null/nope/ov-crash.log"))
    assert kind == SOFTWARE          # reached the end, nothing propagated
