"""Slice 224 — Token-Economic Accounting Fabric.

(1) accounting_ledger.py: read-only rollups over the Aegis spend WAL
    (.jarvis/aegis/spend.jsonl*) answering 'where did the money go' by
    day x provider x route. Provider attribution is HONEST-heuristic: the
    spend WAL doesn't carry an explicit provider field, so we attribute by
    op-id prefix (dw-* = doubleword) + route topology (immediate=claude-
    direct), and label it 'inferred' in the output.
(2) The P2a call-site wire: generate() now passes stable_prefix_out into
    _assemble_codegen_prompt -> _build_lean_codegen_prompt, and folds the
    returned stable blocks into the CACHED system prefix (the
    'on_without_sink_falls_back' gap closed).
"""
from __future__ import annotations

import json
from pathlib import Path

from backend.core.ouroboros.governance.accounting_ledger import (
    rollup_spend, format_spend_report,
)

_GOV = Path(__file__).resolve().parents[2] / "backend" / "core" \
    / "ouroboros" / "governance"


def _write_ledger(tmp_path, rows):
    d = tmp_path / "aegis"; d.mkdir(parents=True, exist_ok=True)
    p = d / "spend.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return tmp_path


def _rec(op, route, cost, ts):
    return {"kind": "reconcile", "op_id": op, "route": route,
            "actual_cost_usd": cost, "ts": ts}


def test_rollup_by_day_provider_route(tmp_path):
    base = 1781290000  # 2026-06-12 UTC
    root = _write_ledger(tmp_path, [
        _rec("op-aaa", "immediate", 0.03, base),
        _rec("op-bbb", "standard", 0.01, base),
        _rec("dw-123", "standard", 0.0005, base),
        _rec("op-ccc", "immediate", 0.02, base + 86400),  # next day
        {"kind": "admit", "op_id": "op-zzz", "route": "standard",
         "estimated_cost_usd": 0.05, "ts": base},  # admits NOT counted
    ])
    r = rollup_spend(jarvis_dir=root)
    assert abs(r.total_usd - 0.0605) < 1e-9
    assert r.by_provider["claude(inferred)"] > r.by_provider["doubleword(inferred)"]
    assert r.by_route["immediate"] == 0.05
    assert len(r.by_day) == 2
    assert r.calls == 4


def test_rollup_includes_backups_and_never_raises(tmp_path):
    root = _write_ledger(tmp_path, [_rec("op-a", "standard", 0.01, 1781290000)])
    bak = root / "aegis" / "spend.jsonl.bak-pre-bt-1"
    bak.write_text(json.dumps(_rec("op-b", "standard", 0.02, 1781290000)) + "\n"
                   + "NOT JSON\n")
    r = rollup_spend(jarvis_dir=root)
    assert abs(r.total_usd - 0.03) < 1e-9  # backups counted, garbage skipped


def test_rollup_missing_dir_safe(tmp_path):
    r = rollup_spend(jarvis_dir=tmp_path / "nope")
    assert r.total_usd == 0.0 and r.calls == 0


def test_report_renders_all_dimensions(tmp_path):
    root = _write_ledger(tmp_path, [_rec("op-a", "immediate", 0.03, 1781290000)])
    out = format_spend_report(rollup_spend(jarvis_dir=root))
    assert "TOTAL" in out and "immediate" in out and "claude(inferred)" in out
    assert "inferred" in out  # the honesty label


def test_p2a_callsite_wired():
    """The 'on_without_sink_falls_back' gap is closed: generate's assembly
    passes a stable_prefix sink and folds it into the cached system base."""
    src = (_GOV / "providers.py").read_text(encoding="utf-8")
    assert "stable_prefix_out=_p2a_stable_prefix" in src
    assert "_p2a_stable_prefix" in src.split("def _assemble_codegen_prompt")[0]
    # fold into the CACHED system blocks (at the CALL-site, not the def)
    call_idx = src.index("self._build_cached_system_blocks(")
    assert "_p2a_sys_base" in src[call_idx - 600:call_idx + 200]
