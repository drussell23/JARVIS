from __future__ import annotations
from backend.core.ouroboros.governance.comms.karen_synth.ledger_view import (
    LedgerView, strip_code, first_line,
)

def test_strip_code_removes_fences_and_traceback():
    raw = "It failed.\n```\nTraceback (most recent call last):\n  File x\n```\nmore"
    out = strip_code(raw)
    assert "Traceback" not in out and "```" not in out and "File x" not in out
    assert "It failed." in out

def test_first_line_only():
    assert first_line("line one\nline two\nline three") == "line one"

def test_from_payload_basenames_and_compresses_root_cause():
    v = LedgerView.from_payload("postmortem", {
        "root_cause": "AssertionError in test\n```\nassert 1==2\n```\nstack...",
        "target_files": ["backend/core/auth/login.py", "backend/util/x.py"],
        "risk_tier": "notify_apply",
    })
    assert v.files == ("login.py", "x.py")            # basenames
    assert "```" not in v.root_cause                  # code stripped
    assert "\n" not in v.root_cause                   # single line
    assert v.phase == "postmortem"

def test_to_context_line_has_no_code_or_newlines():
    v = LedgerView.from_payload("decision", {"outcome": "applied", "target_files": ["a/b.py"]})
    line = v.to_context_line()
    assert "```" not in line and "\n" not in line
    assert "b.py" in line and "applied" in line
