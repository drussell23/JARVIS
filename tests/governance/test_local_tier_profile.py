"""The deployment profile, the warm swap, and the triage fallback.

Three things are pinned here:

  1. **No dead config.** Every `JARVIS_*` setting in the profile must be read
     by code somewhere. A profile documenting a knob nothing consults is the
     same defect class as a capability with no caller -- it looks configured
     and does nothing.
  2. **The warm swap is paid outside the op's clock.** Ollama loads on first
     use; if that load happens inside a generation window calibrated for
     generation, the op dies on a deadline that had nothing to do with the
     model's speed.
  3. **"Unknowable" is not "empty".** A server without `/api/ps` must not be
     read as holding no models, or every op would trigger a needless swap.
"""
from __future__ import annotations

import io
import os
import re
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import inference_gateway as ig

PROFILE = Path("deploy/local_tier_windows.env")


def _profile_settings():
    out = {}
    for line in io.open(PROFILE, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


class TestTheProfileIsReal:
    def test_the_profile_exists_and_parses(self):
        assert PROFILE.is_file()
        assert _profile_settings()

    def test_every_jarvis_setting_is_actually_read_by_code(self):
        """No dead config. A knob nothing consults is documentation pretending
        to be configuration."""
        settings = [k for k in _profile_settings() if k.startswith("JARVIS_")]
        assert settings, "profile declares no JARVIS_* settings"
        src = ""
        for path in Path("backend/core/ouroboros/governance").rglob("*.py"):
            try:
                src += io.open(path, encoding="utf-8").read()
            except Exception:  # noqa: BLE001
                continue
        missing = [k for k in settings if k not in src]
        assert not missing, f"profile sets knobs no code reads: {missing}"

    def test_the_ollama_settings_are_marked_as_server_side(self):
        """OLLAMA_* are read by `ollama serve` ON THE WINDOWS BOX. Sourcing
        this file on the Mac does not apply them, and a profile that did not
        say so would be actively misleading."""
        text = io.open(PROFILE, encoding="utf-8").read()
        assert "OLLAMA_KEEP_ALIVE=-1" in text
        assert "OLLAMA_MAX_LOADED_MODELS=1" in text
        assert "OLLAMA_KV_CACHE_TYPE=q8_0" in text
        assert "WINDOWS" in text.upper()
        head = text[:text.index("PART B")]
        assert "cannot set them remotely" in head or "NOT apply them" in head

    def test_the_endpoint_is_not_a_code_default(self):
        """The LAN address lives in the profile, never in code -- an unset
        endpoint is the single-machine case, not a misconfiguration."""
        import inspect
        assert "192.168." not in inspect.getsource(ig)

    def test_sharding_is_explicitly_off_for_a_single_gpu_host(self):
        assert _profile_settings().get("JARVIS_LOCAL_ACCEL_SHARDING") == "0"

    def test_the_resident_model_is_the_moe_workhorse(self):
        s = _profile_settings()
        assert s["JARVIS_REMOTE_INFERENCE_MODEL"] == "qwen3-coder:30b"
        assert s["JARVIS_LOCAL_TRIAGE_MODEL"]      # M1 fallback declared


class TestKeepAliveReachesTheWire:
    def test_minus_one_survives_parsing_and_is_sent_verbatim(self,
                                                             monkeypatch):
        """`-1` is ollama's "never unload". If parsing clamped it to 0 the
        meaning would INVERT to "unload immediately" -- the exact opposite of
        the profile's intent."""
        from backend.core.ouroboros.governance import (
            local_inference_director as lid,
        )
        monkeypatch.setenv("JARVIS_LOCAL_MODEL_KEEP_ALIVE_SECONDS", "-1")
        cfg = lid.LocalConfig.from_env()
        assert cfg.keep_alive_seconds == -1

    def test_it_cannot_starve_the_http_connector(self):
        """The same value feeds the aiohttp connector keepalive, which must
        stay positive. The `max(30, ...)` guard is what keeps one env var
        from meaning two incompatible things."""
        import inspect
        from backend.core.ouroboros.governance import (
            local_inference_director as lid,
        )
        src = inspect.getsource(lid)
        assert "max(30, self._cfg.keep_alive_seconds)" in src


class TestResidencyProbe:
    @pytest.mark.asyncio
    async def test_an_unreachable_host_is_unknowable_not_empty(self):
        """None and () mean different things: () would make every op warm-swap
        against a server that simply does not implement /api/ps."""
        g = ig.InferenceGateway()
        assert await g.resident_models("http://127.0.0.1:9") is None

    @pytest.mark.parametrize("wanted,resident,expect", [
        ("qwen3-coder:30b", ("qwen3-coder:30b",), True),
        ("qwen3-coder:30b", ("qwen3-coder:30b-instruct-q4_K_M",), True),
        ("qwen3-coder", ("qwen3-coder:latest",), True),
        ("qwen3-coder:30b", ("qwen3.8:27b",), False),
        ("qwen3-coder:30b", (), False),
        ("", ("anything",), True),
    ])
    def test_tag_variants_do_not_trigger_a_needless_swap(self, wanted,
                                                         resident, expect):
        """`qwen3-coder:30b` and `...:30b-instruct-q4_K_M` are the same weights
        to an operator. An over-strict match burns a cold load to load what is
        already loaded."""
        assert ig.InferenceGateway._model_matches(wanted, resident) is expect


class TestTheWarmSwap:
    class _Client:
        def __init__(self):
            self.warmed_with = None

        async def warmup(self, *, timeout_s):
            self.warmed_with = timeout_s
            return True

    def _gw(self, resident):
        g = ig.InferenceGateway()
        client = self._Client()

        async def _rm(_url):
            return resident
        g.resident_models = _rm                      # type: ignore[assignment]
        g._client_for = lambda t: (client, None)     # type: ignore[assignment]
        return g, client

    def _target(self, model="qwen3.8:27b"):
        return ig.GatewayTarget(base_url="http://h:11434", model_name=model,
                                scope="remote", state=ig.HostState.HEALTHY,
                                reason="t")

    @pytest.mark.asyncio
    async def test_a_resident_model_is_not_reloaded(self):
        g, client = self._gw(("qwen3.8:27b",))
        rep = await g.ensure_model_resident(self._target())
        assert rep["swapped"] is False and client.warmed_with is None
        assert rep["reason"] == "already resident"

    @pytest.mark.asyncio
    async def test_a_different_resident_model_triggers_a_swap(self):
        g, client = self._gw(("qwen3-coder:30b",))
        rep = await g.ensure_model_resident(self._target("qwen3.8:27b"))
        assert rep["swapped"] is True
        assert client.warmed_with == ig.warm_swap_budget_s()

    @pytest.mark.asyncio
    async def test_the_swap_uses_its_own_budget_not_a_route_budget(self,
                                                                   monkeypatch):
        """A cold load is PCIe transfer, not generation. Charging it to the
        route budget would fail the op on a deadline that says nothing about
        the model."""
        monkeypatch.setenv("JARVIS_GATEWAY_WARM_SWAP_BUDGET_S", "240")
        g, client = self._gw(("other:1b",))
        await g.ensure_model_resident(self._target())
        assert client.warmed_with == 240.0
        from backend.core.ouroboros.governance.route_budgets import (
            route_generation_budget_s,
        )
        assert client.warmed_with != route_generation_budget_s("background")

    @pytest.mark.asyncio
    async def test_unknowable_residency_dispatches_without_swapping(self):
        g, client = self._gw(None)
        rep = await g.ensure_model_resident(self._target())
        assert rep["swapped"] is False and client.warmed_with is None
        assert "unknown" in rep["reason"]

    @pytest.mark.asyncio
    async def test_preflight_off_is_a_clean_no_op(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GATEWAY_PREFLIGHT_ENABLED", "0")
        g, client = self._gw(("other:1b",))
        rep = await g.ensure_model_resident(self._target())
        assert rep["checked"] is False and client.warmed_with is None

    @pytest.mark.asyncio
    async def test_a_broken_preflight_never_blocks_dispatch(self):
        g, _c = self._gw(("other:1b",))

        async def _boom(_url):
            raise OSError("probe exploded")
        g.resident_models = _boom                    # type: ignore[assignment]
        rep = await g.ensure_model_resident(self._target())
        assert "degraded" in rep["reason"]

    @pytest.mark.asyncio
    async def test_residency_is_cached_within_its_ttl(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GATEWAY_RESIDENCY_TTL_S", "300")
        calls = {"n": 0}
        g = ig.InferenceGateway()

        async def _rm(_url):
            calls["n"] += 1
            return ("qwen3.8:27b",)
        g.resident_models = _rm                      # type: ignore[assignment]
        await g.ensure_model_resident(self._target())
        await g.ensure_model_resident(self._target())
        assert calls["n"] == 1

    def test_the_swap_runs_outside_the_failure_classifier(self):
        """A slow cold load must not be recorded as a host fault -- it would
        open the breaker on a healthy machine that was merely loading."""
        import ast
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(ig.InferenceGateway.dispatch))
        tree = ast.parse(src)
        tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try)]
        assert tries, "dispatch has no try block"
        in_try = {
            n.func.attr for t in tries for n in ast.walk(t)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "ensure_model_resident" not in in_try


class TestTheTriageFallback:
    def test_the_fallback_names_the_configured_triage_model(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LOCAL_TRIAGE_MODEL", "tiny:1b")
        t = ig.InferenceGateway()._local_target("outage")
        assert t.scope == "local" and t.model_name == "tiny:1b"

    def test_it_falls_back_to_the_local_config_when_unset(self, monkeypatch):
        monkeypatch.delenv("JARVIS_LOCAL_TRIAGE_MODEL", raising=False)
        assert ig.InferenceGateway()._local_target("x").model_name

    def test_there_is_exactly_one_local_target_builder(self):
        """DRY pin: the fallback must not be re-derived per call site, or two
        outage paths will disagree about which model triage runs on."""
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(ig)))
        builders = [n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_local_target"]
        assert len(builders) == 1
