# tests/governance/test_fleet_calibration_store.py
from __future__ import annotations
from backend.core.ouroboros.governance import fleet_calibration_store as s


def test_ewma_first_sample_and_update():
    assert s.ewma_update(None, 1.0, 0.4) == 1.0
    assert abs(s.ewma_update(0.0, 1.0, 0.5) - 0.5) < 1e-9


def test_quality_score_composites():
    sc = s.QualityScore(model_id="m", ast_pass_rate=0.5, label_adherence=1.0,
                        ttft_ms=100.0, tok_per_s=80.0, sample_count=5, updated_at=1.0)
    assert abs(s.valid_tok_per_s(sc) - 40.0) < 1e-9         # 80 * 0.5
    assert s.triage_fitness(sc) > 0


def test_store_record_and_persist_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "fleet_calibration.json"
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(p))
    st = s.FleetCalibrationStore()
    st.record_probe("deepseek", kind="code", code_pass=True, ttft_ms=200, tok_per_s=90, now=1.0)
    st.record_probe("deepseek", kind="triage", label_score=1.0, ttft_ms=200, tok_per_s=90, now=2.0)
    st.save()
    st2 = s.FleetCalibrationStore()
    sc = st2.score("deepseek")
    assert sc is not None and sc.sample_count == 2 and sc.ast_pass_rate > 0


def test_rerank_orders_measured_good_above_bad():
    scores = {
        "qwen397": s.QualityScore("qwen397", 0.0, 0.0, 150, 120, 8, 1.0),   # fast, no valid code
        "deepseek": s.QualityScore("deepseek", 1.0, 1.0, 200, 90, 8, 1.0),  # slower, valid
    }
    out = s.fleet_rerank("standard", ("qwen397", "deepseek"), scores, route_kind="code")
    assert out[0] == "deepseek"      # valid_tok_per_s: deepseek 90 > qwen397 0


def test_rerank_leaves_unbenchmarked_alone_and_noop_on_thin_data():
    scores = {"deepseek": s.QualityScore("deepseek", 1.0, 1.0, 200, 90, 8, 1.0)}
    # only 1 scored model -> input returned unchanged
    assert s.fleet_rerank("standard", ("qwen397", "deepseek"), scores, route_kind="code") == ("qwen397", "deepseek")
    assert s.fleet_rerank("standard", ("a", "b"), {}, route_kind="code") == ("a", "b")


def test_rerank_preserves_unscored_positions():
    # good(scored) should rise above bad(scored), but unscored 'x' keeps its slot index.
    scores = {
        "bad": s.QualityScore("bad", 0.0, 0.0, 100, 120, 5, 1.0),
        "good": s.QualityScore("good", 1.0, 1.0, 200, 90, 5, 1.0),
    }
    out = s.fleet_rerank("standard", ("bad", "x", "good"), scores, route_kind="code")
    assert out[1] == "x"                      # unscored model holds its index
    assert out.index("good") < out.index("bad")


def test_graduation_ready_fires_on_measured_bad_default():
    scores = {
        "qwen397": s.QualityScore("qwen397", 0.05, 0.0, 150, 120, 8, 1.0),
        "deepseek": s.QualityScore("deepseek", 0.95, 1.0, 200, 90, 8, 1.0),
    }
    winner = s.graduation_ready(scores, default_model="qwen397",
                                min_samples=5, min_margin=1.5)
    assert winner == "deepseek"


def test_graduation_ready_none_below_thresholds():
    scores = {"deepseek": s.QualityScore("deepseek", 0.95, 1.0, 200, 90, 2, 1.0)}  # too few samples
    assert s.graduation_ready(scores, default_model="qwen397", min_samples=5, min_margin=1.5) is None


def test_graduation_margin_gate_when_default_is_ok():
    # default is decent (ast 0.6, vtps = 120*0.6 = 72); candidate vtps must beat 1.5x = 108.
    scores = {
        "okdefault": s.QualityScore("okdefault", 0.6, 0.0, 150, 120, 8, 1.0),  # vtps 72
        "weakwin": s.QualityScore("weakwin", 0.85, 1.0, 200, 100, 8, 1.0),     # vtps 85 < 108 -> no
    }
    assert s.graduation_ready(scores, default_model="okdefault", min_samples=5, min_margin=1.5) is None


def test_apply_rerank_failsoft_returns_input_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_FLEET_CALIBRATION_PATH", str(tmp_path / "none.json"))
    # no store data -> input unchanged
    assert s.fleet_apply_rerank("standard", ("a", "b")) == ("a", "b")
