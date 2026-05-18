"""Trace-2 fix spine — SWE-bench envelope anti-starvation urgency.

Soak bt-2026-05-17-225244: the injected benchmark op defaulted to
``urgency="low"`` -> lowest intake_priority_queue rank (urgency_rank=3)
with deadline=inf -> structurally starved by higher-urgency
background-sensor ops (django: 0 BG submissions vs 46 sensor ops). Fix:
``_DEFAULT_URGENCY="normal"`` so the op gets a finite per-urgency
deadline + starvation-guard protection. Operators keep the env escape
hatch (``JARVIS_SWE_BENCH_PRO_ENVELOPE_URGENCY=low``) for the old
DW-only bulk economics.

Also pins the Trace-1 diagnostic probe as flag-gated + crash-safe.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance.swe_bench_pro import (
    envelope_builder as eb,
)
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (
    PreparedProblem,
)


@pytest.fixture
def _clean(monkeypatch):
    monkeypatch.delenv(eb.ENVELOPE_URGENCY_ENV_VAR, raising=False)
    yield


def test_default_urgency_is_normal_not_low(_clean):
    """The anti-starvation default — load-bearing assertion."""
    assert eb._DEFAULT_URGENCY == "normal"
    assert eb._derive_urgency() == "normal"


def test_low_is_no_longer_the_default_regression_pin():
    """Regression pin: reverting to 'low' re-opens the
    bt-2026-05-17-225244 starvation vector."""
    assert eb._DEFAULT_URGENCY not in ("low",)


def test_env_override_still_honored(monkeypatch):
    # Operators can still opt back into low for bulk DW-only economics.
    monkeypatch.setenv(eb.ENVELOPE_URGENCY_ENV_VAR, "low")
    assert eb._derive_urgency() == "low"
    monkeypatch.setenv(eb.ENVELOPE_URGENCY_ENV_VAR, "high")
    assert eb._derive_urgency() == "high"


def test_invalid_env_falls_back_to_normal(monkeypatch):
    monkeypatch.setenv(eb.ENVELOPE_URGENCY_ENV_VAR, "bogus-urgency")
    assert eb._derive_urgency() == "normal"


def test_built_envelope_carries_normal_urgency(tmp_path, _clean):
    problem = ProblemSpec(
        instance_id="psf__requests-3362",
        repo="psf/requests",
        base_commit="36453b95b130",
        problem_statement="iter_content vs text",
        test_patch="--- a/t.py\n+++ b/t.py\n@@ -1 +1 @@\n-a\n+b\n",
        gold_patch="--- a/requests/models.py\n+++ b/requests/models.py\n",
        repo_url="https://github.com/psf/requests",
    )
    wt = tmp_path / "wt"
    wt.mkdir()
    prepared = PreparedProblem(
        problem_instance_id=problem.instance_id,
        worktree_path=wt,
        base_commit=problem.base_commit,
        repo_url=problem.repo_url,
        branch_name="swebp/psf__requests-3362",
        target_paths=("tests/test_requests.py",),
        elapsed_s=0.8,
    )
    env = eb.build_evaluation_envelope(problem, prepared)
    assert env.urgency == "normal", (
        "injected SWE-bench envelope MUST be 'normal' urgency so it is "
        "not starved at the lowest priority-queue rank"
    )
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        _VALID_URGENCIES,
    )
    assert env.urgency in _VALID_URGENCIES


def test_probe_is_gated_default_off_and_crash_safe():
    """The Trace-1 probe MUST be flag-gated (byte-identical when unset)
    and wrapped so it can never raise into the pipeline."""
    from backend.core.ouroboros.governance import orchestrator as orch
    src = inspect.getsource(orch)
    assert "JARVIS_DEBUG_CLASSIFY_PROBE" in src
    assert "[Trace1Probe]" in src
    i = src.index("[Trace1Probe]")
    assert "except Exception:" in src[i:i + 1400], (
        "probe MUST be wrapped in try/except — never raise into the "
        "classify path"
    )
