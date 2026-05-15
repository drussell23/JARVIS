"""Spine + AST-pin regression for auto_commit_graduation_gate.py.

Covers the full verdict matrix (MASTER_OFF / LEDGER_NOT_ELIGIBLE /
NO_GIT_HISTORY / EVIDENCE_INSUFFICIENT / READY), genuine per-soak-window
attribution math, the OV+Yellow body classifier, env-reader clamps,
frozen-artifact roundtrip, fail-closed degradation, and the 6 AST pins
(canonical-pass + synthetic-regression).

The gate's collaborators (ledger progress, clean-soak windows, git log,
canonical markers) are injected via monkeypatch so every verdict is
exercised deterministically without a real ledger or git history.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import (
    auto_commit_graduation_gate as g,
)

_OV = "Ouroboros+Venom [O+V] — Autonomous Self-Development Engine"
_YEL = "Risk: NOTIFY_APPLY"


def _yellow_body() -> str:
    return f"feat: x\n\n{_OV}\nRisk: NOTIFY_APPLY (Yellow)\n"


def _other_body() -> str:
    return f"feat: x\n\n{_OV}\nRisk: SAFE_AUTO (Green)\n"


def _plain_body() -> str:
    return "feat: human commit\n\nno signature\n"


# ===========================================================================
# Body classifier (pure, deterministic)
# ===========================================================================


class TestClassifier:
    def test_yellow_tier(self):
        assert (
            g.classify_commit_body(
                _yellow_body(), ov_marker=_OV, yellow_marker=_YEL
            )
            is g.CommitEvidenceKind.YELLOW_TIER
        )

    def test_other_tier(self):
        assert (
            g.classify_commit_body(
                _other_body(), ov_marker=_OV, yellow_marker=_YEL
            )
            is g.CommitEvidenceKind.OTHER_TIER
        )

    def test_not_ov(self):
        assert (
            g.classify_commit_body(
                _plain_body(), ov_marker=_OV, yellow_marker=_YEL
            )
            is g.CommitEvidenceKind.NOT_OV
        )

    def test_empty_ov_marker_is_not_ov(self):
        # Fail-closed: no marker → cannot classify as OV.
        assert (
            g.classify_commit_body(
                _yellow_body(), ov_marker="", yellow_marker=_YEL
            )
            is g.CommitEvidenceKind.NOT_OV
        )


# ===========================================================================
# Env readers
# ===========================================================================


class TestEnvReaders:
    def test_master_default_false(self):
        assert g.master_enabled() is False

    def test_master_true(self, monkeypatch):
        monkeypatch.setenv(g._ENV_MASTER, "true")
        assert g.master_enabled() is True

    def test_lookback_clamped(self, monkeypatch):
        monkeypatch.setenv(g._ENV_LOOKBACK_DAYS, "9999")
        assert g._lookback_days() == g._MAX_LOOKBACK_DAYS
        monkeypatch.setenv(g._ENV_LOOKBACK_DAYS, "0")
        assert g._lookback_days() == g._DEFAULT_LOOKBACK_DAYS
        monkeypatch.setenv(g._ENV_LOOKBACK_DAYS, "junk")
        assert g._lookback_days() == g._DEFAULT_LOOKBACK_DAYS

    def test_git_timeout_clamped(self, monkeypatch):
        monkeypatch.setenv(g._ENV_GIT_TIMEOUT_S, "99999")
        assert g._git_timeout_s() == g._MAX_GIT_TIMEOUT_S
        monkeypatch.setenv(g._ENV_GIT_TIMEOUT_S, "-3")
        assert g._git_timeout_s() == g._DEFAULT_GIT_TIMEOUT_S

    def test_target_flag_default(self, monkeypatch):
        monkeypatch.delenv(g._ENV_TARGET_FLAG, raising=False)
        assert g._target_flag() == g._DEFAULT_TARGET_FLAG
        monkeypatch.setenv(g._ENV_TARGET_FLAG, "JARVIS_X")
        assert g._target_flag() == "JARVIS_X"


# ===========================================================================
# Verdict matrix
# ===========================================================================


class TestVerdictMatrix:
    async def test_master_off_single_report_no_side_effects(
        self, monkeypatch
    ):
        monkeypatch.setenv(g._ENV_MASTER, "false")
        rep = await g.evaluate_graduation_evidence()
        assert rep.verdict is g.AutoCommitEvidenceVerdict.MASTER_OFF
        assert rep.per_soak_evidence == ()
        assert rep.ledger_clean_count == 0

    async def test_ledger_unavailable_fail_closed(self, monkeypatch):
        monkeypatch.setenv(g._ENV_MASTER, "true")
        monkeypatch.setattr(g, "_ledger_progress", lambda f: None)
        rep = await g.evaluate_graduation_evidence()
        assert (
            rep.verdict
            is g.AutoCommitEvidenceVerdict.LEDGER_NOT_ELIGIBLE
        )
        assert "fail-closed" in rep.diagnostic

    async def test_ledger_not_eligible(self, monkeypatch):
        monkeypatch.setenv(g._ENV_MASTER, "true")
        monkeypatch.setattr(
            g, "_ledger_progress", lambda f: (1, 3, 0, False)
        )
        rep = await g.evaluate_graduation_evidence()
        assert (
            rep.verdict
            is g.AutoCommitEvidenceVerdict.LEDGER_NOT_ELIGIBLE
        )
        assert rep.ledger_clean_count == 1
        assert rep.ledger_required == 3

    async def test_no_git_history_fail_closed(self, monkeypatch):
        monkeypatch.setenv(g._ENV_MASTER, "true")
        monkeypatch.setattr(
            g, "_ledger_progress", lambda f: (3, 3, 0, True)
        )
        monkeypatch.setattr(
            g,
            "_clean_soak_windows",
            lambda f, lookback_days: [
                ("s1", 100.0, 200.0, True),
                ("s2", 200.0, 300.0, False),
                ("s3", 300.0, 400.0, False),
            ],
        )

        async def _no_git(*, since_epoch, timeout_s):
            return None

        monkeypatch.setattr(g, "_read_git_log", _no_git)
        rep = await g.evaluate_graduation_evidence()
        assert (
            rep.verdict
            is g.AutoCommitEvidenceVerdict.NO_GIT_HISTORY
        )
        assert rep.soaks_missing_evidence == 3

    async def test_evidence_insufficient_when_a_soak_has_no_yellow(
        self, monkeypatch
    ):
        monkeypatch.setenv(g._ENV_MASTER, "true")
        monkeypatch.setattr(
            g, "_ledger_progress", lambda f: (3, 3, 0, True)
        )
        monkeypatch.setattr(
            g,
            "_clean_soak_windows",
            lambda f, lookback_days: [
                ("s1", 100.0, 200.0, True),
                ("s2", 200.0, 300.0, False),
                ("s3", 300.0, 400.0, False),
            ],
        )
        monkeypatch.setattr(g, "_ov_marker", lambda: _OV)
        monkeypatch.setattr(g, "_yellow_marker", lambda: _YEL)

        async def _git(*, since_epoch, timeout_s):
            # s1 + s2 windows have a Yellow OV commit; s3 has NONE.
            return [
                ("h1", 150.0, _yellow_body()),
                ("h2", 250.0, _yellow_body()),
                ("h3", 350.0, _other_body()),  # OV but SAFE_AUTO
            ]

        monkeypatch.setattr(g, "_read_git_log", _git)
        rep = await g.evaluate_graduation_evidence()
        assert (
            rep.verdict
            is g.AutoCommitEvidenceVerdict.EVIDENCE_INSUFFICIENT
        )
        assert rep.soaks_with_evidence == 2
        assert rep.soaks_missing_evidence == 1
        assert "92.16-class overclaim" in rep.diagnostic
        # the bare soak is s3
        bare = [
            e.session_id
            for e in rep.per_soak_evidence
            if not e.has_evidence
        ]
        assert bare == ["s3"]

    async def test_ready_when_every_soak_has_yellow_evidence(
        self, monkeypatch
    ):
        monkeypatch.setenv(g._ENV_MASTER, "true")
        monkeypatch.setattr(
            g, "_ledger_progress", lambda f: (3, 3, 0, True)
        )
        monkeypatch.setattr(
            g,
            "_clean_soak_windows",
            lambda f, lookback_days: [
                ("s1", 100.0, 200.0, False),
                ("s2", 200.0, 300.0, False),
                ("s3", 300.0, 400.0, False),
            ],
        )
        monkeypatch.setattr(g, "_ov_marker", lambda: _OV)
        monkeypatch.setattr(g, "_yellow_marker", lambda: _YEL)

        async def _git(*, since_epoch, timeout_s):
            return [
                ("ha", 150.0, _yellow_body()),
                ("hb", 250.0, _yellow_body()),
                ("hc", 350.0, _yellow_body()),
                ("hd", 360.0, _other_body()),  # extra, non-Yellow
            ]

        monkeypatch.setattr(g, "_read_git_log", _git)
        rep = await g.evaluate_graduation_evidence()
        assert rep.verdict is g.AutoCommitEvidenceVerdict.READY
        assert rep.soaks_with_evidence == 3
        assert rep.soaks_missing_evidence == 0
        assert "Operator may flip with evidence" in rep.diagnostic

    async def test_ready_first_soak_bounded_note(self, monkeypatch):
        monkeypatch.setenv(g._ENV_MASTER, "true")
        monkeypatch.setattr(
            g, "_ledger_progress", lambda f: (1, 1, 0, True)
        )
        monkeypatch.setattr(
            g,
            "_clean_soak_windows",
            lambda f, lookback_days: [("s1", 0.0, 200.0, True)],
        )
        monkeypatch.setattr(g, "_ov_marker", lambda: _OV)
        monkeypatch.setattr(g, "_yellow_marker", lambda: _YEL)

        async def _git(*, since_epoch, timeout_s):
            return [("ha", 150.0, _yellow_body())]

        monkeypatch.setattr(g, "_read_git_log", _git)
        rep = await g.evaluate_graduation_evidence()
        assert rep.verdict is g.AutoCommitEvidenceVerdict.READY
        assert "lookback-bounded" in rep.diagnostic
        assert rep.per_soak_evidence[0].is_first_soak_bounded is True

    async def test_window_boundaries_are_inclusive_and_scoped(
        self, monkeypatch
    ):
        # A commit OUTSIDE a soak window must not count for it.
        monkeypatch.setenv(g._ENV_MASTER, "true")
        monkeypatch.setattr(
            g, "_ledger_progress", lambda f: (1, 1, 0, True)
        )
        monkeypatch.setattr(
            g,
            "_clean_soak_windows",
            lambda f, lookback_days: [("s1", 100.0, 200.0, False)],
        )
        monkeypatch.setattr(g, "_ov_marker", lambda: _OV)
        monkeypatch.setattr(g, "_yellow_marker", lambda: _YEL)

        async def _git(*, since_epoch, timeout_s):
            return [("out", 250.0, _yellow_body())]  # after window

        monkeypatch.setattr(g, "_read_git_log", _git)
        rep = await g.evaluate_graduation_evidence()
        assert (
            rep.verdict
            is g.AutoCommitEvidenceVerdict.EVIDENCE_INSUFFICIENT
        )
        assert rep.per_soak_evidence[0].yellow_tier_count == 0


# ===========================================================================
# evidence_summary + render
# ===========================================================================


class TestSummaryAndRender:
    async def test_summary_master_off(self, monkeypatch):
        monkeypatch.setenv(g._ENV_MASTER, "false")
        s = await g.evidence_summary()
        assert s["verdict"] == "master_off"
        assert s["meets_evidence_gate"] is False

    async def test_summary_ready_gate_true(self, monkeypatch):
        monkeypatch.setenv(g._ENV_MASTER, "true")
        monkeypatch.setattr(
            g, "_ledger_progress", lambda f: (1, 1, 0, True)
        )
        monkeypatch.setattr(
            g,
            "_clean_soak_windows",
            lambda f, lookback_days: [("s1", 100.0, 200.0, False)],
        )
        monkeypatch.setattr(g, "_ov_marker", lambda: _OV)
        monkeypatch.setattr(g, "_yellow_marker", lambda: _YEL)

        async def _git(*, since_epoch, timeout_s):
            return [("ha", 150.0, _yellow_body())]

        monkeypatch.setattr(g, "_read_git_log", _git)
        s = await g.evidence_summary()
        assert s["verdict"] == "ready"
        assert s["meets_evidence_gate"] is True

    def test_render_report_json_roundtrips(self):
        rep = g._master_off_report("JARVIS_AUTO_COMMIT_ENABLED")
        js = g.render_report_json(rep)
        import json

        d = json.loads(js)
        assert d["verdict"] == "master_off"
        back = g.AutoCommitGraduationReport.from_dict(d)
        assert back.verdict is rep.verdict
        assert (
            back.schema_version == g.AUTOCOMMIT_GRAD_SCHEMA_VERSION
        )

    def test_soak_evidence_roundtrip(self):
        e = g.SoakCommitEvidence(
            session_id="s1",
            window_start_epoch=1.0,
            window_end_epoch=2.0,
            yellow_tier_count=2,
            other_tier_count=1,
            sample_yellow_hashes=("abc", "def"),
            is_first_soak_bounded=True,
        )
        back = g.SoakCommitEvidence.from_dict(e.to_dict())
        assert back == e
        assert back.has_evidence is True


# ===========================================================================
# AST pins — canonical pass
# ===========================================================================


@pytest.fixture
def canonical_src_tree():
    src = Path(g.__file__).read_text(encoding="utf-8")
    return src, ast.parse(src)


@pytest.fixture
def pins():
    return g.register_shipped_invariants()


class TestAstPinsCanonicalPass:
    def test_6_pins_registered(self, pins):
        assert len(pins) == 6
        assert {p.invariant_name for p in pins} == {
            "autocommit_grad_kind_taxonomy_closed",
            "autocommit_grad_verdict_taxonomy_closed",
            "autocommit_grad_authority_asymmetry",
            "autocommit_grad_no_shell_subprocess",
            "autocommit_grad_no_hardcoded_markers",
            "autocommit_grad_master_default_false",
        }

    def test_all_pins_pass_on_canonical_source(
        self, canonical_src_tree, pins
    ):
        src, tree = canonical_src_tree
        for pin in pins:
            v = pin.validate(tree, src)
            assert v == (), f"{pin.invariant_name}: {v}"


# ===========================================================================
# AST pins — synthetic regression
# ===========================================================================


def _pin(pins, name):
    return next(p for p in pins if p.invariant_name == name)


class TestAstPinsSyntheticRegression:
    def test_kind_taxonomy_fires_on_missing(self, pins):
        syn = (
            "import enum\n"
            "class CommitEvidenceKind(str, enum.Enum):\n"
            "    YELLOW_TIER = 'yellow_tier'\n"
        )
        v = _pin(
            pins, "autocommit_grad_kind_taxonomy_closed"
        ).validate(ast.parse(syn), syn)
        assert v and "missing" in v[0]

    def test_verdict_taxonomy_fires_on_drift(self, pins):
        syn = (
            "import enum\n"
            "class AutoCommitEvidenceVerdict(str, enum.Enum):\n"
            "    READY = 'ready'\n"
            "    LEDGER_NOT_ELIGIBLE = 'ledger_not_eligible'\n"
            "    EVIDENCE_INSUFFICIENT = 'evidence_insufficient'\n"
            "    NO_GIT_HISTORY = 'no_git_history'\n"
            "    MASTER_OFF = 'master_off'\n"
            "    SNEAKY = 'sneaky'\n"
        )
        v = _pin(
            pins, "autocommit_grad_verdict_taxonomy_closed"
        ).validate(ast.parse(syn), syn)
        assert v and "drift" in v[0]

    def test_authority_asymmetry_fires(self, pins):
        syn = (
            "from backend.core.ouroboros.governance.iron_gate "
            "import IronGate\n"
        )
        v = _pin(
            pins, "autocommit_grad_authority_asymmetry"
        ).validate(ast.parse(syn), syn)
        assert v and "forbidden import" in v[0]

    def test_no_shell_subprocess_fires_on_shell_true(self, pins):
        syn = (
            "import asyncio\n"
            "async def f():\n"
            "    await asyncio.create_subprocess_shell('git log')\n"
        )
        v = _pin(
            pins, "autocommit_grad_no_shell_subprocess"
        ).validate(ast.parse(syn), syn)
        assert v and "forbidden shell call" in v[0]

    def test_no_shell_subprocess_fires_on_shell_kwarg(self, pins):
        syn = (
            "import subprocess\n"
            "def f():\n"
            "    subprocess.run(['git'], shell=True)\n"
        )
        v = _pin(
            pins, "autocommit_grad_no_shell_subprocess"
        ).validate(ast.parse(syn), syn)
        assert v and "shell=True" in v[0]

    def test_no_hardcoded_markers_fires_in_operational_code(
        self, pins
    ):
        # A hardcoded marker assigned as an operational value (NOT a
        # docstring) must fire.
        syn = (
            "def detect(body):\n"
            "    m = 'Ouroboros+Venom ' '[O+V]'\n"
            "    return m in body\n"
        )
        v = _pin(
            pins, "autocommit_grad_no_hardcoded_markers"
        ).validate(ast.parse(syn), syn)
        assert v and "operational code" in v[0]

    def test_no_hardcoded_markers_allows_docstring_prose(self, pins):
        # Documentation legitimately names the marker — must NOT fire.
        syn = (
            '"""This module detects the Risk: NOTIFY_APPLY body '
            'marker on Ouroboros+Venom [O+V] commits."""\n'
            "def f():\n"
            '    """Explains Risk: NOTIFY_APPLY in prose."""\n'
            "    return 1\n"
        )
        v = _pin(
            pins, "autocommit_grad_no_hardcoded_markers"
        ).validate(ast.parse(syn), syn)
        assert v == ()

    def test_master_default_false_fires_on_truthy(self, pins):
        syn = (
            "import os\n"
            "def master_enabled():\n"
            "    raw = os.environ.get('X', 'true')"
            ".strip().lower()\n"
            "    return raw in ('true',)\n"
        )
        v = _pin(
            pins, "autocommit_grad_master_default_false"
        ).validate(ast.parse(syn), syn)
        assert v and "truthy" in v[0]


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


class TestFlagSeeds:
    def test_register_flags_seeds_all_four(self):
        from backend.core.ouroboros.governance.flag_registry import (
            FlagRegistry,
        )

        reg = FlagRegistry()
        assert g.register_flags(reg) == 4

    def test_master_seeded_default_false(self):
        from backend.core.ouroboros.governance.flag_registry import (
            FlagRegistry,
        )

        reg = FlagRegistry()
        g.register_flags(reg)
        spec = reg.get_spec(g._ENV_MASTER)
        assert spec is not None
        assert spec.default is False
