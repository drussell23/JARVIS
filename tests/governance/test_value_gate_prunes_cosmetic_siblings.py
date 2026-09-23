"""A cosmetic sibling cannot win VALIDATE over a substantive one.

bt-2026-09-23-180828: op-01a0cf75-0df6 drew one substantive and two cosmetic
siblings. The value gate judged the POOL (not all cosmetic -> proceed), a
cosmetic sibling passed first, and 474341904e landed a quote-style +
docstring-dedent change that is AST-identical to its base.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from backend.core.ouroboros.governance.op_context import GenerationResult
from backend.core.ouroboros.governance.orchestrator import Orchestrator

_REPO = Path(__file__).resolve().parents[2]

BASE = 'import json\n\ndef f(p):\n    """Doc."""\n    return json.dumps(p, separators=(",", ":"))\n'
COSMETIC = "import json\n\ndef f(p):\n    \"\"\"Doc.\"\"\"\n    return json.dumps(p, separators=(',', ':'))\n"
SUBSTANTIVE = BASE.replace("return json", "if p is None:\n        return None\n    return json")


def _orch(root: Path) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    o._config = SimpleNamespace(execution_root=root)
    return o


def _gen(*bodies):
    return GenerationResult(
        candidates=tuple({"file_path": "mod.py", "full_content": b} for b in bodies),
        provider_name="t", generation_duration_s=0.0,
    )


async def test_cosmetic_siblings_are_dropped_from_a_mixed_pool(tmp_path):
    (tmp_path / "mod.py").write_text(BASE)
    ctx = SimpleNamespace(op_id="op-x")
    out = await _orch(tmp_path)._prune_cosmetic_siblings(ctx, _gen(COSMETIC, SUBSTANTIVE, COSMETIC))
    assert [c["full_content"] for c in out.candidates] == [SUBSTANTIVE]


async def test_an_all_cosmetic_pool_is_left_to_the_terminal(tmp_path):
    (tmp_path / "mod.py").write_text(BASE)
    gen = _gen(COSMETIC, COSMETIC)
    assert await _orch(tmp_path)._prune_cosmetic_siblings(SimpleNamespace(op_id="x"), gen) is gen


async def test_a_pool_with_nothing_cosmetic_is_untouched(tmp_path):
    (tmp_path / "mod.py").write_text(BASE)
    gen = _gen(SUBSTANTIVE)
    assert await _orch(tmp_path)._prune_cosmetic_siblings(SimpleNamespace(op_id="x"), gen) is gen


async def test_the_switch_leaves_the_pool_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CANDIDATE_VALUE_GATE_ENABLED", "false")
    (tmp_path / "mod.py").write_text(BASE)
    gen = _gen(COSMETIC, SUBSTANTIVE)
    assert await _orch(tmp_path)._prune_cosmetic_siblings(SimpleNamespace(op_id="x"), gen) is gen


def _calls(path: str, func: str, name: str) -> bool:
    tree = ast.parse((_REPO / path).read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == func:
            return any(isinstance(n, ast.Attribute) and n.attr == name for n in ast.walk(fn))
    return False


def test_both_routes_prune_after_the_terminal_gate():
    """The live route is the dispatcher; the inline seam is the legacy route.
    A gate wired on one only is wired and inert on the other."""
    assert _calls("backend/core/ouroboros/governance/phase_dispatcher.py",
                  "dispatch_pipeline", "_prune_cosmetic_siblings")
    assert _calls("backend/core/ouroboros/governance/orchestrator.py",
                  "_run_pipeline", "_prune_cosmetic_siblings")
