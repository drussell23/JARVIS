"""The cockpit says only what is true, and one file is worked by one op.

Found live (2026-10-13, daemon bt-2026-10-13-202452):
  1. raw generation JSON -- escaped diffs, a model-claimed "gpt-4o" -- in the
     transcript (two mirrors forwarding model output verbatim);
  2. "✓ ... Landed" and the landed counter for an op whose VERIFY regressed
     and rolled back (ChangeEngine announced `applied` before VERIFY);
  3. three ops on one file at once (the router's file registry had no writer);
  4. the exploration sensor re-picking a file whose work had landed;
  5. bare "○ Operation" rows (an op that never sent INTENT);
  plus: an operator-approved session change could never land a comment.
"""
from __future__ import annotations

import ast
import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.battle_test.generation_view import GenerationView
from backend.core.ouroboros.governance import claude_style_transport as cst
from backend.core.ouroboros.governance import foreground_session_ops as fso
from backend.core.ouroboros.governance import goal_reconciliation_ledger as L
from backend.core.ouroboros.governance import landed_metrics as lm
from backend.core.ouroboros.governance import op_recap

_REPO = Path(__file__).resolve().parents[2]


# ---- 1. the generation view ---------------------------------------------------------

def _view(lines):
    v = GenerationView()
    out: List[str] = []
    for ln in lines:
        out.extend(v.feed_line(ln))
    return out + v.finish(), v


def test_prose_passes_through_untouched():
    out, _ = _view(["The admission gate keeps one slot free.", "", "[a link](x.md) too."])
    assert out == ["The admission gate keeps one slot free.", "", "[a link](x.md) too."]


def test_a_tool_call_round_shows_nothing_its_blocks_already_show():
    raw = json.dumps({"tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}],
                      "preamble": "Checking state first."}, indent=2).splitlines()
    out, _ = _view(raw)
    assert out == []                       # tool blocks + preamble narration own these


def test_a_candidate_envelope_reduces_to_its_rationale_and_never_the_metadata():
    env = {"schema_version": "2b.1-diff", "candidates": [{
        "candidate_id": "c1", "file_path": "x.py",
        "unified_diff": "@@ -1,1 +1,1 @@\n-a = {\n+a = }\n", "rationale": "Bump a."}],
        "provider_metadata": {"model_id": "gpt-4o-2024-08-06", "rationale": "made up"}}
    out, _ = _view(["Here is the change:"] + json.dumps(env, indent=2).splitlines())
    assert out == ["Here is the change:", "Bump a."]   # braces inside the diff string do not end it


def test_malformed_json_is_healed_not_dumped():
    out, v = _view(['{"candidates": [{"rationale": "Fix it.",}],'])   # trailing comma, truncated
    assert out == ["Fix it."] and v.dropped == 0


def test_a_fenced_value_and_an_unhealable_one():
    out, _ = _view(["```json", '{"candidates": [{"rationale": "Fenced."}]}', "```", "after"])
    assert out == ["Fenced.", "after"]
    out, v = _view(["{", "  not json at all ::: <<<", "  more garbage"])
    assert out == [] and v.dropped == 1     # dropped and counted, never shown


def test_the_renderer_mirror_sends_interpreted_lines(monkeypatch):
    from backend.core.ouroboros.battle_test import stream_renderer as sr
    sent: List[str] = []
    monkeypatch.setattr(sr.StreamRenderer, "_emit_deck_line", lambda self, lead: sent.append(lead) or True)
    monkeypatch.setattr(sr, "mirror_stream_enabled", lambda: True)
    r = sr.StreamRenderer()
    r._buffer = 'Thinking.\n{\n  "candidates": [{"rationale": "Do X."}],\n  "provider_metadata": {"model_id": "gpt-4o"}\n}\n'
    r._mirror_completed_lines(final=True)
    assert len(sent) == 2 and "Thinking." in sent[0] and "Do X." in sent[1]
    assert not any("{" in s or "gpt-4o" in s for s in sent)


def test_the_stream_mirror_interprets_and_escapes():
    from backend.core.ouroboros.battle_test.stream_mirror import StreamMirror
    frames: List[str] = []
    m = StreamMirror(emit=frames.append, flush_chars=10_000)
    for tok in ['[bold]prose[/bold]\n{"candidates": [', '{"rationale": "Why."}]}\n']:
        m.on_token(tok)
    m.end()
    text = "\n".join(frames)
    assert "\\[bold]" in text and "Why." in text and "candidates" not in text


def test_serpent_flow_arms_one_mirror_only():
    src = (_REPO / "backend/core/ouroboros/battle_test/serpent_flow.py").read_text(encoding="utf-8")
    assert 'hasattr(\n                    self._stream_renderer, "_mirror_completed_lines"' in src


# ---- 2. two-phase landing -------------------------------------------------------------

def test_only_an_applied_verdict_with_a_commit_is_a_landing():
    assert lm.decision_landed({"outcome": "applied", lm.COMMIT_KEY: "abc123"})
    assert not lm.decision_landed({"outcome": "applied", lm.COMMIT_KEY: ""})
    assert not lm.decision_landed({"outcome": lm.WRITTEN_OUTCOME, lm.COMMIT_KEY: "abc"})


def test_the_counter_ignores_written_and_uncommitted(monkeypatch):
    lm.reset_for_tests()
    t = lm.LandedMetricsTransport()
    monkeypatch.setattr(t, "_file", lambda *_a, **_k: None)

    def _d(op, **p):
        return SimpleNamespace(msg_type=SimpleNamespace(value="DECISION"), op_id=op, payload=p)
    asyncio.run(t.send(_d("op-1", outcome=lm.WRITTEN_OUTCOME)))
    asyncio.run(t.send(_d("op-2", outcome="applied", reason_code="verified_uncommitted")))
    assert lm.get_landed_metrics().snapshot().total == 0
    asyncio.run(t.send(_d("op-3", outcome="applied", **{lm.COMMIT_KEY: "c0ffee"})))
    assert lm.get_landed_metrics().snapshot().total == 1
    lm.reset_for_tests()


class _Console:
    def __init__(self) -> None:
        self.prints: List[str] = []

    def print(self, text: str, **_kw: Any) -> None:
        self.prints.append(str(text))


def _transport():
    con = _Console()
    t = cst.ClaudeStyleTransport(console=con)
    t._handle_intent("op-1", {"goal": "Lift the fallback policy", "outcome_source": "Roadmap"})
    con.prints.clear()
    return t, con


def test_written_keeps_the_op_open_and_a_failure_after_it_is_a_failure():
    t, con = _transport()
    t._handle_decision("op-1", {"outcome": lm.WRITTEN_OUTCOME})
    assert "op-1" in t._op_state and not any("✓" in p or "Landed" in p for p in con.prints)
    t._handle_decision("op-1", {"outcome": "failed", "reason_code": "verify_regression"})
    assert any("verify regression" in p for p in con.prints)
    assert not any("Landed" in p for p in con.prints)


def test_landed_is_said_only_with_a_commit():
    t, con = _transport()
    t._handle_decision("op-1", {"outcome": "applied", lm.COMMIT_KEY: "c0ffee"})
    assert any("Landed for" in p for p in con.prints)
    t2, con2 = _transport()
    t2._handle_decision("op-1", {"outcome": "applied", lm.COMMIT_KEY: ""})
    assert any("not committed" in p for p in con2.prints)
    assert not any("Landed" in p for p in con2.prints)


def test_the_recap_never_draws_landed_at_random(monkeypatch):
    monkeypatch.setenv("JARVIS_RECAP_VERBS", "Landed,Forged")
    assert {op_recap.recap_verb(f"op-{i}") for i in range(50)} == {"Forged"}
    assert op_recap.recap_verb("op-x", landed=True) == op_recap.LANDED_VERB


def test_change_engine_announces_written_and_the_pipeline_the_verdict():
    gov = _REPO / "backend/core/ouroboros/governance"
    ce = (gov / "change_engine.py").read_text(encoding="utf-8")
    assert "outcome=WRITTEN_OUTCOME" in ce and 'root_cause="none"' not in ce.split("WRITTEN_OUTCOME,")[1][:600]
    runner = (gov / "phase_runners/slice4b_runner.py").read_text(encoding="utf-8")
    probe, announce, promo_fail = (runner.index("_emit_terminal_durability_probe(ctx"),
                                   runner.index("_announce_verified_outcome(ctx"),
                                   runner.index('reason="promotion_failed:%s"'))
    assert probe < promo_fail < announce      # never a verdict for a failed promotion


@pytest.mark.parametrize("rel", ["phase_runners/slice4b_runner.py", "orchestrator.py"])
def test_a_verify_run_that_crashes_is_a_failure(rel):
    """The broad except once logged at DEBUG and left the True default: a
    crashed test run landed the change."""
    src = (_REPO / "backend/core/ouroboros/governance" / rel).read_text(encoding="utf-8")
    assert "Verify scoped test error" not in src            # the silent DEBUG pass is gone
    handlers = [h for h in ast.walk(ast.parse(src))
                if isinstance(h, ast.ExceptHandler) and "failed to complete" in ast.unparse(h)]
    assert handlers, "no handler for a scoped verify run that failed to complete"
    for h in handlers:
        body = ast.unparse(h)
        assert "_verify_test_passed = False" in body and "_verify_crash =" in body
    assert "scoped verify could not run:" in src


async def test_the_verdict_carries_the_commit_only_when_durable():
    from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator
    comm = SimpleNamespace(emit_decision=AsyncMock(), emit_postmortem=AsyncMock())
    orch = SimpleNamespace(_stack=SimpleNamespace(comm=comm),
                           _terminal_durability=GovernedOrchestrator._terminal_durability)
    ctx = SimpleNamespace(op_id="op-1", target_files=("a.py",))
    await GovernedOrchestrator._announce_verified_outcome(orch, ctx, "c0ffee", None)
    kw = comm.emit_decision.call_args.kwargs
    assert kw["outcome"] == "applied" and kw[lm.COMMIT_KEY] == "c0ffee"
    promo = SimpleNamespace(attempted=True, promoted=False)       # pending
    await GovernedOrchestrator._announce_verified_outcome(orch, ctx, "c0ffee", promo)
    assert comm.emit_decision.call_args.kwargs[lm.COMMIT_KEY] == ""
    assert comm.emit_postmortem.await_count == 2                  # the op's closure


# ---- 4 (bypass). operator-approved changes skip the cosmetic gate ----------------------

def test_the_seed_needs_both_halves(monkeypatch):
    seeded = SimpleNamespace(intake_evidence_json=json.dumps(
        {"foreground_session": {"seed_ref": "r1"}}))
    plain = SimpleNamespace(intake_evidence_json="{}")
    from_seed = SimpleNamespace(provider_name=fso.SEED_PROVIDER)
    regenerated = SimpleNamespace(provider_name="gcp-jprime")
    assert fso.is_operator_seed(seeded, from_seed)
    assert not fso.is_operator_seed(seeded, regenerated)
    assert not fso.is_operator_seed(plain, from_seed)


def test_the_value_gate_does_not_judge_the_operators_seed(monkeypatch):
    from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator
    monkeypatch.delenv("JARVIS_CANDIDATE_VALUE_GATE_ENABLED", raising=False)
    seeded = SimpleNamespace(op_id="op-s", intake_evidence_json=json.dumps(
        {"foreground_session": {"seed_ref": "r1"}}))
    applies = GovernedOrchestrator._value_gate_applies
    assert applies(None, seeded, SimpleNamespace(provider_name=fso.SEED_PROVIDER)) is False
    assert applies(None, seeded, SimpleNamespace(provider_name="gcp-jprime")) is True
    monkeypatch.setenv("JARVIS_CANDIDATE_VALUE_GATE_ENABLED", "false")
    assert applies(None, seeded, SimpleNamespace(provider_name="gcp-jprime")) is False


# ---- 5. every row has a title -----------------------------------------------------------

def test_an_op_that_never_announced_is_named_from_its_context(monkeypatch):
    from backend.core.ouroboros.governance import in_flight_registry as ifr
    ctx = SimpleNamespace(description="Make the broad except blocks observable",
                          target_files=("backend/api/sse_contract.py",), signal_source="roadmap")
    reg = SimpleNamespace(lookup=lambda op: SimpleNamespace(ctx_ref=ctx) if op == "op-r" else None)
    monkeypatch.setattr(ifr, "get_default_registry", lambda: reg)
    t = cst.ClaudeStyleTransport(console=_Console())
    sensor, summary = t._context_named("op-r")
    assert sensor == "Roadmap" and summary.startswith("Make the broad except")
    sensor, summary = t._context_named("op-unknown-01a136f1")
    assert sensor == "Operation" and summary                  # never empty


# ---- 3. one file, one op -----------------------------------------------------------------

def _env(op_id, files=("backend/x.py",)):
    from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
    return make_envelope(source="backlog", description=f"work {op_id}", target_files=tuple(files),
                         repo="jarvis", confidence=0.9, urgency="normal",
                         evidence={"signature": op_id}, requires_human_ack=False, causal_id=op_id)


def _router(tmp_path, live=lambda op: True):
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        IntakeRouterConfig, UnifiedIntakeRouter,
    )
    gls = MagicMock()
    gls.submit = AsyncMock()
    gls.op_is_live = live
    return UnifiedIntakeRouter(gls=gls, config=IntakeRouterConfig(project_root=tmp_path,
                                                                 dedup_window_s=0.0))


def test_admission_claims_files_atomically_and_a_second_op_waits(tmp_path):
    r = _router(tmp_path)
    assert r._claim_target_files(_env("op-a")) is None
    assert r._claim_target_files(_env("op-b", files=("backend/y.py", "backend/x.py"))) == "op-a"
    assert "backend/y.py" not in r._active_file_ops          # nothing claimed on refusal
    assert r._claim_target_files(_env("op-a")) is None        # idempotent for the holder


def test_a_not_enqueued_ingest_releases_only_what_it_took(tmp_path):
    r = _router(tmp_path)
    r._claim_target_files(_env("op-a"))
    r._settle_file_claims("op-a", "backpressure")
    assert not r._active_file_ops
    r._active_file_ops["backend/x.py"] = ("op-live", 0.0)     # held by a live op, not this ingest
    r._settle_file_claims("op-live", "deduplicated")
    assert r._active_file_ops["backend/x.py"][0] == "op-live"


def test_an_expired_lock_frees_only_when_its_holder_has_died(tmp_path):
    alive = {"op-a": True}
    r = _router(tmp_path, live=lambda op: alive.get(op, False))
    r._file_lock_ttl_s = 0.0
    r._claim_target_files(_env("op-a"))
    assert r._claim_target_files(_env("op-b")) == "op-a"     # past TTL but alive: held
    alive["op-a"] = False                                     # its worker crashed
    assert r._claim_target_files(_env("op-b")) is None
    assert r._active_file_ops["backend/x.py"][0] == "op-b"


def test_liveness_is_the_pool_and_the_running_set():
    from backend.core.ouroboros.governance.background_agent_pool import (
        BackgroundAgentPool, BackgroundOp,
    )
    from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService
    pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=1, queue_size=4)
    pool._ops["bg-1"] = BackgroundOp(op_id="bg-1", goal="g",
                                     context=SimpleNamespace(op_id="op-q"), status="queued")
    pool._ops["bg-2"] = BackgroundOp(op_id="bg-2", goal="g",
                                     context=SimpleNamespace(op_id="op-done"), status="completed")
    gls = SimpleNamespace(_active_ops={"op-run"}, _bg_pool=pool)
    live = GovernedLoopService.op_is_live
    assert live(gls, "op-run") and live(gls, "op-q")
    assert not live(gls, "op-done") and not live(gls, "")


# ---- 4. work-finders skip landed files ----------------------------------------------------

@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv(L._ENV_LEDGER_PATH, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(L._ENV_REPO_ROOT, str(tmp_path))
    monkeypatch.setenv("JARVIS_ROADMAP_READER_HMAC_SECRET", "s")
    monkeypatch.setenv("JARVIS_ROADMAP_PATH", str(tmp_path / "no-roadmap.yaml"))
    monkeypatch.setenv("JARVIS_ROADMAP_READER_PATH", str(tmp_path / "no-roadmap.yaml"))
    monkeypatch.delenv(L._ENV_ENABLED, raising=False)
    return tmp_path


async def test_a_file_whose_work_landed_is_settled(ledger):
    from backend.core.ouroboros.governance.autonomy.goal_discovery import settled_files

    def git(*a):
        return subprocess.run(["git", *a], cwd=ledger, check=True, capture_output=True,
                              text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
    sha = git("rev-parse", "HEAD")
    await L.record_landing(goal_id="ov-auto-uncovered-module-model-status-api",
                           goal_digest_hex="", commit_sha=sha, op_id="op-1")
    got = await settled_files(["backend/api/model_status_api.py", "backend/api/other.py", "."])
    assert got == frozenset({"backend/api/model_status_api.py"})
