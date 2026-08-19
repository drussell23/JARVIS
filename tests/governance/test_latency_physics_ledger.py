"""The Amnesia Cure: cross-run latency-physics persistence.

The LatencyProfiler learns the node's TRUE round physics (EWMA ~800s/round on
the L4 vs the 240s cold seed -- a 3.4x underestimate) and forgets ALL of it at
process exit; every ignition re-learns from scratch and usually dies of the
miscalibration it was correcting (operator had to hand-feed measured seeds).

Cure (bandit_router ledger idiom -- .jarvis/, write_text/read_text, fail-soft,
NEVER raises into the dispatch path): a keyed physics ledger at
``.jarvis/latency_physics.json``. KEY = (model, ctx-bucket) -- NOT endpoint:
node IPs change every run; the physics belongs to the brain+window, not the
address. Profiler warm-starts from the persisted prior; record()/penalty
write through; the cold-path cycle formula reads the same prior so run N+1's
floors/walls/plans are sized from run N's MEASURED truth at second zero.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

import backend.core.ouroboros.governance.local_inference_director as lid


def _cfg(**over):
    return dataclasses.replace(lid.LocalConfig.from_env(), **over)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "latency_physics.json"
    monkeypatch.setenv("JARVIS_LATENCY_LEDGER_PATH", str(p))
    return p


class TestLedgerBasics:
    def test_physics_key_carries_model_and_ctx_but_never_the_address(self):
        """The original contract, minus the exact-string assertion.

        The key still ends in model@ctx and still must not contain a port or
        an address -- node IPs change every run. What was added in front is a
        HARDWARE axis; see the host-isolation tests below for why.
        """
        cfg = _cfg(model_name="qwen2.5-coder:32b", num_ctx=16640)
        key = lid.physics_key(cfg)
        assert key.endswith("qwen2.5-coder:32b@16640")
        assert _cfg(model_name="qwen2.5-coder:3b", num_ctx=0) and \
            lid.physics_key(_cfg(model_name="qwen2.5-coder:3b",
                                 num_ctx=0)).endswith("qwen2.5-coder:3b@cpu")
        # the address must not survive into the identity
        assert "11434" not in key and "127.0.0.1" not in key

    def test_two_machines_do_not_share_one_ledger_key(self):
        """The defect this axis exists for.

        Same model name, two hosts. Under the old key they wrote the SAME
        entry, so a 16GB M1 that measured ~95ms/token would size an RTX
        5090's lane count -- the ThroughputGovernor computing a wrong answer
        from honest data.
        """
        from backend.core.ouroboros.governance import hardware_signature as hs
        hs.reset_for_tests()
        local = lid.physics_key(_cfg(model_name="m", num_ctx=8192,
                                     base_url="http://127.0.0.1:11434"))
        remote = lid.physics_key(_cfg(model_name="m", num_ctx=8192,
                                      base_url="http://192.168.1.50:11434"))
        assert local != remote
        assert local.endswith("m@8192") and remote.endswith("m@8192")

    def test_one_machine_serving_two_ports_is_one_machine(self):
        """A port is not a piece of hardware. Splitting the ledger by port
        would re-introduce the amnesia it exists to cure."""
        from backend.core.ouroboros.governance import hardware_signature as hs
        hs.reset_for_tests()
        a = lid.physics_key(_cfg(model_name="m", num_ctx=8192,
                                 base_url="http://192.168.1.50:11434"))
        b = lid.physics_key(_cfg(model_name="m", num_ctx=8192,
                                 base_url="http://192.168.1.50:8080"))
        assert a == b

    def test_the_flag_off_restores_the_legacy_shape_byte_for_byte(self,
                                                                  monkeypatch):
        from backend.core.ouroboros.governance import hardware_signature as hs
        monkeypatch.setenv("JARVIS_HARDWARE_SIGNATURE_ENABLED", "0")
        hs.reset_for_tests()
        assert lid.physics_key(
            _cfg(model_name="qwen2.5-coder:32b", num_ctx=16640)
        ) == "qwen2.5-coder:32b@16640"

    def test_corrupt_ledger_failsoft(self, ledger):
        ledger.write_text("{not json")
        assert lid._physics_ledger_load() == {}


class TestWarmStart:
    def test_record_persists_and_new_profiler_warm_starts(self, ledger):
        cfg = _cfg(model_name="qwen2.5-coder:32b", num_ctx=16640, min_samples=1)
        key = lid.physics_key(cfg)
        p1 = lid.LatencyProfiler(cfg, ledger_key=key)
        assert p1.is_warm() is False
        p1.record(ttft_ms=120_000.0, total_ms=800_000.0, output_tokens=8000)
        assert ledger.exists()

        # A FRESH profiler (new process simulation) warm-starts from the ledger.
        p2 = lid.LatencyProfiler(cfg, ledger_key=key)
        assert p2.is_warm() is True
        est = p2.adaptive_timeout_ms(prompt_tokens=4000)
        assert est >= 300_000.0            # sized from measured truth, not the seed

    def test_timeout_penalty_persists_ewma(self, ledger):
        cfg = _cfg(model_name="qwen2.5-coder:32b", num_ctx=16640)
        key = lid.physics_key(cfg)
        p1 = lid.LatencyProfiler(cfg, ledger_key=key)
        p1.record_timeout_penalty(600_000.0)
        data = json.loads(ledger.read_text())
        assert data[key]["ewma_ms"] >= 600_000.0

    def test_no_key_is_legacy_no_writes(self, ledger):
        cfg = _cfg(model_name="qwen2.5-coder:32b", num_ctx=16640)
        p = lid.LatencyProfiler(cfg)
        p.record(ttft_ms=1.0, total_ms=2.0, output_tokens=1)
        assert not ledger.exists()

    def test_keys_are_isolated(self, ledger):
        cfg32 = _cfg(model_name="qwen2.5-coder:32b", num_ctx=16640)
        cfg3 = _cfg(model_name="qwen2.5-coder:3b", num_ctx=0, min_samples=1)
        lid.LatencyProfiler(cfg32, ledger_key=lid.physics_key(cfg32)).record_timeout_penalty(900_000.0)
        p3 = lid.LatencyProfiler(cfg3, ledger_key=lid.physics_key(cfg3))
        assert p3.is_warm() is False       # the 3B never inherits 32B physics


class TestColdPathReadsPrior:
    def test_cycle_formula_uses_persisted_truth(self, ledger, monkeypatch):
        """Run N measured ~800s rounds; run N+1's COLD cycle estimate (no live
        profiler yet) must be sized from the ledger, not the blind seed."""
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "4")
        monkeypatch.setenv("JARVIS_HYBRID_MESH_EXPECTED_NUM_CTX", "16384")
        cfg = _cfg(model_name="qwen2.5-coder:32b", num_ctx=16640)
        p = lid.LatencyProfiler(cfg, ledger_key=lid.physics_key(cfg))
        p.record_timeout_penalty(800_000.0)   # persisted ewma >= 800s

        cold = lid.expected_agentic_cycle_s()
        assert cold >= 4 * 800.0 * 0.9        # 4 rounds x measured truth

    def test_cycle_formula_seed_when_ledger_empty(self, ledger, monkeypatch):
        monkeypatch.setenv("JARVIS_A1_MAX_AGENTIC_ROUNDS", "5")
        monkeypatch.setenv("JARVIS_LOCAL_INFERENCE_TIMEOUT_SEED_MS", "30000")
        monkeypatch.setenv("JARVIS_JPRIME_HEAVY_COLDSTART_MULT", "4.0")
        monkeypatch.setenv("JARVIS_LOCAL_SEED_CTX_BASELINE", "8192")
        monkeypatch.setenv("JARVIS_HYBRID_MESH_EXPECTED_NUM_CTX", "16384")
        assert lid.expected_agentic_cycle_s() == pytest.approx(1200.0, rel=0.05)


def test_dispatch_profiler_gets_ledger_key():
    """Source pin: the per-endpoint profiler singleton is constructed with the
    physics ledger key so cross-run persistence flows through the dispatch."""
    import pathlib
    import backend.core.ouroboros.governance.candidate_generator as cg
    src = pathlib.Path(cg.__file__).read_text()
    assert "ledger_key=" in src and "physics_key" in src
