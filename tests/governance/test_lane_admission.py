"""Lane admission: an organism never boots into a lane lent to a training cycle.

2026-10-07: J-Prime keeps listing its models (``/api/tags``) while a
Training Lifecycle Handoff holds the card and refuses every generation, so
the boot's lane gate passed and ``ov`` would start into a lane that cannot
serve for hours. And the gate's cloud-key shortcut returned before looking
at the local lane at all whenever a key sat in the environment -- even with
paid lanes switched off -- so no local check could ever run on this host.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.core.ouroboros.cli import thin_client
from backend.core.ouroboros.governance import lane_admission as la
from backend.core.ouroboros.governance import trinity_siblings as ts
from backend.core.ouroboros.governance.observability import training_handoff as th


class _Engine:
    """A real HTTP engine: J-Prime's lease surface, or none (Ollama)."""

    def __init__(self, lease=None, *, has_lease_surface=True, tags=("qwen3-coder-ov:30b",)):
        self.lease = lease if lease is not None else {"state": "serving", "lease": None, "last_error": ""}
        eng = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/v1/lease" and has_lease_surface:
                    return self._json(200, eng.lease)
                if self.path == "/api/tags":
                    return self._json(200, {"models": [{"name": n} for n in tags]})
                if self.path == "/api/version":
                    return self._json(200, {"version": "x"})
                return self._json(404, {"error": "not found"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def _lent(holder="training-handoff:handoff-1@host", state="released", expires_in=600.0):
    return {"state": state, "last_error": "",
            "lease": {"holder": holder, "purpose": "GRPO fine-tune of qwen3-coder-ov:30b",
                      "expires_at": time.time() + expires_in}}


@pytest.fixture
def engine():
    made = []

    def make(*a, **kw):
        e = _Engine(*a, **kw)
        made.append(e)
        return e
    yield make
    for e in made:
        e.close()


@pytest.fixture(autouse=True)
def _fast_probe(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_TRINITY_PROBE_TIMEOUT_S", "0.05")
    monkeypatch.setenv("JARVIS_TRAINING_HANDOFF_DIR", str(tmp_path / "handoff"))


# ------------------------------------------------------------------ the reader

def test_a_serving_engine_admits(engine):
    e = engine()
    assert la.read_admission(e.url, cycle_reader=lambda: None).admitting is True


def test_a_lent_engine_refuses_and_names_its_holder(engine):
    e = engine(_lent())
    adm = la.read_admission(e.url, cycle_reader=lambda: None)
    assert adm.admitting is False and adm.engine_state == "released"
    assert adm.holder.startswith("training-handoff:") and 590 < adm.release_in_s() <= 600


def test_an_engine_with_nothing_to_lend_admits(engine):
    e = engine(has_lease_surface=False)            # plain Ollama: /v1/lease is a 404
    adm = la.read_admission(e.url, cycle_reader=lambda: None)
    assert adm.admitting is True and adm.engine_state == "no_lease_surface"


def test_an_unreachable_engine_is_not_proven_either_way():
    adm = la.read_admission("http://127.0.0.1:9", cycle_reader=lambda: None)
    assert adm.admitting is None                   # reachability has its own owner


def test_verification_after_the_lease_still_holds_the_lane(engine):
    # The lease is back (engine serving) but the cycle is VERIFYING: it uses
    # the served model exclusively and may swap its adapter on rejection.
    e = engine()
    cyc = {"run_id": "handoff-9", "state": "VERIFYING", "holds_lane": True,
           "release_by": time.time() + 1500, "model": "qwen3-coder-ov:30b"}
    adm = la.read_admission(e.url, cycle_reader=lambda: cyc)
    assert adm.admitting is False and 1490 < adm.release_in_s() <= 1500


def test_the_cycle_bound_wins_over_a_renewable_lease_expiry(engine):
    e = engine(_lent(expires_in=300))
    cyc = {"run_id": "h", "state": "TRAINING", "holds_lane": True, "release_by": time.time() + 20000}
    adm = la.read_admission(e.url, cycle_reader=lambda: cyc)
    assert adm.release_in_s() > 19000             # renewals make the lease expiry a floor


def test_a_pre_lease_cycle_does_not_hold_the_lane(engine):
    # BASELINE has not taken the card; the cycle yields at its LEASING
    # re-check to an organism that is live by then.
    e = engine()
    cyc = {"run_id": "h", "state": "BASELINE", "holds_lane": False, "release_by": None}
    assert la.read_admission(e.url, cycle_reader=lambda: cyc).admitting is True


# ------------------------------------------------------------------ waiting

def _seq(*readings):
    it = iter(readings)
    last = {}

    def reader(_url):
        try:
            last["v"] = next(it)
        except StopIteration:
            pass
        return last["v"]
    return reader


def _adm(admitting, in_s=None):
    return la.LaneAdmission(admitting, "restoring" if admitting is False else "serving",
                            lease_expires_at=None if in_s is None else time.time() + in_s)


def test_a_lane_back_within_the_budget_is_waited_for():
    slept = []
    said = []
    out = la.await_admission("u", 120, reader=_seq(_adm(False, 5), _adm(False, 3), _adm(True)),
                             sleep=slept.append, say=said.append)
    assert out.admitting is True and len(slept) == 2 and len(said) == 1


def test_a_lane_lent_for_hours_is_reported_at_once():
    slept = []
    out = la.await_admission("u", 120, reader=_seq(_adm(False, 6 * 3600)), sleep=slept.append, say=lambda s: None)
    assert out.admitting is False and slept == []


def test_a_lane_with_no_known_return_is_reported_at_once():
    out = la.await_admission("u", 120, reader=_seq(_adm(False, None)), sleep=lambda s: None, say=lambda s: None)
    assert out.admitting is False


def test_waiting_never_outlasts_the_budget():
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s
    out = la.await_admission("u", 10, reader=_seq(_adm(False, 8)), sleep=sleep, say=lambda s: None,
                             clock=lambda: t["now"])
    assert out.admitting is False and t["now"] <= 10.0


def test_describe_says_who_why_and_when():
    adm = la.LaneAdmission(False, "released", holder="training-handoff:h@x", purpose="GRPO fine-tune",
                           cycle={"run_id": "handoff-7", "state": "TRAINING", "holds_lane": True,
                                  "model": "qwen3-coder-ov:30b", "trigger": "session_end:bt-1",
                                  "release_by": 1000.0 + 3 * 3600})
    text = "\n".join(la.describe(adm, now=1000.0))
    assert "handoff-7" in text and "TRAINING" in text and "GRPO fine-tune" in text
    assert "no later than" in text and "3h00m" in text and "session_end:bt-1" in text


# ------------------------------------------------------------------ the cycle's own bound

def test_occupancy_bounds_the_rest_of_the_cycle_from_its_own_knobs(monkeypatch):
    monkeypatch.setenv("JARVIS_GRPO_AUTOTRAIN_TIMEOUT_S", "25200")
    monkeypatch.setenv("JARVIS_TRAINING_CONVERT_TIMEOUT_S", "900")
    monkeypatch.setenv("JARVIS_TRAINING_JPRIME_TIMEOUT_S", "60")
    monkeypatch.setenv("JARVIS_TRAINING_SMOKE_TIMEOUT_S", "100")
    monkeypatch.setattr(th, "cycle_alive", lambda: True)
    cyc = th.Cycle(run_id="handoff-x", trigger="session_end:s", model="m")
    th._record(cyc, "TRAINING", argv=["x"])
    since = th._state_entered_at("handoff-x", "TRAINING")
    occ = th.occupancy()
    # TRAINING + CONVERTING + PUBLISHING + RESTORING + VERIFYING(2 x 6 tasks x 100 + 60)
    expected = 25200 + 900 + 60 + 60 + (2 * 6 * 100 + 60)
    assert occ["holds_lane"] is True and occ["state"] == "TRAINING"
    assert occ["release_by"] == pytest.approx(since + expected)


def test_no_live_cycle_means_no_occupancy_even_with_a_stale_state_file(monkeypatch):
    cyc = th.Cycle(run_id="handoff-dead", trigger="t", model="m")
    th._record(cyc, "TRAINING")                    # a crashed cycle's leftover
    assert th.cycle_alive() is False and th.occupancy() is None


# ------------------------------------------------------------------ the gate (the daemon's boot)

@pytest.fixture
def gate(monkeypatch):
    import scripts.ouroboros_battle_test as mod
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")
    monkeypatch.setenv("JARVIS_PAID_LANES_ENABLED", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-present-but-switched-off")
    monkeypatch.setenv("JARVIS_JPRIME_START_BUDGET_S", "0.2")
    return mod


def _point_lane_at(monkeypatch, url):
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_BASE_URL", url)
    monkeypatch.setenv("JARVIS_PRIME_URL", url)


def test_gate_refuses_a_lent_lane_with_ex_unavailable(gate, engine, monkeypatch, capsys):
    e = engine(_lent(expires_in=3 * 3600))
    _point_lane_at(monkeypatch, e.url)
    with pytest.raises(SystemExit) as ei:
        gate._check_api_keys_or_die()
    assert ei.value.code == la.EXIT_LANE_LENT == 69
    out = capsys.readouterr().out
    assert "lent out" in out and "training-handoff:" in out and "back" in out


def test_gate_passes_a_serving_lane(gate, engine, monkeypatch, capsys):
    e = engine()
    _point_lane_at(monkeypatch, e.url)
    gate._check_api_keys_or_die()
    assert "LOCAL J-Prime" in capsys.readouterr().out


def test_a_switched_off_key_no_longer_skips_the_local_check(gate, engine, monkeypatch):
    # The pre-fix gate returned on ANTHROPIC_API_KEY alone, never reaching here.
    e = engine(_lent(expires_in=3 * 3600))
    _point_lane_at(monkeypatch, e.url)
    with pytest.raises(SystemExit):
        gate._check_api_keys_or_die()


def test_an_allowed_paid_lane_still_carries_the_boot(gate, engine, monkeypatch):
    e = engine(_lent(expires_in=3 * 3600))
    _point_lane_at(monkeypatch, e.url)
    monkeypatch.setattr(gate, "_usable_paid_lanes", lambda: ["claude"])
    gate._check_api_keys_or_die()                  # a lent local lane is irrelevant then


# ------------------------------------------------------------------ the sibling line and the client

def test_sibling_bring_up_reports_lent_not_serving(engine, monkeypatch):
    e = engine(_lent())
    _point_lane_at(monkeypatch, e.url)
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")
    monkeypatch.setenv("REACTOR_CORE_API_URL", e.url)       # answers /health? no: 404 -> not serving
    monkeypatch.delenv("JARVIS_REACTOR_START_CMD", raising=False)
    said = []
    out = ts.ensure_siblings(say=said.append, spawn=lambda *a: pytest.fail("never restart a lent engine"))
    jp = [s for s in out if s.key == "jprime"][0]
    assert jp.state == "lent" and "training-handoff:" in jp.detail


def test_client_and_daemon_agree_on_the_exit_code():
    assert thin_client.EXIT_LANE_LENT == la.EXIT_LANE_LENT


def test_client_renders_the_holder_and_return_time(engine, monkeypatch):
    e = engine(_lent(expires_in=2 * 3600))
    _point_lane_at(monkeypatch, e.url)
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")
    lines = thin_client._lane_lent_lines()
    assert lines[0].startswith("⚠ the organism declined to start")
    assert any("training-handoff:" in ln for ln in lines) and any("back" in ln for ln in lines)


def test_only_an_error_of_the_current_lease_is_repeated(engine):
    stale = _lent()
    stale["lease"]["acquired_at"] = time.time() - 60
    stale.update(last_error="restore: llama-server exited rc=1", last_error_at=time.time() - 3600)
    assert la.read_admission(engine(stale).url, cycle_reader=lambda: None).detail == ""
    current = _lent()
    current["lease"]["acquired_at"] = time.time() - 60
    current.update(last_error="acquire failed: card busy", last_error_at=time.time() - 5)
    assert "card busy" in la.read_admission(engine(current).url, cycle_reader=lambda: None).detail
    undated = _lent()
    undated.update(last_error="from an engine that does not date its errors")
    assert la.read_admission(engine(undated).url, cycle_reader=lambda: None).detail == ""
