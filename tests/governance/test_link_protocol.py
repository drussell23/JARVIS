"""Regression spine for the Body/Engine link protocol.

Pure logic under a controllable clock — no sockets, no sleeps, no network.
Every test states the distributed failure it prevents.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import link_protocol as lp


class FakeClock:
    """A monotonic clock the test drives, so no test ever sleeps."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ---------------------------------------------------------------------------
# 1. Clock drift — the Mac and the WSL2 VM do not share "now"
# ---------------------------------------------------------------------------


def test_causal_order_survives_a_peer_whose_wall_clock_is_wrong():
    """THE drift regression. A WSL2 VM resumes from host sleep with a wall
    clock seconds behind. Causal order must be unaffected because no wall
    clock participates in it."""
    body = lp.LogicalClock("body")
    engine = lp.LogicalClock("engine")

    a = body.tick()                    # Body sends
    b = engine.observe(a)              # Engine receives, then acts
    c = engine.tick()
    d = body.observe(c)                # Body receives the reply

    assert a < b < c < d


def test_a_lamport_clock_never_moves_backwards():
    """Exactly what a re-syncing NTP daemon does to a wall clock, and the
    reason ordering may not depend on one."""
    c = lp.LogicalClock("n")
    seen = [c.tick() for _ in range(5)]
    c.observe(2)                       # a peer far behind
    seen.append(c.peek())
    c.observe(10_000)                  # a peer far ahead
    seen.append(c.peek())
    assert seen == sorted(seen)


def test_a_malformed_remote_stamp_cannot_stall_local_ordering():
    c = lp.LogicalClock("n")
    before = c.peek()
    for junk in (None, "abc", -5, float("nan")):
        c.observe(junk)  # type: ignore[arg-type]
    assert c.peek() > before


def test_stamps_totally_order_for_display_without_any_wall_clock():
    a = (7, "body")
    b = (7, "engine")
    assert lp.happens_before(a, b)
    assert not lp.happens_before(b, a)


def test_the_wall_clock_field_is_named_to_make_misuse_obvious():
    """It is display-only. The name is the guardrail; this pins it so a
    rename cannot quietly turn it into a decision input."""
    f = lp.LinkFrame(seq=1, lamport=1, node_id="n", kind="k")
    assert hasattr(f, "sender_wall_ns")
    assert "sender_wall_ns" in f.to_dict()


# ---------------------------------------------------------------------------
# Frame parsing — a malformed frame is garbage, not a frame with defaults
# ---------------------------------------------------------------------------


def test_a_frame_missing_ordering_fields_is_rejected_not_defaulted():
    """Admitting it with seq=0 would corrupt resume arithmetic on an
    otherwise healthy link."""
    assert lp.LinkFrame.from_dict({"kind": "x"}) is None
    assert lp.LinkFrame.from_dict({"seq": 1, "lamport": 1}) is None
    assert lp.LinkFrame.from_dict({"seq": -1, "lamport": 1,
                                   "node_id": "n", "kind": "k"}) is None
    assert lp.LinkFrame.from_dict("not a dict") is None
    assert lp.LinkFrame.from_dict(None) is None


def test_a_wellformed_frame_round_trips():
    f = lp.LinkFrame(seq=3, lamport=9, node_id="engine", kind="verdict",
                     payload={"command_id": "c1"})
    back = lp.LinkFrame.from_dict(f.to_dict())
    assert back is not None
    assert (back.seq, back.lamport, back.node_id, back.kind) == (3, 9, "engine", "verdict")


def test_a_non_dict_payload_degrades_to_empty_rather_than_failing():
    got = lp.LinkFrame.from_dict(
        {"seq": 1, "lamport": 1, "node_id": "n", "kind": "k", "payload": "junk"})
    assert got is not None and got.payload == {}


# ---------------------------------------------------------------------------
# 2. Flapping — 50 reconnects in 10s must not exhaust the Engine
# ---------------------------------------------------------------------------


def test_a_reconnect_storm_is_throttled_before_resources_are_committed(monkeypatch):
    monkeypatch.setenv("JARVIS_LINK_FLAP_MAX_ATTEMPTS", "8")
    monkeypatch.setenv("JARVIS_LINK_FLAP_WINDOW_S", "10")
    clock = FakeClock()
    breaker = lp.FlapBreaker(clock=clock)

    admitted = sum(1 for _ in range(50) if breaker.admit("mac").admitted)
    assert admitted <= 8, "the storm reached the Engine"
    assert breaker.snapshot()["tripped_total"] >= 1


def test_a_throttled_client_is_told_how_long_to_wait(monkeypatch):
    """A cooperative peer converges; guessing produces a second storm."""
    monkeypatch.setenv("JARVIS_LINK_FLAP_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("JARVIS_LINK_FLAP_OPEN_S", "15")
    breaker = lp.FlapBreaker(clock=FakeClock())
    for _ in range(5):
        d = breaker.admit("mac")
    assert not d.admitted
    assert d.retry_after_s > 0


def test_the_breaker_reopens_after_its_window(monkeypatch):
    monkeypatch.setenv("JARVIS_LINK_FLAP_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("JARVIS_LINK_FLAP_OPEN_S", "15")
    clock = FakeClock()
    breaker = lp.FlapBreaker(clock=clock)
    for _ in range(5):
        breaker.admit("mac")
    assert not breaker.admit("mac").admitted
    clock.advance(16.0)
    assert breaker.admit("mac").admitted


def test_a_stable_connection_retires_its_flap_history(monkeypatch):
    """Otherwise a client reconnecting once an hour is eventually throttled
    for being long-lived — the opposite of the intent."""
    monkeypatch.setenv("JARVIS_LINK_FLAP_MAX_ATTEMPTS", "3")
    breaker = lp.FlapBreaker(clock=FakeClock())
    for _ in range(3):
        breaker.admit("mac")
    breaker.on_established("mac")
    assert breaker.admit("mac").admitted


def test_one_noisy_identity_does_not_throttle_another(monkeypatch):
    monkeypatch.setenv("JARVIS_LINK_FLAP_MAX_ATTEMPTS", "2")
    breaker = lp.FlapBreaker(clock=FakeClock())
    for _ in range(10):
        breaker.admit("noisy")
    assert breaker.admit("quiet").admitted


def test_the_breaker_is_bounded_under_many_identities():
    breaker = lp.FlapBreaker(clock=FakeClock())
    for i in range(600):
        breaker.admit(f"id-{i}")
    assert breaker.snapshot()["tracked_identities"] <= 512


def test_backoff_is_bounded_and_monotone(monkeypatch):
    monkeypatch.setenv("JARVIS_LINK_BACKOFF_BASE_S", "0.5")
    monkeypatch.setenv("JARVIS_LINK_BACKOFF_MAX_S", "30")
    delays = [lp.backoff_delay_s(n) for n in range(0, 12)]
    assert delays == sorted(delays)
    assert max(delays) <= 30.0
    assert lp.backoff_delay_s(999) <= 30.0


def test_backoff_jitter_is_a_parameter_so_schedules_are_reproducible():
    a = lp.backoff_delay_s(3, jitter=0.0)
    assert lp.backoff_delay_s(3, jitter=0.0) == a
    assert lp.backoff_delay_s(3, jitter=0.4) > a


# ---------------------------------------------------------------------------
# 3. Split-brain — the verdict fired, the socket ruptured
# ---------------------------------------------------------------------------


def test_a_verdict_lost_in_transmission_is_replayed_on_reconnect():
    """THE split-brain regression. The Engine emitted seq 42; the Body never
    applied it. Reconnection must ask for exactly the gap."""
    plan = lp.plan_resume(peer_last_applied=41, source_latest=42,
                          source_oldest_retained=1)
    assert plan.action is lp.ResumeAction.REPLAY
    assert (plan.from_seq, plan.to_seq) == (42, 42)


def test_a_current_peer_replays_nothing():
    plan = lp.plan_resume(peer_last_applied=42, source_latest=42,
                          source_oldest_retained=1)
    assert plan.action is lp.ResumeAction.CONTINUE


def test_a_gap_older_than_retention_demands_resync_not_a_partial_replay():
    """Resuming from the oldest retained record would SILENTLY drop
    everything before it — the worst shape a data-loss bug can take."""
    plan = lp.plan_resume(peer_last_applied=5, source_latest=900,
                          source_oldest_retained=400)
    assert plan.action is lp.ResumeAction.RESYNC
    assert "evicted" in plan.reason


def test_an_incoherent_peer_is_rejected_not_served():
    """Claiming to have applied more than the Engine emitted means a stale
    session id after a restart, or two Bodies sharing one identity."""
    plan = lp.plan_resume(peer_last_applied=99, source_latest=10,
                          source_oldest_retained=1)
    assert plan.action is lp.ResumeAction.REJECT


def test_a_fresh_peer_gets_everything_retained():
    plan = lp.plan_resume(peer_last_applied=0, source_latest=5,
                          source_oldest_retained=1)
    assert plan.action is lp.ResumeAction.REPLAY
    assert (plan.from_seq, plan.to_seq) == (1, 5)


# ---------------------------------------------------------------------------
# Idempotent application — exactly-once EFFECT
# ---------------------------------------------------------------------------


def _frame(seq, cid=None):
    return lp.LinkFrame(seq=seq, lamport=seq, node_id="engine", kind="verdict",
                        payload={"command_id": cid} if cid else {})


def test_a_redelivered_verdict_is_applied_exactly_once():
    ledger = lp.DeliveryLedger()
    applied = []
    f = _frame(1, "cmd-a")
    assert ledger.apply_once(f, applied.append) is True
    assert ledger.apply_once(f, applied.append) is False
    assert len(applied) == 1
    assert ledger.snapshot()["duplicates_suppressed"] == 1


def test_a_handler_that_raises_leaves_the_frame_unapplied_for_retry():
    """Recording first and running second would convert a transient fault
    into permanent silent loss."""
    ledger = lp.DeliveryLedger()

    def _boom(_f):
        raise RuntimeError("sink down")

    with pytest.raises(RuntimeError):
        ledger.apply_once(_frame(1, "cmd-a"), _boom)

    applied = []
    assert ledger.apply_once(_frame(1, "cmd-a"), applied.append) is True
    assert len(applied) == 1


def test_the_watermark_is_contiguous_not_highest_seen():
    """THE subtle one. Tracking the highest value would advance past a hole,
    the resume would never ask for it, and the loss would be permanent and
    invisible."""
    ledger = lp.DeliveryLedger()
    ledger.apply_once(_frame(1), lambda f: None)
    ledger.apply_once(_frame(3), lambda f: None)      # 2 is missing
    assert ledger.contiguous_applied == 1
    ledger.apply_once(_frame(2), lambda f: None)      # hole fills
    assert ledger.contiguous_applied == 3


def test_the_watermark_drives_a_correct_resume_after_a_hole():
    ledger = lp.DeliveryLedger()
    for s in (1, 2, 4, 5):
        ledger.apply_once(_frame(s), lambda f: None)
    plan = lp.plan_resume(peer_last_applied=ledger.contiguous_applied,
                          source_latest=5, source_oldest_retained=1)
    assert plan.action is lp.ResumeAction.REPLAY
    assert plan.from_seq == 3


def test_the_ledger_is_bounded():
    ledger = lp.DeliveryLedger(capacity=64)
    for i in range(1, 500):
        ledger.apply_once(_frame(i, f"c{i}"), lambda f: None)
    assert ledger.snapshot()["tracked_ids"] <= 64


# ---------------------------------------------------------------------------
# Sessions outlive sockets — a Wi-Fi drop is a PAUSE, not a loss
# ---------------------------------------------------------------------------


def test_a_reconnect_resumes_rather_than_allocating():
    """The root-cause fix for flapping: if a reconnect costs no state, fifty
    reconnects are a latency problem, not a resource one."""
    reg = lp.SessionRegistry(clock=FakeClock())
    s1, resumed1 = reg.attach("sess-1", "body")
    s1.ledger.apply_once(_frame(1, "c1"), lambda f: None)
    reg.detach("sess-1")
    s2, resumed2 = reg.attach("sess-1", "body")
    assert resumed1 is False and resumed2 is True
    assert s2 is s1
    assert s2.ledger.contiguous_applied == 1
    assert s2.reconnects == 1


def test_fifty_reconnects_allocate_one_session():
    reg = lp.SessionRegistry(clock=FakeClock())
    for _ in range(50):
        reg.attach("sess-1", "body")
        reg.detach("sess-1")
    assert reg.snapshot()["sessions"] == 1


def test_a_detached_session_survives_a_wifi_drop(monkeypatch):
    monkeypatch.setenv("JARVIS_LINK_SESSION_EXPIRY_S", "900")
    clock = FakeClock()
    reg = lp.SessionRegistry(clock=clock)
    reg.attach("sess-1", "body")
    reg.detach("sess-1")
    clock.advance(300.0)
    _, resumed = reg.attach("sess-1", "body")
    assert resumed is True, "a 5-minute outage lost the session"


def test_a_long_dead_session_is_reaped_lazily(monkeypatch):
    monkeypatch.setenv("JARVIS_LINK_SESSION_EXPIRY_S", "60")
    clock = FakeClock()
    reg = lp.SessionRegistry(clock=clock)
    reg.attach("sess-1", "body")
    reg.detach("sess-1")
    clock.advance(120.0)
    _, resumed = reg.attach("sess-2", "body")   # any access expires
    assert reg.snapshot()["sessions"] == 1


def test_an_attached_session_is_never_reaped(monkeypatch):
    monkeypatch.setenv("JARVIS_LINK_SESSION_EXPIRY_S", "1")
    clock = FakeClock()
    reg = lp.SessionRegistry(clock=clock)
    reg.attach("sess-1", "body")
    clock.advance(10_000.0)
    reg.attach("sess-2", "body")
    assert reg.snapshot()["attached"] >= 1


# ---------------------------------------------------------------------------
# Posture
# ---------------------------------------------------------------------------


def test_default_off_pending_graduation():
    assert lp.is_enabled() is False


def test_the_module_holds_no_transport():
    """Pure logic: correctness must be provable at a desk, not by running
    two machines."""
    import pathlib
    src = pathlib.Path(lp.__file__).read_text(encoding="utf-8")
    for banned in ("import socket", "import asyncio", "aiohttp", "requests"):
        assert banned not in src, banned


def test_no_hardcoded_endpoints():
    import pathlib
    import re
    src = pathlib.Path(lp.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", src), "hardcoded IP"
    assert not re.search(r":\d{4,5}\b", src), "hardcoded port"
