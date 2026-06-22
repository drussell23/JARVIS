from __future__ import annotations
import pytest
from backend.core.ouroboros.governance import epistemic_shedder as es

SRC = '''
"""Module docstring that is fairly long and counts as fluff padding padding."""
import os
class Big:
    """Class doc."""
    def build(self, x):
        """Build doc."""
        # a comment
        total = 0
        for i in range(1000):
            total += i * i * i  # heavy body line padding padding padding
        return total
    def small(self):
        return 1
'''

def test_tier1_strips_docstrings_still_parseable():
    out, tier = es.shed_to_fit(SRC, target_chars=len(SRC) - 40)
    import ast; ast.parse(out)                 # still valid
    assert "Module docstring" not in out or tier in ("tier2", "tier3")

def test_tier2_omits_bodies_keeps_signatures():
    out, tier = es.shed_to_fit(SRC, target_chars=120)   # force deep shed
    assert "[SOVEREIGN YIELD: Implementation Omitted]" in out or tier == "tier3"
    assert "def build" in out                  # signature kept

def test_tier3_truncates_to_fit():
    out, tier = es.shed_to_fit(SRC, target_chars=40)
    assert len(out) <= 60                       # truncated near target

def test_parse_error_falls_to_truncation():
    bad = "def (:\n  oops" * 50
    out, tier = es.shed_to_fit(bad, target_chars=30)
    assert len(out) <= 50 and tier == "tier3"

def test_never_execs(monkeypatch):
    import builtins
    monkeypatch.setattr(builtins, "exec", lambda *a, **k: (_ for _ in ()).throw(AssertionError("exec")))
    es.shed_to_fit(SRC, target_chars=50)

def test_already_fits_returns_none_tier():
    out, tier = es.shed_to_fit("x = 1\n", target_chars=10_000)
    assert tier == "none"
