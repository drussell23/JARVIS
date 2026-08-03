"""The whole path: a HUD tool call reaches a federated subsystem and comes back.

Discovery, hydration and naming are proven next door. This is the part that
actually runs: `execute_tool` → `CapabilityRouter.route` → the right INSTANCE →
the right METHOD → a lease that outlives the call and gets reaped.

The mandated scenario is `test_a_gated_session_suspends_then_executes_and_books`:
`video.start_streaming` is gated (a START is never SAFE_AUTO), so the turn is
released rather than blocked on a human; the operator answers; the call executes
against the video provider; and a lease appears that the reaper can discharge.

THE TESTS TO KEEP
-------------------
`test_an_alias_is_invoked_by_its_method_not_its_export`. `touch.stop_actuator`
is exported under an alias and implemented as `stop`. Calling the export name on
the instance raises `AttributeError` — the collision fix breaking the very calls
it was added to make possible.

`test_the_reaper_release_is_qualified_into_its_namespace`. A tag says
`release=stop_streaming` because that is what its author calls it. Routed
unqualified, that is looked up as a bare macOS capability, misses, and leaves the
session open — a leak whose only symptom is a line in a log nobody is reading.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from backend.system_control import capability_federation as cf
from backend.system_control import capability_leases as cl
from backend.system_control import capability_router as cr
from backend.system_control.capability_federation import CapabilityFederation
from backend.system_control.capability_leases import LeaseBook
from backend.system_control.capability_router import (
    CapabilityRouter,
    DENIED_PAYLOAD,
    Outcome,
)


# ---------------------------------------------------------------------------
# A fake subsystem, hydrated directly rather than through the AST scan (which
# `test_capability_federation` proves separately). This suite is about routing.
# ---------------------------------------------------------------------------


class FakeStreamer:
    """A stand-in video subsystem.

    Capability-Namespace: fakevid
    """

    def __init__(self, vision_analyzer):
        self.vision_analyzer = vision_analyzer
        self.running = False
        self.calls: List[str] = []

    async def start_streaming(self) -> bool:
        """Begin capturing.

        Capability: session-start, release=stop_streaming
        """
        self.calls.append("start_streaming")
        self.running = True
        return True

    async def stop_streaming(self):
        """Stop capturing.

        Capability: session-end
        """
        self.calls.append("stop_streaming")
        self.running = False

    async def stop(self):
        """A stop exported under an alias, like SilentActuator's.

        Capability: session-end, as=stop_aliased
        """
        self.calls.append("stop")

    def get_metrics(self) -> dict:
        """Metrics.

        Capability: read-only
        """
        self.calls.append("get_metrics")
        return {"running": self.running}

    async def analyze(self, query: str) -> str:
        """Analyze something.

        Capability: read-only

        Args:
            query: What to look for
        """
        return f"analyzed:{query}"

    async def explodes(self) -> None:
        """Always fails.

        Capability: read-only
        """
        raise RuntimeError("boom")


class _Provider:
    """The minimal `ApprovalProvider` shape the router needs."""

    def __init__(self, rid: str = "req-1") -> None:
        self.rid = rid
        self.requests: List[Any] = []

    async def request(self, ctx: Any) -> str:
        self.requests.append(ctx)
        return self.rid


def _verdict(nonce: str, approved: bool = True) -> Dict[str, Any]:
    return {"status": "APPROVED" if approved else "DENIED", "nonce": nonce}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("JARVIS_CAPABILITY_ROUTER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CAPABILITY_LEASES_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CAPABILITY_LEASE_GRACE_S", "0")
    cf.reset_federation()
    cf.reset_bindings()
    cl.reset_lease_book()
    cr.reset_capability_router()
    yield
    cf.reset_federation()
    cf.reset_bindings()
    cl.reset_lease_book()
    cr.reset_capability_router()


@pytest.fixture
def fed() -> CapabilityFederation:
    """A federation hydrated with FakeStreamer, no filesystem involved."""
    from backend.system_control.capability_federation import ProviderSpec

    federation = CapabilityFederation([ProviderSpec(
        namespace="fakevid", module=__name__, class_name="FakeStreamer")])
    cf.bind("vision_analyzer", object())
    return federation


@pytest.fixture
def book() -> LeaseBook:
    return LeaseBook()


@pytest.fixture
def router(fed: CapabilityFederation, book: LeaseBook) -> CapabilityRouter:
    return CapabilityRouter(provider=_Provider(), federation=fed, leases=book)


async def _hydrated(fed: CapabilityFederation) -> CapabilityFederation:
    await fed.ensure("fakevid")
    return fed


class TestTheMandatedScenario:
    async def test_a_gated_session_suspends_then_executes_and_books(
            self, router, fed, book):
        """Gate → SUSPEND → operator answers → execute → lease exists."""
        await _hydrated(fed)
        cl.set_principal("hud:a")

        routed = await router.route("video_is_not_this", {})
        assert routed.outcome == Outcome.UNKNOWN_CAPABILITY.value

        routed = await router.route("fakevid.start_streaming", {})

        # The turn is RELEASED, not blocked on a human.
        assert routed.outcome == Outcome.SUSPENDED.value
        assert routed.request_id and routed.nonce
        assert book.active() == [], "booked a session before consent"

        resumed = await router.resume(routed.request_id,
                                      _verdict(routed.nonce))

        assert resumed.outcome == Outcome.EXECUTED.value
        target = fed.resolve_target("fakevid.start_streaming")
        assert target.running is True
        assert target.calls == ["start_streaming"]

        # THE lease: the session outlived its call, and something knows.
        active = book.active()
        assert len(active) == 1
        assert active[0].capability == "fakevid.start_streaming"
        assert active[0].release == "fakevid.stop_streaming"
        assert active[0].owner == "hud:a"
        assert resumed.lease_id == active[0].lease_id
        # And the model is TOLD, or it has no reason to ever stop it.
        assert "SESSION OPEN" in resumed.context_note
        assert "fakevid.stop_streaming" in resumed.context_note


class TestExecutionTargeting:
    async def test_it_executes_against_the_federated_instance(self, router, fed):
        await _hydrated(fed)
        routed = await router.route("fakevid.get_metrics", {})
        assert routed.outcome == Outcome.EXECUTED.value
        assert routed.result == {"running": False}

    async def test_an_alias_is_invoked_by_its_method_not_its_export(
            self, router, fed):
        """The collision fix must not break the calls it enables."""
        await _hydrated(fed)
        assert "fakevid.stop_aliased" in fed.names()
        assert fed.method_for("fakevid.stop_aliased") == "stop"

        routed = await router.route("fakevid.stop_aliased", {})

        assert routed.outcome == Outcome.EXECUTED.value, routed.detail
        assert fed.resolve_target("fakevid.stop_aliased").calls == ["stop"]

    async def test_arguments_are_passed_through(self, router, fed):
        await _hydrated(fed)
        routed = await router.route("fakevid.analyze", {"query": "spaces"})
        assert routed.result == "analyzed:spaces"

    async def test_a_bad_argument_says_so_usefully(self, router, fed):
        """A model inventing an argument deserves better than a bare type name."""
        await _hydrated(fed)
        routed = await router.route("fakevid.analyze", {"nope": 1})
        assert routed.outcome == Outcome.FAILED.value
        assert "declared parameters" in routed.detail

    async def test_a_raising_capability_is_a_result_not_a_crash(self, router, fed):
        await _hydrated(fed)
        routed = await router.route("fakevid.explodes", {})
        assert routed.outcome == Outcome.FAILED.value
        assert "RuntimeError" in routed.detail

    async def test_a_bare_name_still_reaches_the_macos_registry(self, book):
        """Federating must not break the vocabulary that already worked."""

        class _Ctl:
            async def get_battery(self) -> dict:
                """Battery.

                Capability: read-only
                """
                return {"percent": 88}

        from backend.system_control.capability_registry import CapabilityRegistry

        router = CapabilityRouter(
            registry=CapabilityRegistry(_Ctl()).hydrate(),
            target=_Ctl(), leases=book)
        routed = await router.route("get_battery", {})
        assert routed.outcome == Outcome.EXECUTED.value
        assert routed.result == {"percent": 88}


class TestHonestUnknowns:
    async def test_an_unhydrated_namespace_says_so(self, router, fed):
        """UNHYDRATED and ABSENT get different words, or a model gives up early."""
        routed = await router.route("fakevid.get_metrics", {})
        assert routed.outcome == Outcome.UNKNOWN_CAPABILITY.value
        assert "has not hydrated" in routed.detail

    async def test_an_unknown_namespace_lists_the_known_ones(self, router, fed):
        await _hydrated(fed)
        routed = await router.route("nosuchns.thing", {})
        assert "no namespace 'nosuchns'" in routed.detail
        assert "fakevid" in routed.detail

    async def test_an_unbuildable_provider_explains_itself(self, book):
        """Described fine, constructed never — the worst available shape."""
        from backend.system_control.capability_federation import ProviderSpec

        cf.reset_bindings()      # nothing bound: no `vision_analyzer`
        fed = CapabilityFederation([ProviderSpec(
            namespace="fakevid", module=__name__, class_name="FakeStreamer")])
        await fed.ensure("fakevid")
        router = CapabilityRouter(provider=_Provider(), federation=fed,
                                  leases=book)

        routed = await router.route("fakevid.get_metrics", {})

        assert routed.outcome == Outcome.UNKNOWN_CAPABILITY.value
        assert "vision_analyzer" in routed.detail


class TestSessionBookkeeping:
    async def test_an_explicit_stop_discharges_without_double_calling(
            self, router, fed, book):
        await _hydrated(fed)
        started = await router.resume(
            *_consent(await router.route("fakevid.start_streaming", {})))
        assert started.outcome == Outcome.EXECUTED.value
        target = fed.resolve_target("fakevid.start_streaming")

        stopped = await router.route("fakevid.stop_streaming", {})

        assert stopped.outcome == Outcome.EXECUTED.value
        assert book.active() == []
        # Called exactly once — the discharge must not invoke it again.
        assert target.calls == ["start_streaming", "stop_streaming"]

    async def test_a_start_that_reports_failure_opens_no_lease(
            self, router, fed, book, monkeypatch):
        await _hydrated(fed)
        target = fed.resolve_target("fakevid.start_streaming")

        async def _fails() -> bool:
            return False

        monkeypatch.setattr(target, "start_streaming", _fails)
        out = await router.resume(
            *_consent(await router.route("fakevid.start_streaming", {})))

        assert out.outcome == Outcome.EXECUTED.value
        assert out.result is False
        assert book.active() == [], "booked a session that never started"

    async def test_a_denial_executes_nothing_and_books_nothing(
            self, router, fed, book):
        await _hydrated(fed)
        routed = await router.route("fakevid.start_streaming", {})

        denied = await router.resume(routed.request_id,
                                     _verdict(routed.nonce, approved=False))

        assert denied.outcome == Outcome.DENIED.value
        assert denied.context_note == DENIED_PAYLOAD
        assert fed.resolve_target("fakevid.start_streaming").calls == []
        assert book.active() == []

    async def test_a_read_only_capability_is_never_gated(self, router, fed, book):
        await _hydrated(fed)
        routed = await router.route("fakevid.get_metrics", {})
        assert routed.outcome == Outcome.EXECUTED.value
        assert routed.lease_id == ""


class TestTheReaper:
    async def test_the_reaper_release_is_qualified_into_its_namespace(
            self, router, fed, book):
        """An unqualified release misses, and the session stays open forever."""
        await _hydrated(fed)
        cl.set_principal("hud:a")
        await router.resume(
            *_consent(await router.route("fakevid.start_streaming", {})))
        target = fed.resolve_target("fakevid.start_streaming")
        assert target.running is True

        book.set_releaser(router._release)
        book.note_departure("hud:a")
        await book.sweep()

        # The whole point: the reaper actually reached the provider.
        assert target.running is False
        assert target.calls == ["start_streaming", "stop_streaming"]
        assert book.active() == []
        assert book.orphans() == []

    async def test_an_expired_session_is_reaped_through_the_same_gate(
            self, router, fed, book):
        """An END is SAFE_AUTO by RULE, so the reaper needs no bypass."""
        await _hydrated(fed)
        routed = await router.route("fakevid.start_streaming", {})
        out = await router.resume(routed.request_id, _verdict(routed.nonce))
        book.set_releaser(router._release)

        lease = book.active()[0]
        lease.opened_at -= lease.ttl_s + 1
        await book.sweep()

        assert fed.resolve_target("fakevid.start_streaming").running is False
        assert book.active() == []

    async def test_shutdown_releases_what_is_still_open(self, router, fed, book):
        await _hydrated(fed)
        await router.resume(
            *_consent(await router.route("fakevid.start_streaming", {})))
        book.set_releaser(router._release)

        n = await book.release_all()

        assert n == 1
        assert fed.resolve_target("fakevid.start_streaming").running is False


class TestTheDisabledPaths:
    async def test_router_off_executes_without_gating(self, monkeypatch,
                                                      router, fed):
        monkeypatch.setenv("JARVIS_CAPABILITY_ROUTER_ENABLED", "0")
        await _hydrated(fed)
        routed = await router.route("fakevid.start_streaming", {})
        assert routed.outcome == Outcome.EXECUTED.value

    async def test_leases_off_still_gates_and_still_executes(
            self, monkeypatch, router, fed, book):
        """Off means no bookkeeping — never a crash, and never an open gate."""
        monkeypatch.setenv("JARVIS_CAPABILITY_LEASES_ENABLED", "0")
        await _hydrated(fed)
        routed = await router.route("fakevid.start_streaming", {})
        assert routed.outcome == Outcome.SUSPENDED.value
        out = await router.resume(routed.request_id, _verdict(routed.nonce))
        assert out.outcome == Outcome.EXECUTED.value
        assert book.active() == []


class TestTheHudSeam:
    async def test_execute_tool_reaches_a_federated_capability(self, fed,
                                                               monkeypatch, book):
        """`execute_tool`'s else-branch is the seam this all hangs from."""
        from backend.hud.tool_definitions import ToolCall, execute_tool

        await _hydrated(fed)
        router = CapabilityRouter(provider=_Provider(), federation=fed,
                                  leases=book)
        monkeypatch.setattr(cr, "_ROUTER", router)

        result = await execute_tool(ToolCall(
            name="fakevid.analyze", args={"query": "desktops"}, call_id="c1"))

        assert result.success is True
        assert "analyzed:desktops" in result.output

    async def test_a_suspended_call_is_not_reported_as_an_error(
            self, fed, monkeypatch, book):
        """A SUSPENDED call must end the turn cleanly, not look like a failure
        the model should retry with a reworded name."""
        from backend.hud.tool_definitions import ToolCall, execute_tool

        await _hydrated(fed)
        monkeypatch.setattr(cr, "_ROUTER", CapabilityRouter(
            provider=_Provider(), federation=fed, leases=book))

        result = await execute_tool(ToolCall(
            name="fakevid.start_streaming", args={}, call_id="c2"))

        assert result.success is False
        assert "Awaiting operator consent" in result.output

    async def test_the_open_session_note_names_the_stop(self, router, fed, book,
                                                        monkeypatch):
        """Four turns later the prompt must still say the camera is on."""
        from backend.hud import tool_definitions as td

        await _hydrated(fed)
        monkeypatch.setattr(cl, "_BOOK", book)
        assert td.open_sessions_note() == ""

        await router.resume(
            *_consent(await router.route("fakevid.start_streaming", {})))

        note = td.open_sessions_note()
        assert "fakevid.start_streaming" in note
        assert "fakevid.stop_streaming" in note


def _consent(routed: Any) -> tuple:
    """(request_id, verdict) for a SUSPENDED call. Fails loudly if it was not."""
    assert routed.outcome == Outcome.SUSPENDED.value, (
        f"expected a gated call, got {routed.outcome}: {routed.detail}")
    return (routed.request_id, _verdict(routed.nonce))
