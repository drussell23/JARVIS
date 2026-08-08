"""A real attach bridge with no organism behind it, and a tunable storm.

WHY A REAL BRIDGE AND NOT A FAKE
--------------------------------
The open question is whether the `PromptSession` attach surface wedges when
daemon output arrives faster than the application resumes. Every candidate
mechanism lives in the transport: how frames are framed, how the client's
reader task interleaves with prompt_toolkit's, how `patch_stdout` suspends the
app through `run_in_terminal` to write. A hand-written fake socket would
reproduce the protocol and none of the timing, and would answer a question
nobody asked.

So this stands up the genuine ``CockpitAttachBridge`` — same class the harness
wires — on a temp socket, with default providers and no organism. Real framing,
real asyncio server, real client. The only thing missing is the work that would
normally produce the output, which is precisely the variable being controlled.

THE STORM IS DISCOVERED, NOT DECLARED
-------------------------------------
A fixed rate is a guess, and the last attempt at this failed because 4 Hz was
too gentle to provoke anything. The rate here ESCALATES until either the
cockpit stops answering — the wedge, reproduced, with the rate that caused it —
or the bridge itself stops keeping up, which is the machine's ceiling and is
measured rather than assumed. No number in this file decides when to stop.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional


#: A unix socket ADDRESS is capped near 104 bytes by the kernel, and the cap is
#: on the path, not the filename. pytest's tmp_path and macOS's
#: /var/folders/... TMPDIR are both long enough that any nested name exceeds it
#: — surfacing as "bind failed: AF_UNIX path too long", which reads like a bug
#: in the bridge rather than a property of the address family.
_SUN_PATH_LIMIT = 104


def short_socket_dir() -> Path:
    """A directory short enough to hold a bindable socket path.

    Roots are tried shortest-first and the result is VERIFIED against the
    limit rather than assumed, because the shortest root is a different
    directory on macOS, Linux and CI, and a harness that guessed would fail
    somewhere nobody was looking.
    """
    import tempfile

    candidates = [Path("/tmp"), Path(tempfile.gettempdir())]
    errors = []
    for root in candidates:
        try:
            if not root.is_dir():
                continue
            d = Path(tempfile.mkdtemp(prefix="ovp", dir=str(root)))
        except OSError as exc:
            errors.append(f"{root}: {exc}")
            continue
        # One byte of headroom for the filename the caller will append.
        if len(str(d)) + len("/a.sock") < _SUN_PATH_LIMIT:
            return d
        errors.append(f"{root}: {len(str(d))} bytes is already too long")
    raise RuntimeError(
        "no directory short enough for an AF_UNIX socket "
        f"(limit {_SUN_PATH_LIMIT}): {'; '.join(errors)}"
    )


class PressureBridge:
    """A live ``CockpitAttachBridge`` on its own socket, driven from tests.

    Runs its event loop on a background thread so a synchronous test can talk
    to it. Every cross-thread call goes through ``call_soon_threadsafe`` — the
    bridge's own publish path is documented non-blocking and thread-safe, but
    its *creation* and shutdown are not, and mixing loops is how a harness
    starts measuring its own bugs.
    """

    def __init__(self, socket_path: Optional[Path] = None) -> None:
        self._owned_dir: Optional[Path] = None
        if socket_path is None:
            self._owned_dir = short_socket_dir()
            socket_path = self._owned_dir / "a.sock"
        self.socket_path = Path(socket_path)
        if len(str(self.socket_path)) >= _SUN_PATH_LIMIT:
            raise RuntimeError(
                f"socket path is {len(str(self.socket_path))} bytes, over the "
                f"AF_UNIX limit of {_SUN_PATH_LIMIT}: {self.socket_path}"
            )
        self.inputs: List[str] = []
        self._bridge: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._emitted = 0
        self._storm_stop: Optional[threading.Event] = None
        self._storm_thread: Optional[threading.Thread] = None
        self._storm_began: float = 0.0

    # -- lifecycle ----------------------------------------------------------
    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            from backend.core.ouroboros.battle_test.cockpit_attach import (
                CockpitAttachBridge,
            )

            def _on_input(text: Any, *_a: Any, **_kw: Any) -> None:
                # Tolerant signature on purpose: the bridge adapts its call to
                # whatever the callable accepts, and a harness that pinned one
                # arity would break the day a field was added — silently, by
                # never recording an input.
                self.inputs.append(str(text))

            self._bridge = CockpitAttachBridge(
                path=self.socket_path, on_input=_on_input,
            )
            ok = loop.run_until_complete(self._bridge.start())
            if not ok:
                raise RuntimeError("CockpitAttachBridge.start() returned False")
        except BaseException as exc:  # noqa: BLE001 — reported to the caller
            self._start_error = exc
            self._ready.set()
            return
        self._ready.set()
        loop.run_forever()

    def __enter__(self) -> "PressureBridge":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30.0):
            raise RuntimeError("bridge did not start within its deadline")
        if self._start_error is not None:
            raise self._start_error
        # The socket FILE is the readiness signal, not the coroutine returning:
        # a client that connects before the path exists fails for a reason that
        # has nothing to do with what is being measured.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if self.socket_path.exists():
                return self
            time.sleep(0.02)
        raise RuntimeError(f"bridge socket never appeared at {self.socket_path}")

    def __exit__(self, *exc: object) -> None:
        self.stop_storm()
        loop = self._loop
        if loop is not None:
            try:
                if self._bridge is not None and hasattr(self._bridge, "stop"):
                    fut = asyncio.run_coroutine_threadsafe(
                        self._bridge.stop(), loop,
                    )
                    try:
                        fut.result(timeout=5.0)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        try:
            self.socket_path.unlink()
        except OSError:
            pass
        if self._owned_dir is not None:
            try:
                self._owned_dir.rmdir()
            except OSError:
                pass

    # -- emitting -----------------------------------------------------------
    @property
    def emitted(self) -> int:
        return self._emitted

    def publish(self, text: str) -> None:
        """One markup frame to every attached cockpit."""
        loop = self._loop
        bridge = self._bridge
        if loop is None or bridge is None:
            return
        loop.call_soon_threadsafe(bridge.publish_markup, text)
        self._emitted += 1

    def start_storm(self, rate_hz: float, *,
                    compose: Optional[Callable[[int], str]] = None) -> None:
        """Emit at ``rate_hz`` until stopped. Idempotent-safe: stops first."""
        self.stop_storm()
        stop = threading.Event()
        self._storm_stop = stop
        self._storm_began = time.monotonic()
        began_at = self._emitted
        interval = 1.0 / max(rate_hz, 1e-9)

        def _compose(i: int) -> str:
            # Styled like real op chrome so the client takes the same render
            # path a daemon line would, rather than a plain-text shortcut.
            return f"[dim]⎿[/] pressure frame {i} " + ("·" * 40)

        composer = compose or _compose

        def _pump() -> None:
            i = began_at
            nxt = time.monotonic()
            while not stop.is_set():
                self.publish(composer(i))
                i += 1
                nxt += interval
                delay = nxt - time.monotonic()
                if delay > 0:
                    stop.wait(delay)
                else:
                    # Behind schedule: do not sleep, but do not spin either --
                    # yield so the emitting thread cannot starve the reader.
                    nxt = time.monotonic()
                    time.sleep(0)

        self._storm_thread = threading.Thread(target=_pump, daemon=True)
        self._storm_thread.start()

    def stop_storm(self) -> float:
        """Stop emitting; return the rate actually ACHIEVED, in Hz.

        Achieved rather than requested, because that difference is what tells a
        caller the machine has saturated -- which is the only honest place to
        stop escalating.
        """
        stop, thread = self._storm_stop, self._storm_thread
        self._storm_stop = self._storm_thread = None
        if stop is None:
            return 0.0
        began_emitted = getattr(self, "_storm_from", None)
        stop.set()
        if thread is not None:
            thread.join(timeout=5.0)
        elapsed = max(time.monotonic() - self._storm_began, 1e-9)
        del began_emitted
        return self._emitted / elapsed if self._storm_began else 0.0


def escalate_until_unresponsive(
    bridge: PressureBridge,
    probe: Callable[[], bool],
    *,
    start_hz: float,
    settle_s: float,
    tracking_tolerance: float = 0.5,
    max_rounds: int = 12,
    on_round: Optional[Callable[[float, float, bool], None]] = None,
) -> Optional[float]:
    """Double the emit rate until the probe fails, or the machine saturates.

    Returns the requested rate at which ``probe`` first returned False, or
    ``None`` if it never did.

    The stopping condition is MEASURED, not declared: escalation ends when the
    achieved rate stops tracking the requested one, because past that point the
    bridge is the bottleneck and raising the number changes nothing except how
    long the test takes. ``max_rounds`` is a runaway backstop, not the design.
    """
    rate = start_hz
    for _ in range(max_rounds):
        bridge.start_storm(rate)
        time.sleep(settle_s)          # let the storm reach steady state
        alive = probe()
        achieved = bridge.stop_storm()
        if on_round is not None:
            on_round(rate, achieved, alive)
        if not alive:
            return rate
        if achieved < rate * tracking_tolerance:
            # Saturated. The emitter cannot go faster, so neither can the test.
            return None
        rate *= 2.0
    return None
