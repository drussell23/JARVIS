"""The pre-APPLY structural gate is in-memory, polymorphic, and reachable.

Both APPLY engines held a private copy of the same gate: make a temp
directory, write the candidate into it, read it back, ``ast.parse`` it. Three
synchronous filesystem operations, inside an ``async def``, to parse a string
that was already in memory -- and Python-only:

  * ``ChangeEngine`` guarded the call with a hardcoded extension set that
    *included* ``.c/.cpp/.h``. ``ast.parse`` rejects every C file ever
    written, so a C-family change could not pass VALIDATE, and one that
    reached disk by another route was rolled back by post-apply VERIFY.
  * ``MultiFileChangeEngine`` had no extension guard at all, so a ``.json``
    or ``.yaml`` member was failed on a grammar it does not have -- and one
    failed member fails the whole atomic set.

These tests pin the behaviour, not the implementation: they drive real
``execute`` calls and assert on what lands on disk.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.change_engine import (
    ChangeEngine,
    ChangeRequest,
    ChangePhase,
)
from backend.core.ouroboros.governance.ledger import OperationLedger
from backend.core.ouroboros.governance.multi_file_engine import (
    MultiFileChangeEngine,
    MultiFileChangeRequest,
)
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
)

VALID_C = """#include <stdio.h>
/* a block { comment holding an unmatched brace */
int main(void) {
    char *s = "a } string with braces {";
    char q = '}';
    printf("ok\\n");  // a trailing } in a comment
    return 0;
}
"""
TRUNCATED_C = VALID_C.replace("    return 0;\n}\n", "    return 0;\n")


def _profile(*targets: Path) -> OperationProfile:
    return OperationProfile(
        files_affected=list(targets),
        change_type=ChangeType.MODIFY,
        blast_radius=len(targets),
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=1.0,
    )


def _engine(tmp_path: Path) -> ChangeEngine:
    return ChangeEngine(
        project_root=tmp_path,
        ledger=OperationLedger(storage_dir=tmp_path / "ledger"),
    )


async def _run(tmp_path: Path, name: str, content: str, op_id: str):
    target = tmp_path / name
    target.write_text("/* seed */\n" if name.endswith((".c", ".h")) else "seed\n")
    result = await _engine(tmp_path).execute(
        ChangeRequest(
            goal="precompile gate",
            target_file=target,
            proposed_content=content,
            profile=_profile(target),
            op_id=op_id,
        )
    )
    return result, target


# ---------------------------------------------------------------------------
# The defect: C-family changes were categorically impossible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_c_file_survives_validate_and_lands(tmp_path):
    result, target = await _run(tmp_path, "main.c", VALID_C, "op-c-good")
    assert result.success is True, (
        f"valid C rejected at {result.phase_reached}: it was routed to a "
        "Python parser"
    )
    # The engine prepends a language-correct attribution header, so the
    # candidate is contained rather than equal to the file.
    landed = target.read_text()
    assert "int main(void) {" in landed
    assert landed.startswith("// ["), "C file got a non-C comment header"


@pytest.mark.asyncio
async def test_truncated_c_file_is_rejected_before_apply(tmp_path):
    result, target = await _run(tmp_path, "main.c", TRUNCATED_C, "op-c-bad")
    assert result.success is False
    assert result.phase_reached == ChangePhase.VALIDATE
    assert target.read_text() == "/* seed */\n", "rejected candidate reached disk"


@pytest.mark.asyncio
async def test_c_header_is_routed_like_c(tmp_path):
    result, _ = await _run(tmp_path, "api.h", "#define X 1\nint f(void);\n", "op-h")
    assert result.success is True


# ---------------------------------------------------------------------------
# Python is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_python_still_lands(tmp_path):
    src = "def f():\n    return 1\n"
    result, target = await _run(tmp_path, "m.py", src, "op-py-good")
    assert result.success is True
    assert src in target.read_text()


@pytest.mark.asyncio
async def test_broken_python_still_rejected_before_apply(tmp_path):
    result, target = await _run(tmp_path, "m.py", "def f(:\n", "op-py-bad")
    assert result.success is False
    assert result.phase_reached == ChangePhase.VALIDATE
    assert target.read_text() == "seed\n"


@pytest.mark.asyncio
async def test_ledger_records_the_fracture_detail(tmp_path):
    """A bare False told the operator nothing. The detail names the line."""
    engine = _engine(tmp_path)
    target = tmp_path / "m.py"
    target.write_text("seed\n")
    await engine.execute(
        ChangeRequest(
            goal="fracture detail",
            target_file=target,
            proposed_content="def f(:\n",
            profile=_profile(target),
            op_id="op-detail",
        )
    )
    entries = await engine._ledger.get_history("op-detail")
    fractures = [
        e.data.get("fracture") for e in entries if e.data.get("fracture")
    ]
    assert fractures, "the gate recorded no reason for the rejection"
    assert "SyntaxError" in fractures[0]


# ---------------------------------------------------------------------------
# It is EPHEMERAL: no temp directory, no disk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_touches_no_temporary_directory(tmp_path, monkeypatch):
    """The gate parsed a string it already held by writing it to disk first.
    Detonate the tempfile machinery: a gate that still reaches for it fails
    loudly here rather than silently costing three syscalls per candidate."""
    def _boom(*_a, **_k):  # noqa: ANN002, ANN003
        raise AssertionError("gate reached for a temporary directory")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", _boom)
    monkeypatch.setattr(tempfile, "mkdtemp", _boom)

    result, _ = await _run(tmp_path, "m.py", "def f():\n    return 1\n", "op-nodisk")
    assert result.success is True


# ---------------------------------------------------------------------------
# Multi-file: a non-Python member no longer fails the whole atomic set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_file_accepts_a_json_member(tmp_path):
    py, cfg = tmp_path / "m.py", tmp_path / "cfg.json"
    py.write_text("x = 1\n")
    cfg.write_text("{}\n")
    files = {py: "x = 2\n", cfg: json.dumps({"a": 1}, indent=2) + "\n"}

    result = await MultiFileChangeEngine(
        project_root=tmp_path,
        ledger=OperationLedger(storage_dir=tmp_path / "ledger"),
    ).execute(
        MultiFileChangeRequest(
            goal="json member", files=files, profile=_profile(py, cfg),
        )
    )
    assert result.success is True, (
        f"stopped at {result.phase_reached}: the JSON member was parsed as "
        "Python"
    )
    assert json.loads(cfg.read_text()) == {"a": 1}


@pytest.mark.asyncio
async def test_multi_file_still_rejects_a_broken_json_member(tmp_path):
    py, cfg = tmp_path / "m.py", tmp_path / "cfg.json"
    py.write_text("x = 1\n")
    cfg.write_text("{}\n")

    result = await MultiFileChangeEngine(
        project_root=tmp_path,
        ledger=OperationLedger(storage_dir=tmp_path / "ledger"),
    ).execute(
        MultiFileChangeRequest(
            goal="broken json member",
            files={py: "x = 2\n", cfg: '{"a": 1,}\n'},
            profile=_profile(py, cfg),
        )
    )
    assert result.success is False
    assert py.read_text() == "x = 1\n", "the atomic set was not held together"
