"""Trinity sibling bring-up: the organism starts J-Prime and Reactor-Core it needs."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import trinity_siblings as ts


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("JARVIS_PRIME_URL", "JARVIS_JPRIME_START_CMD", "JARVIS_REACTOR_START_CMD",
              "JARVIS_TRINITY_AUTOSTART", "REACTOR_CORE_API_URL", "JARVIS_LOCAL_NUM_CTX"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_NAME", "qwen3-coder-ov:30b")
    monkeypatch.setenv("JARVIS_NUM_CTX_CEILING", "32768")
    monkeypatch.setenv("JARVIS_TRINITY_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_JPRIME_START_BUDGET_S", "5")
    monkeypatch.setenv("JARVIS_REACTOR_START_BUDGET_S", "5")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _run(up, spawned=None, comes_up_after=None, lent=None):
    clock = Clock()
    spawned = spawned if spawned is not None else []

    def probe(url, _timeout):
        if url in up:
            return True
        return comes_up_after is not None and url in comes_up_after and clock.t >= comes_up_after[url]

    def spawn(cmd, values):
        spawned.append((cmd, values))

    lines = []
    # The network is simulated: admission is too (never the live engine).
    out = ts.ensure_siblings(say=lines.append, probe=probe, spawn=spawn, sleep=clock.sleep, clock=clock,
                             lent=lambda sib, base: (lent or {}).get(sib.key, ""))
    return {s.key: s for s in out}, spawned, lines


def test_serving_siblings_are_left_alone():
    st, spawned, _ = _run({"http://127.0.0.1:8000/api/version", "http://127.0.0.1:8090/health"})
    assert st["jprime"].state == "serving" and st["reactor"].state == "serving"
    assert spawned == []


def test_down_sibling_is_started_with_template_filled_from_the_organisms_config(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_START_CMD", "pwsh -File x.ps1 -Preload {model} -Port {port} -Ctx {ctx}")
    st, spawned, _ = _run({"http://127.0.0.1:8090/health"},
                          comes_up_after={"http://127.0.0.1:8000/api/version": 3.0})
    assert st["jprime"].state == "started"
    cmd, values = spawned[0]
    assert values == {"model": "qwen3-coder-ov:30b", "port": "8000", "ctx": "32768"}


def test_sibling_that_never_answers_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("JARVIS_REACTOR_START_CMD", "bash reactor_core.sh start")
    st, spawned, lines = _run({"http://127.0.0.1:8000/api/version"})
    assert st["reactor"].state == "failed" and "within 5s" in st["reactor"].detail
    assert len(spawned) == 1


def test_unconfigured_start_is_reported(monkeypatch):
    st, spawned, lines = _run(set())
    assert st["jprime"].state == "not_configured" and spawned == []
    assert any("(required)" in ln for ln in lines)


def test_jprime_skipped_when_local_lane_off(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "false")
    monkeypatch.setenv("JARVIS_JPRIME_START_CMD", "x")
    st, spawned, _ = _run({"http://127.0.0.1:8090/health"})
    assert st["jprime"].state == "skipped" and spawned == []


def test_prime_url_takes_precedence_like_the_lane_resolver(monkeypatch):
    monkeypatch.setenv("JARVIS_PRIME_URL", "http://10.0.0.5:8000")
    st, _, _ = _run({"http://10.0.0.5:8000/api/version", "http://127.0.0.1:8090/health"})
    assert st["jprime"].url == "http://10.0.0.5:8000"


def test_master_switch_off_does_nothing(monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_AUTOSTART", "false")
    st, spawned, _ = _run(set())
    assert st == {} and spawned == []


def test_spawn_error_never_raises(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_START_CMD", "x")

    def boom(cmd, values):
        raise OSError("no such file")
    out = ts.ensure_siblings(say=lambda _l: None, probe=lambda u, t: u.endswith("/health"),
                             spawn=boom, sleep=lambda s: None, clock=lambda: 0.0)
    assert {s.key: s.state for s in out}["jprime"] == "failed"


def test_boot_calls_bring_up_before_the_lane_gate():
    """The call site is pinned: bring-up must precede the fatal lane gate."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "scripts" / "ouroboros_battle_test.py").read_text()
    assert src.index("ensure_siblings()") < src.index("    _check_api_keys_or_die()\n")


def test_a_lent_mind_is_reported_lent_and_never_restarted():
    st, spawned, lines = _run({"http://127.0.0.1:8000/api/version", "http://127.0.0.1:8090/health"},
                              lent={"jprime": "training cycle handoff-1 in TRAINING"})
    assert st["jprime"].state == "lent" and "handoff-1" in st["jprime"].detail
    assert spawned == [] and any("lent" in ln for ln in lines)
