"""Priority F — Evidence collector extension regression spine.

Closes the verification loop end-to-end. Pre-F, postmortems carried
the right claims (Priority A working) but every claim evaluated to
INSUFFICIENT_EVIDENCE because the legacy ctx_evidence_collector
didn't know how to populate the three Priority A evidence shapes.

Pins:
  §1   Master flag default true (graduated; hot-revert path)
  §2   Master flag empty/whitespace reads as default true
  §3   Master flag false-class disables
  §4   EvidenceGatherer is frozen + hashable
  §5   Registry — idempotent + overwrite + alphabetical-stable
  §6   Three seed gatherers registered at module load
  §7   is_kind_registered returns True for seeds
  §8   reset_for_tests clears + re-seeds
  §9   dispatch — master-off returns empty
  §10  dispatch — None claim returns empty
  §11  dispatch — claim with no property returns empty
  §12  dispatch — unknown kind returns empty
  §13  dispatch — gatherer that raises swallowed; returns empty
  §14  dispatch — gatherer that returns non-mapping coerced to {}
  §15  file_parses_after_change — pre-stamped ctx pass-through
  §16  file_parses_after_change — self-gathers from disk when
       ctx.target_files set but target_files_post not stamped
  §17  file_parses_after_change — handles missing files (records
       with empty content)
  §18  file_parses_after_change — non-existent ctx.target_files
       returns empty mapping
  §19  test_set_hash_stable — pre+post stamped pass-through
  §20  test_set_hash_stable — pre missing returns empty (honest
       INSUFFICIENT — pre cannot self-gather post-APPLY)
  §21  test_set_hash_stable — pre stamped + post self-gathered
       via tests/**/*.py glob
  §22  no_new_credential_shapes — diff_text pre-stamped pass-through
  §23  no_new_credential_shapes — bytes diff decoded
  §24  no_new_credential_shapes — no diff → empty (honest
       INSUFFICIENT)
  §25  Integration — ctx_evidence_collector dispatches via registry
       FIRST for Priority A kinds
  §26  Integration — ctx_evidence_collector falls back to legacy
       hardcoded paths for test_passes / key_present
  §27  Integration — Priority A kind with empty registry result
       returns empty (does NOT mask with legacy false-positive)
  §28  Authority invariants — no orchestrator/policy/iron_gate imports
  §29  Public API surface
  §30  All gatherers NEVER raise on garbage ctx
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from backend.core.ouroboros.governance.verification.evidence_collectors import (
    EVIDENCE_COLLECTOR_SCHEMA_VERSION,
    EvidenceGatherer,
    dispatch_evidence_gather,
    evidence_collectors_enabled,
    is_kind_registered,
    list_evidence_gatherers,
    register_evidence_gatherer,
    reset_registry_for_tests,
    unregister_evidence_gatherer,
)


@pytest.fixture
def fresh_registry():
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


def _make_claim(kind: str, name: str = "test"):
    """Synthesize a minimal claim shape for dispatch tests. The
    dispatcher only reads claim.property.kind."""
    return SimpleNamespace(
        property=SimpleNamespace(kind=kind, name=name),
    )


# ===========================================================================
# §1-§3 — Master flag
# ===========================================================================


def test_master_flag_default_true(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_EVIDENCE_COLLECTORS_ENABLED", raising=False,
    )
    assert evidence_collectors_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  ", "\t"])
def test_master_flag_empty_reads_default_true(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_EVIDENCE_COLLECTORS_ENABLED", val)
    assert evidence_collectors_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_master_flag_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_EVIDENCE_COLLECTORS_ENABLED", val)
    assert evidence_collectors_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_master_flag_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_EVIDENCE_COLLECTORS_ENABLED", val)
    assert evidence_collectors_enabled() is False


# ===========================================================================
# §4 — Frozen schema
# ===========================================================================


def test_gatherer_is_frozen() -> None:
    async def fn(c, ctx):
        return {}
    g = EvidenceGatherer(
        kind="x", description="d", gather=fn,
    )
    with pytest.raises((AttributeError, FrozenInstanceError, TypeError)):
        g.kind = "y"  # type: ignore[misc]
    assert g.schema_version == EVIDENCE_COLLECTOR_SCHEMA_VERSION


# ===========================================================================
# §5 — Registry surface
# ===========================================================================


def test_register_idempotent_on_identical(fresh_registry) -> None:
    async def fn(c, ctx):
        return {}
    g = EvidenceGatherer(
        kind="custom", description="d", gather=fn,
    )
    register_evidence_gatherer(g)
    register_evidence_gatherer(g)
    custom = [
        x for x in list_evidence_gatherers()
        if x.kind == "custom"
    ]
    assert len(custom) == 1


def test_register_rejects_different_without_overwrite(
    fresh_registry,
) -> None:
    async def f1(c, ctx):
        return {"a": 1}

    async def f2(c, ctx):
        return {"b": 2}
    g1 = EvidenceGatherer(
        kind="custom", description="A", gather=f1,
    )
    g2 = EvidenceGatherer(
        kind="custom", description="B", gather=f2,
    )
    register_evidence_gatherer(g1)
    register_evidence_gatherer(g2)
    custom = [
        x for x in list_evidence_gatherers()
        if x.kind == "custom"
    ]
    assert len(custom) == 1
    assert custom[0].description == "A"


def test_register_overwrite_replaces(fresh_registry) -> None:
    async def f1(c, ctx):
        return {}

    async def f2(c, ctx):
        return {}
    g1 = EvidenceGatherer(
        kind="custom", description="A", gather=f1,
    )
    g2 = EvidenceGatherer(
        kind="custom", description="B", gather=f2,
    )
    register_evidence_gatherer(g1)
    register_evidence_gatherer(g2, overwrite=True)
    custom = [
        x for x in list_evidence_gatherers()
        if x.kind == "custom"
    ]
    assert len(custom) == 1
    assert custom[0].description == "B"


def test_unregister_returns_correct_status(fresh_registry) -> None:
    async def fn(c, ctx):
        return {}
    register_evidence_gatherer(
        EvidenceGatherer(
            kind="ephemeral", description="d", gather=fn,
        ),
    )
    assert unregister_evidence_gatherer("ephemeral") is True
    assert unregister_evidence_gatherer("ephemeral") is False
    assert unregister_evidence_gatherer("never") is False


def test_list_alphabetical_stable(fresh_registry) -> None:
    gs = list_evidence_gatherers()
    kinds = [g.kind for g in gs]
    assert kinds == sorted(kinds)


# ===========================================================================
# §6-§8 — Seed gatherers
# ===========================================================================


def test_three_seed_gatherers_registered(fresh_registry) -> None:
    kinds = sorted(g.kind for g in list_evidence_gatherers())
    assert kinds == [
        "file_parses_after_change",
        "no_new_credential_shapes",
        "test_set_hash_stable",
    ]


def test_is_kind_registered_for_seeds(fresh_registry) -> None:
    assert is_kind_registered("file_parses_after_change") is True
    assert is_kind_registered("no_new_credential_shapes") is True
    assert is_kind_registered("test_set_hash_stable") is True
    assert is_kind_registered("nonexistent") is False
    assert is_kind_registered("") is False


def test_reset_for_tests_clears_and_reseeds(fresh_registry) -> None:
    async def fn(c, ctx):
        return {}
    register_evidence_gatherer(
        EvidenceGatherer(
            kind="extra", description="d", gather=fn,
        ),
    )
    assert is_kind_registered("extra") is True
    reset_registry_for_tests()
    assert is_kind_registered("extra") is False
    # Seeds re-registered
    assert is_kind_registered("file_parses_after_change") is True


# ===========================================================================
# §9-§14 — Dispatcher contracts
# ===========================================================================


def test_dispatch_master_off_returns_empty(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_EVIDENCE_COLLECTORS_ENABLED", "false")
    claim = _make_claim("file_parses_after_change")
    ctx = SimpleNamespace(target_files=("a.py",))
    result = asyncio.run(dispatch_evidence_gather(claim, ctx))
    assert result == {}


def test_dispatch_none_claim_returns_empty() -> None:
    result = asyncio.run(
        dispatch_evidence_gather(None, SimpleNamespace()),
    )
    assert result == {}


def test_dispatch_claim_no_property_returns_empty() -> None:
    claim = SimpleNamespace(property=None)
    result = asyncio.run(
        dispatch_evidence_gather(claim, SimpleNamespace()),
    )
    assert result == {}


def test_dispatch_unknown_kind_returns_empty(fresh_registry) -> None:
    claim = _make_claim("unknown-not-registered")
    result = asyncio.run(
        dispatch_evidence_gather(claim, SimpleNamespace()),
    )
    assert result == {}


def test_dispatch_gatherer_raises_swallowed(fresh_registry) -> None:
    async def boom(c, ctx):
        raise RuntimeError("boom")
    register_evidence_gatherer(
        EvidenceGatherer(
            kind="bad", description="d", gather=boom,
        ),
    )
    claim = _make_claim("bad")
    result = asyncio.run(
        dispatch_evidence_gather(claim, SimpleNamespace()),
    )
    assert result == {}


def test_dispatch_non_mapping_result_coerced_to_empty(
    fresh_registry,
) -> None:
    async def returns_list(c, ctx):
        return [1, 2, 3]  # type: ignore[return-value]
    register_evidence_gatherer(
        EvidenceGatherer(
            kind="list_returner", description="d",
            gather=returns_list,
        ),
    )
    claim = _make_claim("list_returner")
    result = asyncio.run(
        dispatch_evidence_gather(claim, SimpleNamespace()),
    )
    assert result == {}


# ===========================================================================
# §15-§18 — file_parses_after_change gatherer
# ===========================================================================


def test_file_parses_pre_stamped_pass_through(fresh_registry) -> None:
    """When ctx.target_files_post is already stamped, the gatherer
    passes through without re-reading from disk."""
    pre_stamped = [
        {"path": "a.py", "content": "x = 1\n"},
        {"path": "b.py", "content": "def f(): pass\n"},
    ]
    ctx = SimpleNamespace(
        target_files_post=pre_stamped,
        target_files=("a.py", "b.py"),  # ignored when post stamped
    )
    claim = _make_claim("file_parses_after_change")
    result = asyncio.run(dispatch_evidence_gather(claim, ctx))
    assert "target_files_post" in result
    assert result["target_files_post"] == pre_stamped


def test_file_parses_self_gathers_from_disk(
    fresh_registry, tmp_path,
) -> None:
    """When target_files_post is NOT stamped but ctx.target_files is
    set, the gatherer reads each file from disk."""
    # Create real files on disk
    f1 = tmp_path / "a.py"
    f1.write_text("x = 1\n")
    f2 = tmp_path / "b.py"
    f2.write_text("def hello(): return 'world'\n")
    ctx = SimpleNamespace(
        target_files=(str(f1), str(f2)),
    )
    claim = _make_claim("file_parses_after_change")
    result = asyncio.run(dispatch_evidence_gather(claim, ctx))
    assert "target_files_post" in result
    files = result["target_files_post"]
    assert len(files) == 2
    paths = {f["path"] for f in files}
    assert str(f1) in paths
    assert str(f2) in paths
    # Content should be read from disk
    contents = {f["path"]: f["content"] for f in files}
    assert contents[str(f1)] == "x = 1\n"
    assert "hello" in contents[str(f2)]


def test_file_parses_handles_missing_files(
    fresh_registry, tmp_path,
) -> None:
    """Missing files get recorded with empty content (so the
    evaluator can detect their absence as a regression)."""
    nonexistent = tmp_path / "ghost.py"
    ctx = SimpleNamespace(
        target_files=(str(nonexistent),),
    )
    claim = _make_claim("file_parses_after_change")
    result = asyncio.run(dispatch_evidence_gather(claim, ctx))
    files = result["target_files_post"]
    assert len(files) == 1
    assert files[0]["path"] == str(nonexistent)
    assert files[0]["content"] == ""


def test_file_parses_no_targets_returns_empty(fresh_registry) -> None:
    """No target_files → no evidence to gather → INSUFFICIENT."""
    ctx = SimpleNamespace()  # no attrs at all
    claim = _make_claim("file_parses_after_change")
    result = asyncio.run(dispatch_evidence_gather(claim, ctx))
    assert result == {}


# ===========================================================================
# §19-§21 — test_set_hash_stable gatherer
# ===========================================================================


def test_test_set_pre_post_stamped_pass_through(fresh_registry) -> None:
    pre = ("tests/test_a.py", "tests/test_b.py")
    post = ("tests/test_a.py", "tests/test_b.py", "tests/test_c.py")
    ctx = SimpleNamespace(
        test_files_pre=pre, test_files_post=post,
    )
    claim = _make_claim("test_set_hash_stable")
    result = asyncio.run(dispatch_evidence_gather(claim, ctx))
    assert result["test_files_pre"] == list(pre)
    assert result["test_files_post"] == list(post)


def test_test_set_pre_missing_returns_empty(fresh_registry) -> None:
    """Without a PLAN-time pre-stamp, the claim cannot be evaluated
    (we cannot reconstruct the pre-state post-APPLY). Honest
    INSUFFICIENT_EVIDENCE."""
    ctx = SimpleNamespace(target_dir=".")  # no test_files_pre
    claim = _make_claim("test_set_hash_stable")
    result = asyncio.run(dispatch_evidence_gather(claim, ctx))
    assert result == {}


def test_test_set_pre_stamped_post_self_gathered(
    fresh_registry, tmp_path,
) -> None:
    """Pre is stamped at PLAN; post is self-gathered by globbing
    tests/**/*.py under ctx.target_dir."""
    # Create a fake project tree
    (tmp_path / "tests").mkdir()
    f1 = tmp_path / "tests" / "test_one.py"
    f2 = tmp_path / "tests" / "test_two.py"
    f1.write_text("def test_x(): pass\n")
    f2.write_text("def test_y(): pass\n")

    pre = ("tests/test_one.py",)
    ctx = SimpleNamespace(
        test_files_pre=pre,
        target_dir=str(tmp_path),
    )
    claim = _make_claim("test_set_hash_stable")
    result = asyncio.run(dispatch_evidence_gather(claim, ctx))
    assert result["test_files_pre"] == list(pre)
    # Post should contain BOTH files (one + two, glob discovered)
    post = result["test_files_post"]
    assert len(post) == 2
    assert any("test_one.py" in p for p in post)
    assert any("test_two.py" in p for p in post)


# ===========================================================================
# §22-§24 — no_new_credential_shapes gatherer
# ===========================================================================


def test_no_new_credentials_diff_pass_through(fresh_registry) -> None:
    diff = "+def hello(): return 'world'\n"
    ctx = SimpleNamespace(diff_text=diff)
    claim = _make_claim("no_new_credential_shapes")
    result = asyncio.run(dispatch_evidence_gather(claim, ctx))
    assert result == {"diff_text": diff}


def test_no_new_credentials_bytes_diff_decoded(fresh_registry) -> None:
    diff_bytes = b"+def f(): pass\n"
    ctx = SimpleNamespace(diff_text=diff_bytes)
    claim = _make_claim("no_new_credential_shapes")
    result = asyncio.run(dispatch_evidence_gather(claim, ctx))
    assert isinstance(result["diff_text"], str)
    assert "def f()" in result["diff_text"]


def test_no_new_credentials_no_diff_returns_empty(
    fresh_registry,
) -> None:
    """Without diff_text, the gatherer cannot faithfully detect
    'newly introduced' credentials. Honest INSUFFICIENT_EVIDENCE."""
    ctx = SimpleNamespace()  # no diff_text
    claim = _make_claim("no_new_credential_shapes")
    result = asyncio.run(dispatch_evidence_gather(claim, ctx))
    assert result == {}


# ===========================================================================
# §25-§27 — Integration with ctx_evidence_collector
# ===========================================================================


def test_ctx_collector_dispatches_via_registry_for_priority_a(
    fresh_registry, tmp_path,
) -> None:
    """ctx_evidence_collector now calls dispatch_evidence_gather
    FIRST for Priority A claim kinds."""
    from backend.core.ouroboros.governance.verification import (
        ctx_evidence_collector,
    )
    f1 = tmp_path / "a.py"
    f1.write_text("x = 1\n")
    ctx = SimpleNamespace(target_files=(str(f1),))
    claim = _make_claim("file_parses_after_change")
    result = asyncio.run(ctx_evidence_collector(claim, ctx))
    # Priority A path engaged → registry self-gathered the file content
    assert "target_files_post" in result
    assert len(result["target_files_post"]) == 1


def test_ctx_collector_falls_back_to_legacy_for_test_passes(
    fresh_registry,
) -> None:
    """test_passes is NOT registered in the new evidence_collectors
    registry — falls through to the legacy hardcoded path that
    reads ctx.validation_passed."""
    from backend.core.ouroboros.governance.verification import (
        ctx_evidence_collector,
    )
    ctx = SimpleNamespace(validation_passed=True)
    claim = _make_claim("test_passes")
    result = asyncio.run(ctx_evidence_collector(claim, ctx))
    # Legacy path stamped exit_code=0
    assert result == {"exit_code": 0}


def test_ctx_collector_priority_a_empty_does_not_mask_with_legacy(
    fresh_registry,
) -> None:
    """When a Priority A kind's gatherer returns empty (honest
    INSUFFICIENT), the integration MUST NOT mask with a false-
    positive legacy path. The post-A semantics are: empty for
    Priority A means 'we honestly can't evaluate' — not 'fake
    a key from validation_passed'."""
    from backend.core.ouroboros.governance.verification import (
        ctx_evidence_collector,
    )
    # No diff_text → no_new_credential_shapes returns empty
    # AND validation_passed is True (legacy might falsely say "yes")
    ctx = SimpleNamespace(validation_passed=True)  # no diff_text
    claim = _make_claim("no_new_credential_shapes")
    result = asyncio.run(ctx_evidence_collector(claim, ctx))
    # MUST be empty — honest INSUFFICIENT_EVIDENCE, not faked
    assert result == {}


# ===========================================================================
# §28 — Authority invariants
# ===========================================================================


def test_no_authority_imports() -> None:
    from backend.core.ouroboros.governance.verification import (
        evidence_collectors,
    )
    src = inspect.getsource(evidence_collectors)
    forbidden = (
        "orchestrator", "phase_runner", "candidate_generator",
        "iron_gate", "change_engine", "policy", "semantic_guardian",
    )
    for token in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{token}" not in src
        ), f"evidence_collectors must not import {token}"
        assert (
            f"import backend.core.ouroboros.governance.{token}" not in src
        ), f"evidence_collectors must not import {token}"


# ===========================================================================
# §29 — Public API surface
# ===========================================================================


def test_public_api_exposed_from_package() -> None:
    from backend.core.ouroboros.governance import verification
    expected = {
        "EvidenceGatherer",
        "dispatch_evidence_gather",
        "evidence_collectors_enabled",
        "list_evidence_gatherers",
        "register_evidence_gatherer",
    }
    for name in expected:
        assert name in verification.__all__


# ===========================================================================
# §30 — Defensive — never raises on garbage ctx
# ===========================================================================


@pytest.mark.parametrize(
    "kind",
    [
        "file_parses_after_change",
        "test_set_hash_stable",
        "no_new_credential_shapes",
    ],
)
def test_gatherers_never_raise_on_garbage_ctx(fresh_registry, kind) -> None:
    """Each gatherer must return a mapping (possibly empty), never
    raise. Probe with: None ctx, ctx with missing attrs, ctx with
    wrong-typed attrs."""
    claim = _make_claim(kind)
    for ctx in [
        None,
        SimpleNamespace(),
        SimpleNamespace(
            target_files=42,  # not iterable
            test_files_pre="not-a-tuple",
            test_files_post=None,
            diff_text={"not": "a string"},
        ),
    ]:
        result = asyncio.run(
            dispatch_evidence_gather(claim, ctx),
        )
        assert isinstance(result, Mapping)
