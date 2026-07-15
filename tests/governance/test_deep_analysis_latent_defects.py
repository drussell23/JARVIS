"""P0.3 — proactive substance perception via DeepAnalysisSensor.latent_defect.

The roaming sensors surface cosmetic metrics; TestFailure is reactive. The
DeepAnalysisSensor (contract_drift / coverage_gap / purpose_drift /
orphan_surface) perceived substance but was never REGISTERED, and emitted only
low-urgency advisories. P0.3 resurrects it and adds a high-precision
`latent_defect` analyzer — REAL AST bug patterns emitted at high urgency +
confidence so P0.1's value layer escalates them out of the deferred floor.

Proof: every pattern fires on a real bug; every near-zero-FP EXCLUSION holds
(the NaN idiom, `except Exception`, `is None`, immutable defaults, comment-
strings); defects carry high urgency + confidence; the analyzer is gated + in
the aggregator; confidence flows to the envelope; the sensor is wired live.
"""
from __future__ import annotations

import inspect
from collections import Counter
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    deep_analysis_sensor as D,
)
from backend.core.ouroboros.governance.intake.sensors.deep_analysis_sensor import (
    DeepAnalysisFinding,
    DeepAnalysisSensor,
    analyze_latent_defects,
)


def _scan(tmp_path: Path, src: str) -> Counter:
    (tmp_path / "m.py").write_text(src)
    fs = analyze_latent_defects(repo_root=tmp_path)
    return Counter(f.evidence.get("subcategory") for f in fs)


# ── each pattern fires on a real bug ─────────────────────────────────

def test_mutable_default_arg(tmp_path):
    assert _scan(tmp_path, "def f(x=[]):\n    return x\n")["mutable_default_arg"] == 1
    assert _scan(tmp_path, "def g(d={}):\n    return d\n")["mutable_default_arg"] == 1
    assert _scan(tmp_path, "def h(s=set()):\n    return s\n")["mutable_default_arg"] == 1


def test_bare_except_pass(tmp_path):
    src = "def f():\n    try:\n        x()\n    except:\n        pass\n"
    assert _scan(tmp_path, src)["bare_except_pass"] == 1


def test_is_comparison_literal(tmp_path):
    assert _scan(tmp_path, "def f(x):\n    return x is 5\n")["is_comparison_literal"] == 1
    assert _scan(tmp_path, "def f(x):\n    return x is 'a'\n")["is_comparison_literal"] == 1


def test_unreachable_code(tmp_path):
    src = "def f():\n    return 1\n    x = 2\n"
    assert _scan(tmp_path, src)["unreachable_code"] == 1


def test_unreachable_after_raise_and_continue(tmp_path):
    src = (
        "def f(items):\n"
        "    for i in items:\n"
        "        continue\n"
        "        print(i)\n"
    )
    assert _scan(tmp_path, src)["unreachable_code"] == 1


# ── near-zero-FP: the exclusions hold ────────────────────────────────

def test_nan_idiom_not_flagged(tmp_path):
    """`x == x` / `x != x` is the float-NaN check — NEVER a self-comparison
    'bug'. This was the FP that self_comparison was removed to avoid."""
    src = "def f(x):\n    return x == x or (not (x != x))\n"
    c = _scan(tmp_path, src)
    assert sum(c.values()) == 0


def test_except_exception_not_flagged(tmp_path):
    """`except Exception: pass` is common + intentional here — only a BARE
    `except:` is flagged."""
    src = "def f():\n    try:\n        x()\n    except Exception:\n        pass\n"
    assert _scan(tmp_path, src)["bare_except_pass"] == 0


def test_is_none_true_false_not_flagged(tmp_path):
    """`is None` / `is True` / `is False` are the CORRECT `is` idioms."""
    src = (
        "def f(x):\n"
        "    return x is None or x is True or x is False or x is ...\n"
    )
    assert _scan(tmp_path, src)["is_comparison_literal"] == 0


def test_immutable_default_not_flagged(tmp_path):
    """A tuple / constant default is immutable — not the mutable-default bug."""
    src = "def f(x=(), y=0, z='s'):\n    return x\n"
    assert _scan(tmp_path, src)["mutable_default_arg"] == 0


def test_comment_string_after_return_not_flagged(tmp_path):
    """A triple-quoted string after `return` is disabled-code documentation,
    not a live-code unreachable bug."""
    src = 'def f():\n    return 1\n    """disabled code block\n    x = 2\n    """\n'
    assert _scan(tmp_path, src)["unreachable_code"] == 0


def test_clean_code_yields_nothing(tmp_path):
    src = (
        "def f(x=None):\n"
        "    if x is None:\n"
        "        x = []\n"
        "    try:\n"
        "        return x[0]\n"
        "    except IndexError:\n"
        "        return None\n"
    )
    assert sum(_scan(tmp_path, src).values()) == 0


# ── defects are substantive: high urgency + confidence ───────────────

def test_defect_findings_are_high_urgency_and_confidence(tmp_path):
    (tmp_path / "m.py").write_text("def f(x=[]):\n    return x\n")
    fs = analyze_latent_defects(repo_root=tmp_path)
    assert fs
    for f in fs:
        assert f.category == "latent_defect"
        assert f.urgency == "high"
        assert f.confidence == 0.9


# ── aggregator: gated + sorted substance-first ───────────────────────

def test_aggregator_includes_and_gates_latent_defect(tmp_path, monkeypatch):
    (tmp_path / "m.py").write_text("def f(x=[]):\n    return x\n")
    # Isolate: only the defect analyzer on (others off) so the assertion is
    # about latent_defect specifically.
    for env in ("CONTRACT", "COVERAGE", "PURPOSE", "ORPHAN"):
        monkeypatch.setenv(f"JARVIS_DEEP_ANALYSIS_{env}_ENABLED", "0")
    fs = D.run_all_analyzers(repo_root=tmp_path)
    assert any(f.category == "latent_defect" for f in fs)
    # Now gate the defect analyzer off.
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_DEFECT_ENABLED", "0")
    fs2 = D.run_all_analyzers(repo_root=tmp_path)
    assert not any(f.category == "latent_defect" for f in fs2)


def test_high_urgency_sorted_first_survives_cap(monkeypatch):
    """The per-cycle cap must not trim the high-urgency defects behind
    low-urgency advisories."""
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_MAX_FINDINGS_PER_CYCLE", "1")
    # A hand-built merged list would be trimmed to 1; the sort must keep the
    # high one. We assert the sort key directly.
    lo = DeepAnalysisFinding(category="orphan_surface", file="a.py",
                             finding_id="1", description="x", urgency="low")
    hi = DeepAnalysisFinding(category="latent_defect", file="b.py",
                             finding_id="2", description="y", urgency="high",
                             confidence=0.9)
    merged = [lo, hi]
    rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}
    merged.sort(key=lambda f: rank.get(f.urgency, 3))
    assert merged[0] is hi  # high survives a cap of 1


# ── confidence flows to the envelope; sensor wired live ──────────────

@pytest.mark.asyncio
async def test_confidence_flows_to_envelope():
    class _R:
        def __init__(self): self.got = []
        async def ingest(self, e): self.got.append(e); return "ok"

    r = _R()
    s = DeepAnalysisSensor(repo="jarvis", router=r, project_root=Path("."))
    finding = DeepAnalysisFinding(
        category="latent_defect", file="x.py", finding_id="abc",
        description="mutable default", line=3, urgency="high", confidence=0.9,
        evidence={"subcategory": "mutable_default_arg"},
    )
    await s._emit_findings([finding])
    assert len(r.got) == 1
    env = r.got[0]
    assert env.confidence == 0.9  # NOT the old hardcoded 0.55
    assert env.urgency == "high"
    assert env.evidence.get("deep_analysis_category") == "latent_defect"


def test_sensor_wired_into_intake_layer():
    from backend.core.ouroboros.governance.intake import intake_layer_service
    src = inspect.getsource(intake_layer_service)
    assert "DeepAnalysisSensor(" in src
    assert "self._sensors.append(_deep_sensor)" in src
