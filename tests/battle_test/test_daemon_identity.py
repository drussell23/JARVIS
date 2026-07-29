"""A daemon must be able to say what it is, without being watched.

The whole chase — export a flag, still hear the voice, export again, add
guards, add a sentinel — happened because nothing told the operator (or
me) that the process talking had been running for 23 hours on code from
the previous day. The instrument existed. It was stamped inside
`_start_cockpit_attach_bridge`, so a daemon only recorded what it was made
of IF someone attached a cockpit to it.

A process's identity is a property of the PROCESS, not of whether anyone
is watching it.
"""
from __future__ import annotations

import ast
import json
import pathlib
import time

import pytest

from backend.core.ouroboros.battle_test import daemon_provenance as dp


class TestIdentityIsStampedAtBoot:
    def test_the_stamp_is_NOT_gated_on_a_cockpit(self):
        """It was written inside the attach-bridge setup, so the headless
        daemon — the one an operator cannot see — never stamped itself."""
        src = pathlib.Path(
            "backend/core/ouroboros/battle_test/harness.py").read_text()
        tree = ast.parse(src)
        gated = boot = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(src, node) or ""
            if "write_provenance()" not in body:
                continue
            if node.name == "_start_cockpit_attach_bridge":
                gated = True
            else:
                boot = True
        assert boot, "provenance is written nowhere outside the attach path"

    def test_it_records_what_the_operator_needs(self, tmp_path):
        path = dp.write_provenance(tmp_path / "p.json")
        assert path is not None
        data = dp.read_provenance(tmp_path / "p.json")
        for key in ("commit", "branch", "booted_at", "pid", "env"):
            assert key in data, key


class TestTheIncidentIsNowVISIBLE:
    def test_a_stale_daemon_says_so(self, tmp_path):
        """The exact shape: 23 hours old, behind HEAD."""
        p = tmp_path / "p.json"
        dp.write_provenance(p)
        d = dp.read_provenance(p)
        d["booted_at"] = time.time() - 23 * 3600
        d["commit"] = "0" * 40
        p.write_text(json.dumps(d))
        line = dp.staleness_line(p)
        assert "23h" in line and "behind HEAD" in line

    def test_a_stale_ENVIRONMENT_says_so_even_on_the_same_commit(
            self, tmp_path, monkeypatch):
        """The half that actually bit. Code current, environment a day
        old — no code warning fires, and `export` still cannot reach it."""
        monkeypatch.delenv("JARVIS_VOICE_MUTED", raising=False)
        p = tmp_path / "p.json"
        dp.write_provenance(p)
        monkeypatch.setenv("JARVIS_VOICE_MUTED", "1")
        drift = dp.env_drift(dp.read_provenance(p))
        assert any("JARVIS_VOICE_MUTED" in d for d in drift), drift
        assert "ABSENT in the daemon" in " ".join(drift)

    def test_the_line_names_the_SETTING_not_just_the_fact(self, tmp_path,
                                                          monkeypatch):
        """"different env" is another thing to go and check. The whole
        point is that the operator should not have to."""
        monkeypatch.delenv("JARVIS_VOICE_MUTED", raising=False)
        p = tmp_path / "p.json"
        dp.write_provenance(p)
        monkeypatch.setenv("JARVIS_VOICE_MUTED", "1")
        monkeypatch.setattr(dp, "read_provenance",
                            lambda *_a, **_k: dp.read_provenance.__wrapped__(p)
                            if hasattr(dp.read_provenance, "__wrapped__")
                            else json.loads(p.read_text()))
        line = dp.env_drift_line()
        assert "JARVIS_VOICE_MUTED" in line
        assert "export" in line and "restart" in line

    def test_agreement_is_silent(self, tmp_path):
        dp.write_provenance(tmp_path / "p.json")
        assert dp.env_drift(dp.read_provenance(tmp_path / "p.json")) == []


class TestItRefusesToGuess:
    def test_a_daemon_predating_the_field_claims_NOTHING(self):
        """An older daemon has no `env` key. Claiming agreement we cannot
        verify is how the last three answers went wrong."""
        assert dp.env_drift({"commit": "abc"}) == []

    @pytest.mark.parametrize("junk", [{}, {"env": "x"}, {"env": None},
                                      {"env": {}}, 42])
    def test_junk_degrades(self, junk):
        """`env: null` and `env: {}` mean the daemon recorded NOTHING, not
        that it ran with an empty environment. Treating them alike
        reported every local setting as "absent in the daemon" — a
        confident answer built on a field that was never written.

        `None` is deliberately NOT in this list: it means "use the
        default provenance", which is a legitimate call, not junk."""
        assert dp.env_drift(junk) == []          # type: ignore[arg-type]

    def test_only_JARVIS_vars_are_captured(self, tmp_path, monkeypatch):
        """A full environ dump on disk is a credential leak waiting for a
        postmortem to read it."""
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-never-land")
        dp.write_provenance(tmp_path / "p.json")
        blob = (tmp_path / "p.json").read_text()
        assert "should-never-land" not in blob
        assert "AWS_SECRET" not in blob


class TestTheOperatorSeesIt:
    def test_attach_shows_BOTH_halves(self):
        """Stale code and a stale environment are different failures with
        the same symptom; showing only one sent an operator chasing a flag
        at a process that could never have seen it."""
        src = pathlib.Path("backend/core/ouroboros/cli/ov.py").read_text()
        assert "env_drift_line" in src
        assert "staleness_line" in src
