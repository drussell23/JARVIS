"""DeepAnalysisSensor tests — four semantic-adjacent analyzers + sensor shell.

Scope axes:

  1. Contract-drift analyzer — return-type mismatch, docstring raises-
    without-raise, params-added-but-undocumented.
  2. Coverage-gap analyzer — newly-added modules without tests,
    newly-added public symbols without test references.
  3. Purpose-drift analyzer — module docstring tokens vs. identifier
    tokens (disjoint → finding).
  4. Orphan-surface analyzer — public function defined but never
    referenced elsewhere.
  5. Aggregator + sensor — per-category gates, cooldown dedup,
    intake-router emission, error isolation per analyzer.
  6. AST canaries — sensor file name, authority invariant in docstring.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import textwrap
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.intake.sensors.deep_analysis_sensor import (
    DeepAnalysisFinding,
    DeepAnalysisSensor,
    analyze_contract_drift,
    analyze_coverage_gap,
    analyze_orphan_surface,
    analyze_purpose_drift,
    run_all_analyzers,
    sensor_enabled,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_DEEP_ANALYSIS_"):
            monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# Env gates — fail-closed discipline
# ---------------------------------------------------------------------------


def test_sensor_disabled_by_default():
    assert sensor_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_sensor_enabled_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_SENSOR_ENABLED", val)
    assert sensor_enabled() is True


# ---------------------------------------------------------------------------
# (1) Contract drift
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def test_contract_drift_return_type_mismatch(tmp_path):
    """``-> bool`` annotation with ``return None`` + ``return True``
    branches must fire."""
    _write(tmp_path / "backend/m.py", """
        def checker(x) -> bool:
            if x:
                return True
            return None  # lies about returning bool
    """)
    findings = analyze_contract_drift(repo_root=tmp_path)
    drift = [f for f in findings if f.category == "contract_drift"]
    assert any(
        "return-type drift" in f.description for f in drift
    )
    hit = next(f for f in drift if "return-type drift" in f.description)
    assert hit.evidence["function"] == "checker"
    assert hit.evidence["subcategory"] == "return_type_mismatch"


def test_contract_drift_raises_claimed_but_absent(tmp_path):
    _write(tmp_path / "backend/m.py", '''
        def fetch(url):
            """Fetch a URL.

            Raises:
                ValueError: if URL is malformed
            """
            return url.lower()
    ''')
    findings = analyze_contract_drift(repo_root=tmp_path)
    hit = next(
        (f for f in findings
         if f.evidence.get("subcategory") == "raises_claimed_but_absent"),
        None,
    )
    assert hit is not None
    assert "ValueError" in hit.evidence["claimed_exceptions"]


def test_contract_drift_params_added_not_documented(tmp_path):
    """Function adds a new param but docstring's Parameters section
    doesn't mention it → flag."""
    _write(tmp_path / "backend/m.py", '''
        def run(x, y, new_param):
            """Run with params.

            Parameters
            ----------
            x: int
                first
            y: int
                second
            """
            return x + y + new_param
    ''')
    findings = analyze_contract_drift(repo_root=tmp_path)
    hit = next(
        (f for f in findings
         if f.evidence.get("subcategory") == "params_added_not_documented"),
        None,
    )
    assert hit is not None
    assert "new_param" in hit.evidence["missing_params"]


def test_contract_drift_silent_on_clean_code(tmp_path):
    """Well-matched code should produce NO findings."""
    _write(tmp_path / "backend/m.py", '''
        def is_even(n: int) -> bool:
            """Return True iff n is even."""
            return n % 2 == 0
    ''')
    findings = analyze_contract_drift(repo_root=tmp_path)
    assert findings == []


def test_contract_drift_silent_when_no_params_section(tmp_path):
    """Function without a Parameters section in the docstring is NOT
    flagged for missing params (author never claimed to document them)."""
    _write(tmp_path / "backend/m.py", '''
        def run(x, y, new_param):
            """Run with some args."""
            return x + y + new_param
    ''')
    findings = analyze_contract_drift(repo_root=tmp_path)
    missing_param = [
        f for f in findings
        if f.evidence.get("subcategory") == "params_added_not_documented"
    ]
    assert missing_param == []


# ---------------------------------------------------------------------------
# (2) Coverage gap
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Iterator[Path]:
    """Tmp git repo with a seed commit so ``HEAD~N`` resolves."""
    def g(*args):
        return subprocess.run(
            ["git", *args], cwd=str(tmp_path),
            capture_output=True, text=True,
        )
    g("init", "-q")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "T")
    g("config", "commit.gpgsign", "false")
    (tmp_path / "README.md").write_text("init\n")
    g("add", ".")
    g("commit", "-q", "-m", "init")
    yield tmp_path


def _commit(repo: Path, msg: str) -> None:
    subprocess.run(["git", "add", "."], cwd=str(repo))
    subprocess.run(
        ["git", "commit", "-q", "-m", msg],
        cwd=str(repo),
    )


def test_coverage_gap_flags_new_module_without_tests(tmp_git_repo):
    _write(tmp_git_repo / "backend/foo.py", """
        def process(x):
            return x * 2

        class Handler:
            pass
    """)
    _commit(tmp_git_repo, "feat: add foo module")
    findings = analyze_coverage_gap(repo_root=tmp_git_repo, lookback=5)
    hit = next(
        (f for f in findings
         if f.evidence.get("subcategory") == "no_test_file"),
        None,
    )
    assert hit is not None
    assert "foo.py" in hit.file
    assert set(hit.evidence["public_symbols"]) >= {"process", "Handler"}


def test_coverage_gap_flags_symbols_not_referenced_in_tests(tmp_git_repo):
    """Test file exists but doesn't reference the new symbol."""
    _write(tmp_git_repo / "backend/bar.py", """
        def existing():
            return 1

        def brand_new_symbol():
            return 2
    """)
    # Test file exists but only tests the old symbol.
    _write(tmp_git_repo / "tests/test_bar.py", """
        from backend.bar import existing
        def test_existing():
            assert existing() == 1
    """)
    _commit(tmp_git_repo, "feat: add brand_new_symbol")
    findings = analyze_coverage_gap(repo_root=tmp_git_repo, lookback=5)
    hit = next(
        (f for f in findings
         if f.evidence.get("subcategory")
            == "symbols_not_referenced_in_tests"),
        None,
    )
    assert hit is not None
    assert "brand_new_symbol" in hit.evidence["missing_symbols"]


def test_coverage_gap_silent_when_test_references_symbol(tmp_git_repo):
    _write(tmp_git_repo / "backend/baz.py", """
        def good_symbol():
            return 1
    """)
    _write(tmp_git_repo / "tests/test_baz.py", """
        from backend.baz import good_symbol
        def test_good_symbol():
            assert good_symbol() == 1
    """)
    _commit(tmp_git_repo, "feat: add baz + test")
    findings = analyze_coverage_gap(repo_root=tmp_git_repo, lookback=5)
    assert findings == []


def test_coverage_gap_skips_test_files_themselves(tmp_git_repo):
    """A change that only touches tests/ should not produce a coverage-
    gap finding — the gap we're looking for is new source without tests,
    not new tests."""
    _write(tmp_git_repo / "tests/test_something.py", """
        def test_placeholder():
            assert True
    """)
    _commit(tmp_git_repo, "chore: add placeholder test")
    findings = analyze_coverage_gap(repo_root=tmp_git_repo, lookback=5)
    assert findings == []


# ---------------------------------------------------------------------------
# (3) Purpose drift
# ---------------------------------------------------------------------------


def test_purpose_drift_disjoint_docstring_and_identifiers(tmp_path):
    """Module docstring talks about one thing; functions do something
    else. Zero token overlap → finding."""
    _write(tmp_path / "backend/payment_processing.py", '''
        """Handles authentication, session management, and security tokens.

        This module authenticates users, manages session lifetimes, and
        rotates security credentials on schedule.
        """

        def charge_card(amount):
            return amount

        def issue_refund(order_id):
            return order_id

        def apply_discount(cart):
            return cart
    ''')
    findings = analyze_purpose_drift(repo_root=tmp_path)
    hit = next(
        (f for f in findings
         if f.evidence.get("subcategory")
            == "docstring_identifier_disjoint"),
        None,
    )
    assert hit is not None


def test_purpose_drift_silent_when_overlap_exists(tmp_path):
    """Docstring + function names share at least one non-stopword
    token → analyzer is silent."""
    _write(tmp_path / "backend/billing.py", '''
        """Handles billing operations — charging cards, issuing refunds,
        applying discounts.
        """

        def charge_card(amount):
            return amount

        def issue_refund(order_id):
            return order_id
    ''')
    findings = analyze_purpose_drift(repo_root=tmp_path)
    assert findings == []


def test_purpose_drift_skips_short_docstrings(tmp_path):
    """Modules with only a one-line docstring don't have enough signal
    to compare — skip silently."""
    _write(tmp_path / "backend/short.py", '''
        """Utils."""
        def some_function():
            pass
    ''')
    findings = analyze_purpose_drift(repo_root=tmp_path)
    assert findings == []


# ---------------------------------------------------------------------------
# (4) Orphan surface
# ---------------------------------------------------------------------------


def test_orphan_surface_flags_unreferenced_public_function(tmp_path):
    _write(tmp_path / "backend/orphan_mod.py", """
        def i_am_orphaned():
            return "nobody calls me"

        def _private_helper():
            return "private, skip"
    """)
    # A different file that doesn't reference the orphan.
    _write(tmp_path / "backend/other.py", """
        def something_else():
            return 42
    """)
    findings = analyze_orphan_surface(repo_root=tmp_path)
    hit = next(
        (f for f in findings if f.evidence.get("function") == "i_am_orphaned"),
        None,
    )
    assert hit is not None
    assert "orphan public function" in hit.description


def test_orphan_surface_silent_when_referenced(tmp_path):
    _write(tmp_path / "backend/lib.py", """
        def used_symbol():
            return 1
    """)
    _write(tmp_path / "backend/caller.py", """
        from backend.lib import used_symbol
        def entry():
            return used_symbol()
    """)
    findings = analyze_orphan_surface(repo_root=tmp_path)
    assert not any(
        f.evidence.get("function") == "used_symbol" for f in findings
    )


def test_orphan_surface_skips_private_and_init_files(tmp_path):
    """Names starting with underscore + __init__.py re-exports don't
    count."""
    _write(tmp_path / "backend/mod/__init__.py", """
        def re_exported_name():
            pass
    """)
    _write(tmp_path / "backend/mod/impl.py", """
        def _private_function():
            return 1

        def __dunder_placeholder__():
            return 2
    """)
    findings = analyze_orphan_surface(repo_root=tmp_path)
    names = [f.evidence.get("function") for f in findings]
    assert "_private_function" not in names
    assert "__dunder_placeholder__" not in names
    assert "re_exported_name" not in names


# ---------------------------------------------------------------------------
# (5) Aggregator + per-category gates + error isolation
# ---------------------------------------------------------------------------


def test_aggregator_respects_per_category_gates(tmp_path, monkeypatch):
    """Disabling a single category via env removes its findings;
    other categories still run."""
    _write(tmp_path / "backend/orphan_mod.py", """
        def orphan_fn():
            return 1
    """)
    # Disable ONLY orphan; contract/coverage/purpose still run.
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_ORPHAN_ENABLED", "0")
    findings = run_all_analyzers(repo_root=tmp_path)
    assert not any(f.category == "orphan_surface" for f in findings)


def test_aggregator_isolates_broken_analyzer(tmp_path, monkeypatch):
    """A broken analyzer should not prevent others from producing findings.

    We monkey-patch one analyzer function to raise; the aggregator
    must still call the rest + return their findings."""
    from backend.core.ouroboros.governance.intake.sensors import (
        deep_analysis_sensor as mod,
    )
    def _boom(**kwargs):
        raise RuntimeError("analyzer exploded")
    monkeypatch.setattr(mod, "analyze_contract_drift", _boom)
    # Seed a purpose-drift trigger.
    _write(tmp_path / "backend/drifted.py", '''
        """Handles authentication, sessions, and security tokens.

        Manages user logins, session lifetimes, and credential rotation.
        """
        def charge_card(amount): return amount
        def issue_refund(x): return x
        def apply_discount(x): return x
    ''')
    findings = run_all_analyzers(repo_root=tmp_path)
    # Purpose-drift still fires despite contract-drift raising.
    assert any(f.category == "purpose_drift" for f in findings)


def test_aggregator_respects_max_findings_cap(tmp_path, monkeypatch):
    """MAX_FINDINGS_PER_CYCLE caps the aggregator output (rough cap —
    final intake flood guard is on the sensor layer, this is the
    aggregator's soft cap at 4× per-cycle)."""
    # Seed many orphans.
    _write(tmp_path / "backend/many.py", "\n".join(
        f"def orphan_{i}(): return {i}" for i in range(50)
    ))
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_MAX_FINDINGS_PER_CYCLE", "2")
    findings = run_all_analyzers(repo_root=tmp_path)
    # 2 (per-cycle) * 4 (aggregator soft cap) = 8 max
    assert len(findings) <= 8


# ---------------------------------------------------------------------------
# (6) Sensor integration — cooldown dedup + router emission
# ---------------------------------------------------------------------------


def test_sensor_emits_through_router_when_enabled(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_SENSOR_ENABLED", "1")
    # Seed an obvious orphan.
    _write(tmp_path / "backend/target.py", """
        def only_me_exists():
            return 'orphan'
    """)
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = DeepAnalysisSensor(
        repo="jarvis", router=router, project_root=tmp_path,
    )
    findings = asyncio.run(sensor.scan_once())
    assert findings
    # At least one ingest call with the right source tag.
    assert router.ingest.call_count >= 1
    envelope = router.ingest.call_args.args[0]
    assert envelope.source == "exploration"
    assert envelope.evidence.get("deep_analysis_category") in (
        "contract_drift", "coverage_gap", "purpose_drift", "orphan_surface",
    )


def test_sensor_cooldown_dedups_repeated_findings(
    tmp_path, monkeypatch,
):
    """First cycle emits; second cycle with unchanged code emits 0 new
    findings (cooldown)."""
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_SENSOR_ENABLED", "1")
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_COOLDOWN_S", "3600")
    _write(tmp_path / "backend/only_orphan.py", """
        def the_orphan():
            return 1
    """)
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = DeepAnalysisSensor(
        repo="jarvis", router=router, project_root=tmp_path,
    )
    first = asyncio.run(sensor.scan_once())
    second = asyncio.run(sensor.scan_once())
    # Second cycle: same finding → filtered by cooldown.
    assert len(first) >= 1
    assert len(second) == 0


def test_sensor_per_cycle_cap(tmp_path, monkeypatch):
    """MAX_FINDINGS_PER_CYCLE limits how many we emit per tick."""
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_SENSOR_ENABLED", "1")
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_MAX_FINDINGS_PER_CYCLE", "1")
    _write(tmp_path / "backend/many_orphans.py", "\n".join(
        f"def orphan_{i}(): return {i}" for i in range(10)
    ))
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = DeepAnalysisSensor(
        repo="jarvis", router=router, project_root=tmp_path,
    )
    findings = asyncio.run(sensor.scan_once())
    # Cap enforced at the sensor level.
    assert len(findings) <= 1


def test_sensor_no_emission_when_router_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_SENSOR_ENABLED", "1")
    _write(tmp_path / "backend/o.py", "def only(): return 1")
    sensor = DeepAnalysisSensor(
        repo="jarvis", router=None, project_root=tmp_path,
    )
    # Must not raise even though router is None.
    findings = asyncio.run(sensor.scan_once())
    # Findings still detected (internal state), just not emitted.
    assert isinstance(findings, list)


# ---------------------------------------------------------------------------
# (7) AST canaries — wiring + authority invariant
# ---------------------------------------------------------------------------


def _read_module_src() -> str:
    p = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/intake/sensors/deep_analysis_sensor.py"
    )
    return p.read_text(encoding="utf-8")


def test_module_declares_authority_invariant_in_docstring():
    """Regression guard: the module's authority boundary must stay
    explicit in the docstring so a refactor can't quietly grant it
    gate-bypassing capabilities."""
    src = _read_module_src()
    assert "authority invariant" in src.lower()
    # Must route through intake — NOT bypass risk/guardian/gate.
    assert "risk engine" in src.lower() or "risk_engine" in src.lower()
    assert "semanticguardian" in src.lower() or "semantic_guardian" in src.lower()


def test_module_declares_honest_scope_caveat():
    """V1 scope honesty: must literally say 'heuristics' and not claim
    semantic understanding."""
    src = _read_module_src()
    assert "heuristics" in src.lower()
    # Must disclaim LLM-based semantic understanding.
    assert "not semantic understanding" in src.lower()


def test_module_has_all_four_analyzers():
    src = _read_module_src()
    for name in (
        "analyze_contract_drift",
        "analyze_coverage_gap",
        "analyze_purpose_drift",
        "analyze_orphan_surface",
    ):
        assert f"def {name}" in src


def test_all_findings_use_low_urgency():
    """Authority invariant: DeepAnalysisSensor emits ANALYTICAL signals,
    not urgent ones. All findings should default to low urgency so
    intake priority + risk classification stay dominated by operator-
    driven or sensor-urgent signals."""
    finding = DeepAnalysisFinding(
        category="contract_drift", file="x.py",
        finding_id="abc1234567", description="test",
    )
    assert finding.urgency == "low"
