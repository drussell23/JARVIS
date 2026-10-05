"""Error-named contracts for first-party types (bt-2026-10-04-215048).

The 30B wrote ``ConversationTurn.from_dict`` on three attempts with the full
signature anchor in every prompt. These pin the targeted correction that was
missing: the contract of the ONE type the error names (first-party now, not
only installed packages), dataclass fields + constructor, and the facts the
run PROVED -- delivered through one section to all three repair prompts.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import library_contract as lc
from backend.core.ouroboros.governance.episodic_memory import EpisodicFailureMemory
from backend.core.ouroboros.governance.interactive_repair import (
    ExtractedError,
    InteractiveRepairLoop,
)

_SUBJECT = textwrap.dedent('''
    from dataclasses import dataclass, field
    from typing import Any, Dict, Optional

    @dataclass
    class Entry:
        key: str
        value: int = 0
        def to_dict(self) -> Dict[str, Any]:
            return {"key": self.key}
        @classmethod
        def from_dict(cls, data: Dict[str, Any]) -> "Entry":
            return cls(**data)

    @dataclass
    class Turn:
        turn_id: str
        role: str
        meta: Dict[str, Any] = field(default_factory=dict)
        def to_dict(self) -> Dict[str, Any]:
            return {"turn_id": self.turn_id}

    class Manager:
        def __init__(self, store: Optional[object] = None, size: int = 100):
            self.store = store

    def helper(x: int) -> int:
        return x
''')

_DESCRIPTION = "Write tests for pkg/mod.py"
_TARGETS = ["tests/test_mod.py"]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text(_SUBJECT)
    (tmp_path / "tests").mkdir()
    return tmp_path


def _section(repo: Path, error: str) -> str:
    return lc.error_contract_section(error, _TARGETS, _DESCRIPTION, repo)


def test_missing_member_names_the_class_that_really_has_it(repo):
    s = _section(repo, "AttributeError: type object 'Turn' has no attribute 'from_dict'")
    assert "# PROVEN: `Turn.from_dict` does NOT exist" in s
    assert "`from_dict` is defined on `Entry` (pkg.mod) -- a DIFFERENT class" in s
    assert s.startswith(lc.CONTRACT_SECTION_HEADER)


def test_dataclass_contract_shows_fields_and_how_to_construct_it(repo):
    s = _section(repo, "AttributeError: 'Turn' object has no attribute 'from_dict'")
    assert "class Turn:   # pkg.mod" in s
    assert "# fields: turn_id: str; role: str; meta: Dict[str, Any] = field(default_factory=dict)" in s
    assert "# construct: Turn(turn_id=..., role=..., meta=...)" in s
    assert "def to_dict(self) -> Dict[str, Any]" in s
    assert "def from_dict" not in s.split("class Turn:")[1]  # never listed on Turn


def test_unexpected_keyword_gets_the_real_signature(repo):
    s = _section(repo, "TypeError: Manager.__init__() got an unexpected keyword argument 'max_entries'")
    assert "`Manager.__init__()` accepts no `max_entries` argument" in s
    assert "def __init__(self, store: Optional[object] = None, size: int = 100)" in s


def test_import_and_name_errors_point_at_the_defining_module(repo):
    s = _section(repo, "ImportError: cannot import name 'Turn' from 'pkg.other'\n"
                       "NameError: name 'Entry' is not defined")
    assert "`Turn` is not in `pkg.other`; it is defined in `pkg.mod` -- `from pkg.mod import Turn`" in s
    assert "`Entry` is used without being imported: `from pkg.mod import Entry`" in s


def test_module_attribute_lists_the_real_public_names(repo):
    s = _section(repo, "AttributeError: module 'pkg.mod' has no attribute 'make_turn'")
    assert "module `pkg.mod` has no `make_turn`. Its public names are: Entry, Turn, Manager, helper." in s


def test_no_claims_about_types_this_repo_does_not_define(repo):
    s = _section(repo, "AttributeError: 'str' object has no attribute 'exists'\nAssertionError: assert 1 == 2")
    assert s == ""


def test_budget_keeps_whole_blocks_and_facts_first(repo, monkeypatch):
    monkeypatch.setenv("JARVIS_ERROR_CONTRACT_MAX_CHARS", "400")
    s = _section(repo, "AttributeError: type object 'Turn' has no attribute 'from_dict'")
    assert "# PROVEN:" in s and "class Turn:" not in s  # the class block did not fit whole


# ── one section, three consumers ────────────────────────────────────────


def test_validate_retry_feedback_carries_the_first_party_contract(repo):
    ctx = SimpleNamespace(op_id="op-1", target_files=tuple(_TARGETS), description=_DESCRIPTION)
    memory = EpisodicFailureMemory.for_op(ctx, repo)
    memory.record(file_path="tests/test_mod.py", attempt=1, failure_class="test",
                  error_summary="test_turn failed",
                  specific_errors=["AttributeError: type object 'Turn' has no attribute 'from_dict'"])
    memory.record(file_path="tests/test_mod.py", attempt=2, failure_class="test",
                  error_summary="test_turn failed again",
                  specific_errors=["AttributeError: type object 'Turn' has no attribute 'from_dict'"])
    text = memory.format_for_prompt()
    assert text.count(lc.CONTRACT_SECTION_HEADER) == 1
    assert text.count("class Turn:") == 1, "one contract per distinct type across attempts"
    assert "`Turn.from_dict` does NOT exist" in text


async def test_micro_fix_prompt_carries_the_contract(repo):
    loop = InteractiveRepairLoop(provider=None, project_root=repo)
    err = ExtractedError(
        error_type="AttributeError", message="type object 'Turn' has no attribute 'from_dict'",
        file_path="tests/test_mod.py", line_number=3,
        traceback_excerpt="E   AttributeError: type object 'Turn' has no attribute 'from_dict'",
        full_output="",
    )
    content = "from pkg.mod import Turn\n\ndef test_t():\n    Turn.from_dict({})\n"
    contract = await loop._api_contract("tests/test_mod.py", content, err)
    prompt = loop._build_micro_prompt("tests/test_mod.py", content, err, api_contract=contract)
    assert "`Turn.from_dict` does NOT exist" in prompt
    assert prompt.index("Traceback:") < prompt.index(lc.CONTRACT_SECTION_HEADER) < prompt.index("Fix ONLY")


def test_l2_repair_context_and_renderer_carry_the_contract():
    root = Path(__file__).resolve().parents[2]
    gov = root / "backend/core/ouroboros/governance"
    engine = (gov / "repair_engine.py").read_text(encoding="utf-8")
    providers = (gov / "providers.py").read_text(encoding="utf-8")
    assert "api_contract=_api_contract" in engine and "error_contract_section" in engine
    assert 'getattr(_rc, "api_contract", "")' in providers
    # Rendered beside the trace, before the subject bodies.
    assert providers.index('getattr(_rc, "api_contract"') < providers.index('getattr(_rc, "subject_source"')


def test_third_party_resolution_still_comes_first(monkeypatch, repo):
    calls = []
    monkeypatch.setattr(lc, "contract_for_type", lambda name, src: calls.append(name) or f"class {name}: # lib")
    blocks = lc.error_contract_blocks(
        "AttributeError: type object 'Turn' has no attribute 'x'",
        anchor_sources=[("pkg/mod.py", repo / "pkg" / "mod.py")],
    )
    assert calls == ["Turn"] and "class Turn: # lib" in blocks
