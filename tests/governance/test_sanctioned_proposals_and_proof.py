"""Sanctioned model proposals, memory hygiene, and the proof-of-work trail
(2026-10-09).

* Model-proposed work (Epistemic Planner, DreamEngine via the conception
  bridge, IntentDiscovery) is sanctioned the way signed goals are: filed into
  the proposal inbox, ranked by Goal Discovery BELOW evidence under every cage
  refusal, signed by the one signer. Off -> legacy human-ack route.
* PostmortemRecall matched every op at 0.50-0.59 cosine: related now means
  standing out from the query's own similarity distribution.
* Op closures are no longer copied into the ConversationBridge (dialogue).
* Proof of work: per-session record, hash-chained, with a cockpit verb and
  one evidence channel every capability reports through.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from backend.core.ouroboros.governance.autonomy import proposal_inbox as pi


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PROPOSAL_INBOX_PATH", str(tmp_path / "inbox.jsonl"))
    return tmp_path


def _p(target="backend/pkg/mod.py", desc="add caching to lookup", score=0.8, producer="intent_discovery"):
    return pi.Proposal(producer=producer, target_file=target, description=desc,
                       rationale="hot path", score=score, evidence={"ev": 0.7})


# ---------------------------------------------------------------- the inbox


def test_inbox_files_dedupes_and_reads_back(inbox):
    assert pi.submit(_p(), inbox) == pi.INBOXED
    assert pi.submit(_p(producer="epistemic_planner"), inbox) == pi.DUPLICATE  # same task, any producer
    assert pi.submit(_p(desc="a different task"), inbox) == pi.INBOXED
    assert {p.description for p in pi.pending(inbox)} == {"add caching to lookup", "a different task"}


@pytest.mark.parametrize("proposal,reason", [
    (_p(target="backend/core/ouroboros/governance/orchestrator.py"), "refused_governance"),
    (_p(target=""), "refused_unscoped"),
    (_p(desc="   "), "refused_empty"),
])
def test_inbox_refuses_at_the_door(inbox, proposal, reason):
    assert pi.submit(proposal, inbox) == reason
    assert pi.pending(inbox) == []


def test_proposals_expire_and_are_bounded(inbox, monkeypatch):
    monkeypatch.setenv("JARVIS_PROPOSAL_TTL_S", "100")
    monkeypatch.setenv("JARVIS_PROPOSAL_INBOX_MAX", "2")
    pi.submit(_p(desc="old"), inbox, now=1000.0)
    pi.submit(_p(desc="mid"), inbox, now=1050.0)
    pi.submit(_p(desc="new"), inbox, now=1090.0)
    assert [p.description for p in pi.pending(inbox, now=1095.0)] == ["new", "mid"]
    assert [p.description for p in pi.pending(inbox, now=1160.0)] == ["new"]


def test_route_proposal_sanctions_or_falls_back(inbox, monkeypatch):
    class Router:
        def __init__(self):
            self.envelopes = []

        async def ingest(self, env):
            self.envelopes.append(env)
            return "pending_ack"

    router = Router()
    monkeypatch.setenv("JARVIS_AUTONOMOUS_PROPOSAL_SANCTION_ENABLED", "true")
    out = asyncio.run(pi.route_proposal(router=router, repo_root=inbox, proposal=_p(),
                                        envelope_factory=lambda: "legacy"))
    assert out == pi.INBOXED and router.envelopes == []
    # No known repository -> no guessed inbox: the legacy route.
    out = asyncio.run(pi.route_proposal(router=router, repo_root=None, proposal=_p(desc="x"),
                                        envelope_factory=lambda: "legacy"))
    assert out == "pending_ack" and router.envelopes == ["legacy"]
    monkeypatch.setenv("JARVIS_AUTONOMOUS_PROPOSAL_SANCTION_ENABLED", "false")
    assert asyncio.run(pi.route_proposal(router=router, repo_root=inbox, proposal=_p(desc="y"),
                                         envelope_factory=lambda: "legacy2")) == "pending_ack"


def test_both_producers_route_through_the_one_seam():
    from backend.core.ouroboros.governance import conception_proposal_bridge as cpb
    from backend.core.ouroboros.governance.intake.sensors import intent_discovery_sensor as ids
    for mod in (cpb, ids):
        assert "route_proposal(" in inspect.getsource(mod)
    assert {"inboxed", "duplicate"} <= cpb._ROUTED_RESULTS


# ------------------------------------------------------------ goal discovery


def test_proposals_become_discovered_work_below_every_evidence_kind(inbox):
    from backend.core.ouroboros.governance.autonomy import goal_discovery as gd
    pi.submit(_p(score=1.0), inbox)
    works = gd._from_proposal_inbox(inbox)
    assert len(works) == 1
    w = works[0]
    assert w.kind == "model_proposal" and w.target_file == "backend/pkg/mod.py"
    assert w.weight < min(gd._KIND_WEIGHT.values())
    assert "Verify the premise" in w.describe() and "add caching" in w.describe()


def test_the_ceiling_can_never_reach_a_fact(monkeypatch):
    from backend.core.ouroboros.governance.autonomy import goal_discovery as gd
    monkeypatch.setenv("JARVIS_PROPOSAL_WEIGHT_CEILING", "5.0")
    assert gd._proposal_weight_ceiling() < min(gd._KIND_WEIGHT.values())


def test_distinct_proposals_for_one_file_are_distinct_goals(inbox):
    from backend.core.ouroboros.governance.autonomy import goal_discovery as gd
    pi.submit(_p(desc="task one"), inbox)
    pi.submit(_p(desc="task two"), inbox)
    ids = {w.goal_id for w in gd._from_proposal_inbox(inbox)}
    assert len(ids) == 2


def test_discover_puts_proposals_in_the_same_pool():
    from backend.core.ouroboros.governance.autonomy import goal_discovery as gd
    assert "pool = signed + reds + proposed" in inspect.getsource(gd.discover)


# ------------------------------------------------------------------ memory


def test_adaptive_cutoff_requires_standing_out():
    from backend.core.ouroboros.governance.postmortem_recall import adaptive_cutoff
    noise = [0.50, 0.52, 0.51, 0.53, 0.50, 0.52, 0.51, 0.55, 0.54, 0.50]
    cutoff = adaptive_cutoff(noise, 0.5)
    assert cutoff > max(noise[:-3])                    # ordinary noise no longer matches
    assert adaptive_cutoff(noise + [0.90], 0.5) < 0.90  # a real outlier still does
    assert adaptive_cutoff([0.6, 0.7], 0.5) == 0.5     # too few to judge: the floor


def test_op_closures_no_longer_enter_the_dialogue_bridge():
    from backend.core.ouroboros.governance import comm_protocol
    src = inspect.getsource(comm_protocol)
    assert 'source="postmortem"' not in src


@pytest.mark.parametrize("key", [
    "JARVIS_CONVERSATION_BRIDGE_ENABLED", "JARVIS_AUTONOMOUS_PROPOSAL_SANCTION_ENABLED",
    "JARVIS_EPISTEMIC_PLANNER_ENABLED", "JARVIS_CONCEPTION_BRIDGE_ENABLED",
])
def test_envelope_arms_the_sanctioned_producers_in_both_profiles(key):
    from backend.core.ouroboros.governance.production_envelope import build
    assert build("soak").values[key] == build("cockpit").values[key] == "true"


# ----------------------------------------------------------- proof of work


def _record(**over):
    rec = {"session_id": "bt-2026-10-09-154724",
           "capabilities": {"firing_counts": {"FIRING": 0}}, "landings": {"verified": [], "unpromoted": 0}}
    rec.update(over)
    return rec


@pytest.mark.parametrize("rec,expected", [
    (_record(landings={"verified": [{"sha": "a", "exists": True, "subject": "s"}]}), "productive"),
    (_record(landings={"verified": [], "unpromoted": 2}), "landing"),
    (_record(capabilities={"firing_counts": {"FIRING": 5}}), "active"),
    (_record(), "dark"),
    (_record(landings={"verified": [{"sha": "a", "exists": False, "subject": "s"}]}), "dark"),
])
def test_verdict_is_earned_by_evidence(rec, expected):
    from backend.core.ouroboros.governance.proof_of_work import verdict
    assert verdict(rec) == expected


def test_chain_detects_an_altered_record(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import proof_of_work as p
    monkeypatch.setenv("JARVIS_PROOF_CHAIN_PATH", str(tmp_path / "chain.jsonl"))
    for i in range(3):
        assert p.append_to_chain(tmp_path, _record(session_id=f"s{i}"))
    assert p.verify_chain(tmp_path) == (True, 3, None)
    lines = (tmp_path / "chain.jsonl").read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["record"]["landings"]["unpromoted"] = 99
    lines[1] = json.dumps(tampered)
    (tmp_path / "chain.jsonl").write_text("\n".join(lines) + "\n")
    assert p.verify_chain(tmp_path) == (False, 3, 1)


def test_a_removed_link_breaks_the_chain(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import proof_of_work as p
    monkeypatch.setenv("JARVIS_PROOF_CHAIN_PATH", str(tmp_path / "chain.jsonl"))
    for i in range(3):
        p.append_to_chain(tmp_path, _record(session_id=f"s{i}"))
    lines = (tmp_path / "chain.jsonl").read_text().splitlines()
    (tmp_path / "chain.jsonl").write_text(lines[0] + "\n" + lines[2] + "\n")
    intact, _n, broken = p.verify_chain(tmp_path)
    assert not intact and broken == 1


def test_emit_evidence_tallies_and_publishes(monkeypatch):
    from backend.core.ouroboros.governance import ide_observability_stream as stream
    from backend.core.ouroboros.governance import proof_of_work as p
    sent = []
    monkeypatch.setattr(stream, "publish_task_event", lambda et, op, payload: sent.append((et, payload)))
    p._EVIDENCE.clear()
    p.emit_evidence("test_prune", "landed_majority", op_id="op-1", detail="pruned 2/9")
    p.emit_evidence("test_prune", "landed_majority")
    assert p.evidence_tally() == {"test_prune": {"landed_majority": 2}}
    assert sent[0][0] == "capability_evidence" and sent[0][1]["capability"] == "test_prune"


def test_evidence_is_a_known_stream_event_with_a_breadcrumb():
    from backend.core.ouroboros.governance.event_breadcrumb_registry import build_default_registry
    from backend.core.ouroboros.governance.ide_observability_stream import _VALID_EVENT_TYPES
    assert "capability_evidence" in _VALID_EVENT_TYPES
    sev, text = build_default_registry().render(
        "capability_evidence", {"capability": "dream_engine", "outcome": "dreamed", "detail": "via local"})
    assert "dream_engine: dreamed" in text and sev >= 2


@pytest.mark.parametrize("module,call", [
    ("backend.core.ouroboros.governance.orchestrator", '"test_prune"'),
    ("backend.core.ouroboros.governance.adversarial_reviewer_hook", '"adversarial_reviewer"'),
    ("backend.core.ouroboros.governance.self_critique", '"self_critique"'),
    ("backend.core.ouroboros.consciousness.dream_engine", '"dream_engine"'),
    ("backend.core.ouroboros.consciousness.memory_engine", '"memory_engine"'),
    ("backend.core.ouroboros.governance.lesson_memory", '"lesson_memory"'),
    ("backend.core.ouroboros.governance.postmortem_recall", '"postmortem_recall"'),
    ("backend.core.ouroboros.governance.antivenom_observer", '"antivenom"'),
    ("backend.core.ouroboros.governance.review_coordinator", '"review_branch"'),
    ("backend.core.ouroboros.governance.intake.sensors.deep_analysis_sensor", '"deep_analysis"'),
    ("backend.core.ouroboros.governance.intake.sensors.intent_discovery_sensor", '"intent_discovery"'),
    ("backend.core.ouroboros.governance.epistemic_planner", '"epistemic_planner"'),
    ("backend.core.ouroboros.governance.autonomy.sentinel_loop", '"sentinel"'),
])
def test_each_capability_reports_through_the_one_channel(module, call):
    import importlib
    src = inspect.getsource(importlib.import_module(module))
    assert "emit_evidence(" in src and call in src


def test_proof_verb_verifies_the_chain(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import proof_of_work as p
    from backend.core.ouroboros.governance.proof_repl import dispatch_proof_command
    monkeypatch.setenv("JARVIS_PROOF_CHAIN_PATH", str(tmp_path / "chain.jsonl"))
    monkeypatch.setenv("JARVIS_PROJECT_ROOT", str(tmp_path))
    p.append_to_chain(tmp_path, _record())
    out = asyncio.run(dispatch_proof_command("/proof verify"))
    assert out.ok and "intact: 1" in out.text
    hist = asyncio.run(dispatch_proof_command("/proof history"))
    assert hist.ok and "DARK" in hist.text


def test_session_end_writes_the_proof():
    from backend.core.ouroboros.battle_test import harness
    assert "write_session_proof(" in inspect.getsource(harness)
