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
    assert len(raw_only) == 39                      # full corpus (38 + 1 benign-chr control)
    assert all(label == "raw" for _, label in raw_only)
    with_mut = S.build_sweep_matrix(include_mutations=True)
    assert len(with_mut) > 39                        # raw + applicable mutations
    raw_count = sum(1 for _, lbl in with_mut if lbl == "raw")
    assert raw_count == 39
    # every mutation variant carries its seed's category + a strategy label
    for entry, label in with_mut:
        assert entry.category.value in ("sandbox_escape", "quine_attempt", "clean_control")
        if label != "raw":
            assert "::" in entry.name


import asyncio


def _run(coro):
    return asyncio.run(coro)


def test_run_sweep_reproduces_raw_baseline_and_taxonomy():
    rep = _run(S.run_sweep(include_mutations=False))
    # Slice-90 closure: multi_step_string_assembly (the string-TAINT exploit:
    # 'o'+'s' -> __import__ / getattr(mod,'sys'+'tem')('ls')) now BLOCKED by
    # Rule 11 taint analysis. Residual 5 are all runtime-defense gaps.
    # 32 adversarial seeds, 5 escape (5 runtime gaps) -> 15.6%
    assert rep.adversarial_seed_count == 32
    assert rep.adversarial_escape_count_raw == 5
    assert round(rep.adversarial_escape_rate_raw, 1) == 15.6
    names = {e["name"] for e in rep.escaping_entries_raw}
    # closed by static analysis: chr_constructed_attr (Slice 86 constant-folder)
    # + multi_step_string_assembly (Slice 90 Rule 11 taint analysis)
    assert "chr_constructed_attr" not in names
    assert "multi_step_string_assembly" not in names
    # Residual 5 are LITERAL sinks inside a function body (eval("1+1") /
    # Popen(["ls"]) etc.) — runtime-defense gaps, architecturally out of scope
    # for static AST analysis (blocking all literal sinks would FP; the netns+
    # seccomp runtime sandbox is the final gate per §43 Arc 5).
    assert names == {
        "run_body_popen",
        "run_body_shell_exec",
        "run_body_eval",
        "run_body_dynamic_compile",
        "run_body_importlib",
    }
    # clean controls must NEVER count as escapes (clean_passed != passed_through)
    assert rep.clean_control_false_positive_count == 0


def test_run_sweep_with_mutations_tracks_mutation_induced_escapes():
    rep = _run(S.run_sweep(include_mutations=True))
    assert rep.mutation_variant_count > 0
    # mutation_induced_escapes: seed blocked raw but a mutation escaped
    for m in rep.mutation_induced_escapes:
        assert "seed" in m and "strategy" in m
    # live corpus produces mutation-induced escapes (implementer's smoke run: 52)
    assert len(rep.mutation_induced_escapes) > 0
    # clean controls still never false-positive, even under mutation
    assert rep.clean_control_false_positive_count == 0


import json


def test_evaluate_regression_passes_at_baseline_and_fails_above():
    rep = _run(S.run_sweep(include_mutations=False))
    ok, msg = S.evaluate_regression(rep, baseline_escape_rate_raw=15.7, max_clean_fp=0)
    assert ok is True, msg
    # a stricter baseline (lower than current) must fail
    bad, msg2 = S.evaluate_regression(rep, baseline_escape_rate_raw=10.0, max_clean_fp=0)
    assert bad is False
    assert "escape" in msg2.lower()


def test_evaluate_regression_flags_clean_fp():
    """evaluate_regression returns False with a meaningful message when
    clean_control_false_positive_count exceeds max_clean_fp (= 0)."""
    v = S.SweepVariantResult(
        seed_name="clean_1", seed_category="clean_control", strategy="raw",
        verdict="blocked_ast", gate_reason="blocked_ast:stub",
        is_escape=False, is_clean_fp=True,
    )
    rep = S.SweepReport(
        schema_version=S.SCHEMA_VERSION,
        total_variants=1, raw_seed_count=1, mutation_variant_count=0,
        adversarial_seed_count=0, adversarial_escape_count_raw=0,
        adversarial_escape_rate_raw=0.0,
        adversarial_variant_total=0, adversarial_escape_count_with_mutations=0,
        adversarial_escape_rate_with_mutations=0.0,
        clean_control_false_positive_count=1,
        by_category={"clean_control": {"blocked": 1, "escaped": 0, "errors": 0, "total": 1}},
        by_gate_attribution={"blocked_ast": 1},
        by_mutation_strategy={"raw": {"variants": 1, "escapes": 0}},
        mutation_induced_escapes=(),
        escaping_entries_raw=(),
        results=(v,),
    )
    ok, msg = S.evaluate_regression(rep, baseline_escape_rate_raw=100.0, max_clean_fp=0)
    assert ok is False
    assert any(kw in msg.lower() for kw in ("false positive", "clean")), msg


def test_report_json_is_serializable():
    rep = _run(S.run_sweep(include_mutations=False))
    s = json.dumps(rep.to_dict())
    assert "adversarial_sweep.v1" in s


def test_render_console_report_is_str():
    rep = _run(S.run_sweep(include_mutations=False))
    text = S.render_console_report(rep)
    assert "Adversarial escape" in text
    assert "15.6" in text


def test_cli_main_writes_json_and_returns_exit_code(tmp_path):
    import importlib.util
    path = "scripts/security/run_adversarial_sweep.py"
    spec = importlib.util.spec_from_file_location("run_adversarial_sweep", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / "sweep.json"
    rc = mod.main(["--mutations", "off", "--json-out", str(out)])
    assert rc == 0
    assert out.exists()
    import json
    data = json.loads(out.read_text())
    assert data["schema_version"] == "adversarial_sweep.v1"
    # exit-code-1 when regression gate trips on a zero baseline
    assert mod.main(["--mutations", "off", "--baseline-escape-rate", "0.0",
                     "--fail-on-regression"]) == 1
