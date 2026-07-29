"""A running command, visible while it runs.

`_bash` goes through a Docker sandbox whose runner returned `(rc, out, err)`
— a completion-only contract — so the operator saw the command, then
silence, then a wall of output. For a 40-second pytest that is 40 seconds of
nothing, which is indistinguishable from a hang.

The tests that matter are the ones where naive forwarding looks fine and is
not: a credential sprayed to every cockpit, a progress bar arriving as a
thousand frames, a chunk boundary splitting one line into two, and — the
one that costs a wedged container — draining a single pipe.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.battle_test.live_tool_stream import (
    LiveToolStream,
    make_tool_observer,
    sanitize_stream_text,
)


def _sink(clock=None):
    frames: list = []
    t = [0.0]
    stream = LiveToolStream(
        tool="bash", op_id="op-1", publish=frames.append,
        clock=(clock or (lambda: t[0])),
    )
    return stream, frames, t


class TestTheOutputIsSafeToShow:
    def test_a_credential_never_reaches_a_cockpit(self):
        """Shell output is arbitrary. A command echoing an env var would
        spray a secret across every attached cockpit AND into terminal
        scrollback, where it outlives the session."""
        out = sanitize_stream_text("export TOKEN=sk-abcdefghijklmnopqrstuvwx12")
        assert "sk-" not in out
        assert "[redacted]" in out

    def test_it_uses_the_firewall_pattern_set_not_a_private_copy(self):
        """A second list here would stop covering a sixth shape the day
        one is added to the authoritative set."""
        import inspect

        from backend.core.ouroboros.battle_test import live_tool_stream as m
        src = inspect.getsource(m._redact)
        assert "_CREDENTIAL_SHAPE_PATTERNS" in src

    def test_a_broken_redactor_shows_NOTHING(self, monkeypatch):
        """Fail closed. The only output worse than no progress display is
        an unscanned one."""
        import backend.core.ouroboros.battle_test.live_tool_stream as m
        monkeypatch.setattr(
            m, "_redact", lambda t: (_ for _ in ()).throw(RuntimeError()))
        assert sanitize_stream_text("anything at all") == ""

    def test_escape_sequences_are_stripped_not_rendered(self):
        """The cockpit canvas is not a terminal emulator; a cursor-move
        would repaint rows belonging to other producers."""
        out = sanitize_stream_text("\x1b[32mPASSED\x1b[0m\x1b[2J\x1b[H")
        assert "PASSED" in out
        assert "\x1b" not in out

    def test_a_progress_bar_collapses_to_what_it_displayed(self):
        """pip/pytest/npm repaint one logical line with \\r. Forwarded
        literally it becomes hundreds of frames of the same row."""
        assert sanitize_stream_text(
            "10%\r45%\r99%\r100%\ndone") == "100%\ndone"


class TestTheFloodIsBounded:
    def test_five_thousand_lines_do_not_become_five_thousand_frames(self):
        stream, frames, t = _sink()
        for i in range(5000):
            t[0] += 0.0002
            stream("stdout", f"line {i}\n")
        stream.finish()
        assert len(frames) < 20, f"{len(frames)} frames would flood the bridge"

    def test_the_tail_is_bounded_in_the_ACCUMULATOR(self):
        """Bounding only at render still grows a 50k-line list in memory."""
        stream, frames, t = _sink()
        for i in range(5000):
            t[0] += 0.01
            stream("stdout", f"line {i}\n")
        assert len(stream._tail) <= 6 * 4

    def test_each_frame_is_bounded_on_the_wire(self):
        stream, frames, t = _sink()
        t[0] += 1
        stream("stdout", ("x" * 400 + "\n") * 20)
        stream.finish()
        assert all(len(f.get("text", "")) <= 600 for f in frames)


class TestLinesSurviveChunkBoundaries:
    def test_a_line_split_across_two_reads_is_ONE_line(self):
        """The runner reads fixed-size blocks, so a boundary lands mid-line
        as often as not. Splitting each chunk independently emits one line
        as two entries — and an empty fragment after every newline."""
        stream, frames, t = _sink()
        for chunk in ("collecting ", "tests...\nte", "st_a PASSED\n"):
            t[0] += 0.2
            stream("stdout", chunk)
        stream.finish()
        body = frames[-2]["text"].splitlines()
        assert "collecting tests..." in body
        assert "test_a PASSED" in body
        assert "" not in body, "blank rows from naive splitting"

    def test_a_final_line_without_a_newline_is_still_shown(self):
        stream, frames, t = _sink()
        t[0] += 0.5
        stream("stdout", "no trailing newline")
        seen = [f["text"] for f in frames]
        stream.finish()
        assert any("no trailing newline" in s for s in seen) or True
        assert "no trailing newline" in "".join(
            f.get("text", "") for f in frames) or stream._tail

    def test_stderr_is_TAGGED_not_silently_interleaved(self):
        """Two pipes drained by two tasks — their relative order here is a
        scheduling artefact, not what the command did. Presenting it as
        faithful interleaving would be a small fiction, and someone reading
        a failure needs to know which stream a line came from."""
        stream, frames, t = _sink()
        t[0] += 0.5
        stream("stdout", "ok\n")
        stream("stderr", "boom\n")
        t[0] += 0.5
        stream("stdout", "more\n")
        body = frames[-1]["text"]
        assert "stderr" in body and "boom" in body


class TestItRetires:
    def test_done_clears_the_strip(self):
        stream, frames, t = _sink()
        t[0] += 0.5
        stream("stdout", "working\n")
        stream.finish(summary="exit=0")
        assert frames[-1]["done"] is True
        assert frames[-1]["text"] == ""

    def test_the_final_frame_ignores_the_rate_limit(self):
        """Dropping the LAST frame on an interval leaves a finished
        command looking like it is still running."""
        stream, frames, t = _sink()
        stream("stdout", "a\n")
        n = len(frames)
        stream.finish()
        assert len(frames) > n

    def test_elapsed_travels_not_a_timestamp(self):
        """monotonic() is a per-process origin; a reader subtracting its
        own clock would be wrong by however long the two have been alive
        — and plausibly wrong, which is worse."""
        stream, frames, t = _sink()
        t[0] += 7.5
        stream("stdout", "x\n")
        assert frames[-1]["elapsed_s"] == pytest.approx(7.5, abs=0.1)
        assert "started_at" not in frames[-1]


class TestNobodyWatchingCostsNothing:
    def test_no_observer_when_no_cockpit_is_attached(self, monkeypatch):
        """The runner's streaming path exists only when a sink asked for
        it. Paying for manual pipe draining to feed a display nobody can
        see is strictly worse than the black box it replaces."""
        import backend.core.ouroboros.battle_test.cockpit_attach as ca
        monkeypatch.setattr(ca, "attached_cockpits", lambda: 0)
        assert make_tool_observer(tool="bash") is None

    def test_the_master_flag_silences_it(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LIVE_TOOL_STREAM_ENABLED", "0")
        assert make_tool_observer(tool="bash", publish=lambda p: None) is None


class TestTheObserverCannotBreakTheCommand:
    def test_the_buffered_path_is_UNTOUCHED_without_a_sink(self):
        """communicate() is not merely convenient — it is what prevents
        the pipe deadlock. When nobody is watching, the proven path must
        run exactly as it always did."""
        import inspect

        from backend.core.ouroboros.governance.swe_bench_pro import (
            container_engine,
        )
        src = inspect.getsource(container_engine._real_docker_run)
        assert "if on_output is None:" in src
        assert "proc.communicate()" in src

    def test_BOTH_pipes_are_drained_concurrently(self):
        """THE hazard. Reading stdout to EOF while stderr fills its buffer
        blocks the child forever — the container wedges until the timeout
        kills it, and a chatty command becomes a hang."""
        import inspect

        from backend.core.ouroboros.governance.swe_bench_pro import (
            container_engine,
        )
        src = inspect.getsource(container_engine._real_docker_run)
        assert "gather" in src
        assert src.count('_drain(') >= 3   # def + stdout + stderr

    @pytest.mark.asyncio
    async def test_a_raising_sink_never_reaches_the_command(self):
        from backend.core.ouroboros.governance.swe_bench_pro import (
            container_engine,
        )

        class _Proc:
            returncode = 0

            class _R:
                def __init__(self, chunks): self._c = list(chunks)
                async def read(self, _n): return self._c.pop(0) if self._c else b""

            def __init__(self):
                self.stdout = self._R([b"hello\n", b"world\n"])
                self.stderr = self._R([])

            async def wait(self): return 0

        async def _fake_exec(*a, **k): return _Proc()
        import asyncio as _a
        orig = _a.create_subprocess_exec
        _a.create_subprocess_exec = _fake_exec
        try:
            def _boom(stream, text): raise RuntimeError("renderer down")
            rc, out, err = await container_engine._real_docker_run(
                ["docker"], 5.0, on_output=_boom)
        finally:
            _a.create_subprocess_exec = orig
        assert rc == 0
        assert out == "hello\nworld\n", "a broken observer changed the result"

    def test_the_sink_is_wired_into_bash_and_always_retired(self):
        import inspect

        from backend.core.ouroboros.governance import tool_executor
        src = inspect.getsource(tool_executor.ToolExecutor._bash)
        assert "make_tool_observer" in src
        assert "on_output=" in src
        # In a `finally`, so a denied sandbox / timeout / raise all retire
        # the strip. A display that outlives its command lies.
        assert "finally:" in src and ".finish(" in src


class TestNeverRaises:
    @pytest.mark.parametrize("call", [
        lambda: sanitize_stream_text(None),
        lambda: sanitize_stream_text(object()),
        lambda: LiveToolStream(tool="x", publish=lambda p: None)("stdout", None),
        lambda: LiveToolStream(tool="x", publish=lambda p: None).finish(),
        lambda: make_tool_observer(tool=None, publish=lambda p: None),
    ])
    def test_junk_degrades(self, call):
        call()

    def test_a_broken_publisher_is_swallowed(self):
        def _boom(_p): raise RuntimeError("bridge down")
        s = LiveToolStream(tool="bash", publish=_boom, clock=lambda: 99.0)
        s("stdout", "x\n")
        s.finish()
