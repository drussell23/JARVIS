"""The 2026-10-14 12:19 boot's warnings, each traced to a wrong answer.

1. "Stuck processes detected on ports: [8000]" -- the backend's fallback
   ports listed J-Prime's, and a substring test judged "running" (it has a
   'u') an unkillable state.
2. "Hot-swap blocked ... prime:/health missing 'ready_for_inference'" -- the
   gate validated the /v1/models liveness body against the /health schema,
   and "localhost" -> "127.0.0.1" was taken for a promotion.
3. Memory tier flapping optimal <-> elevated -- the policy's exit thresholds
   were declared and never applied, and the quantizer kept its own table.
4. Ghost Display: 'WindowsDesktopProvider' has no 'get_ghost_display_space'.
5. ECAPA DualAuthorityError -- the kernel built a second facade in-process.
6. Speaker verification "0 loaded" / "1 profiles loaded" as WARNINGs --
   three concurrent initialisations of private instances.
8. ~25 optional-extra warnings -- one summary instead.
9. ANTHROPIC_API_KEY not set x4 -- the paid lane is off by declaration.
10. Stalls the sentinel warns about had no stacks -- the sampler's trigger.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]


# -- 1. a sibling's port is never the backend's -------------------------------
def test_sibling_ports_are_reserved(monkeypatch):
    from backend.core.ouroboros.governance import trinity_siblings as ts
    monkeypatch.setenv("JARVIS_PRIME_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("REACTOR_CORE_API_URL", "http://localhost:8090")
    assert ts.reserved_ports() == {8000, 8090}
    monkeypatch.setenv("JARVIS_PRIME_URL", "http://10.0.0.5:8000")   # a remote Mind
    assert 8000 not in ts.reserved_ports()


def _load_supervisor_class(name: str):
    """One class from unified_supervisor.py, without importing the kernel."""
    src = (REPO / "unified_supervisor.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    return ast.get_source_segment(src, node)


def test_running_is_not_an_unkillable_state():
    seg = _load_supervisor_class("DynamicPortManager")
    assert "status.strip().lower() in" in seg
    ns = SimpleNamespace(UE_STATE_INDICATORS=['disk-sleep', 'uninterruptible', 'D', 'U'])
    method_src = seg[seg.index("    def _is_unkillable_state"):seg.index("    def _port_is_bindable")]
    scope: dict = {}
    exec("from typing import *\n" + "\n".join(l[4:] for l in method_src.splitlines()), scope)
    judge = scope["_is_unkillable_state"]
    assert judge(ns, "running") is False          # contains 'u': was "stuck"
    assert judge(ns, "sleeping") is False
    assert judge(ns, "disk-sleep") is True
    assert judge(ns, "D") is True


def test_fallback_ports_exclude_sibling_ports():
    seg = _load_supervisor_class("DynamicPortManager")
    assert "reserved = self._sibling_ports()" in seg
    assert "if port not in reserved" in seg


# -- 2. the hot-swap gate reads the contract's endpoint -----------------------
class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def get(self, url, timeout=None):
        self.seen.append(url)
        path = "/" + url.split("/", 3)[3]
        status, body = self.routes.get(path, (404, {}))
        return _Resp(status, body)


class _Pool:
    def __init__(self, session):
        self.session = session

    def get_session(self):
        session = self.session

        class _Ctx:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def _client(monkeypatch, routes):
    from backend.core import prime_client as pc
    monkeypatch.delenv("JARVIS_PRIME_HEALTH_ENDPOINT", raising=False)
    client = pc.PrimeClient.__new__(pc.PrimeClient)
    client._config = pc.PrimeClientConfig()
    client._config.prime_host, client._config.prime_port = "127.0.0.1", 8000
    client._pool = _Pool(_Session(routes))
    client._last_health_data = {"object": "list", "data": []}     # the /v1/models body
    return client


def test_the_gate_validates_the_health_body_not_the_liveness_body(monkeypatch):
    from backend.core.protocol_version_gate import validate_health_before_swap
    health = {"status": "healthy", "ready_for_inference": True, "protocol_version": "1.0.0",
              "version": "0.1.0"}
    client = _client(monkeypatch, {"/health": (200, health)})
    body = asyncio.run(client._contract_body("prime:/health"))
    assert body == health
    assert client._pool.session.seen == ["http://127.0.0.1:8000/health"]
    assert validate_health_before_swap("prime:/health", body)[0] is True
    # the liveness body, which the gate used to read, fails the contract
    assert validate_health_before_swap("prime:/health", client._last_health_data)[0] is False


def test_a_protocol_version_is_compared_not_the_package_version():
    from backend.core.protocol_version_gate import validate_health_before_swap
    ok, why = validate_health_before_swap(
        "prime:/health", {"ready_for_inference": True, "protocol_version": "1.0.0", "version": "0.1.0"})
    assert ok, why


def test_an_unreadable_contract_endpoint_is_an_empty_body(monkeypatch):
    client = _client(monkeypatch, {})
    assert asyncio.run(client._contract_body("prime:/health")) == {}


def test_the_same_endpoint_spelled_differently_is_not_a_promotion(monkeypatch):
    from backend.core import prime_client as pc
    client = _client(monkeypatch, {"/v1/models": (200, {"data": []})})
    client._config.prime_host = "localhost"
    client._lock, client._status = asyncio.Lock(), pc.PrimeStatus.UNKNOWN
    client._last_health_check = 0.0

    async def no_swap(*a, **k):
        raise AssertionError("a swap was attempted")
    monkeypatch.setattr(pc, "PrimeConnectionPool", no_swap)
    assert asyncio.run(client.update_endpoint("127.0.0.1", 8000)) is True   # probed, not "unknown"
    assert client._config.prime_host == "localhost"


@pytest.mark.parametrize("a,b,same", [
    ("localhost", "127.0.0.1", True),
    ("127.0.0.1", "127.0.0.1", True),
    ("localhost", "::1", True),
    ("127.0.0.1", "203.0.113.9", False),
])
def test_same_host_compares_addresses(a, b, same):
    from backend.core.prime_client import PrimeClient
    assert asyncio.run(PrimeClient._same_host(a, b)) is same


# -- 3. one policy, with its deadbands ---------------------------------------
def test_classify_enters_at_enter_and_leaves_below_exit():
    from backend.core.memory_types import PressurePolicy, PressureTier as T
    p = PressurePolicy.for_ram_gb(32.0)            # prosumer: ELEVATED 65/60, OPTIMAL 50/45
    assert p.classify(64.9) == T.OPTIMAL
    assert p.classify(65.0) == T.ELEVATED
    assert p.classify(62.0, previous=T.ELEVATED) == T.ELEVATED      # inside the deadband
    assert p.classify(59.9, previous=T.ELEVATED) == T.OPTIMAL
    assert p.classify(62.0, previous=T.OPTIMAL) == T.OPTIMAL        # not yet entered
    assert p.classify(44.0, previous=T.OPTIMAL) == T.ABUNDANT
    assert p.classify(77.0, previous=T.CRITICAL) == T.CONSTRAINED   # one band down, not two
    assert p.classify(99.0) == T.EMERGENCY


def test_every_profile_declares_a_deadband_for_every_tier():
    from backend.core.memory_types import PressurePolicy
    for gb in (8, 16, 32, 64):
        p = PressurePolicy.for_ram_gb(gb)
        assert set(p.enter_thresholds) == set(p.exit_thresholds)
        assert all(p.exit_thresholds[t] < p.enter_thresholds[t] for t in p.enter_thresholds)


def test_the_quantizer_holds_its_tier_on_a_boundary(monkeypatch):
    from backend.core import memory_quantizer as mq
    from backend.core.memory_types import PressurePolicy
    policy = PressurePolicy.for_host(62.0, 18.4)
    q = mq.MemoryQuantizer.__new__(mq.MemoryQuantizer)
    q._tier_measured, q.current_tier = False, mq.MemoryTier.OPTIMAL
    swap = SimpleNamespace(used=0, total=1, percent=0.0)
    readings = [76.0, 73.0, 72.5, 74.0, 73.5]     # resting on the old 70-75% boundary
    tiers = []
    for pct in readings:
        tier = q._calculate_tier_macos(mq.MemoryPressure.NORMAL, pct, swap, policy)
        q._tier_measured, q.current_tier = True, tier
        tiers.append(tier)
    assert set(tiers) == {mq.MemoryTier.ELEVATED}


def test_kernel_and_swap_signals_still_escalate():
    from backend.core import memory_quantizer as mq
    from backend.core.memory_types import PressurePolicy
    q = mq.MemoryQuantizer.__new__(mq.MemoryQuantizer)
    q._tier_measured, q.current_tier = False, mq.MemoryTier.OPTIMAL
    policy = PressurePolicy.for_ram_gb(32.0)
    calm = SimpleNamespace(used=0, total=1, percent=0.0)
    assert q._calculate_tier_macos(mq.MemoryPressure.CRITICAL, 10.0, calm, policy) == mq.MemoryTier.EMERGENCY
    # macOS: a kernel that says NORMAL keeps a full file cache from reading CONSTRAINED
    assert q._calculate_tier_macos(mq.MemoryPressure.NORMAL, 78.0, calm, policy) == mq.MemoryTier.ELEVATED
    assert q._calculate_tier_macos(mq.MemoryPressure.UNKNOWN, 78.0, calm, policy) == mq.MemoryTier.CONSTRAINED
    heavy = SimpleNamespace(used=1, total=1, percent=95.0)
    assert q._calculate_tier_macos(mq.MemoryPressure.UNKNOWN, 10.0, heavy, policy) == mq.MemoryTier.CRITICAL


def test_consumers_share_host_pressure_policy():
    for rel in ("backend/core/memory_budget_broker.py", "backend/process_cleanup_manager.py",
                "backend/core/memory_quantizer.py"):
        src = (REPO / rel).read_text(encoding="utf-8")
        assert "host_pressure_policy(" in src, rel
        assert "planned_resident_gb" not in src or rel.endswith("process_cleanup_manager.py"), rel


# -- 4. Ghost Display on a query-only space backend --------------------------
def test_ghost_display_is_not_used_on_a_query_only_backend(caplog):
    from backend.vision import yabai_space_detector as ysd

    class QueryOnly:                     # the Windows desktop provider's surface
        def enumerate_all_spaces(self, include_display_info=True):
            return [{"space_id": 1, "is_visible": True, "is_current": True}]

        def get_current_user_space(self):
            return 1

    mgr = ysd.GhostDisplayManager()
    with caplog.at_level(logging.INFO):
        ok = asyncio.run(mgr.initialize(QueryOnly()))
    assert ok is False
    assert mgr._status == ysd.GhostDisplayStatus.UNAVAILABLE
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# -- 5. one ECAPA facade per process -----------------------------------------
def test_the_kernel_uses_the_facade_singleton():
    src = (REPO / "unified_supervisor.py").read_text(encoding="utf-8")
    assert "EcapaFacade(" not in src
    assert "await get_ecapa_facade(" in src


def test_the_facade_module_is_one_object_under_both_names():
    sys.path.insert(0, str(REPO / "backend"))
    try:
        import backend.core.ecapa_facade as a
        import core.ecapa_facade as b
        assert a is b
    finally:
        sys.path.remove(str(REPO / "backend"))


# -- 6. one speaker verification service, initialised once -------------------
def test_concurrent_initialisers_share_one_run(monkeypatch):
    from backend.voice import speaker_verification_service as svs
    svc = svs.SpeakerVerificationService.__new__(svs.SpeakerVerificationService)
    svc.initialized = False
    runs = []

    async def body(**_):
        runs.append(1)
        await asyncio.sleep(0.05)
        svc.initialized = True

    monkeypatch.setattr(svc, "_initialize_once", body, raising=False)
    monkeypatch.setattr(svc, "_initialize_fast_once", body, raising=False)

    async def go():
        await asyncio.gather(svc.initialize(), svc.initialize_fast(), svc.initialize())
        await svc.initialize_fast()

    asyncio.run(go())
    assert runs == [1]


def test_every_production_site_uses_the_one_instance():
    for rel in ("backend/main.py", "backend/hud/voice_identity.py",
                "backend/voice/voice_unlock_integration.py",
                "backend/neural_mesh/adapters/voice_adapter.py",
                "backend/core/parallel_initializer.py"):
        src = (REPO / rel).read_text(encoding="utf-8")
        assert "SpeakerVerificationService(" not in src, rel


def test_the_service_module_is_one_object_under_both_names():
    sys.path.insert(0, str(REPO / "backend"))
    try:
        import backend.voice.speaker_verification_service as a
        import voice.speaker_verification_service as b
        assert a is b
    finally:
        sys.path.remove(str(REPO / "backend"))


def test_readiness_counts_voiceprints_not_placeholders(caplog):
    from backend.voice import speaker_verification_service as svs
    svc = svs.SpeakerVerificationService.__new__(svs.SpeakerVerificationService)
    svc.speaker_profiles = {"Derek": {"embedding": None, "requires_enrollment": True}}
    svc._use_registry_encoder, svc.speechbrain_engine = False, None
    with caplog.at_level(logging.INFO, logger=svs.logger.name):
        svc._log_readiness()
    rec = [r for r in caplog.records if "Speaker verification ready" in r.getMessage()]
    assert len(rec) == 1 and rec[0].levelno == logging.INFO
    msg = rec[0].getMessage()
    assert "0 voiceprint(s) enrolled" in msg and "1 profile(s) awaiting enrollment" in msg


# -- 8. missing extras, said once --------------------------------------------
@pytest.fixture
def extras(monkeypatch):
    from backend.core import optional_dependencies as od
    monkeypatch.setattr(od, "_missing_extras", {})
    monkeypatch.setattr(od, "_missing_summarised", False)
    return od


def test_missing_extras_are_one_line(extras, caplog):
    od = extras
    od.note_missing("torch", "speaker verification", install="torch")
    od.note_missing("torch", "wake word", install="torch")
    od.note_missing("torch", "speaker verification", install="torch")
    od.note_missing("langgraph", "reasoning graph", install="langgraph")
    od.note_missing("pyobjc", "menu bar", platforms=("no-such-platform",))
    with caplog.at_level(logging.INFO, logger=od.logger.name):
        od.log_missing_summary()
        od.log_missing_summary()
    lines = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    msg = lines[0].getMessage()
    assert "(2)" in msg and "torch (speaker verification, wake word)" in msg
    assert "pyobjc" not in msg and "pip install langgraph torch" in msg


def test_an_extra_noted_after_the_summary_is_said_once(extras, caplog):
    od = extras
    od.log_missing_summary()
    with caplog.at_level(logging.INFO, logger=od.logger.name):
        od.note_missing("playwright", "browser automation", install="playwright")
        od.note_missing("playwright", "browser automation")
    assert len([r for r in caplog.records if r.levelno >= logging.INFO]) == 1


def test_no_site_warns_about_its_own_missing_extra():
    sites = {
        "backend/core/gcp_vm_manager.py": "google-cloud-compute not installed",
        "backend/intelligence/cloud_database_adapter.py": "asyncpg not available",
        "backend/intelligence/learning_database.py": "ChromaDB not available",
        "backend/context_intelligence/automation/google_docs_api.py": "Install: pip install google-auth",
        "backend/neural_mesh/agents/google_workspace_agent.py": "Google API libraries not available. Install",
        "backend/autonomy/langgraph_engine.py": "LangGraph not available",
        "backend/intelligence/chain_of_thought.py": "LangGraph not available",
        "backend/intelligence/reasoning_graph_engine.py": "LangGraph not available",
        "backend/macos_helper/menu_bar/status_indicator.py": "menu bar will use fallback mode",
        "backend/graceful_http_handler.py": "PyTorch not available",
        "backend/ghost_hands/n_optic_nerve.py": "pytesseract not installed",
        "backend/ghost_hands/background_actuator.py": "Playwright not installed",
        "backend/neural_mesh/knowledge/semantic_memory.py": "not available, using",
        "backend/voice/ml_enhanced_voice_system.py": "Deep learning not available",
        "backend/voice/voice_ml_trainer.py": "Deep learning libraries not available",
    }
    for rel, phrase in sites.items():
        src = (REPO / rel).read_text(encoding="utf-8")
        assert phrase not in src, rel
        assert "note_missing(" in src, rel


# -- 9. a switched-off paid lane's credential is absent by declaration ---------
def test_declared_absent_follows_the_switches(monkeypatch):
    from backend.core.ouroboros.governance.paid_lanes import declared_absent
    monkeypatch.setenv("JARVIS_PAID_LANES_ENABLED", "false")
    assert declared_absent("ANTHROPIC_API_KEY") == "JARVIS_PAID_LANES_ENABLED=false"
    monkeypatch.setenv("JARVIS_PAID_LANES_ENABLED", "true")
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    assert declared_absent("ANTHROPIC_API_KEY") is None       # on: a real gap
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    assert declared_absent("ANTHROPIC_API_KEY") == "JARVIS_PROVIDER_CLAUDE_DISABLED=true"
    assert declared_absent("SOME_OTHER_KEY") is None


def test_agentic_config_reports_no_issue_for_a_declared_absence(monkeypatch):
    from backend.core import agentic_config as ac
    monkeypatch.setenv("JARVIS_PAID_LANES_ENABLED", "false")
    cfg = ac.AgenticConfig()
    cfg.computer_use.api_key = None
    assert not [i for i in cfg.validate() if "ANTHROPIC_API_KEY" in i]
    monkeypatch.setenv("JARVIS_PAID_LANES_ENABLED", "true")
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    assert [i for i in cfg.validate() if "ANTHROPIC_API_KEY" in i]


def test_agentic_config_does_not_search_backends_for_a_switched_off_lane(monkeypatch):
    # supervisor-20261015-085516.log 08:56:07: AgenticConfig's api_key
    # default_factory asked SecretManager for 'anthropic-api-key' -> WARNING
    # "not found in any backend" on a host with JARVIS_PAID_LANES_ENABLED=false.
    import backend.core.secret_manager as sm
    from backend.core import agentic_config as ac

    asked = []
    monkeypatch.setattr(sm, "get_secret", lambda sid, *a, **k: asked.append(sid))
    # Empty, not deleted: monkeypatch then restores whatever the .env
    # fallback below may write into os.environ.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("JARVIS_PAID_LANES_ENABLED", "false")
    assert ac._resolve_anthropic_api_key() is None
    assert asked == []

    monkeypatch.setenv("JARVIS_PAID_LANES_ENABLED", "true")
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    ac._resolve_anthropic_api_key()
    assert asked[:1] == ["anthropic-api-key"]     # switched on: a real search


# -- 10. a stall worth a warning is worth a stack ----------------------------
def test_the_sampler_trigger_follows_the_sentinel(monkeypatch):
    from backend.hud import loop_sentinel as ls, stall_sampler as ss
    monkeypatch.delenv("JARVIS_STALL_SAMPLER_TRIGGER_S", raising=False)
    monkeypatch.setenv("JARVIS_LOOP_SENTINEL_INTERVAL_S", "0.25")
    monkeypatch.setenv("JARVIS_LOOP_SENTINEL_STALL_S", "0.25")
    assert ss.trigger_s() == pytest.approx(0.5)
    monkeypatch.setenv("JARVIS_LOOP_SENTINEL_STALL_S", "1.0")
    assert ss.trigger_s() == pytest.approx(1.25)
    monkeypatch.setenv("JARVIS_STALL_SAMPLER_TRIGGER_S", "2.0")
    assert ss.trigger_s() == 2.0


def test_ensure_ready_returns_when_the_probe_concludes_unavailable():
    from backend.core.ecapa_facade import EcapaFacade
    from backend.core.ecapa_types import EcapaState
    import time

    facade = EcapaFacade.__new__(EcapaFacade)

    async def go():
        facade.__init__(registry=SimpleNamespace(get_wrapper=lambda *_: None))
        await facade._try_transition(EcapaState.PROBING, reason="t", warning_code="T")
        waiter = asyncio.ensure_future(facade.ensure_ready(timeout=30.0))
        await asyncio.sleep(0.05)
        await facade._try_transition(EcapaState.LOADING, reason="t", warning_code="T")
        await facade._try_transition(EcapaState.UNAVAILABLE, reason="t", warning_code="T")
        t0 = time.monotonic()
        ok = await waiter
        return ok, time.monotonic() - t0, facade.settled

    ok, waited, settled = asyncio.run(go())
    assert ok is False and settled is True and waited < 1.0     # not the 30 s timeout


def test_no_cloud_sql_password_search_without_cloud_sql(monkeypatch):
    from backend.intelligence import cloud_database_adapter as cda
    from backend.intelligence import cloud_sql_connection_manager as csm
    monkeypatch.setattr(csm, "database_config_path", lambda: None)
    for var in ("JARVIS_DB_TYPE", "JARVIS_DB_CONNECTION_NAME"):
        monkeypatch.delenv(var, raising=False)
    asked = []
    monkeypatch.setattr(cda, "get_db_password", lambda: asked.append(1) or None, raising=False)
    monkeypatch.setattr(cda, "SECRET_MANAGER_AVAILABLE", True, raising=False)
    cfg = cda.DatabaseConfig()
    assert asked == [] and cfg.db_type == "sqlite"
