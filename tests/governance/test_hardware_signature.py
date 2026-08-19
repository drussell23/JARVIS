"""A ledger key must not conflate two machines.

`physics_key` was `model@ctx`, with a docstring explaining that the endpoint
is excluded because "node IPs change every run". That is right about IPs and
wrong about machines: the same model name on a 16GB M1 and an RTX 5090 wrote
ONE entry, so whichever measured last governed both. In the bridged
configuration this project is building, the M1's ~95ms/token would size the
5090's lane count -- the ThroughputGovernor computing a wrong answer from
perfectly honest data.
"""
from __future__ import annotations

import dataclasses

import pytest

from backend.core.ouroboros.governance import hardware_signature as hs
from backend.core.ouroboros.governance import local_inference_director as lid

LOCAL = "http://127.0.0.1:11434"
WIN = "http://192.168.1.50:11434"
OTHER = "http://192.168.1.77:11434"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("JARVIS_HARDWARE_SIGNATURE_ENABLED", raising=False)
    hs.reset_for_tests()
    yield
    hs.reset_for_tests()


class TestIsolation:
    def test_local_and_remote_are_different_machines(self):
        assert hs.signature_for(LOCAL).digest != hs.signature_for(WIN).digest

    def test_two_remote_hosts_are_different_machines(self):
        assert hs.signature_for(WIN).digest != hs.signature_for(OTHER).digest

    @pytest.mark.parametrize("a,b", [
        ("http://192.168.1.50:11434", "http://192.168.1.50:8080"),
        ("http://192.168.1.50:11434", "https://192.168.1.50:443"),
        ("192.168.1.50:11434", "http://192.168.1.50:11434"),
    ])
    def test_one_machine_is_one_signature_regardless_of_port_or_scheme(self, a, b):
        """A port is not hardware. One box serving two models on two ports is
        one machine, and splitting its ledger by port would re-introduce the
        amnesia the physics ledger exists to cure."""
        assert hs.signature_for(a).digest == hs.signature_for(b).digest

    @pytest.mark.parametrize("url", ["http://localhost:11434", "http://[::1]:1",
                                     "http://127.0.0.1:99", ""])
    def test_loopback_spellings_all_resolve_to_this_machine(self, url):
        assert hs.signature_for(url).provenance == "probed"


class TestTheSpineDoesNotMove:
    def test_the_signature_survives_a_topology_flag_flip(self, monkeypatch):
        """A signature must change only when the MACHINE does.

        Accelerator components were originally appended to the machine id, so
        enabling `JARVIS_COMPUTE_TOPOLOGY_ENABLED` changed the digest and
        orphaned every prior measurement on hardware that had not changed at
        all. GPU identity is now a SUBSTITUTE for a missing spine, never an
        addition to a present one.
        """
        monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_ENABLED", "0")
        hs.reset_for_tests()
        off = hs.signature_for(LOCAL).digest
        monkeypatch.setenv("JARVIS_COMPUTE_TOPOLOGY_ENABLED", "1")
        hs.reset_for_tests()
        assert hs.signature_for(LOCAL).digest == off

    def test_component_order_cannot_change_the_digest(self):
        """nvidia-smi and torch may enumerate the same cards in different
        orders; a signature that flipped with enumeration order would split
        one machine's ledger in two."""
        a = hs._digest(("gpu:B", "gpu:A", "machine:X"))
        b = hs._digest(("machine:X", "gpu:A", "gpu:B"))
        assert a == b

    def test_the_digest_is_deterministic_across_calls(self):
        assert hs._digest(("machine:X",)) == hs._digest(("machine:X",))


class TestHonestProvenance:
    def test_a_probed_identity_is_marked_strong(self):
        assert hs.signature_for(LOCAL).is_strong is True

    def test_a_remote_identity_is_not_claimed_as_probed(self):
        """We cannot see that machine's hardware. Distinct is not the same as
        verified, and the difference is recorded rather than smoothed over."""
        sig = hs.signature_for(WIN)
        assert sig.provenance == "endpoint"
        assert sig.is_strong is False

    def test_components_never_carry_the_port_or_a_full_url(self):
        """A signature is an identity, not a connection string."""
        joined = " ".join(hs.signature_for(WIN).components)
        assert "11434" not in joined
        assert "http" not in joined

    def test_the_machine_id_is_not_the_hostname(self, monkeypatch):
        """An operator renames a machine; a signature that moved when they did
        would orphan every measurement taken before the rename."""
        import platform
        node = platform.node()
        if node:
            assert node not in (hs.machine_id() or "")


class TestItNeverBreaksDispatch:
    def test_a_broken_probe_still_yields_a_signature(self, monkeypatch):
        monkeypatch.setattr(hs, "_probe_machine_id",
                            lambda: (_ for _ in ()).throw(OSError("boom")))
        monkeypatch.setattr(hs, "accelerator_components", lambda: ())
        hs.reset_for_tests()
        sig = hs.signature_for(LOCAL)
        assert isinstance(sig.digest, str) and sig.digest

    def test_a_random_node_id_is_declined_rather_than_trusted(self,
                                                              monkeypatch):
        """uuid.getnode() sets the multicast bit when it INVENTED a value. A
        random per-process id is not an identity -- it would look distinct
        every run and prevent the ledger from ever warm-starting."""
        monkeypatch.setattr(hs, "_read_first_line", lambda _p: "")
        monkeypatch.setattr(hs._uuid_mod, "getnode", lambda: 0x010000000000)
        monkeypatch.setattr(hs.sys, "platform", "linux")
        monkeypatch.setattr(hs.os, "name", "posix")
        hs.reset_for_tests()
        assert hs._probe_machine_id() == ""

    def test_the_master_flag_off_is_an_empty_digest(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HARDWARE_SIGNATURE_ENABLED", "0")
        hs.reset_for_tests()
        assert hs.signature_for(LOCAL).digest == ""


class TestTheLedgerKey:
    def _key(self, **over):
        return lid.physics_key(
            dataclasses.replace(lid.LocalConfig.from_env(), **over))

    def test_the_same_model_on_two_hosts_gets_two_keys(self):
        assert self._key(model_name="m", num_ctx=8192, base_url=LOCAL) != \
            self._key(model_name="m", num_ctx=8192, base_url=WIN)

    def test_the_model_and_ctx_tail_is_preserved(self):
        assert self._key(model_name="m", num_ctx=8192).endswith("m@8192")
        assert self._key(model_name="m", num_ctx=0).endswith("m@cpu")

    def test_the_flag_off_restores_the_legacy_key_exactly(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HARDWARE_SIGNATURE_ENABLED", "0")
        hs.reset_for_tests()
        assert self._key(model_name="m", num_ctx=8192) == "m@8192"

    def test_an_unidentifiable_host_never_becomes_a_shared_bucket(
            self, monkeypatch):
        """A key containing a literal "unknown" segment would merge every
        unidentifiable host into ONE entry -- the exact conflation being
        removed, wearing a different name. Falling back to the legacy shape is
        no worse than before; inventing a placeholder would be worse."""
        monkeypatch.setattr(
            hs, "signature_for",
            lambda _u="": hs.HardwareSignature(digest="", provenance="unknown"))
        assert self._key(model_name="m", num_ctx=8192) == "m@8192"


class TestTheFailoverSeam:
    """The store keyed by endpoint, the ledger keyed by cfg -- a mismatch.

    `_failover_profiler_for(endpoint, cfg)` holds ONE profiler per endpoint
    while carrying a SINGLE cfg. Deriving the machine from cfg there would
    stamp every failover target with the base config's identity, re-creating
    the host conflation at the one seam -- cross-node failover -- where it
    matters most.
    """

    def test_an_explicit_endpoint_overrides_the_config(self):
        cfg = dataclasses.replace(lid.LocalConfig.from_env(),
                                  model_name="m", num_ctx=8192, base_url=LOCAL)
        assert lid.physics_key(cfg) != lid.physics_key(cfg, endpoint=WIN)

    def test_two_failover_targets_get_two_ledgers(self):
        cfg = dataclasses.replace(lid.LocalConfig.from_env(),
                                  model_name="m", num_ctx=8192, base_url=LOCAL)
        assert lid.physics_key(cfg, endpoint=WIN) != \
            lid.physics_key(cfg, endpoint=OTHER)

    def test_the_generator_passes_the_endpoint_it_is_measuring(self):
        """Wiring pin. Without the explicit argument the call compiles, runs,
        and is silently wrong."""
        import ast
        import inspect
        import textwrap
        from backend.core.ouroboros.governance import candidate_generator as cg
        src = textwrap.dedent(
            inspect.getsource(cg.CandidateGenerator._failover_profiler_for))
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "physics_key"):
                assert any(k.arg == "endpoint" for k in node.keywords), \
                    "physics_key called without the endpoint being measured"
                return
        raise AssertionError("physics_key call not found in the failover seam")

    def test_no_endpoint_falls_back_to_the_config(self):
        cfg = dataclasses.replace(lid.LocalConfig.from_env(),
                                  model_name="m", num_ctx=8192, base_url=WIN)
        assert lid.physics_key(cfg, endpoint="") == lid.physics_key(cfg)
