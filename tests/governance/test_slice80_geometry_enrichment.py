"""Slice 80 — static geometry injection + pre-exploration weight enrichment.

Gap (EVAL-2 macro sweep #3, §50.11): Slice 79 graduated the adaptive GENERATE
budget, but `compute_payload_weight` reads `ctx.target_files` — which is
DELIBERATELY EMPTY for SWE-bench ops (the agent must localize the bug itself; see
envelope_builder.build_evaluation_envelope). So at GENERATE a genuinely multi-
file instance (ansible 4-file/2028-line, NodeBB 11-file) got the SAME ~1.25×
multiplier as a 1-file fix → ~150s budget → Claude maxed its 16k tokens / budget
and got cut before finishing the multi-round patch.

Slice 80 stamps the reference-patch SCOPE (file + changed-line counts) into the
envelope evidence at inject time (BUDGET-ONLY metadata — `intake_evidence_json`
is consumed for repo-root + budget, never injected into the model's prompt, so it
does NOT leak the solution), and `compute_payload_weight` falls back to it when
`ctx.target_files` is empty → multi-file instances now get 3-6× runway while
localized passes stay efficient.
"""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance import adaptive_gen_budget as agb


class _Ctx:
    """Minimal op-context double — empty target_files + a stamped evidence JSON,
    exactly the SWE-bench GENERATE-phase shape."""

    def __init__(self, *, file_count=0, changed_lines=0, description="",
                 stamp=True, target_files=()):
        self.target_files = tuple(target_files)
        self.description = description
        if stamp:
            self.intake_evidence_json = json.dumps({
                "swe_bench_geometry": {
                    "file_count": file_count, "changed_lines": changed_lines,
                },
            })
        else:
            self.intake_evidence_json = ""


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED", "true")
    for v in ("JARVIS_ADAPTIVE_GEN_MAX_MULTIPLIER", "JARVIS_ADAPTIVE_GEN_LINES_REF",
              "JARVIS_ADAPTIVE_GEN_FILE_REF", "JARVIS_ADAPTIVE_GEN_TOKEN_REF",
              "OUROBOROS_BATTLE_MAX_WALL_SECONDS", "JARVIS_ADAPTIVE_GEN_WALL_FRACTION"):
        monkeypatch.delenv(v, raising=False)


# --- the stamp is read when target_files is empty ---

def test_stamp_drives_file_count_when_target_files_empty():
    w = agb.compute_payload_weight(_Ctx(file_count=11, changed_lines=151))
    assert w.file_count == 11  # came from the stamp, not target_files


def test_no_stamp_no_target_files_is_zero_weight():
    w = agb.compute_payload_weight(_Ctx(stamp=False))
    assert w.file_count == 0
    assert w.score == pytest.approx(0.0)


def test_target_files_take_precedence_over_stamp():
    # a populated target_files (normal op) wins; the stamp is only a fallback
    w = agb.compute_payload_weight(
        _Ctx(file_count=99, changed_lines=9999, target_files=("a.py", "b.py"))
    )
    assert w.file_count == 2


# --- the multi-file instances now get 3-6x runway ---

_BASE = 220.0  # standard route base


def test_ansible_many_lines_saturates_high(monkeypatch):
    # ansible-c616e54a: ~4 files, 2028 changed lines → near-max multiplier
    scaled = agb.scale_gen_timeout(_BASE, _Ctx(file_count=4, changed_lines=2028))
    assert scaled >= _BASE * 3.0  # >= 3x runway (≈ 660s+)


def test_nodebb_multi_file_scales_meaningfully():
    # NodeBB: 11 files, 151 lines → solidly above baseline
    scaled = agb.scale_gen_timeout(_BASE, _Ctx(file_count=11, changed_lines=151))
    assert scaled >= _BASE * 1.8  # >= ~1.8x (≈ 400s+)


def test_localized_instance_stays_efficient():
    # qutebrowser: 1 file, 23 lines → near-baseline (cost-efficient)
    scaled = agb.scale_gen_timeout(_BASE, _Ctx(file_count=1, changed_lines=23))
    assert scaled < _BASE * 1.6  # stays tight


def test_localized_strictly_below_distributed():
    local = agb.scale_gen_timeout(_BASE, _Ctx(file_count=1, changed_lines=23))
    distributed = agb.scale_gen_timeout(_BASE, _Ctx(file_count=4, changed_lines=2028))
    assert distributed > local


# --- safety / robustness ---

def test_malformed_evidence_is_failsoft():
    class _Bad:
        target_files = ()
        description = ""
        intake_evidence_json = "{not valid json"
    w = agb.compute_payload_weight(_Bad())
    # geometry didn't parse → no file_count / changed_lines contribution (the
    # tiny text-token term from the raw evidence string is existing behavior).
    assert w.file_count == 0
    assert w.score < 0.01


def test_flag_off_ignores_stamp(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED", "false")
    assert agb.scale_gen_timeout(_BASE, _Ctx(file_count=11, changed_lines=2028)) == _BASE


def test_lines_ref_is_env_tunable(monkeypatch):
    base_scaled = agb.scale_gen_timeout(_BASE, _Ctx(file_count=0, changed_lines=400))
    monkeypatch.setenv("JARVIS_ADAPTIVE_GEN_LINES_REF", "100")  # lines count 4x more
    tighter_ref = agb.scale_gen_timeout(_BASE, _Ctx(file_count=0, changed_lines=400))
    assert tighter_ref > base_scaled
