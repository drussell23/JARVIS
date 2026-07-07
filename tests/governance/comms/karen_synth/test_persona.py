from __future__ import annotations
from backend.core.ouroboros.governance.comms.karen_synth.ledger_view import LedgerView
from backend.core.ouroboros.governance.comms.karen_synth.persona import build_prompt

def test_system_prompt_encodes_persona_and_safety_rules():
    sys, _ = build_prompt(LedgerView(phase="generate"))
    low = sys.lower()
    assert "australian" in low
    assert "concise" in low or "brief" in low
    # mandate #4 hard rules present:
    assert "never" in low and ("code" in low and ("stack" in low or "traceback" in low))
    assert "two sentences" in low or "2 sentences" in low or "one or two" in low

def test_user_prompt_carries_ledger_context_no_code():
    sys, user = build_prompt(LedgerView.from_payload(
        "postmortem", {"root_cause": "boom ```code```", "target_files": ["a/x.py"]}))
    assert "x.py" in user
    assert "```" not in user and "```" not in sys

def test_persona_ctx_injected_when_present():
    sys, _ = build_prompt(LedgerView(phase="intent"), {"user_name": "Derek", "time_of_day": "evening"})
    assert "Derek" in sys and "evening" in sys
