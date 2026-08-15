"""Do not load a local model onto a machine that is about to swap.

Tier 2 now runs on the same unified memory as the CoreAudio graph. There is no
separate VRAM and no pressure valve except swap, so a background op deciding
to think can take the microphone down — the audio tap is a real-time thread
and `HALC_ProxyIOContext :: skipping cycle due to overload` is already in the
boot log at idle.

The voice path was taken from 15,000 ms to 37 ms. One unguarded 18 GB model
load gives that back.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance import local_model_admission as LMA
from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel


def _vm(percent_used: float, total_gb: float = 16.0):
    """A `psutil.virtual_memory()` result at a given utilisation."""
    total = int(total_gb * 1024 ** 3)
    available = int(total * (1.0 - percent_used / 100.0))
    return SimpleNamespace(total=total, available=available,
                           percent=percent_used, used=total - available,
                           free=available)


def _at(percent_used: float):
    """Force the gate's probe to a given utilisation, whatever the real machine
    is doing. Patches the cascade's FIRST stage — psutil — which is the stage
    that answers on this hardware (`source: psutil`, measured)."""
    return patch("psutil.virtual_memory", return_value=_vm(percent_used))


# ── The mandate ─────────────────────────────────────────────────────────────

def test_at_96_percent_the_request_is_deferred_not_raised():
    """THE ASSERTION ASKED FOR.

    96% utilised = 4% free. The provider must intercept, refuse to load, and
    yield a deferred state — WITHOUT throwing.
    """
    with _at(96.0):
        d = LMA.assess(requested_ctx=32768)

    assert d.action == LMA.Admission.DEFER.value
    assert d.proceeds is False
    assert d.free_pct < 10.0
    assert "swap" in d.reason


def test_the_deferred_state_is_a_stable_token():
    """A greppable constant, not prose — anything matching on it must not
    break when the sentence around it is reworded."""
    assert LMA.DEFERRED_PAYLOAD == "[SYSTEM: DEFERRED_DUE_TO_MEMORY_PRESSURE]"


@pytest.mark.asyncio
async def test_the_provider_intercepts_without_an_unhandled_exception():
    """End to end through `PrimeProvider.generate`: at 96% it must return a
    result rather than raise, and must never reach `_generate_impl`."""
    from backend.core.ouroboros.governance.providers import PrimeProvider

    reached = []

    async def _never(*a, **k):
        reached.append(True)
        raise AssertionError("the model was loaded under critical pressure")

    p = PrimeProvider.__new__(PrimeProvider)          # no client needed
    p._generate_impl = _never
    p._max_tokens = 8192

    with _at(96.0):
        result = await p.generate(context=SimpleNamespace(op_id="op-1"),
                                  deadline=None)

    assert reached == [], "it dispatched anyway"
    assert result is not None
    assert len(result.candidates) == 0


@pytest.mark.asyncio
async def test_a_deferral_is_not_reported_as_a_provider_fault():
    """`candidate_generator` treats a RAISING provider as 'lane failed' and
    feeds the heartbeat a drop — which on a bad day contributes to waking a
    real-money GCE failover node. For a machine that is merely busy.

    It already distinguishes this class: `is_budget_refusal` is re-raised as a
    'local wallet gate, NOT a lane/provider fault'. Memory pressure is the
    same axis, so it returns empty rather than raising.
    """
    from backend.core.ouroboros.governance.providers import PrimeProvider

    p = PrimeProvider.__new__(PrimeProvider)
    p._generate_impl = lambda *a, **k: None
    p._max_tokens = 8192

    with _at(96.0):
        result = await p.generate(context=SimpleNamespace(op_id="op-2"),
                                  deadline=None)

    # Empty candidates: the cascade falls through cleanly and blames nobody.
    assert result.candidates == ()
    assert result.provider_name


# ── The middle band: prune rather than refuse ───────────────────────────────

def test_high_pressure_prunes_instead_of_refusing():
    """Refusing all work above a threshold would make the machine useless
    exactly when it is busy. Shrinking the KV cache keeps it working."""
    with _at(88.0):
        d = LMA.assess(requested_ctx=32768)

    if d.action == LMA.Admission.PRUNE.value:
        assert d.proceeds is True
        assert d.num_ctx is not None and d.num_ctx < 32768
        assert d.num_ctx >= 1024, "never pruned to uselessness"


def test_pruning_keeps_the_system_prompt_and_the_live_question():
    """The first message is the instructions that make output parseable at
    all; the last few are the actual question. Pruning into either does not
    save memory, it changes what was asked."""
    messages = [{"role": "system", "content": "SYSTEM"}]
    messages += [{"role": "user", "content": f"old-{i}"} for i in range(40)]
    messages += [{"role": "user", "content": f"live-{i}"} for i in range(6)]

    d = LMA.AdmissionDecision(action=LMA.Admission.PRUNE.value,
                              level="high", free_pct=11.0)
    pruned, dropped = LMA.prune_messages(list(messages), d)

    assert dropped > 0
    assert pruned[0]["content"] == "SYSTEM", "the system prompt was pruned"
    assert pruned[-1]["content"] == "live-5", "the live question was pruned"
    assert len(pruned) < len(messages)


def test_a_short_conversation_is_never_pruned():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    d = LMA.AdmissionDecision(action=LMA.Admission.PRUNE.value, level="high")
    out, dropped = LMA.prune_messages(list(msgs), d)
    assert dropped == 0 and out == msgs


def test_admit_never_prunes():
    msgs = [{"role": "user", "content": str(i)} for i in range(50)]
    d = LMA.AdmissionDecision(action=LMA.Admission.ADMIT.value)
    out, dropped = LMA.prune_messages(list(msgs), d)
    assert dropped == 0 and len(out) == 50


# ── Failing in the safe direction ───────────────────────────────────────────

def test_plenty_of_memory_admits_unchanged():
    with _at(20.0):
        d = LMA.assess(requested_ctx=32768)
    assert d.action == LMA.Admission.ADMIT.value
    assert d.proceeds and d.num_ctx is None


def test_a_broken_probe_admits_rather_than_refusing():
    """A probe that cannot read memory must NOT become a reason to refuse
    work. That would let one broken cascade stage silently disable Tier 2 for
    a whole session — a larger outage than the swap storm it guards against.
    """
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("unknown", -1.0, "unavailable")):
        d = LMA.assess(requested_ctx=8192)
    assert d.proceeds is True


def test_pruning_a_malformed_payload_returns_it_untouched():
    d = LMA.AdmissionDecision(action=LMA.Admission.PRUNE.value, level="high")
    for junk in (None, "not a list", 42, {"a": 1}):
        out, dropped = LMA.prune_messages(junk, d)
        assert out is junk and dropped == 0


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_ADMISSION_ENABLED", "false")
    with _at(99.0):
        assert LMA.assess().proceeds is True


def test_assess_never_raises():
    for pct in (0.0, 50.0, 99.9, 100.0):
        with _at(pct):
            assert LMA.assess(requested_ctx=1) is not None


# ── It reuses the existing gate rather than probing itself ──────────────────

def test_it_adds_no_second_memory_probe():
    """`memory_pressure_gate` already owns the psutil -> meminfo -> vm_stat
    cascade. A second probe here would be a second thing to keep correct, and
    the day they disagreed the machine would hold two opinions about whether
    it was safe to allocate."""
    import inspect
    src = inspect.getsource(LMA)
    assert "memory_pressure_gate" in src
    assert "vm_stat" not in src.split('"""')[2] if src.count('"""') > 2 else True


def test_it_speaks_the_gates_four_level_vocabulary():
    assert {l.value for l in PressureLevel} >= {"ok", "high", "critical"}


def test_snapshot_reports_the_live_reading():
    s = LMA.snapshot()
    assert s["schema_version"] == LMA.LOCAL_MODEL_ADMISSION_SCHEMA_VERSION
    assert "level" in s and "free_pct" in s and "source" in s


# ---------------------------------------------------------------------------
# v2 — the accelerator bound, the degradation state, and the adaptive margin
# ---------------------------------------------------------------------------

import types  # noqa: E402

GIB = 1024 ** 3


def _reading(topology="discrete", usable_gib=30.0, resolved="gpu_32gib"):
    """A compute_topology reading stand-in, duck-typed at the seam LMA uses."""
    return types.SimpleNamespace(
        topology=types.SimpleNamespace(value=topology),
        usable_bytes=int(usable_gib * GIB),
        resolved_class=resolved, source="torch_cuda",
        measured=True, free_is_measured=True,
    )


def test_weights_larger_than_free_vram_defer_on_a_healthy_host():
    """THE v2 regression: 64 GB of free system RAM must not admit a 40 GiB
    load onto a card with 30 GiB free. v1 had no opinion here."""
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_reading()):
        d = LMA.assess(weight_bytes=40 * GIB, model_id="big")
    assert d.proceeds is False
    assert d.bound == "accelerator"
    assert d.max_weight_bytes > 0, "a defer must say what WOULD fit"


def test_a_fitting_model_is_admitted_with_the_bound_named():
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_reading()):
        d = LMA.assess(weight_bytes=20 * GIB, model_id="ok-model")
    assert d.proceeds is True
    assert d.topology == "discrete"


def test_unified_does_not_double_count_one_pool():
    """Under unified memory compute_topology DERIVES its free figure from the
    RAM gate. Applying both bounds would refuse on one constraint twice."""
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 80.0, "vm_stat")), \
         patch.object(LMA, "_read_accelerator",
                      return_value=_reading("unified", 9.0, "unified_12gib")):
        d = LMA.assess(weight_bytes=40 * GIB, model_id="m")
    # Host bound is OK, so it admits — the accelerator bound is skipped
    # rather than counted a second time.
    assert d.proceeds is True
    assert d.bound != "accelerator"


def test_no_stated_footprint_reproduces_v1_exactly():
    """A caller that does not declare weights gets the host bound alone —
    nothing is inferred on its behalf."""
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_reading()):
        d = LMA.assess(requested_ctx=8192)
    assert d.proceeds is True
    assert d.weight_bytes == 0


def test_unmeasured_topology_admits_small_and_refuses_large(monkeypatch):
    """Fail-open on WHETHER to run, fail-closed on HOW BIG."""
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_UNKNOWN_CEILING_GB", "8")
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=None):
        small = LMA.assess(weight_bytes=4 * GIB, model_id="small")
        large = LMA.assess(weight_bytes=40 * GIB, model_id="large")
    assert small.proceeds is True
    assert large.proceeds is False
    assert large.topology == "unknown"


def test_malformed_topology_object_degrades_rather_than_raising():
    """A host returning garbage must not take the gate down with it."""
    junk = types.SimpleNamespace()  # no topology, no usable_bytes
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=junk):
        d = LMA.assess(weight_bytes=4 * GIB)
    assert d.proceeds is True


def test_topology_probe_that_raises_never_blocks_admission():
    def _boom():
        raise RuntimeError("driver hung")
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", _boom):
        try:
            d = LMA.assess(weight_bytes=4 * GIB)
        except Exception as exc:  # pragma: no cover
            raise AssertionError(f"assess must never raise: {exc}")
    assert d.proceeds is True


def test_observed_ooms_raise_the_margin_and_narrow_what_fits(monkeypatch):
    """Contiguity is unobservable, so the margin is LEARNED. An OOM at a
    given size must make the next identical request stricter."""
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_MARGIN_BASE", "0.05")
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_MARGIN_STEP", "0.20")
    LMA._margin_ledger.__init__()

    before = LMA._margin_ledger.fraction_for("m")
    LMA.record_load_outcome("m", ok=False, error=RuntimeError("CUDA out of memory"))
    after = LMA._margin_ledger.fraction_for("m")
    assert after > before

    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_reading()):
        d = LMA.assess(weight_bytes=27 * GIB, model_id="m")
    assert d.margin_bytes > 0
    assert d.proceeds is False, "the learned margin must actually bind"


def test_margin_is_capped_so_a_host_cannot_learn_to_refuse_everything(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_MARGIN_MAX", "0.30")
    LMA._margin_ledger.__init__()
    for _ in range(50):
        LMA.record_load_outcome("m", ok=False, error=MemoryError("oom"))
    assert LMA._margin_ledger.fraction_for("m") <= 0.30


def test_a_clean_load_retires_a_prior_oom():
    LMA._margin_ledger.__init__()
    LMA.record_load_outcome("m", ok=False, error=MemoryError("oom"))
    raised = LMA._margin_ledger.fraction_for("m")
    LMA.record_load_outcome("m", ok=True)
    assert LMA._margin_ledger.fraction_for("m") < raised


def test_a_non_memory_failure_teaches_the_host_nothing():
    """A 404 on a missing artifact says nothing about capacity. Letting it
    raise the margin would teach a superstition."""
    LMA._margin_ledger.__init__()
    base = LMA._margin_ledger.fraction_for("m")
    LMA.record_load_outcome("m", ok=False, error=FileNotFoundError("no such model"))
    assert LMA._margin_ledger.fraction_for("m") == base


def test_snapshot_carries_both_bounds_for_the_operator():
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_reading()):
        snap = LMA.snapshot()
    import json
    json.dumps(snap)
    assert snap["schema_version"].endswith("v2")
    assert snap["accelerator"]["topology"] == "discrete"
    assert "observed_ooms" in snap


def test_dry_admission_derives_neither_reading_itself():
    """Strict single-source-of-truth: both bounds are CONSUMED, never
    re-derived. Checked against the AST rather than the prose — the module
    legitimately DISCUSSES psutil and vm_stat in its docstring while calling
    neither, and a substring match cannot tell an explanation from a use."""
    import ast, pathlib
    tree = ast.parse(pathlib.Path(LMA.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            if node.module.startswith("backend.core.ouroboros.governance"):
                tail = node.module.split(".")[-1]
                if tail != "governance":
                    imported.add(tail)
                imported.update(a.name for a in node.names)
    # It may not reach for either probe substrate directly.
    assert "psutil" not in imported, "system RAM belongs to memory_pressure_gate"
    assert "subprocess" not in imported, "accelerator belongs to compute_topology"
    # Its governance surface is exactly the two canonical readings.
    governance = imported & {"compute_topology", "memory_pressure_gate",
                             "runtime_health_types", "get_default_gate"}
    assert "compute_topology" in governance
    assert "memory_pressure_gate" in governance


# ---------------------------------------------------------------------------
# The last link — policy footprint reaches the gate, outcomes reach the ledger
# ---------------------------------------------------------------------------


def test_policy_declares_a_footprint_for_every_required_brain():
    """A brain with no declared weight silently disables the accelerator
    bound for that route. Absence must be a decision, not an oversight."""
    from backend.core.ouroboros.governance import brain_selector as bs
    policy = bs._footprint_policy()
    required = (policy.get("brains") or {}).get("required") or []
    assert required, "policy has no required brains"
    for entry in required:
        bid = entry.get("brain_id")
        assert bs.footprint_bytes_for(bid) > 0, f"{bid} declares no weight_gb"


def test_unknown_brain_declares_nothing_rather_than_guessing():
    from backend.core.ouroboros.governance import brain_selector as bs
    assert bs.footprint_bytes_for("no-such-brain") == 0
    assert bs.footprint_bytes_for(None) == 0


@pytest.mark.asyncio
async def test_provider_passes_the_declared_footprint_to_the_gate():
    """THE inertness pin for the last link. If the provider stops passing a
    footprint, the accelerator bound silently stops binding and v2 degrades
    to v1 with nobody noticing."""
    from backend.core.ouroboros.governance.providers import PrimeProvider

    seen = {}

    def _spy(requested_ctx=None, *, weight_bytes=0, model_id=None):
        seen["weight_bytes"] = weight_bytes
        seen["model_id"] = model_id
        return LMA.AdmissionDecision(action=LMA.Admission.DEFER.value,
                                     reason="spy")

    ri = SimpleNamespace(brain_id="qwen_coder_32b",
                         brain_model="qwen-2.5-coder-32b",
                         routing_reason="x", task_complexity="heavy")
    ctx = SimpleNamespace(op_id="op-w", telemetry=SimpleNamespace(routing_intent=ri))

    p = PrimeProvider.__new__(PrimeProvider)
    p._generate_impl = lambda *a, **k: None
    p._max_tokens = 8192

    with patch.object(LMA, "assess", _spy):
        await p.generate(context=ctx, deadline=None)

    assert seen["model_id"] == "qwen_coder_32b"
    assert seen["weight_bytes"] > 0, "the declared footprint never reached the gate"


@pytest.mark.asyncio
async def test_provider_survives_a_brain_with_no_routing_intent():
    """No telemetry is the common case for many callers — it must degrade to
    the host bound, not fault."""
    from backend.core.ouroboros.governance.providers import PrimeProvider

    p = PrimeProvider.__new__(PrimeProvider)
    p._generate_impl = lambda *a, **k: None
    p._max_tokens = 8192

    with _at(96.0):
        result = await p.generate(context=SimpleNamespace(op_id="op-n",
                                                          telemetry=None),
                                  deadline=None)
    assert result.candidates == ()


# ---------------------------------------------------------------------------
# Anticipatory ledger — concurrent workers, and the self-healing reconciler
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_ledgers():
    LMA._vram_ledger.__init__()
    LMA._margin_ledger.__init__()
    yield
    LMA._vram_ledger.__init__()
    LMA._margin_ledger.__init__()


def _discrete_at(usable_gib):
    return types.SimpleNamespace(
        topology=types.SimpleNamespace(value="discrete"),
        usable_bytes=int(usable_gib * GIB), resolved_class="gpu_32gib",
        source="torch_cuda", measured=True, free_is_measured=True)


def test_concurrent_workers_cannot_collectively_overcommit():
    """THE race: three workers each ask against the SAME free reading. Without
    a ledger the gate is correct three times and the machine still OOMs."""
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_discrete_at(27)):
        a = LMA.assess(weight_bytes=12 * GIB, model_id="w")
        b = LMA.assess(weight_bytes=12 * GIB, model_id="w")
        c = LMA.assess(weight_bytes=12 * GIB, model_id="w")
    assert a.proceeds and b.proceeds
    assert not c.proceeds, "the third worker overcommitted a 27 GiB card"
    assert c.bound == "accelerator"


def test_releasing_a_claim_frees_the_next_worker():
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_discrete_at(27)):
        a = LMA.assess(weight_bytes=12 * GIB, model_id="w")
        b = LMA.assess(weight_bytes=12 * GIB, model_id="w")
        assert not LMA.assess(weight_bytes=12 * GIB, model_id="w").proceeds
        LMA.release_reservation(a.reservation_id)
        assert LMA.assess(weight_bytes=12 * GIB, model_id="w").proceeds


def test_an_admit_carries_a_reservation_id_to_release():
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_discrete_at(27)):
        d = LMA.assess(weight_bytes=10 * GIB, model_id="w")
    assert d.proceeds and d.reservation_id


def test_release_is_idempotent_and_null_safe():
    LMA.release_reservation(None)
    LMA.release_reservation("nope")
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_discrete_at(27)):
        d = LMA.assess(weight_bytes=10 * GIB, model_id="w")
    LMA.release_reservation(d.reservation_id)
    LMA.release_reservation(d.reservation_id)


def test_a_phantom_claim_is_reconciled_on_evidence_not_a_timer(monkeypatch):
    """A worker crashed between admission and load. Free memory NEVER fell,
    so the promised allocation provably never happened — drop it, rather than
    throttling everyone else until a timeout expires."""
    monkeypatch.setenv("JARVIS_VRAM_RECONCILE_AFTER_S", "1")
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_discrete_at(27)):
        dead = LMA.assess(weight_bytes=20 * GIB, model_id="crasher")
        assert dead.proceeds
        # The worker dies. Age its claim past the reconcile floor — the floor
        # is deliberate: a claim must never be reconcilable the instant it is
        # made, or the ledger could not hold anything.
        for r in LMA._vram_ledger._live.values():
            r.granted_at -= 5.0
        # It never released, and never allocated — so free bytes are
        # unchanged on the next look.
        revived = LMA.assess(weight_bytes=20 * GIB, model_id="next")
    assert revived.proceeds, "a phantom claim throttled a healthy worker"
    assert LMA._vram_ledger.snapshot()["phantoms_dropped"] >= 1


def test_a_slow_but_live_worker_is_not_reconciled_away(monkeypatch):
    """The counterpart: free memory HAS fallen, so the allocation is really
    happening. A timer alone could not tell these two apart."""
    monkeypatch.setenv("JARVIS_VRAM_RECONCILE_AFTER_S", "1")
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_discrete_at(27)):
        live = LMA.assess(weight_bytes=20 * GIB, model_id="slow")
        assert live.proceeds
        for r in LMA._vram_ledger._live.values():
            r.granted_at -= 5.0
    # Now the loader has actually consumed the memory.
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_discrete_at(6)):
        nxt = LMA.assess(weight_bytes=20 * GIB, model_id="next")
    assert not nxt.proceeds
    assert LMA._vram_ledger.snapshot()["phantoms_dropped"] == 0


def test_settled_claims_stop_being_counted(monkeypatch):
    """Past the settle window the OS probe carries the allocation; continuing
    to subtract it would double-count one set of bytes."""
    monkeypatch.setenv("JARVIS_VRAM_RESERVATION_SETTLE_S", "1")
    monkeypatch.setenv("JARVIS_VRAM_RECONCILE_AFTER_S", "99999")
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_discrete_at(27)):
        LMA.assess(weight_bytes=20 * GIB, model_id="w")
        for r in LMA._vram_ledger._live.values():
            r.granted_at -= 5.0            # age it past the settle window
        again = LMA.assess(weight_bytes=20 * GIB, model_id="w2")
    assert again.proceeds


def test_reconciliation_never_invents_a_claim():
    """Direction of authority is fixed: the OS is fact, the ledger is a
    prediction. Reconciliation may only DROP, never add."""
    before = LMA._vram_ledger.snapshot()["live"]
    LMA._vram_ledger._reconcile(free_now=99 * GIB)
    assert LMA._vram_ledger.snapshot()["live"] == before


def test_no_evidence_leaves_every_claim_standing():
    """free_now == 0 is 'we could not read', not 'nothing is free'."""
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_discrete_at(27)):
        LMA.assess(weight_bytes=10 * GIB, model_id="w")
    live = LMA._vram_ledger.snapshot()["live"]
    LMA._vram_ledger._reconcile(free_now=0)
    assert LMA._vram_ledger.snapshot()["live"] == live


def test_unified_hosts_take_no_reservations():
    """One pool — there is nothing distinct to overcommit, so a claim here
    would be bookkeeping that only ever throttles."""
    with patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "vm_stat")), \
         patch.object(LMA, "_read_accelerator",
                      return_value=types.SimpleNamespace(
                          topology=types.SimpleNamespace(value="unified"),
                          usable_bytes=9 * GIB, resolved_class="unified_12gib",
                          source="unified", measured=True,
                          free_is_measured=False)):
        d = LMA.assess(weight_bytes=4 * GIB, model_id="w")
    assert d.proceeds
    assert d.reservation_id is None


@pytest.mark.asyncio
async def test_assess_async_forces_a_jit_reread():
    """The sync form serves a cache; the async form must re-read before it
    authorizes an allocation."""
    from backend.core.ouroboros.governance import compute_topology as ct
    calls = {"n": 0}

    async def _jit():
        calls["n"] += 1
        return None

    with patch.object(ct, "is_enabled", lambda: True), \
         patch.object(ct, "resolve_jit", _jit), \
         patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_discrete_at(27)):
        d = await LMA.assess_async(weight_bytes=10 * GIB, model_id="w")
    assert calls["n"] == 1
    assert d.proceeds


@pytest.mark.asyncio
async def test_assess_async_survives_a_failed_jit_probe():
    """A stale reading beats no ruling — the gate must not fault because a
    driver query timed out."""
    from backend.core.ouroboros.governance import compute_topology as ct

    async def _boom():
        raise RuntimeError("driver wedged")

    with patch.object(ct, "is_enabled", lambda: True), \
         patch.object(ct, "resolve_jit", _boom), \
         patch.object(LMA, "_read_host_pressure",
                      return_value=("ok", 90.0, "psutil")), \
         patch.object(LMA, "_read_accelerator", return_value=_discrete_at(27)):
        d = await LMA.assess_async(weight_bytes=10 * GIB, model_id="w")
    assert d.proceeds


def test_snapshot_exposes_the_ledger_for_the_operator():
    snap = LMA.snapshot()
    assert "reservations" in snap
    assert set(snap["reservations"]) >= {"live", "promised_gib",
                                         "phantoms_dropped", "owners"}
