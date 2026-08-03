"""What holds a session open, and what closes it when nobody is left.

`video.start_streaming` returns True and then holds the display capture open
forever. The HUD that asked for it can quit, crash, or be rebuilt; the green
recording dot stays lit. The mandated scenario is
`test_the_last_holder_releases_and_the_others_do_not`: two principals hold one
stream, one leaves, the stream KEEPS RUNNING, the second leaves, the stream
stops — exactly once.

THE TESTS TO KEEP
-------------------
`test_a_failed_release_orphans_rather_than_forgets`. Deleting the record on a
failed release would make the book report zero open sessions while the camera
light stays on — the leak PLUS a lie. An orphan is the only honest output when a
release does not work, and the number an operator can act on is the whole value
of keeping a book.

`test_a_reconnect_within_grace_never_interrupts`. Reaping the instant a socket
drops makes every HUD rebuild kill a stream the operator wanted. A one-second
blip and a quit are indistinguishable at the socket; only time tells them apart.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

import pytest

from backend.system_control import capability_leases as cl
from backend.system_control.capability_leases import (
    LeaseBook,
    LeaseState,
    ReapReason,
)


class _Releases:
    """Records every release call, and can be told to fail."""

    def __init__(self, fail: bool = False, hang: bool = False) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.fail = fail
        self.hang = hang

    async def __call__(self, name: str, args: Dict[str, Any]) -> bool:
        self.calls.append((name, dict(args or {})))
        if self.hang:
            await asyncio.sleep(3600)
        return not self.fail

    @property
    def names(self) -> List[str]:
        return [n for n, _ in self.calls]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Deterministic knobs. Every test sets what it depends on, explicitly."""
    monkeypatch.setenv("JARVIS_CAPABILITY_LEASES_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CAPABILITY_LEASE_TTL_S", "1800")
    monkeypatch.setenv("JARVIS_CAPABILITY_LEASE_GRACE_S", "20")
    monkeypatch.setenv("JARVIS_CAPABILITY_RELEASE_ATTEMPTS", "3")
    cl.reset_lease_book()
    yield
    cl.reset_lease_book()


@pytest.fixture
def rel() -> _Releases:
    return _Releases()


@pytest.fixture
def book(rel: _Releases) -> LeaseBook:
    return LeaseBook(releaser=rel)


class TestTheMandatedScenario:
    async def test_the_last_holder_releases_and_the_others_do_not(self, book, rel):
        """Two holders, one stream. It stops when the SECOND one leaves."""
        a = book.open("video.start_streaming", "video.stop_streaming", owner="hud:a")
        b = book.open("video.start_streaming", "video.stop_streaming", owner="hud:b")
        assert a is not None and b is not None
        assert book.holders("video.start_streaming") == 2

        await book.close(a.lease_id)
        # THE assertion: A left, and the stream B is using is still running.
        assert rel.names == [], "released a stream another principal still holds"
        assert book.holders("video.start_streaming") == 1

        await book.close(b.lease_id)
        assert rel.names == ["video.stop_streaming"]
        # Released exactly once — not once per holder.
        assert len(rel.calls) == 1
        assert book.holders("video.start_streaming") == 0
        assert book.active() == []


class TestHonestyAboutFailure:
    async def test_a_failed_release_orphans_rather_than_forgets(self, monkeypatch):
        """A release that does not work leaves a NAMED orphan, not a clean book."""
        monkeypatch.setenv("JARVIS_CAPABILITY_RELEASE_ATTEMPTS", "1")
        rel = _Releases(fail=True)
        book = LeaseBook(releaser=rel)
        lease = book.open("video.start_streaming", "video.stop_streaming")

        ok = await book.close(lease.lease_id)

        assert ok is False
        assert lease.state == LeaseState.ORPHANED.value
        # The book must not claim nothing is running.
        stats = book.stats()
        assert stats["orphaned"] == 1
        assert "video.start_streaming" in stats["orphaned_capabilities"]

    async def test_it_retries_before_giving_up(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CAPABILITY_RELEASE_ATTEMPTS", "3")
        rel = _Releases(fail=True)
        book = LeaseBook(releaser=rel)
        lease = book.open("video.start_streaming", "video.stop_streaming")

        await book.close(lease.lease_id)
        # Back to OPEN so a sweep tries again — staying RELEASING would hold a
        # refcount nobody would ever clear.
        assert lease.state == LeaseState.OPEN.value
        assert book.holders("video.start_streaming") == 1

        await book.close(lease.lease_id)
        await book.close(lease.lease_id)
        assert lease.state == LeaseState.ORPHANED.value
        assert len(rel.calls) == 3

    async def test_a_hanging_release_is_bounded(self, monkeypatch):
        """The release is the call most likely to hang — it must not wedge the sweep."""
        monkeypatch.setenv("JARVIS_CAPABILITY_RELEASE_TIMEOUT_S", "1")
        monkeypatch.setenv("JARVIS_CAPABILITY_RELEASE_ATTEMPTS", "1")
        book = LeaseBook(releaser=_Releases(hang=True))
        lease = book.open("video.start_streaming", "video.stop_streaming")

        await asyncio.wait_for(book.close(lease.lease_id), timeout=10)

        assert lease.state == LeaseState.ORPHANED.value
        assert "exceeded" in lease.detail

    async def test_no_releaser_is_reported_not_assumed(self):
        book = LeaseBook(releaser=None)
        lease = book.open("video.start_streaming", "video.stop_streaming")
        for _ in range(3):
            await book.close(lease.lease_id)
        assert lease.state == LeaseState.ORPHANED.value
        assert "no releaser" in lease.detail

    def test_a_start_with_no_release_is_refused(self, book):
        """It cannot be reaped, so recording it would promise what cannot be kept."""
        assert book.open("video.start_streaming", "") is None
        assert book.active() == []


class TestDeparture:
    async def test_a_disconnect_does_not_reap_immediately(self, book, rel):
        book.open("video.start_streaming", "video.stop_streaming", owner="hud:a")
        book.note_departure("hud:a")
        await book.sweep()
        assert rel.names == [], "reaped inside the grace window"
        assert book.holders("video.start_streaming") == 1

    async def test_a_reconnect_within_grace_never_interrupts(self, book, rel):
        """A HUD rebuild must not kill the stream the operator asked for."""
        book.open("video.start_streaming", "video.stop_streaming", owner="hud:a")
        book.note_departure("hud:a")
        book.note_arrival("hud:a")

        await book.sweep()
        await book.sweep()

        assert rel.names == []
        assert book.holders("video.start_streaming") == 1
        assert book.stats()["departing"] == []

    async def test_an_elapsed_grace_reaps(self, monkeypatch, rel):
        monkeypatch.setenv("JARVIS_CAPABILITY_LEASE_GRACE_S", "0")
        book = LeaseBook(releaser=rel)
        book.open("video.start_streaming", "video.stop_streaming", owner="hud:a")
        book.note_departure("hud:a")

        await book.sweep()

        assert rel.names == ["video.stop_streaming"]
        assert book.stats()["reaped_owner"] == 1

    async def test_one_principal_leaving_leaves_anothers_session_alone(
            self, monkeypatch, rel):
        monkeypatch.setenv("JARVIS_CAPABILITY_LEASE_GRACE_S", "0")
        book = LeaseBook(releaser=rel)
        book.open("video.start_streaming", "video.stop_streaming", owner="hud:a")
        book.open("space.start_monitoring", "space.stop_monitoring", owner="hud:b")
        book.note_departure("hud:a")

        await book.sweep()

        assert rel.names == ["video.stop_streaming"]
        assert book.holders("space.start_monitoring") == 1

    async def test_opening_a_lease_marks_the_owner_present(self, book, rel):
        """A principal that is opening sessions is, definitionally, not gone."""
        book.note_departure("hud:a")
        book.open("video.start_streaming", "video.stop_streaming", owner="hud:a")
        assert book.stats()["departing"] == []


class TestExpiry:
    async def test_an_expired_lease_is_reaped(self, monkeypatch, rel):
        monkeypatch.setenv("JARVIS_CAPABILITY_LEASE_TTL_S", "30")
        book = LeaseBook(releaser=rel)
        lease = book.open("video.start_streaming", "video.stop_streaming")
        # Reach into monotonic age rather than sleeping: the reaper reads a
        # clock and nothing else, which is exactly what makes it testable.
        lease.opened_at -= 31

        await book.sweep()

        assert rel.names == ["video.stop_streaming"]
        assert book.stats()["reaped_expired"] == 1

    async def test_a_ttl_of_zero_never_expires(self, rel):
        book = LeaseBook(releaser=rel)
        lease = book.open("video.start_streaming", "video.stop_streaming",
                          ttl_s=0)
        lease.opened_at -= 10 ** 6
        await book.sweep()
        assert rel.names == []


class TestDischarge:
    def test_an_explicit_stop_records_without_re_calling(self, book, rel):
        """The release already ran. Calling it again is the bug this prevents."""
        book.open("video.start_streaming", "video.stop_streaming", owner="hud:a")

        n = book.discharge("video.stop_streaming", owner="hud:a")

        assert n == 1
        assert rel.names == [], "discharge must not invoke the releaser"
        assert book.holders("video.start_streaming") == 0

    def test_it_closes_every_holder_because_the_provider_is_shared(self, book):
        """One principal stopping a singleton stops it for everyone. Say so."""
        book.open("video.start_streaming", "video.stop_streaming", owner="hud:a")
        book.open("video.start_streaming", "video.stop_streaming", owner="hud:b")

        assert book.discharge("video.stop_streaming", owner="hud:a") == 2
        assert book.holders("video.start_streaming") == 0

    async def test_it_skips_a_lease_the_reaper_is_mid_release_on(self, book):
        """The re-entrancy that would deadlock on a non-reentrant per-cap lock."""
        lease = book.open("video.start_streaming", "video.stop_streaming")
        lease.state = LeaseState.RELEASING.value

        assert book.discharge("video.stop_streaming") == 0
        assert lease.state == LeaseState.RELEASING.value

    def test_discharging_an_unheld_release_is_a_no_op(self, book):
        assert book.discharge("video.stop_streaming") == 0
        assert book.discharge("") == 0


class TestReentrancy:
    async def test_a_releaser_that_discharges_does_not_deadlock(self):
        """The REAL shape: reaper -> release -> router -> discharge, same lock.

        The router's `_execute` discharges on any `session-end` call, and the
        reaper's release IS such a call. Without `discharge` skipping RELEASING
        leases this re-enters `_close_lease` and hangs on the capability lock.
        """
        book = LeaseBook()

        async def releaser(name: str, args: Dict[str, Any]) -> bool:
            book.discharge(name)          # what the router does on the way back
            return True

        book.set_releaser(releaser)
        lease = book.open("video.start_streaming", "video.stop_streaming")

        await asyncio.wait_for(book.close(lease.lease_id), timeout=5)

        assert lease.state == LeaseState.CLOSED.value
        assert book.active() == []


class TestIdempotenceAndShutdown:
    async def test_closing_twice_is_a_no_op_that_reports_success(self, book, rel):
        lease = book.open("video.start_streaming", "video.stop_streaming")
        assert await book.close(lease.lease_id) is True
        assert await book.close(lease.lease_id) is True
        assert len(rel.calls) == 1

    async def test_closing_an_unknown_lease_reports_success(self, book):
        assert await book.close("nope") is True

    async def test_release_all_closes_everything(self, book, rel):
        book.open("video.start_streaming", "video.stop_streaming", owner="a")
        book.open("space.start_monitoring", "space.stop_monitoring", owner="b")

        n = await book.release_all()

        assert n == 2
        assert sorted(rel.names) == ["space.stop_monitoring", "video.stop_streaming"]
        assert book.active() == []

    async def test_release_all_leaves_named_orphans_when_it_cannot(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CAPABILITY_RELEASE_ATTEMPTS", "1")
        book = LeaseBook(releaser=_Releases(fail=True))
        book.open("video.start_streaming", "video.stop_streaming")

        await book.release_all()

        assert book.stats()["orphaned_capabilities"] == ["video.start_streaming"]


class TestTheDisabledPath:
    def test_off_means_no_bookkeeping_not_a_crash(self, monkeypatch, book):
        monkeypatch.setenv("JARVIS_CAPABILITY_LEASES_ENABLED", "0")
        assert cl.leases_enabled() is False
        assert book.open("video.start_streaming", "video.stop_streaming") is None
        assert book.active() == []


class TestTheReaperIsIsolated:
    def test_it_reads_only_a_clock_and_the_book(self):
        """The Watchdog Isolation Invariant, enforced on the source.

        A reaper that consults the health of what it guards deadlocks WITH it
        when that thing wedges — which is the state a leaked session is usually
        in. `sweep` must therefore never reach for an orchestrator, a phase, or
        any liveness signal.
        """
        import ast
        import inspect

        src = inspect.getsource(LeaseBook.sweep)
        tree = ast.parse(src.strip())
        called = {
            n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        forbidden = {"get_orchestrator", "get_governed_loop", "phase",
                     "is_healthy", "health", "get_status", "peek_blast"}
        assert not (called & forbidden), f"sweep consults its subject: {called}"
        # Time is read monotonically — wall-clock would let an NTP step reap a
        # session that is thirty seconds old.
        assert "monotonic" in src and "time.time()" not in src


class TestTheSingleton:
    def test_it_is_process_wide_and_resettable(self):
        cl.reset_lease_book()
        a = cl.get_lease_book()
        assert cl.get_lease_book() is a
        cl.reset_lease_book()
        assert cl.get_lease_book() is not a


class TestThePrincipal:
    def test_it_defaults_to_unowned_rather_than_guessing(self):
        assert cl.current_principal() == ""

    async def test_it_is_isolated_per_task(self):
        """Two clients' sessions must never be attributed to each other."""
        seen: Dict[str, str] = {}

        async def act(name: str) -> None:
            cl.set_principal(name)
            await asyncio.sleep(0)
            seen[name] = cl.current_principal()

        await asyncio.gather(act("hud:a"), act("hud:b"))

        assert seen == {"hud:a": "hud:a", "hud:b": "hud:b"}
        # And the caller's own context is untouched by either task.
        assert cl.current_principal() == ""
