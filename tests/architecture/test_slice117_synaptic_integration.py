"""Slice 117 — Synaptic Integration: live escape → immune synthesizer → UI.

Proves the synapse (a verified breach's exact AST source is re-derived and fed
to the Adaptive Immune Synthesizer → a shadow proposal), gated by the immunity
master, and the operator Approval-Matrix gateway surface.
"""

from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance import red_blue_matrix as RB
from backend.core.ouroboros.governance import antibody_synthesizer as AB


_INTROSPECTION_SRC = "leaked = type(obj).__mro__[1]\n"
_CLEAN = ["def run(self, ctx):\n    return ctx.value + 1\n"]


class TestReDerivation:
    def test_raw_strategy_reproduces_seed_source(self):
        from tests.governance.adversarial_corpus.corpus import build_corpus
        seed = build_corpus()[0]
        src = RB._re_derive_escape_source(seed.name, "raw")
        assert src == seed.source

    def test_unknown_seed_returns_none(self):
        assert RB._re_derive_escape_source("no_such_seed_xyz", "raw") is None

    def test_clean_controls_are_loaded(self):
        # The zero-FP guard the synthesizer validates against must be non-empty.
        assert len(RB._clean_controls()) >= 1


class TestSynapse:
    def test_escape_feeds_immunity_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_ADAPTIVE_IMMUNITY_ENABLED", "1")
        proposals = tmp_path / "proposals.jsonl"
        monkeypatch.setenv("JARVIS_ANTIBODY_PROPOSALS_PATH", str(proposals))
        monkeypatch.setenv("JARVIS_ANTIBODY_ACTIVE_PATH", str(tmp_path / "active.jsonl"))
        # The sweep records metadata only → re-derive the exact source. Stub the
        # re-derivation to a known introspection payload (decouples from corpus).
        monkeypatch.setattr(RB, "_re_derive_escape_source", lambda n, s: _INTROSPECTION_SRC)
        RB._feed_escape_to_immunity({"seed_name": "x", "strategy": "alias"}, _CLEAN)
        assert proposals.exists()
        recs = [json.loads(l) for l in proposals.read_text().splitlines() if l.strip()]
        assert recs and "__mro__" in recs[-1]["attr_block"]
        # SHADOW only — never armed.
        assert not (tmp_path / "active.jsonl").exists()

    def test_synapse_inert_when_immunity_off(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_ADAPTIVE_IMMUNITY_ENABLED", raising=False)
        proposals = tmp_path / "proposals.jsonl"
        monkeypatch.setenv("JARVIS_ANTIBODY_PROPOSALS_PATH", str(proposals))
        monkeypatch.setattr(RB, "_re_derive_escape_source", lambda n, s: _INTROSPECTION_SRC)
        RB._feed_escape_to_immunity({"seed_name": "x", "strategy": "alias"}, _CLEAN)
        assert not proposals.exists()  # gated off

    def test_synapse_swallows_unreproducible_escape(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ADAPTIVE_IMMUNITY_ENABLED", "1")
        monkeypatch.setattr(RB, "_re_derive_escape_source", lambda n, s: None)
        RB._feed_escape_to_immunity({"seed_name": "x", "strategy": "raw"}, _CLEAN)  # must not raise


class TestApprovalMatrixGateway:
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.api.observability_gateway import build_router
        app = FastAPI(); app.include_router(build_router())
        return TestClient(app)

    def test_proposals_endpoint_broadcasts_alert(self, tmp_path, monkeypatch):
        proposals = tmp_path / "proposals.jsonl"
        monkeypatch.setenv("JARVIS_ANTIBODY_PROPOSALS_PATH", str(proposals))
        ab = AB.synthesize_antibody(_INTROSPECTION_SRC, _CLEAN)
        AB.propose_antibody(ab, path=proposals)
        r = self._client().get("/api/observability/antibody-proposals")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["armed"] is False
        assert "IMMUNE SYSTEM ALERT" in body["alert"]
        assert body["proposals"][0]["antibody_id"] == ab.antibody_id

    def test_proposals_endpoint_empty_no_alert(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_ANTIBODY_PROPOSALS_PATH", str(tmp_path / "none.jsonl"))
        body = self._client().get("/api/observability/antibody-proposals").json()
        assert body["count"] == 0 and body["alert"] == ""
