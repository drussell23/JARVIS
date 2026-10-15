"""The pre-flight checklist: exercised before "ready", reported the same way
everywhere.

2026-10-14: "ready" stood on the backend's /health, the loading page and the
startup lock; nothing the user relies on was exercised, and the loading page
said "JARVIS is online!" whatever the boot found. The first live run of the
checklist then caught the router's 2 s status probe timing out under boot
load and routing a live model as "down".
"""
from __future__ import annotations

import asyncio
import types

import pytest

from backend.core import flight_check as fc


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for name in ("JARVIS_FLIGHT_CHECK_ENABLED", "JARVIS_FLIGHT_CHECK_TIMEOUT_S",
                 "JARVIS_FLIGHT_CHECK_REQUIRED", "JARVIS_FLIGHT_CHECK_SKIP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(fc, "_REGISTRY", {})
    monkeypatch.setattr(fc, "_builtin_registered", True)   # only the items a test adds


def _item(key, status=fc.Status.GO, *, required=False, delay=0.0, raises=None, plural=False):
    async def probe(_limit):
        await asyncio.sleep(delay)
        if raises:
            raise raises
        return fc.Outcome(status, f"{key} detail", "fix it" if status is not fc.Status.GO else "")
    return fc.Item(key, key.title(), f"my {key}", probe, required=required, plural=plural)


# ---------------------------------------------------------------- running
def test_items_run_concurrently_and_each_within_its_limit(monkeypatch):
    monkeypatch.setenv("JARVIS_FLIGHT_CHECK_TIMEOUT_S", "0.5")
    report = asyncio.run(fc.run([_item("a", delay=0.3), _item("b", delay=0.3),
                                 _item("slow", delay=5.0, required=True)]))
    by = {r.item.key: r for r in report.results}
    assert by["a"].ok and by["b"].ok
    assert by["slow"].outcome.status is fc.Status.NO_GO
    assert by["slow"].outcome.detail == "did not answer within 0.5s"
    assert report.elapsed_s < 1.5                       # concurrent, not 5.6 s


def test_a_probe_that_raises_or_answers_nonsense_is_a_failure_never_a_pass():
    async def nonsense(_limit):
        return "fine"
    report = asyncio.run(fc.run([_item("boom", raises=RuntimeError("engine gone")),
                                 fc.Item("odd", "Odd", "odd", nonsense)]))
    by = {r.item.key: r.outcome for r in report.results}
    assert by["boom"].status is fc.Status.NO_GO and "engine gone" in by["boom"].detail
    assert by["odd"].status is fc.Status.NO_GO


def test_the_verdict_holds_readiness_only_for_a_required_item():
    ready = asyncio.run(fc.run([_item("a", required=True), _item("b")]))
    limited = asyncio.run(fc.run([_item("a", required=True), _item("b", fc.Status.LIMITED)]))
    held = asyncio.run(fc.run([_item("a", fc.Status.NO_GO, required=True), _item("b")]))
    assert (ready.verdict, limited.verdict, held.verdict) == ("ready", "limited", "not_ready")
    assert held.blocking == ["a"] and limited.degraded == ["b"]


def test_the_operator_decides_what_is_required_and_what_is_skipped(monkeypatch):
    monkeypatch.setenv("JARVIS_FLIGHT_CHECK_REQUIRED", "b")
    monkeypatch.setenv("JARVIS_FLIGHT_CHECK_SKIP", "c")
    report = asyncio.run(fc.run([_item("a", fc.Status.NO_GO, required=True),
                                 _item("b", fc.Status.NO_GO), _item("c", fc.Status.NO_GO)]))
    assert report.blocking == ["b"] and report.degraded == ["a"]
    skipped = next(r for r in report.results if r.item.key == "c")
    assert skipped.outcome.status is fc.Status.SKIPPED and skipped.ok


def test_settings_typos_fall_back(monkeypatch):
    monkeypatch.setenv("JARVIS_FLIGHT_CHECK_TIMEOUT_S", "soon")
    assert fc.item_timeout_s() == 30.0
    monkeypatch.setenv("JARVIS_FLIGHT_CHECK_ENABLED", "off")
    assert fc.enabled() is False


# ------------------------------------------------------------- reporting
def test_the_report_is_said_and_shown_the_same_way():
    report = asyncio.run(fc.run([
        _item("mind", fc.Status.NO_GO, required=True), _item("google", fc.Status.LIMITED),
        _item("abilities", fc.Status.LIMITED, plural=True), _item("voice")]))
    assert report.issues_spoken() == ["my mind is not working", "my google needs attention",
                                      "my abilities need attention"]
    assert report.summary() == "1 of 4 checks passed; not ready: Mind, Google, Abilities"
    lines = fc.lines(report)
    assert ("failed", "Mind -- mind detail") == lines[0][:2]
    assert ("detail", "fix it", "") == lines[1]
    assert lines[2][1].startswith("Google (optional)")
    payload = report.as_dict()
    assert payload["verdict"] == "not_ready" and payload["items"][0]["remedy"] == "fix it"


def test_a_health_reading_becomes_an_item_of_the_same_report():
    from backend.core.supervisor.elite_dashboard import RepoHealth
    online = RepoHealth("jarvis", "JARVIS (Body)", "ONLINE", detail="200 OK")
    down = RepoHealth("reactor", "Reactor-Core (Nerves)", "OFFLINE")
    report = asyncio.run(fc.run([fc.health_item(online, required=True),
                                 fc.health_item(down, required=False)]))
    by = {r.item.key: r for r in report.results}
    assert by["service:jarvis"].ok and by["service:jarvis"].item.title == "JARVIS"
    assert by["service:reactor"].outcome.status is fc.Status.NO_GO
    assert report.verdict == "limited"


# ------------------------------------------------------------- the probes
def test_the_mind_must_really_answer(monkeypatch):
    from backend.core import prime_router as pr

    class _Router:
        def __init__(self, response):
            self.response = response

        async def generate(self, **_kw):
            return self.response

    good = pr.RouterResponse(content="ready", source="local_prime", latency_ms=600.0, model="probe")
    monkeypatch.setattr(pr, "get_prime_router", lambda: _async(_Router(good)))
    outcome = asyncio.run(fc._probe_mind(5.0))
    assert outcome.status is fc.Status.GO and outcome.detail == "local_prime answered in 0.6s"

    def degraded():
        raise pr.GenerationUnavailable("No backend available")
    monkeypatch.setattr(pr, "require_generation", lambda _r: degraded())
    with pytest.raises(pr.GenerationUnavailable):
        asyncio.run(fc._probe_mind(5.0))                # run() records it as NO_GO


def _async(value):
    async def wrapped():
        return value
    return wrapped()


def test_the_voice_is_rendered_silently_or_called_out(monkeypatch):
    from backend.core import trinity_voice_coordinator as tvc
    from backend.core import voice_mute
    monkeypatch.setattr(voice_mute, "voice_muted", lambda: False)

    async def renders(text, **_kw):
        return tvc.RenderedSpeech(b"mp3", "audio/mpeg", "edge_tts")

    async def nothing(text, **_kw):
        return None

    monkeypatch.setattr(tvc, "render_speech", renders)
    assert asyncio.run(fc._probe_voice(5.0)).status is fc.Status.GO
    monkeypatch.setattr(tvc, "render_speech", nothing)
    assert asyncio.run(fc._probe_voice(5.0)).status is fc.Status.NO_GO
    monkeypatch.setattr(voice_mute, "voice_muted", lambda: True)
    assert asyncio.run(fc._probe_voice(5.0)).status is fc.Status.LIMITED


def test_google_not_signed_in_says_how_to_sign_in(monkeypatch):
    from backend.neural_mesh.agents import google_workspace_agent as g
    monkeypatch.setattr(g, "GOOGLE_API_AVAILABLE", True)

    class _Client:
        auth_state = g.AuthState.UNAUTHENTICATED

        def __init__(self, _config):
            pass

        async def authenticate(self, interactive=False):
            assert interactive is False                  # never opens a browser at boot
            return False

    monkeypatch.setattr(g, "GoogleWorkspaceClient", _Client)
    outcome = asyncio.run(fc._probe_google(5.0))
    assert outcome.status is fc.Status.LIMITED and g.OAUTH_SETUP_COMMAND in outcome.remedy


def test_each_google_service_is_read_and_reported_on_its_own():
    from backend.neural_mesh.agents.google_workspace_agent import verify_workspace

    class _Client:
        async def fetch_unread_emails(self, limit):
            return {"count": 3, "emails": []}

        async def get_calendar_events(self, days):
            return {"error": "calendar API disabled"}

        async def get_contacts(self, limit):
            raise TimeoutError("people API slow")

    assert asyncio.run(verify_workspace(_Client())) == [
        ("Gmail", True, "3 unread"),
        ("Calendar", False, "calendar API disabled"),
        ("Contacts", False, "TimeoutError: people API slow"),
    ]


def test_an_ability_for_another_platform_is_not_a_fault(monkeypatch):
    from backend.core import agent_bindings as ab
    here = types.SimpleNamespace(platform_reason=lambda: "", missing_requirements=lambda: [])
    elsewhere = types.SimpleNamespace(platform_reason=lambda: "runs on darwin",
                                      missing_requirements=lambda: [])
    unset = types.SimpleNamespace(platform_reason=lambda: "", missing_requirements=lambda: ["playwright"])
    monkeypatch.setattr(ab, "get_agent_bindings", lambda: {"a": here, "mac": elsewhere})
    assert asyncio.run(fc._probe_abilities(5.0)).status is fc.Status.GO
    monkeypatch.setattr(ab, "get_agent_bindings", lambda: {"a": here, "mac": elsewhere, "web": unset})
    outcome = asyncio.run(fc._probe_abilities(5.0))
    assert outcome.status is fc.Status.LIMITED and "web (needs playwright)" in outcome.detail


# --------------------------------------------- the router's status probe
def test_a_busy_model_is_not_routed_as_down(monkeypatch):
    """The status probe shares the request's deadline: a 2.5 s health answer
    under load resolves the model as available instead of "down"."""
    import time
    from backend.core import prime_router as pr

    class _Status:
        name = "UNKNOWN"

    client = types.SimpleNamespace(_status=_Status())

    async def slow_health():
        await asyncio.sleep(2.5)
        client._status = types.SimpleNamespace(name="AVAILABLE")

    client._check_health = slow_health
    router = pr.PrimeRouter.__new__(pr.PrimeRouter)
    router._prime_client = client
    asyncio.run(router._resolve_unknown_prime_status(time.monotonic() + 10))
    assert client._status.name == "AVAILABLE"


# --------------------------------------------------- the kernel's checklist
def test_the_kernel_calls_out_the_checklist_and_sends_it_to_the_loading_page(monkeypatch):
    import unified_supervisor as us
    from backend.core import console_view
    from backend.core.supervisor.elite_dashboard import RepoHealth

    said, sent = [], []
    monkeypatch.setattr(console_view, "narrate",
                        lambda text, tone="note", **kw: said.append((tone, text)))
    fc.register(_item("voice", required=True))
    kernel = types.SimpleNamespace(
        logger=types.SimpleNamespace(debug=lambda *_: None, warning=lambda *_: None),
        _current_startup_progress=96,
        _mark_startup_activity=lambda _what: None,
    )

    async def measure(self):
        return [RepoHealth("jarvis", "JARVIS (Body)", "ONLINE", detail="healthy"),
                RepoHealth("reactor", "Reactor-Core (Nerves)", "OFFLINE")]

    async def broadcast(self, **kw):
        sent.append(kw)

    kernel._measure_component_health = types.MethodType(measure, kernel)
    kernel._safe_broadcast = types.MethodType(broadcast, kernel)
    report = asyncio.run(us.JarvisSystemKernel._run_flight_check(kernel))

    assert report.verdict == "limited" and report.degraded == ["service:reactor"]
    assert said[0] == ("note", "Pre-flight checklist")
    assert ("failed", "Reactor-Core (optional) -- offline") in said
    assert said[-1][1].startswith("Checklist limited: 2 of 3 checks passed")
    assert sent[0]["stage"] == "preflight_checklist"
    assert sent[0]["metadata"]["checklist"]["verdict"] == "limited"


def test_disabled_the_kernel_claims_nothing_from_a_checklist(monkeypatch):
    import unified_supervisor as us
    monkeypatch.setenv("JARVIS_FLIGHT_CHECK_ENABLED", "false")
    kernel = types.SimpleNamespace(_mark_startup_activity=lambda _what: None)
    assert asyncio.run(us.JarvisSystemKernel._run_flight_check(kernel)) is None
