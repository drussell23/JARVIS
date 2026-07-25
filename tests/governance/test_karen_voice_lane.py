"""Karen's voice lane — the model that speaks is chosen by measurement.

Live evidence this suite encodes (2026-07-25, real DW):

    deepseek-ai/DeepSeek-V4-Flash        ttft=1.01s  tokens=5   -> ELECTED
    Qwen/Qwen3-VL-30B-A3B-Instruct-FP8   ttft=1.06s  tokens=11
    Qwen/Qwen3.5-9B                      ttft= -1    tokens=0   no_tokens
    Qwen/Qwen3-14B-FP8                   ttft= -1    tokens=0   no_tokens
    Qwen/Qwen3.5-397B-A17B-FP8 (default) ttft=22.84s            22x too slow

Nothing here talks to DW. Every probe is injected, because a suite that needs
the network tests the network.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from backend.core.ouroboros.governance import karen_voice_lane as kvl
from backend.core.ouroboros.governance.karen_voice_lane import (
    VoiceLatencyLedger,
    VoiceModelRecord,
    build_voice_probe_payload,
)


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    """Never touch the operator's real ledger."""
    monkeypatch.setenv(
        "JARVIS_KAREN_VOICE_LEDGER_PATH", str(tmp_path / "voice.json"),
    )
    monkeypatch.delenv("JARVIS_KAREN_VOICE_MODEL", raising=False)
    kvl.reset_default_ledger()
    yield
    kvl.reset_default_ledger()


def _rec(model, ttft, tokens=5, *, age_s=0.0, reason="ok"):
    return VoiceModelRecord(
        model=model, ttft_s=ttft, tokens=tokens, spoke=tokens > 0,
        measured_at=time.time() - age_s, reason=reason,
    )


# ---------------------------------------------------------------------------
# Election — fastest model that actually SPOKE
# ---------------------------------------------------------------------------


def test_elects_the_fastest_model_that_spoke():
    led = VoiceLatencyLedger().load()
    led.record(_rec("slow-but-fine", 1.4))
    led.record(_rec("deepseek-ai/DeepSeek-V4-Flash", 1.01))
    assert led.best() == "deepseek-ai/DeepSeek-V4-Flash"


def test_a_reasoning_only_model_is_never_elected():
    """THE TRAP. Four live candidates answered with pure reasoning_content and
    zero spoken tokens. They are not slow — they are SILENT, which a latency
    number alone cannot express."""
    led = VoiceLatencyLedger().load()
    led.record(_rec("Qwen/Qwen3.5-9B", -1.0, tokens=0, reason="no_tokens"))
    assert led.best() is None

    led.record(_rec("google/gemma-4-26B-A4B-it", 0.84))
    assert led.best() == "google/gemma-4-26B-A4B-it"


def test_a_model_over_the_spoken_budget_is_disqualified(monkeypatch):
    """The 397B measured 22.84s. It writes excellent prose. It is still
    disqualified, because a reply that starts after 22 seconds of silence has
    already failed as conversation."""
    led = VoiceLatencyLedger().load()
    led.record(_rec("Qwen/Qwen3.5-397B-A17B-FP8", 22.84))
    assert led.best() is None

    monkeypatch.setenv("JARVIS_KAREN_VOICE_TTFT_BUDGET_S", "30")
    assert led.best() == "Qwen/Qwen3.5-397B-A17B-FP8", (
        "budget is an env knob, not a constant"
    )


def test_ties_break_deterministically():
    """Two cockpits booted together must converge on the same voice rather
    than drift apart on dict ordering."""
    led = VoiceLatencyLedger().load()
    led.record(_rec("zzz-model", 1.0))
    led.record(_rec("aaa-model", 1.0))
    assert led.best() == "aaa-model"


def test_stale_measurements_are_not_trusted(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_VOICE_LEDGER_TTL_S", "60")
    led = VoiceLatencyLedger().load()
    led.record(_rec("once-was-fast", 0.5, age_s=10_000))
    assert led.best() is None, "a rebalanced cluster must not be remembered"


# ---------------------------------------------------------------------------
# Resolution contract — None means "keep your default"
# ---------------------------------------------------------------------------


def test_cold_system_resolves_to_none_not_a_guess():
    """A cold ledger must degrade to TODAY's behaviour. Returning a plausible
    guess would trade a known-slow voice for an unmeasured one."""
    assert kvl.resolve_voice_model() is None


def test_operator_override_always_wins(monkeypatch):
    led = VoiceLatencyLedger().load()
    led.record(_rec("measured-winner", 0.4))
    monkeypatch.setenv("JARVIS_KAREN_VOICE_MODEL", "operator/choice")
    assert kvl.resolve_voice_model(ledger=led) == "operator/choice"


def test_master_flag_off_resolves_to_none(monkeypatch):
    led = VoiceLatencyLedger().load()
    led.record(_rec("measured-winner", 0.4))
    monkeypatch.setenv("JARVIS_KAREN_VOICE_LANE_ENABLED", "false")
    assert kvl.resolve_voice_model(ledger=led) is None


def test_resolution_never_raises(monkeypatch):
    """This sits on the turn path. It may cost a default; it may never cost
    the answer."""
    def _boom():
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(kvl, "get_default_ledger", _boom)
    assert kvl.resolve_voice_model() is None


# ---------------------------------------------------------------------------
# Probe — the ground truth
# ---------------------------------------------------------------------------


def _sse(chunks):
    """An injected SSE line source."""
    lines = [
        f'data: {json.dumps({"choices": [{"delta": d}]})}\n'.encode()
        for d in chunks
    ] + [b"data: [DONE]\n", b""]
    it = iter(lines)

    async def _readline():
        return next(it, b"")

    async def _dispatch(_payload):
        return _readline

    return _dispatch


async def test_probe_records_a_model_that_speaks():
    rec = await kvl.probe_voice_model(
        "talker", dispatch_fn=_sse([{"content": "I'm here."}]),
    )
    assert rec.spoke is True and rec.tokens == 1 and rec.reason == "ok"


async def test_probe_marks_a_reasoning_only_model_as_silent():
    """A model emitting ONLY reasoning deltas said nothing aloud. The
    discrimination is inherited from stream_watchdog._extract_token, which
    returns "" for reasoning deltas by documented contract."""
    rec = await kvl.probe_voice_model(
        "thinker",
        dispatch_fn=_sse([{"reasoning_content": "hmm"}, {"reasoning_content": "so"}]),
    )
    assert rec.spoke is False and rec.tokens == 0
    assert rec.conversational is False


async def test_a_dead_transport_is_recorded_not_raised():
    async def _boom(_payload):
        raise OSError("connection reset")

    rec = await kvl.probe_voice_model("ghost", dispatch_fn=_boom)
    assert rec.spoke is False and rec.conversational is False


async def test_probe_payload_is_a_spoken_turn_not_a_code_prompt():
    """The probe must exercise the workload it grades: a persona plus a real
    spoken question. A bare 'ok' ping would never provoke the reasoning
    behaviour that disqualifies a model."""
    body = build_voice_probe_payload("m")
    assert body["stream"] is True
    assert body["messages"][0]["role"] == "system"
    assert "spoken sentence" in body["messages"][0]["content"]
    assert body["max_tokens"] <= 128, (
        "a generous budget lets a reasoner finish thinking and then speak, "
        "hiding the exact failure this probe exists to catch"
    )


# ---------------------------------------------------------------------------
# Refresh — bounded, convergent, self-healing
# ---------------------------------------------------------------------------


async def test_refresh_probes_and_elects():
    async def _fake(model, **_kw):
        table = {"fast": 0.9, "slow": 9.0}
        return _rec(model, table.get(model, 5.0))

    kvl_probe = kvl.probe_voice_model
    try:
        kvl.probe_voice_model = _fake  # type: ignore[assignment]
        elected = await kvl.refresh_voice_lane(models=["slow", "fast"])
    finally:
        kvl.probe_voice_model = kvl_probe  # type: ignore[assignment]
    assert elected == "fast"


async def test_refresh_is_bounded_per_pass_but_converges(monkeypatch):
    """The cap bounds one refresh, not the search: every probed model is
    recorded whatever the outcome, so the next pass walks further down the
    ranking instead of re-probing the same head."""
    monkeypatch.setenv("JARVIS_KAREN_VOICE_MAX_CANDIDATES", "2")
    seen = []

    async def _fake(model, **_kw):
        seen.append(model)
        return _rec(model, 0.5 if model == "e" else -1.0,
                    tokens=1 if model == "e" else 0)

    cands = ["a", "b", "c", "d", "e"]
    orig = kvl.probe_voice_model
    try:
        kvl.probe_voice_model = _fake  # type: ignore[assignment]
        elected = None
        for _ in range(3):
            elected = await kvl.refresh_voice_lane(models=cands)
    finally:
        kvl.probe_voice_model = orig  # type: ignore[assignment]

    assert len(seen) == len(set(seen)), f"re-probed a known model: {seen}"
    assert elected == "e", "never reached the working model"


async def test_refresh_never_raises_on_a_hostile_probe():
    async def _boom(_m, **_kw):
        raise RuntimeError("probe exploded")

    orig = kvl.probe_voice_model
    try:
        kvl.probe_voice_model = _boom  # type: ignore[assignment]
        assert await kvl.refresh_voice_lane(models=["x"]) is None
    finally:
        kvl.probe_voice_model = orig  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Ledger durability
# ---------------------------------------------------------------------------


def test_ledger_survives_the_process(tmp_path):
    p = tmp_path / "l.json"
    a = VoiceLatencyLedger(p).load()
    a.record(_rec("keeper", 0.7))
    assert a.save() is True
    assert VoiceLatencyLedger(p).load().best() == "keeper"


def test_a_corrupt_ledger_costs_a_reprobe_not_a_boot(tmp_path):
    p = tmp_path / "l.json"
    p.write_text("{not json at all", encoding="utf-8")
    assert VoiceLatencyLedger(p).load().best() is None


def test_a_foreign_schema_is_discarded_wholesale(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({
        "schema_version": "something.else",
        "records": [{"model": "m", "ttft_s": 0.1, "tokens": 9,
                     "spoke": True, "measured_at": time.time()}],
    }), encoding="utf-8")
    assert VoiceLatencyLedger(p).load().best() is None, (
        "records from an unknown schema must not be trusted field-by-field"
    )


def test_partial_records_are_skipped_individually(tmp_path):
    """One malformed row must not discard the good ones beside it."""
    p = tmp_path / "l.json"
    p.write_text(json.dumps({
        "schema_version": kvl.SCHEMA_VERSION,
        "records": [
            {"nope": True},
            {"model": "good", "ttft_s": 0.5, "tokens": 3,
             "spoke": True, "measured_at": time.time()},
        ],
    }), encoding="utf-8")
    assert VoiceLatencyLedger(p).load().best() == "good"


def test_an_unwritable_ledger_still_works_in_process(tmp_path):
    led = VoiceLatencyLedger(tmp_path / "no" / "such" / "dir" / "x.json")
    led.load()
    led.record(_rec("mem-only", 0.6))
    assert led.best() == "mem-only"


# ---------------------------------------------------------------------------
# Authority + wiring pins
# ---------------------------------------------------------------------------


def test_the_lane_holds_no_mutation_authority():
    """It picks a model STRING. If it ever imports the orchestrator or the
    gate, that is a boundary breach, not a feature."""
    from pathlib import Path

    src = Path(
        "backend/core/ouroboros/governance/karen_voice_lane.py",
    ).read_text(encoding="utf-8")
    for banned in ("orchestrator", "iron_gate", "policy_engine", "change_engine"):
        assert banned not in src, f"voice lane reached for {banned}"


def test_names_are_never_used_to_infer_modality():
    """dw_modality_ledger carries a standing operator mandate: modality
    verdicts come from ground truth, never from pattern-matching a model id.
    The temptation here is real — DW's /models returns nothing but an id — so
    this pin exists to keep the shortcut out."""
    import ast
    from pathlib import Path

    src = Path(
        "backend/core/ouroboros/governance/karen_voice_lane.py",
    ).read_text(encoding="utf-8")

    # Scan EXECUTABLE code only. A line-and-substring sweep flags the module's
    # own prose explaining why the shortcut is banned — the check would then be
    # measuring documentation, which is how a structural pin becomes theatre.
    # Blanking every string constant leaves exactly the literals that could
    # actually drive a decision.
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    body = ast.unparse(tree)

    for smell in ("OCR", "Embedding", "dottxt", "embedding"):
        assert smell not in body, (
            f"model-id pattern matching on {smell!r} — forbidden by the "
            "modality-ledger mandate; the probe is the only authority"
        )
    # And the shapes such matching would take — but only when applied to a
    # MODEL identifier. Classifying the lane's own reason strings
    # (``r.reason.startswith("probe_error:")``) is bookkeeping, not modality
    # inference; a blanket ban on the method name would outlaw that too and
    # the pin would get deleted the first time it blocked legitimate code.
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute)
                and node.attr in ("startswith", "endswith")):
            continue
        recv = ast.unparse(node.value)
        assert "model" not in recv.lower(), (
            f"prefix/suffix matching on {recv!r} — model-id inference is the "
            "same forbidden shortcut wearing a different method name"
        )


def test_karen_answers_request_the_elected_voice_model():
    """Structural pin: without this the answer engine silently inherits
    DOUBLEWORD_MODEL — the 397B code brain, measured at 22.84s to first
    token."""
    from pathlib import Path

    src = Path(
        "backend/core/ouroboros/governance/karen_answer_engine.py",
    ).read_text(encoding="utf-8")
    assert "resolve_voice_model" in src
    assert "dw_model=" in src


def test_voice_is_ranked_but_is_not_an_op_lane():
    """The quarantine/promotion accounting in classify() is defined over the
    op lanes. Adding voice to that tuple would silently change SPECULATIVE
    bookkeeping, so it lives in the auxiliary tuple and registers nothing."""
    from backend.core.ouroboros.governance.dw_catalog_classifier import (
        _ALL_ROUTES, _AUXILIARY_ROUTES, _GENERATIVE_ROUTES,
    )
    assert "voice" in _AUXILIARY_ROUTES
    assert "voice" not in _GENERATIVE_ROUTES
    assert "voice" in _ALL_ROUTES


def test_the_voice_gate_is_a_ceiling_not_a_floor():
    """Every other route wants the biggest model it can afford. Voice is the
    inverse — active parameter count is the only latency proxy metadata
    offers, so the gate excludes the large models."""
    from backend.core.ouroboros.governance.dw_catalog_classifier import (
        gate_for_route,
    )
    voice, complex_ = gate_for_route("voice"), gate_for_route("complex")
    assert voice.max_params_b is not None and voice.min_params_b == 0.0
    assert complex_.min_params_b > 0.0
    assert voice.require_streaming is True, (
        "a lane that cannot stream cannot start speaking before the whole "
        "reply is generated"
    )


# ---------------------------------------------------------------------------
# The warm path — a lane with no caller is theatre
# ---------------------------------------------------------------------------


def test_warm_starts_exactly_one_sweep_per_process(monkeypatch):
    """Several turns arriving together must not each start a sweep."""
    kvl.reset_warm_state()
    started = []
    monkeypatch.setattr(
        kvl.threading, "Thread",
        lambda **kw: type("T", (), {"start": lambda _s: started.append(kw)})(),
    )
    assert kvl.ensure_voice_lane_warm() is True
    assert kvl.ensure_voice_lane_warm() is False
    assert len(started) == 1
    assert started[0]["daemon"] is True, "a warm sweep must not hold the exit"


def test_warm_is_skipped_when_a_model_is_already_elected(monkeypatch):
    kvl.reset_warm_state()
    led = kvl.get_default_ledger()
    led.record(_rec("already-fast", 0.5))
    monkeypatch.setattr(
        kvl.threading, "Thread",
        lambda **kw: pytest.fail("swept despite an elected model"),
    )
    assert kvl.ensure_voice_lane_warm() is False


def test_warm_is_skipped_under_an_operator_override(monkeypatch):
    kvl.reset_warm_state()
    monkeypatch.setenv("JARVIS_KAREN_VOICE_MODEL", "operator/choice")
    monkeypatch.setattr(
        kvl.threading, "Thread",
        lambda **kw: pytest.fail("spent probes the operator did not ask for"),
    )
    assert kvl.ensure_voice_lane_warm() is False


def test_warm_never_raises(monkeypatch):
    kvl.reset_warm_state()

    def _boom(**_kw):
        raise RuntimeError("no threads left")

    monkeypatch.setattr(kvl.threading, "Thread", _boom)
    assert kvl.ensure_voice_lane_warm() is False


def test_the_answer_path_warms_the_lane():
    """Structural pin against the wired-but-inert trap: refresh_voice_lane had
    zero production callers when it was written, and a learner nobody starts
    never learns."""
    from pathlib import Path

    src = Path(
        "backend/core/ouroboros/governance/karen_answer_engine.py",
    ).read_text(encoding="utf-8")
    assert "ensure_voice_lane_warm" in src


# ---------------------------------------------------------------------------
# Ledger poisoning — a network fault is not a model verdict
# ---------------------------------------------------------------------------
#
# Observed live: a sandboxed run's probes all failed with DNS errors, and the
# lane cached those as fresh "never spoke" verdicts for six hours. The
# remote-only host then resolved None, had no engine to fall back to, and
# answered a heard utterance with silence.


def test_a_transport_failure_expires_fast(monkeypatch):
    """THE REGRESSION. A DNS error describes the network at one instant, not
    the model — it must not poison the lane for the full TTL."""
    monkeypatch.setenv("JARVIS_KAREN_VOICE_FAILURE_TTL_S", "60")
    dead = _rec("victim", -1.0, tokens=0, age_s=300,
                reason="dispatch_error:ClientConnectorDNSError")
    spoke = _rec("witness", 0.9, age_s=300, reason="ok")
    assert dead.fresh() is False, "a 5-minute-old network fault is still trusted"
    assert spoke.fresh() is True, "a genuine measurement expired with it"


def test_a_stale_transport_failure_is_reprobed(monkeypatch):
    """stale_or_unknown must offer the victim up for measurement again."""
    monkeypatch.setenv("JARVIS_KAREN_VOICE_FAILURE_TTL_S", "60")
    led = VoiceLatencyLedger().load()
    led.record(_rec("victim", -1.0, tokens=0, age_s=300,
                    reason="probe_error:OSError"))
    assert "victim" in led.stale_or_unknown(["victim"])


def test_behavioural_verdicts_keep_the_full_ttl():
    """no_tokens IS knowledge about the model — a reasoner that never speaks
    stays disqualified for the normal window."""
    rec = _rec("thinker", -1.0, tokens=0, age_s=3600, reason="no_tokens")
    assert rec.transport_failure is False
    assert rec.fresh() is True


# ---------------------------------------------------------------------------
# Degraded election — a slow voice beats no voice
# ---------------------------------------------------------------------------
#
# Observed live an hour after the first benchmark: the SAME models measured
# 2.1-2.7s that had measured 0.9-1.1s. Every candidate missed the 1.5s budget
# and the lane elected nobody — which for a remote-only host means silence.


def test_the_budget_is_a_preference_not_a_cliff():
    """THE REGRESSION. All candidates over budget but under the hard cap →
    elect the fastest of them, not nobody."""
    led = VoiceLatencyLedger().load()
    led.record(_rec("slow-a", 2.13))
    led.record(_rec("slow-b", 2.69))
    assert led.best() == "slow-a"


def test_a_fast_model_always_beats_the_degraded_tier():
    led = VoiceLatencyLedger().load()
    led.record(_rec("degraded", 2.2))
    led.record(_rec("snappy", 0.9))
    assert led.best() == "snappy"


def test_the_hard_cap_is_still_a_cliff(monkeypatch):
    """Somewhere silence genuinely is better; 22.8s is past it."""
    led = VoiceLatencyLedger().load()
    led.record(_rec("Qwen/Qwen3.5-397B-A17B-FP8", 22.84))
    assert led.best() is None
    monkeypatch.setenv("JARVIS_KAREN_VOICE_TTFT_HARD_CAP_S", "30")
    assert led.best() == "Qwen/Qwen3.5-397B-A17B-FP8", "cap is a knob"


def test_the_host_warms_the_lane_at_boot():
    """Remote-only + cold lane = guaranteed first-turn silence; boot is the
    one moment probing costs the operator nothing."""
    from pathlib import Path

    src = Path("backend/audio/audio_plane_host.py").read_text(encoding="utf-8")
    assert "ensure_voice_lane_warm" in src
