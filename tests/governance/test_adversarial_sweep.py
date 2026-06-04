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
