"""Slice 5 graduation pins — First-Class Skill System arc."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest


# ===========================================================================
# Authority invariant — arc modules import no gate/execution
# ===========================================================================


_ARC_MODULES = [
    "backend/core/ouroboros/governance/skill_manifest.py",
    "backend/core/ouroboros/governance/skill_catalog.py",
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
        f"{rel_path} imports forbidden modules: {violations}"
    )


# ===========================================================================
# Schema versions pinned
# ===========================================================================


def test_schema_versions_stable():
    from backend.core.ouroboros.governance.skill_manifest import (
        SKILL_MANIFEST_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.governance.skill_catalog import (
        SKILL_CATALOG_SCHEMA_VERSION,
    )
    assert SKILL_MANIFEST_SCHEMA_VERSION == "skill_manifest.v1"
    assert SKILL_CATALOG_SCHEMA_VERSION == "skill_catalog.v1"


# ===========================================================================
# §1: model source cannot register
# ===========================================================================


def test_model_source_cannot_register():
    from backend.core.ouroboros.governance.skill_catalog import (
        SkillAuthorityError, SkillCatalog,
    )
    from backend.core.ouroboros.governance.skill_manifest import (
        SkillManifest,
    )

    class FakeSource(str):
        pass

    cat = SkillCatalog()
    m = SkillManifest.from_mapping({
        "name": "x",
        "description": "d", "trigger": "t",
        "entrypoint": "mod.x:f",
    })
    with pytest.raises(SkillAuthorityError):
        cat.register(m, source=FakeSource("model"))  # type: ignore[arg-type]


# ===========================================================================
# Permissions allowlist is narrow
# ===========================================================================


def test_permissions_allowlist_is_narrow():
    from backend.core.ouroboros.governance.skill_manifest import (
        SkillManifestError, SkillManifest,
    )

    # Known-safe permissions accepted
    for perm in [
        "read_only", "filesystem_read", "filesystem_write",
        "network", "subprocess", "env_read",
    ]:
        SkillManifest.from_mapping({
            "name": "x",
            "description": "d", "trigger": "t",
            "entrypoint": "mod.x:f",
            "permissions": [perm],
        })

    # Anything else refused
    with pytest.raises(SkillManifestError):
        SkillManifest.from_mapping({
            "name": "x",
            "description": "d", "trigger": "t",
            "entrypoint": "mod.x:f",
            "permissions": ["root_access"],
        })
