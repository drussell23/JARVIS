"""Spine — Stage 2 Slice 2: payload-adaptive GENERATE budget.

Pins the four load-bearing invariants + the single-seam wiring:

  * **Floor = route base** — result is NEVER below base_s (flag off,
    weight 0, huge weight, or any error). Zero regression on the
    Bar-A baseline.
  * **Ceiling = thermodynamic wall cap** — result never exceeds the
    session --max-wall-seconds fraction (D2 containment preserved).
  * **Monotonic** — heavier payload ⇒ non-decreasing budget.
  * **Flag-off byte-identical** — §33.1 default-FALSE → == base_s.
  * **No hardcoded seconds** — scaling bodies carry no second-
    magnitude literals; all calibration is _env_float/_DEFAULT_*.
  * **Single seam (AST)** — orchestrator composes scale_gen_timeout
    once, before the deadline is born, fail-open.
  * **FlagRegistry** — master default-False + tunables seeded.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import adaptive_gen_budget as agb


class _Ctx:
    def __init__(self, desc="", evid="", files=()):
        self.description = desc
        self.intake_evidence_json = evid
        self.target_files = tuple(files)
        self.op_id = "op-test"


class _RaisingCtx:
    @property
    def description(self):
        raise RuntimeError("boom")

    def __getattr__(self, n):
        raise RuntimeError("boom")


_BASE = 220.0  # a STANDARD route base, illustrative


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in (
        "JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED",
        "OUROBOROS_BATTLE_MAX_WALL_SECONDS",
        "JARVIS_ADAPTIVE_GEN_MAX_MULTIPLIER",
        "JARVIS_ADAPTIVE_GEN_TOKEN_REF",
        "JARVIS_ADAPTIVE_GEN_FILE_REF",
        "JARVIS_ADAPTIVE_GEN_WALL_FRACTION",
    ):
        monkeypatch.delenv(v, raising=False)


# ---------------------------------------------------------------------------
# Floor invariant
# ---------------------------------------------------------------------------


def test_flag_off_is_byte_identical(monkeypatch):
    # Slice 79 graduated the master to default-TRUE, so the OFF path must be
    # set explicitly. With the flag OFF, scaling is byte-identical (== base).
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED", "false")
    assert agb.scale_gen_timeout(_BASE, _Ctx("x" * 99999, files=range(50))) == _BASE


def test_graduated_default_on_scales_heavy_payload(monkeypatch):
    # Slice 79 — flag UNSET now defaults ON, so a heavy multi-file swe_bench-
    # shaped payload gets a scaled (larger) budget without any env opt-in.
    monkeypatch.delenv("JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED", raising=False)
    assert agb.adaptive_gen_budget_enabled() is True
    heavy = _Ctx("x" * 80000, files=range(11))  # big problem statement, 11 files
    assert agb.scale_gen_timeout(_BASE, heavy) > _BASE


def test_graduated_default_on_trivial_still_unchanged(monkeypatch):
    # the Floor invariant still holds at default-on: a trivial op is unchanged.
    monkeypatch.delenv("JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED", raising=False)
    assert agb.scale_gen_timeout(_BASE, _Ctx()) == pytest.approx(_BASE)


def test_floor_never_below_base_even_with_huge_payload(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED", "true")
    huge = _Ctx("x" * 5_000_000, "y" * 5_000_000, files=range(500))
    assert agb.scale_gen_timeout(_BASE, huge) >= _BASE


def test_trivial_payload_no_change_even_flag_on(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED", "true")
    # empty payload → score 0 → multiplier 1.0 → base
    assert agb.scale_gen_timeout(_BASE, _Ctx()) == pytest.approx(_BASE)


def test_failopen_returns_base_on_raising_ctx(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED", "true")
    assert agb.scale_gen_timeout(_BASE, _RaisingCtx()) == _BASE


def test_nonpositive_base_returned_unchanged():
    assert agb.scale_gen_timeout(0.0, _Ctx("x" * 1000)) == 0.0
    assert agb.scale_gen_timeout(-5.0, _Ctx("x" * 1000)) == -5.0


# ---------------------------------------------------------------------------
# Ceiling invariant
# ---------------------------------------------------------------------------


def test_ceiling_bounded_by_wall_cap(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_BATTLE_MAX_WALL_SECONDS", "2400")
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_WALL_FRACTION", "0.5")
    huge = _Ctx("x" * 5_000_000, files=range(500))
    out = agb.scale_gen_timeout(_BASE, huge)
    assert _BASE <= out <= 2400 * 0.5  # ceiling = wall_cap * fraction


def test_ceiling_no_wallcap_falls_back_to_bounded_multiple(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED", "true")
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_MAX_MULTIPLIER", "6")
    huge = _Ctx("x" * 5_000_000, files=range(500))
    out = agb.scale_gen_timeout(_BASE, huge)
    # no wall cap → ceiling = base * max_multiplier, never unbounded
    assert _BASE <= out <= _BASE * 6.0


# ---------------------------------------------------------------------------
# Monotonic invariant
# ---------------------------------------------------------------------------


def test_monotonic_non_decreasing_in_payload(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_BATTLE_MAX_WALL_SECONDS", "100000")
    sizes = [0, 1_000, 10_000, 100_000, 1_000_000]
    outs = [
        agb.scale_gen_timeout(_BASE, _Ctx("x" * n, files=range(n // 1000)))
        for n in sizes
    ]
    for a, b in zip(outs, outs[1:]):
        assert b >= a, f"non-monotonic: {outs}"
    assert outs[-1] > outs[0]  # heavy strictly exceeds trivial


def test_more_files_non_decreasing(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_BATTLE_MAX_WALL_SECONDS", "100000")
    o1 = agb.scale_gen_timeout(_BASE, _Ctx(files=range(1)))
    o2 = agb.scale_gen_timeout(_BASE, _Ctx(files=range(20)))
    assert o2 >= o1


def test_deterministic(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED", "true")
    c = _Ctx("payload" * 500, files=range(7))
    assert agb.scale_gen_timeout(_BASE, c) == agb.scale_gen_timeout(_BASE, c)


# ---------------------------------------------------------------------------
# PayloadWeight purity
# ---------------------------------------------------------------------------


def test_weight_zero_for_empty_ctx():
    w = agb.compute_payload_weight(_Ctx())
    assert w.score == 0.0 and w.file_count == 0


def test_weight_monotone_and_frozen():
    light = agb.compute_payload_weight(_Ctx("a" * 100))
    heavy = agb.compute_payload_weight(_Ctx("a" * 100000, files=range(30)))
    assert heavy.score > light.score
    with pytest.raises(Exception):
        heavy.score = 1.0  # frozen


# ---------------------------------------------------------------------------
# No hardcoded seconds (AST)
# ---------------------------------------------------------------------------


def test_ast_no_second_magnitude_literals_in_scaling_bodies():
    src = Path(agb.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for fn_name in ("scale_gen_timeout", "compute_payload_weight",
                    "_wall_ceiling_s"):
        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == fn_name),
            None,
        )
        assert fn is not None, fn_name
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(
                node.value, (int, float)
            ) and not isinstance(node.value, bool):
                # only small structural constants (0, 1, 1.0, 0.0,
                # 0.01) allowed — NO second-magnitude (>= 10) literal
                assert node.value < 10, (
                    f"{fn_name} carries a hardcoded magnitude "
                    f"literal {node.value!r} — calibration must come "
                    f"from _env_float/_DEFAULT_* (no magic seconds)"
                )


# ---------------------------------------------------------------------------
# Single-seam orchestrator wiring (AST/source)
# ---------------------------------------------------------------------------


def test_orchestrator_composes_scale_gen_timeout_single_seam():
    src = (
        Path(agb.__file__).parents[0] / "orchestrator.py"
    ).read_text(encoding="utf-8")
    assert src.count("scale_gen_timeout(") == 1, (
        "orchestrator must call scale_gen_timeout exactly once "
        "(single highest-enforcement seam — no per-layer dup)"
    )
    # Must precede the deadline birth THAT FOLLOWS IT and be
    # fail-open. (orchestrator.py has several `deadline = now +
    # timedelta(...)` sites — anchor to the one after the seam.)
    seam = src.index("scale_gen_timeout(")
    deadline_after = src.index(
        "deadline = datetime.now(tz=timezone.utc) + timedelta(",
        seam,
    )
    assert seam < deadline_after, (
        "scaling must happen BEFORE the deadline it feeds, so it "
        "propagates to deadline + outer wait_for + BudgetPlan"
    )
    # the deadline immediately after the seam must consume _gen_timeout
    assert "seconds=_gen_timeout" in src[deadline_after:deadline_after + 120]
    window = src[src.index("Slice 2 — payload-adaptive"):deadline_after]
    assert "try:" in window and "except Exception:" in window, (
        "the seam must be fail-open (orchestrator never dies if "
        "adaptive budget raises)"
    )


# ---------------------------------------------------------------------------
# FlagRegistry
# ---------------------------------------------------------------------------


def test_flag_registry_master_default_true():
    captured = []

    class _Reg:
        def register(self, spec):
            captured.append(spec)

    n = agb.register_flags(_Reg())
    assert n >= 1
    master = next(
        s for s in captured
        if s.name == "JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED"
    )
    # Slice 79 — graduated default-FALSE → default-TRUE.
    assert master.default is True
    assert "adaptive_gen_budget.py" in master.source_file
