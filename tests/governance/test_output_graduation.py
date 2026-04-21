"""Slice 5 graduation pins — Rich Formatted Output Control arc."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest


# ===========================================================================
# Authority invariant
# ===========================================================================


_ARC_MODULES = [
    "backend/core/ouroboros/governance/output_contract.py",
    "backend/core/ouroboros/governance/output_validator.py",
]

_FORBIDDEN = (
    "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
    "semantic_guardian", "tool_executor", "candidate_generator",
    "change_engine",
)


@pytest.mark.parametrize("rel_path", _ARC_MODULES)
def test_arc_module_has_no_authority_imports(rel_path: str):
    src = Path(rel_path).read_text()
    violations: List[str] = []
    for mod in _FORBIDDEN:
        if re.search(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(mod)}",
            src, re.MULTILINE,
        ):
            violations.append(mod)
    assert violations == [], (
        f"{rel_path} imports forbidden: {violations}"
    )


# ===========================================================================
# Schema versions pinned
# ===========================================================================


def test_schema_versions_stable():
    from backend.core.ouroboros.governance.output_contract import (
        OUTPUT_CONTRACT_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.governance.output_validator import (
        OUTPUT_VALIDATOR_SCHEMA_VERSION,
    )
    assert OUTPUT_CONTRACT_SCHEMA_VERSION == "output_contract.v1"
    assert OUTPUT_VALIDATOR_SCHEMA_VERSION == "output_validator.v1"


# ===========================================================================
# Determinism: same raw + same contract → same result
# ===========================================================================


def test_validation_deterministic():
    from backend.core.ouroboros.governance.output_contract import (
        OutputContract,
    )
    from backend.core.ouroboros.governance.output_validator import (
        OutputValidator,
    )
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {
            "fields": {"ok": {"type": "boolean", "required": True}},
        },
    })
    raw = '{"ok": true}'
    r1 = OutputValidator().validate(c, raw)
    r2 = OutputValidator().validate(c, raw)
    assert r1.ok == r2.ok
    assert [i.code for i in r1.issues] == [i.code for i in r2.issues]


# ===========================================================================
# Repair prompt is deterministic
# ===========================================================================


def test_repair_prompt_stable_wording():
    from backend.core.ouroboros.governance.output_contract import (
        OutputContract,
    )
    from backend.core.ouroboros.governance.output_validator import (
        OutputValidator, build_repair_prompt,
    )
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {"fields": {"ok": {"type": "boolean", "required": True}}},
    })
    r = OutputValidator().validate(c, '{}')
    p1 = build_repair_prompt(
        contract=c, previous_raw="{}", result=r, attempt=1,
    )
    p2 = build_repair_prompt(
        contract=c, previous_raw="{}", result=r, attempt=1,
    )
    assert p1.text == p2.text
