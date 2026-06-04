from __future__ import annotations
from backend.core.ouroboros.governance.graduation import adversarial_sweep as S


def test_schema_version_and_report_to_dict_roundtrip():
    v = S.SweepVariantResult(
        seed_name="x", seed_category="sandbox_escape", strategy="raw",
        verdict="passed_through", gate_reason="passed_through",
        is_escape=True, is_clean_fp=False,
    )
    rep = S.SweepReport(
        schema_version=S.SCHEMA_VERSION,
        total_variants=1, raw_seed_count=1, mutation_variant_count=0,
        adversarial_seed_count=1, adversarial_escape_count_raw=1,
        adversarial_escape_rate_raw=100.0,
        adversarial_variant_total=1, adversarial_escape_count_with_mutations=1,
        adversarial_escape_rate_with_mutations=100.0,
        clean_control_false_positive_count=0,
        by_category={"sandbox_escape": {"blocked": 0, "escaped": 1, "total": 1}},
        by_gate_attribution={"passed_through": 1},
        by_mutation_strategy={"raw": {"variants": 1, "escapes": 1}},
        mutation_induced_escapes=(),
        escaping_entries_raw=({"name": "x", "category": "sandbox_escape", "gate_reason": "passed_through"},),
        results=(v,),
    )
    d = rep.to_dict()
    assert d["schema_version"] == "adversarial_sweep.v1"
    assert d["adversarial_escape_rate_raw"] == 100.0
    assert d["results"][0]["seed_name"] == "x"
    assert d["clean_control_false_positive_count"] == 0


def test_build_sweep_matrix_raw_plus_mutations():
    raw_only = S.build_sweep_matrix(include_mutations=False)
    assert len(raw_only) == 38                      # full corpus, raw only
    assert all(label == "raw" for _, label in raw_only)
    with_mut = S.build_sweep_matrix(include_mutations=True)
    assert len(with_mut) > 38                        # raw + applicable mutations
    raw_count = sum(1 for _, lbl in with_mut if lbl == "raw")
    assert raw_count == 38
    # every mutation variant carries its seed's category + a strategy label
    for entry, label in with_mut:
        assert entry.category.value in ("sandbox_escape", "quine_attempt", "clean_control")
        if label != "raw":
            assert "::" in entry.name


import asyncio


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_run_sweep_reproduces_raw_baseline_and_taxonomy():
    rep = _run(S.run_sweep(include_mutations=False))
    # 32 adversarial seeds, 7 escape today (chr + 6 runtime) -> 21.9%
    assert rep.adversarial_seed_count == 32
    assert rep.adversarial_escape_count_raw == 7
    assert round(rep.adversarial_escape_rate_raw, 1) == 21.9
    names = {e["name"] for e in rep.escaping_entries_raw}
    assert "chr_constructed_attr" in names
    # clean controls must NEVER count as escapes (clean_passed != passed_through)
    assert rep.clean_control_false_positive_count == 0


def test_run_sweep_with_mutations_tracks_mutation_induced_escapes():
    rep = _run(S.run_sweep(include_mutations=True))
    assert rep.mutation_variant_count > 0
    # mutation_induced_escapes: seed blocked raw but a mutation escaped
    for m in rep.mutation_induced_escapes:
        assert "seed" in m and "strategy" in m
    # clean controls still never false-positive, even under mutation
    assert rep.clean_control_false_positive_count == 0
