"""The model that ANSWERS decides its schema capability, not the slot's name.

2026-09-07: a simple single-file production edit routed to the brain slot named
"7b" inherited ``full_content_only``; the 30B coder that actually served every
slot re-emitted a 683-line file as a 32 KB blob to add twelve lines. These
tests pin the resolver: declared slot → served-model policy → observed
evidence, fail-soft to the declaration at every seam.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import served_model_capability as smc

REPO = Path(__file__).resolve().parents[2]
POLICY = {"served_models": [{"match": "*coder*", "min_params_b": 30, "schema_capability": "full_content_and_diff"}]}


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SERVED_CAPABILITY_LEDGER_PATH", str(tmp_path / "cap.jsonl"))
    for k in ("JARVIS_SERVED_CAPABILITY_ENABLED", "JARVIS_SERVED_CAPABILITY_WINDOW", "JARVIS_SERVED_CAPABILITY_MIN_SAMPLES",
              "JARVIS_SERVED_CAPABILITY_MAX_FAILURE_RATE", "JARVIS_SERVED_CAPABILITY_COOLDOWN_S", "JARVIS_PIPELINE_TIMEOUT_S"):
        monkeypatch.delenv(k, raising=False)


# --------------------------------------------------------------------------
# the size tag
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sid,expected", [
    ("qwen3-coder-ov:30b", 30.0), ("qwen2.5-coder:7b", 7.0), ("deepseek-coder-33B", 33.0),
    ("llama3_8b", 8.0), ("mistral:7.5b", 7.5), ("phi3", None), ("", None), ("coder:latest", None),
])
def test_parse_param_billions(sid, expected):
    assert smc.parse_param_billions(sid) == expected


# --------------------------------------------------------------------------
# declaration — the policy section
# --------------------------------------------------------------------------

def test_the_served_coder_is_diff_capable_above_the_floor():
    assert smc.declared_for_served(POLICY, "qwen3-coder-ov:30b") == smc.FULL_AND_DIFF
    assert smc.declared_for_served(POLICY, "QWEN3-CODER-OV:30B") == smc.FULL_AND_DIFF


def test_a_small_coder_or_untagged_id_never_matches_a_floored_entry():
    assert smc.declared_for_served(POLICY, "qwen2.5-coder:7b") is None
    assert smc.declared_for_served(POLICY, "qwen-coder:latest") is None


def test_non_coder_and_empty_ids_declare_nothing():
    assert smc.declared_for_served(POLICY, "llama3:70b") is None
    assert smc.declared_for_served(POLICY, "") is None


@pytest.mark.parametrize("policy", [
    {}, {"served_models": None}, {"served_models": "nope"}, {"served_models": [None, 3, {"match": ""}]},
    {"served_models": [{"match": "*coder*", "schema_capability": "diffs_please"}]},
    {"served_models": [{"match": "*coder*", "min_params_b": "thirty", "schema_capability": "full_content_and_diff"}]},
])
def test_malformed_policy_declares_nothing(policy):
    assert smc.declared_for_served(policy, "qwen3-coder-ov:30b") is None


def test_first_matching_entry_wins_and_an_unfloored_entry_matches_untagged():
    policy = {"served_models": [
        {"match": "*special*", "schema_capability": "full_content_only"},
        {"match": "*", "schema_capability": "full_content_and_diff"},
    ]}
    assert smc.declared_for_served(policy, "special-coder:30b") == smc.FULL_ONLY
    assert smc.declared_for_served(policy, "anything") == smc.FULL_AND_DIFF


def test_the_repos_real_policy_declares_the_served_coder():
    import yaml
    policy = yaml.safe_load((REPO / "backend/core/ouroboros/governance/brain_selection_policy.yaml").read_text(encoding="utf-8"))
    assert smc.declared_for_served(policy, "qwen3-coder-ov:30b") == smc.FULL_AND_DIFF
    assert smc.declared_for_served(policy, "qwen2.5-coder:7b") is None


# --------------------------------------------------------------------------
# evidence — the ledger and the demotion
# --------------------------------------------------------------------------

def _fill(ledger, sid, outcomes, *, t0=1_000.0):
    for i, ok in enumerate(outcomes):
        assert ledger.record(sid, ok, op_id=f"op-{i}", ts=t0 + i)


def test_ledger_round_trips_per_model_oldest_first():
    led = smc.ServedCapabilityLedger()
    _fill(led, "m-a", [True, False, True]); _fill(led, "m-b", [False])
    rows = led.recent("m-a")
    assert [r.ok for r in rows] == [True, False, True]
    assert rows[0].op_id == "op-0" and led.recent("m-b")[0].ok is False
    assert led.recent("m-none") == () and led.recent("") == ()


def test_ledger_reads_a_bounded_tail(monkeypatch):
    monkeypatch.setenv("JARVIS_SERVED_CAPABILITY_TAIL_BYTES", "4096")
    led = smc.ServedCapabilityLedger()
    _fill(led, "m", [True] * 400)
    rows = led.recent("m", limit=1000)
    assert 0 < len(rows) < 400, "only the tail is read"


def test_ledger_never_raises_on_an_unwritable_path(tmp_path):
    led = smc.ServedCapabilityLedger(tmp_path / "dir")
    (tmp_path / "dir").mkdir()
    assert led.record("m", True) is False
    assert led.recent("m") == ()


def test_demotion_needs_min_samples_and_a_failure_rate(monkeypatch):
    monkeypatch.setenv("JARVIS_SERVED_CAPABILITY_WINDOW", "6")
    monkeypatch.setenv("JARVIS_SERVED_CAPABILITY_MIN_SAMPLES", "3")
    led = smc.ServedCapabilityLedger()
    _fill(led, "m", [False, False])
    assert smc.observed_demotion("m", now_ts=1_010.0, ledger=led) is None, "two samples are not evidence"
    _fill(led, "m", [True, False], t0=1_002.0)
    assert smc.observed_demotion("m", now_ts=1_010.0, ledger=led), "3/4 failed"
    _fill(led, "m", [True, True, True, True, True], t0=1_004.0)
    assert smc.observed_demotion("m", now_ts=1_020.0, ledger=led) is None, "the window moved on"


def test_demotion_expires_after_the_cooldown(monkeypatch):
    monkeypatch.setenv("JARVIS_SERVED_CAPABILITY_COOLDOWN_S", "100")
    led = smc.ServedCapabilityLedger()
    _fill(led, "m", [False, False, False, True])
    assert smc.observed_demotion("m", now_ts=1_050.0, ledger=led)
    assert smc.observed_demotion("m", now_ts=1_500.0, ledger=led) is None


def test_cooldown_defaults_to_the_pipeline_wall(monkeypatch):
    monkeypatch.setenv("JARVIS_PIPELINE_TIMEOUT_S", "321")
    assert smc.cooldown_s() == 321.0
    monkeypatch.setenv("JARVIS_SERVED_CAPABILITY_COOLDOWN_S", "5")
    assert smc.cooldown_s() == 5.0


# --------------------------------------------------------------------------
# the resolver
# --------------------------------------------------------------------------

def test_served_policy_corrects_a_small_slots_declaration():
    v = smc.effective_schema_capability(declared="full_content_only", served_model="qwen3-coder-ov:30b", policy=POLICY)
    assert v.capability == smc.FULL_AND_DIFF and v.changed and v.reason == "policy_match"


def test_unknown_served_model_keeps_the_declaration():
    for served in (None, ""):
        v = smc.effective_schema_capability(declared="full_content_and_diff", served_model=served, policy=POLICY)
        assert v.capability == smc.FULL_AND_DIFF and not v.changed and v.reason == "no_served_model"
    v = smc.effective_schema_capability(declared="full_content_only", served_model="llama3:70b", policy=POLICY)
    assert v.capability == smc.FULL_ONLY and v.reason == "no_policy_match"


def test_evidence_overrules_the_declaration_then_heals(monkeypatch):
    monkeypatch.setenv("JARVIS_SERVED_CAPABILITY_COOLDOWN_S", "100")
    led = smc.ServedCapabilityLedger()
    _fill(led, "qwen3-coder-ov:30b", [False, False, True, False])
    v = smc.effective_schema_capability(declared="full_content_only", served_model="qwen3-coder-ov:30b", policy=POLICY, ledger=led, now_ts=1_050.0)
    assert v.capability == smc.FULL_ONLY and v.reason.startswith("observed_demotion")
    v = smc.effective_schema_capability(declared="full_content_only", served_model="qwen3-coder-ov:30b", policy=POLICY, ledger=led, now_ts=2_000.0)
    assert v.capability == smc.FULL_AND_DIFF


def test_disabled_and_garbage_declarations_are_conservative(monkeypatch):
    monkeypatch.setenv("JARVIS_SERVED_CAPABILITY_ENABLED", "0")
    v = smc.effective_schema_capability(declared="full_content_only", served_model="qwen3-coder-ov:30b", policy=POLICY)
    assert v.capability == smc.FULL_ONLY and v.reason == "disabled"
    monkeypatch.setenv("JARVIS_SERVED_CAPABILITY_ENABLED", "1")
    v = smc.effective_schema_capability(declared="???", served_model="", policy=POLICY)
    assert v.capability == smc.FULL_ONLY


@pytest.mark.asyncio
async def test_note_diff_outcome_is_bounded_and_fail_soft():
    assert await smc.note_diff_outcome("qwen3-coder-ov:30b", True, op_id="op-1") is True
    assert await smc.note_diff_outcome("", True) is False
    assert smc.ServedCapabilityLedger().recent("qwen3-coder-ov:30b")[0].op_id == "op-1"


# --------------------------------------------------------------------------
# the wiring — a resolver nobody calls is the defect this closes
# --------------------------------------------------------------------------

def test_brain_selector_resolves_with_its_loaded_policy(tmp_path):
    import yaml
    from backend.core.ouroboros.governance.brain_selector import BrainSelector
    pol = tmp_path / "policy.yaml"
    pol.write_text(yaml.safe_dump({"version": "t", "brains": {}, **POLICY}), encoding="utf-8")
    sel = BrainSelector(policy_path=pol, persist_path=tmp_path / "cost.json")
    assert sel.effective_schema_capability(declared="full_content_only", served_model="qwen3-coder-ov:30b").capability == smc.FULL_AND_DIFF
    # hot reload: an operator edit is honoured without a restart
    time.sleep(0.01)
    pol.write_text(yaml.safe_dump({"version": "t", "brains": {}, "served_models": []}), encoding="utf-8")
    import os; os.utime(pol, None)
    assert sel.effective_schema_capability(declared="full_content_only", served_model="qwen3-coder-ov:30b").capability == smc.FULL_ONLY


def test_routing_intent_carries_the_served_model():
    from backend.core.ouroboros.governance.op_context import RoutingIntentTelemetry
    ri = RoutingIntentTelemetry(expected_provider="x", policy_reason="y", served_model="qwen3-coder-ov:30b")
    assert ri.served_model == "qwen3-coder-ov:30b"
    assert RoutingIntentTelemetry(expected_provider="x", policy_reason="y").served_model == ""


def test_the_governed_loop_stamps_the_served_capability():
    src = (REPO / "backend/core/ouroboros/governance/governed_loop_service.py").read_text(encoding="utf-8")
    assert "await _resolve_served_capability(self, brain)" in src
    assert "schema_capability=_served_cap," in src and "served_model=_served_model," in src
    assert 'getattr(brain, "schema_capability", "full_content_only"),\n            )' not in src


def test_the_diff_apply_seam_records_evidence():
    from backend.core.ouroboros.governance import providers as P
    src = inspect.getsource(P)
    assert "_note_diff_outcome(ctx, True)" in src and src.count("_note_diff_outcome(ctx, False)") == 2


@pytest.mark.asyncio
async def test_resolver_falls_back_to_the_declaration_when_the_node_is_unreachable(monkeypatch):
    from backend.core.ouroboros.governance import governed_loop_service as GLS
    from backend.core.ouroboros.governance import candidate_generator as CG

    async def _none(endpoint, **_kw):
        return None

    monkeypatch.setattr(CG, "_resolve_served_model", _none)
    monkeypatch.setenv("JARVIS_PRIME_URL", "http://127.0.0.1:1")

    class _Brain:
        schema_capability = "full_content_only"; brain_id = "qwen_coder"

    class _Gls:
        _brain_selector = None

    assert await GLS._resolve_served_capability(_Gls(), _Brain()) == ("", "full_content_only")


@pytest.mark.asyncio
async def test_resolver_corrects_the_slot_from_the_served_model(monkeypatch, tmp_path, caplog):
    import logging, yaml
    from backend.core.ouroboros.governance import governed_loop_service as GLS
    from backend.core.ouroboros.governance import candidate_generator as CG
    from backend.core.ouroboros.governance.brain_selector import BrainSelector

    async def _served(endpoint, **_kw):
        return "qwen3-coder-ov:30b"

    monkeypatch.setattr(CG, "_resolve_served_model", _served)
    monkeypatch.setenv("JARVIS_PRIME_URL", "http://127.0.0.1:11434")
    pol = tmp_path / "p.yaml"; pol.write_text(yaml.safe_dump({"brains": {}, **POLICY}), encoding="utf-8")

    class _Brain:
        schema_capability = "full_content_only"; brain_id = "qwen_coder"

    class _Gls:
        _brain_selector = BrainSelector(policy_path=pol, persist_path=tmp_path / "c.json")

    GLS._SERVED_CAP_ANNOUNCED.clear()
    with caplog.at_level(logging.WARNING):
        assert await GLS._resolve_served_capability(_Gls(), _Brain()) == ("qwen3-coder-ov:30b", smc.FULL_AND_DIFF)
        assert await GLS._resolve_served_capability(_Gls(), _Brain()) == ("qwen3-coder-ov:30b", smc.FULL_AND_DIFF)
    assert sum("resolved from the SERVED model" in r.getMessage() for r in caplog.records) == 1, "announced once"
