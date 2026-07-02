"""E2E: cognitive experiences written in 'session A' surface in 'session B' prompt.

No mocks on the persistence layer -- real PersistentIntelligenceManager, real
SQLite, real hydration, real formatter. Guards the wired-but-inert failure
class (per feedback_security_filter_must_be_wired): a capability that only
ever runs against a FakePIM in unit tests is unproven end-to-end.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
async def isolated_pim_env(tmp_path, monkeypatch):
    """Hermetic, isolated PIM: tmpdir SQLite DB, cloud sync off, singletons reset.

    Implementer note: LOCAL_DB_PATH / STATE_DIR / CLOUD_ENABLED on
    PersistentIntelligenceManager are class attributes resolved via
    ``os.getenv`` at *class-definition* (module-import) time -- setting the
    env vars here via monkeypatch.setenv has no effect if the module was
    already imported earlier in the test session. So in addition to setting
    the env vars (for fidelity / in case this is the first import), the
    class attributes are patched directly to guarantee isolation regardless
    of import order. CLOUD_ENABLED is forced False so no real cloud adapter
    (network) is ever touched during this test.
    """
    monkeypatch.setenv("JARVIS_STATE_DB", str(tmp_path / "pi.db"))
    monkeypatch.setenv("JARVIS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_CLOUD_SYNC", "false")
    monkeypatch.setenv("JARVIS_COGNITIVE_PERSISTENCE_ENABLED", "true")

    import backend.core.persistent_intelligence_manager as pim_mod
    import backend.core.ouroboros.governance.cognitive_persistence as cogp

    monkeypatch.setattr(
        pim_mod.PersistentIntelligenceManager, "LOCAL_DB_PATH",
        str(tmp_path / "pi.db"),
    )
    monkeypatch.setattr(
        pim_mod.PersistentIntelligenceManager, "STATE_DIR", str(tmp_path),
    )
    monkeypatch.setattr(
        pim_mod.PersistentIntelligenceManager, "CROSS_REPO_DIR",
        str(tmp_path / "cross_repo"),
    )
    monkeypatch.setattr(
        pim_mod.PersistentIntelligenceManager, "CLOUD_ENABLED", False,
    )

    # Reset singletons so each phase builds fresh against the tmp DB.
    # Real module-level attribute is `_manager` (persistent_intelligence_
    # manager.py:1265), not `_instance` -- verified by reading the source.
    monkeypatch.setattr(pim_mod, "_manager", None, raising=False)
    monkeypatch.setattr(cogp, "_default_store", None)
    monkeypatch.setattr(cogp, "_prior_knowledge_cache", cogp.PriorKnowledgeCache())

    # Collected by the test when it deliberately orphans a PIM instance at
    # the amnesia boundary (session A's manager, once `_manager` is nulled
    # out so session B is forced to rebuild from disk) -- teardown shuts
    # every collected instance down so no background _sync_task /
    # _checkpoint_task survives past the test.
    orphaned_managers: list = []

    yield orphaned_managers

    # Teardown: shut PIM's background loops (_sync_task/_checkpoint_task,
    # started in initialize()) down cleanly so pytest's event loop closes
    # without "task was destroyed but it is pending" warnings.
    if pim_mod._manager is not None:
        await pim_mod.shutdown_persistent_intelligence()
    for mgr in orphaned_managers:
        if mgr is not None:
            await mgr.shutdown()
    cogp._default_store = None


async def test_session_a_writes_session_b_reads_and_injects(isolated_pim_env):
    import backend.core.persistent_intelligence_manager as pim_mod
    import backend.core.ouroboros.governance.cognitive_persistence as cogp

    orphaned_managers = isolated_pim_env

    # --- SESSION A: record a hallucinated tool call ---
    store = await cogp.get_default_store()
    assert store is not None, "real PIM must initialize against tmp DB"
    exp = cogp.CognitiveExperience(
        kind=cogp.ExperienceKind.HALLUCINATED_TOOL,
        footprint="qwen3:32b@16384",
        subject="fetch_url",
        error_class="unknown_tool",
    )
    assert await store.record(exp, op_id="op-session-a") is True

    # --- amnesia boundary: forget ALL in-memory state, including the PIM
    # module singleton itself, so "session B" is forced to open a brand
    # new PersistentIntelligenceManager against the same on-disk SQLite
    # file rather than reusing session A's live in-memory _cache. This is
    # what actually proves persistence (vs. just proving the cogp-level
    # caches were cleared). Session A's manager is orphaned here -- stash
    # it so teardown can cancel its background tasks too.
    orphaned_managers.append(pim_mod._manager)
    cogp._default_store = None
    cogp._prior_knowledge_cache = cogp.PriorKnowledgeCache()
    pim_mod._manager = None

    # --- SESSION B: boot hydration + prompt injection ---
    cache = await cogp.hydrate_prior_knowledge()
    assert len(cache) == 1, "prior experience must survive the session boundary"
    section = cogp.format_for_prompt(cache, footprint="qwen3:32b@16384")
    assert section is not None
    assert "fetch_url" in section and "hallucinated_tool" in section
    assert "BEGIN UNTRUSTED DATA" in section

    # Live injection always calls format_for_prompt with footprint=None
    # (ctx lacks resolved model attrs) -- the global top-K path must also
    # serve the persisted experience, not just the exact-footprint path.
    live_section = cogp.format_for_prompt(cache, footprint=None)
    assert live_section is not None and "fetch_url" in live_section
