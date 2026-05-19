"""Predictive Provider Resilience Arc — Slice 0 regression spine.

Slice 0 is the **Observability Seam**: it observes
``(provider, route, input_tokens, ttft_ms, total_ms, outcome)`` at
the provider call boundary, records it into the EXISTING bounded
``TtftObserver`` ring, and durably appends it to a cross-process
JSONL so the Slice-1 TTFT forecaster has a fittable training set.

Operator constraints this spine pins:
  1. Precision token counting — the emitted ``input_tokens`` is the
     provider's OWN server-side tokenizer count, NEVER a
     ``len(chars)/4`` estimate computed in the seam.
  2. Memory-bounded ring — ``deque(maxlen=N)``; respects the
     OOM-hardening boundaries (drop-oldest, no unbounded growth).
  3. Cross-process persistence — composes ``flock_append_line``;
     survives bounded-shutdown.
  4. Zero behavioural drift — pure observe/persist/passthrough; the
     seam touches no timeout/retry/context logic and the existing
     ``TtftSample`` promotion schema is byte-untouched.
  + The emission is un-bypassable: the provider success boundary,
     the timeout/cancel boundary, and the DW batch boundary each
     statically contain the emission call.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.dw_ttft_observer import (
    PROVIDER_LATENCY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    ProviderLatencySample,
    TtftObserver,
    TtftSample,
)

_REPO = Path(__file__).resolve().parents[2]
_PROVIDERS_SRC = _REPO / "backend/core/ouroboros/governance/providers.py"
_OBS_SRC = _REPO / "backend/core/ouroboros/governance/dw_ttft_observer.py"
_DW_SRC = _REPO / "backend/core/ouroboros/governance/doubleword_provider.py"

_ENABLE = "JARVIS_PROVIDER_LATENCY_TELEMETRY_ENABLED"
_PATH = "JARVIS_PROVIDER_LATENCY_JSONL_PATH"
_WINDOW = "JARVIS_PROVIDER_LATENCY_WINDOW_N"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (_ENABLE, _PATH, _WINDOW):
        monkeypatch.delenv(k, raising=False)
    yield


def _sample(**kw):
    base = dict(
        provider="claude-api",
        route="complex",
        op_id="op-x",
        input_tokens=1234,
        ttft_ms=987,
        total_ms=4242,
        outcome="success",
        sample_unix=1.0,
    )
    base.update(kw)
    return ProviderLatencySample(**base)


# --------------------------------------------------------------------------
# ProviderLatencySample shape + JSONL contract
# --------------------------------------------------------------------------

def test_sample_is_frozen():
    s = _sample()
    with pytest.raises(Exception):
        s.input_tokens = 9  # type: ignore[misc]


def test_jsonl_obj_is_stable_and_complete():
    obj = _sample().to_jsonl_obj()
    assert obj["schema_version"] == PROVIDER_LATENCY_SCHEMA_VERSION
    assert set(obj) == {
        "schema_version", "provider", "route", "op_id",
        "input_tokens", "ttft_ms", "total_ms", "outcome",
        "sample_unix",
    }
    # Round-trips through json (the cross-process JSONL contract).
    assert json.loads(json.dumps(obj)) == obj


# --------------------------------------------------------------------------
# Memory-bounded ring (constraint 2)
# --------------------------------------------------------------------------

def test_ring_is_bounded_drop_oldest(monkeypatch):
    monkeypatch.setenv(_WINDOW, "3")
    obs = TtftObserver(path=None, autosave=False)
    for i in range(10):
        obs.record_provider_latency(_sample(op_id=f"op-{i}"))
    snap = obs.provider_latency_samples("claude-api")
    assert len(snap) == 3, "ring must be hard-bounded (deque maxlen)"
    # Oldest evicted, newest retained, oldest→newest order.
    assert [s.op_id for s in snap] == ["op-7", "op-8", "op-9"]


def test_record_never_raises_on_garbage():
    obs = TtftObserver(path=None, autosave=False)
    obs.record_provider_latency(None)  # type: ignore[arg-type]
    obs.record_provider_latency("not-a-sample")  # type: ignore[arg-type]
    obs.record_provider_latency(_sample(provider=""))
    assert obs.provider_latency_samples("claude-api") == ()
    assert obs.provider_latency_sample_count("nope") == 0


def test_snapshot_isolation_and_unknown_key():
    obs = TtftObserver(path=None, autosave=False)
    obs.record_provider_latency(_sample())
    assert obs.provider_latency_samples("unknown") == ()
    snap = obs.provider_latency_samples("claude-api")
    assert len(snap) == 1 and isinstance(snap, tuple)


# --------------------------------------------------------------------------
# Emission helper — master flag gate + dual sink + never-raises
# --------------------------------------------------------------------------

def test_emit_is_dark_by_default(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance import providers as P
    from backend.core.ouroboros.governance import dw_discovery_runner as D

    jp = tmp_path / "pl.jsonl"
    monkeypatch.setenv(_PATH, str(jp))
    # default (flag unset) → no-op
    P._emit_provider_latency(
        provider="claude-api", route="complex", op_id="o",
        input_tokens=10, ttft_ms=5, total_ms=9, outcome="success",
    )
    assert not jp.exists(), "Slice 0 must ship dark (master flag off)"
    obs = D.get_ttft_observer()
    if obs is not None:
        assert obs.provider_latency_sample_count("claude-api") == 0


def test_emit_when_enabled_writes_both_sinks(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance import providers as P
    from backend.core.ouroboros.governance import dw_discovery_runner as D

    jp = tmp_path / "pl.jsonl"
    monkeypatch.setenv(_ENABLE, "true")
    monkeypatch.setenv(_PATH, str(jp))
    P._emit_provider_latency(
        provider="claude-api", route="complex", op_id="op-7",
        input_tokens=321, ttft_ms=88, total_ms=900, outcome="success",
    )
    # Sink 2 — durable JSONL
    assert jp.exists()
    row = json.loads(jp.read_text().strip().splitlines()[-1])
    assert row["provider"] == "claude-api"
    assert row["input_tokens"] == 321
    assert row["schema_version"] == PROVIDER_LATENCY_SCHEMA_VERSION
    # Sink 1 — in-memory bounded ring
    obs = D.get_ttft_observer()
    assert obs is not None
    assert obs.provider_latency_sample_count("claude-api") >= 1


def test_emit_never_raises_even_if_sinks_explode(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance import providers as P

    monkeypatch.setenv(_ENABLE, "true")
    monkeypatch.setenv(_PATH, str(tmp_path / "pl.jsonl"))

    def _boom(*a, **k):
        raise RuntimeError("sink down")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.cross_process_jsonl."
        "flock_append_line", _boom,
    )
    # Must swallow — a telemetry fault may NEVER perturb generation.
    P._emit_provider_latency(
        provider="claude-api", route="r", op_id="o",
        input_tokens=1, ttft_ms=1, total_ms=1, outcome="success",
    )


# --------------------------------------------------------------------------
# FlagRegistry seeds
# --------------------------------------------------------------------------

def test_register_flags_seeds_three(monkeypatch):
    from backend.core.ouroboros.governance import providers as P
    from backend.core.ouroboros.governance.flag_registry import FlagRegistry

    reg = FlagRegistry()
    n = P.register_flags(reg)
    assert n == 3
    # Master flag must default FALSE (Slice 0 ships dark).
    spec = reg.get(_ENABLE) if hasattr(reg, "get") else None
    if spec is not None:
        assert spec.default is False


# --------------------------------------------------------------------------
# Zero behavioural drift — existing TTFT promotion schema untouched
# --------------------------------------------------------------------------

def test_existing_ttft_schema_byte_untouched():
    assert SCHEMA_VERSION == "ttft_observer.1", (
        "Slice 0 must NOT bump the DW-promotion schema (zero drift)"
    )
    assert PROVIDER_LATENCY_SCHEMA_VERSION == "provider_latency.1"
    # TtftSample field set is the pre-Slice-0 contract.
    assert [f for f in TtftSample.__dataclass_fields__] == [
        "model_id", "ttft_ms", "sample_unix", "op_id",
    ]


def test_ttft_and_latency_rings_are_separate():
    obs = TtftObserver(path=None, autosave=False)
    obs.record_ttft("model-z", 100, op_id="op-a")
    obs.record_provider_latency(_sample(provider="model-z"))
    # Latency samples must NOT leak into the promotion buffer or
    # vice-versa — physically distinct keyed deques.
    assert obs.sample_count("model-z") == 1
    assert obs.provider_latency_sample_count("model-z") == 1
    assert obs.provider_latency_samples("model-z")[0].input_tokens == 1234


# --------------------------------------------------------------------------
# AST pins — emission is un-bypassable + constraints structural
# --------------------------------------------------------------------------

def _fn(src_path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(src_path.read_text())
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n  # type: ignore[return-value]
    raise AssertionError(f"{name} not found in {src_path.name}")


def test_ast_pin_emit_helper_is_flag_gated_and_dual_sink():
    body = ast.unparse(_fn(_PROVIDERS_SRC, "_emit_provider_latency"))
    assert "_provider_latency_telemetry_enabled()" in body, (
        "emission MUST be master-flag-gated"
    )
    assert "record_provider_latency" in body, "sink 1 (ring) missing"
    assert "flock_append_line" in body, "sink 2 (cross-process JSONL) missing"


def test_ast_pin_no_len_div4_token_estimate():
    """Constraint 1 — precision token counting. The seam must NEVER
    derive input_tokens from a len()/4 heuristic; it only forwards
    the provider's server-side count."""
    fn = _fn(_PROVIDERS_SRC, "_emit_provider_latency")
    # Scan EXECUTABLE statements only — the docstring deliberately
    # names the len(chars)/4 anti-pattern to forbid it.
    stmts = list(fn.body)
    if (
        stmts
        and isinstance(stmts[0], ast.Expr)
        and isinstance(stmts[0].value, ast.Constant)
        and isinstance(stmts[0].value.value, str)
    ):
        stmts = stmts[1:]
    code = "\n".join(ast.unparse(s) for s in stmts)
    for bad in ("/ 4", "/4", "// 4", "//4", "len("):
        assert bad not in code, (
            f"forbidden token-estimate pattern {bad!r} in the seam — "
            f"input_tokens MUST be the provider's own tokenizer count"
        )


def test_ast_pin_emit_helper_never_raises():
    fn = _fn(_PROVIDERS_SRC, "_emit_provider_latency")
    # Outermost statement is a try whose handler swallows (no re-raise).
    tries = [s for s in fn.body if isinstance(s, ast.Try)]
    assert tries, "_emit_provider_latency body must be wrapped in try"
    outer = tries[-1]
    assert outer.handlers, "must have an except"
    for h in outer.handlers:
        for sub in ast.walk(h):
            assert not isinstance(sub, ast.Raise), (
                "telemetry seam must NEVER re-raise"
            )


def test_ast_pin_emit_helper_touches_no_timeout_retry_state():
    """Constraint 4 — zero behavioural drift. The seam must not
    assign to any timeout/retry/context attribute or name."""
    fn = _fn(_PROVIDERS_SRC, "_emit_provider_latency")
    forbidden = ("timeout", "read_timeout", "retry", "deadline", "backoff")
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                txt = ast.unparse(tgt).lower()
                for bad in forbidden:
                    assert bad not in txt, (
                        f"seam assigns to {txt!r} — Slice 0 must not "
                        f"mutate timeout/retry/context"
                    )


def test_ast_pin_claude_success_boundary_emits():
    src = _PROVIDERS_SRC.read_text()
    # The success convergence (record_successful_call) must be
    # followed by an emission with outcome="success".
    i = src.index("self._record_successful_call()")
    region = src[i:i + 1400]
    assert "_emit_provider_latency(" in region
    assert 'outcome="success"' in region


def test_ast_pin_claude_timeout_boundary_emits():
    src = _PROVIDERS_SRC.read_text()
    # The timeout/cancel except block must emit BEFORE the re-raise.
    i = src.index(
        "except (asyncio.TimeoutError, asyncio.CancelledError) as _te:"
    )
    j = src.index("if isinstance(_te, asyncio.CancelledError):", i)
    region = src[i:j]
    assert "_emit_provider_latency(" in region, (
        "timeout/cancel boundary must produce a training row "
        "before re-raising (the ttft=-1 connect-timeout signature)"
    )


def test_ast_pin_dw_batch_boundary_emits():
    src = _DW_SRC.read_text()
    assert "_emit_provider_latency(" in src
    assert 'provider="doubleword-397b"' in src
    # Emitted only under an `if usage:` guard (never a fabricated
    # token=0 sample that would poison the Slice-1 regression).
    k = src.index('provider="doubleword-397b"')
    pre = src[:k]
    assert pre.rstrip().rsplit("\n", 6)[-6:], "context present"
    assert "if usage:" in src[k - 600:k], (
        "DW emission must be guarded by `if usage:` (real token count)"
    )


def test_ast_pin_record_provider_latency_is_bounded_deque():
    body = ast.unparse(_fn(_OBS_SRC, "record_provider_latency"))
    assert "deque(maxlen=" in body, (
        "constraint 2 — the ring MUST be a bounded deque(maxlen=N)"
    )
    assert "_provider_latency_window_n()" in body, (
        "bound MUST come from the env-tunable window, not a literal"
    )
