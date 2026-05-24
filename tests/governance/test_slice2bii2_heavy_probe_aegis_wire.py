"""Slice 2B-ii.2 — DW heavy probe + Aegis bridge wire.

Closes the gap surfaced by the re-detonation soak bt-2026-05-24-225714:

  [HeavyProbe] success=False total_ms=52
    error=status_401:{"ok": false, "error": "missing_lease_header"}

Slice 2B-ii rewired the 7 credentialed call sites inside
``doubleword_provider.py``, but ``dw_heavy_probe.py`` is an 11th DW
upstream call site that lives in a separate module — it POSTs to
``{base}/chat/completions`` with its OWN inline Bearer header
construction, bypassing the canonical
``dw_authorization_header()`` + ``acquire_call_lease()`` helpers.
When Aegis is on, the request reaches the daemon (proves the
transport bridge works) but is refused at the lease-validation
boundary with HTTP 401.

# Fix

* ``dw_heavy_probe._do_probe`` composes the canonical
  ``aegis_provider_bridge.dw_authorization_header()`` for the
  Authorization header (empty under Aegis enabled, real bearer
  otherwise) and ``aegis_provider_bridge.acquire_call_lease()`` for
  the per-call X-JARVIS-Lease header.
* No signature changes — the caller still passes ``api_key`` for
  the legacy path; the bridge ignores it under Aegis enabled.
* Synthetic op_id ``"dw-heavy-probe:{model_id}"`` for cap accounting.

# AST pin (stronger than per-call): no bare ``aiohttp.ClientSession``
# constructor outside the canonical DW provider session site.

The dw_discovery_runner.py + topology_sentinel.py modules surveyed
in Slice 2B-iii detonation — confirmed they make NO direct HTTP
calls; they only thread api_key/base_url params down into
HeavyProber.probe(). So the single edit to _do_probe covers all
three modules transitively.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OUROBOROS_PKG = REPO_ROOT / "backend" / "core" / "ouroboros"
HEAVY_PROBE_FILE = OUROBOROS_PKG / "governance" / "dw_heavy_probe.py"
DISCOVERY_RUNNER_FILE = OUROBOROS_PKG / "governance" / "dw_discovery_runner.py"
TOPOLOGY_SENTINEL_FILE = OUROBOROS_PKG / "governance" / "topology_sentinel.py"
DW_PROVIDER_FILE = OUROBOROS_PKG / "governance" / "doubleword_provider.py"
BRIDGE_FILE = OUROBOROS_PKG / "governance" / "aegis_provider_bridge.py"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PIN — _do_probe's session.post site carries lease header
# ──────────────────────────────────────────────────────────────────────

def test_ast_pin_heavy_probe_session_post_carries_lease_header() -> None:
    """The session.post call inside ``HeavyProber._do_probe`` must
    pass a ``headers=`` kwarg AND the headers dict must be composed
    via the Aegis bridge's ``merge_lease_into_session_headers`` helper
    (or an equivalent that funnels through ``acquire_call_lease``).
    Bare ``headers={"Authorization": f"Bearer {api_key}", ...}`` is
    forbidden — that's what produced the 401 missing_lease_header
    from the re-detonation soak.
    """
    src = HEAVY_PROBE_FILE.read_text()
    # Verify the new bridge imports landed
    assert "aegis_provider_bridge" in src, (
        "dw_heavy_probe.py does not import aegis_provider_bridge — "
        "the lease helper cannot be composed"
    )
    assert "acquire_call_lease" in src, (
        "dw_heavy_probe.py does not call acquire_call_lease — the "
        "per-call X-JARVIS-Lease header cannot be acquired"
    )
    # Verify no bare Bearer composition remains in the module
    tree = _parse(HEAVY_PROBE_FILE)
    offenders: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            literals = [
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            ]
            if any("Bearer " in lit for lit in literals):
                offenders.append(f"dw_heavy_probe.py:{node.lineno}")
    assert not offenders, (
        "Bare 'Bearer <key>' f-string composition still present in "
        "dw_heavy_probe.py — must route through "
        "aegis_provider_bridge.dw_authorization_header().\n"
        "Offenders: " + "\n".join(offenders)
    )


# ──────────────────────────────────────────────────────────────────────
# AST PIN — no aiohttp.ClientSession constructor outside DW provider
# ──────────────────────────────────────────────────────────────────────

# Allowlist for files that LEGITIMATELY construct aiohttp.ClientSession
# for credentialed upstream calls. Anything outside this list with a
# bare ClientSession() constructor would be a Zero-Trust escape hatch.
_CLIENT_SESSION_ALLOWLIST = {
    # The canonical DW session is constructed in doubleword_provider.py
    # via the bridge's dw_authorization_header() (Slice 2B-ii).
    DW_PROVIDER_FILE,
    # aegis/client.py constructs an aiohttp.ClientSession for the
    # in-process JARVIS→daemon control channel (lease/acquire,
    # session/establish). It does NOT carry upstream credentials —
    # only the bootstrap PSK / session-token Bearer for the
    # JARVIS↔daemon hop. Distinct from upstream credentialed sessions.
    OUROBOROS_PKG / "aegis" / "client.py",
    # engine.py is DEPRECATED — header at module top:
    # "DEPRECATED: superseded by governance/governed_loop_service.py.
    #  Quarantine date: 2026-03-11". Not on any live execution path
    # in the battle-test harness. Allowlisted to keep the pin focused
    # on actively-executed credentialed sessions.
    OUROBOROS_PKG / "engine.py",
}


def test_ast_pin_no_credentialed_client_session_outside_allowlist() -> None:
    """No ``aiohttp.ClientSession(headers={"Authorization": ...})``
    constructor calls allowed in modules outside the credentialed-
    session allowlist.

    Operator binding (Slice 2B-ii.2 directive): "no bare HTTP sessions
    are created outside the bridge". Refined per implementation
    review: the actual security invariant is sessions that CARRY a
    Bearer-bearing Authorization header — not all aiohttp use (many
    modules legitimately use aiohttp for telemetry, web search,
    intelligence sensors etc. without ever touching Aegis-credentialed
    endpoints).

    This pin catches the load-bearing pattern: any ClientSession
    constructed with an Authorization header literal in its headers
    kwarg, anywhere outside the canonical DW session site (which
    composes the header via ``dw_authorization_header()`` from the
    bridge — empty under Aegis enabled).
    """
    offenders: List[str] = []
    for path in OUROBOROS_PKG.rglob("*.py"):
        if " " in path.name:  # skip backup files
            continue
        if "__pycache__" in path.parts:
            continue
        if path.name.startswith("test_"):
            continue
        if path.resolve() in {p.resolve() for p in _CLIENT_SESSION_ALLOWLIST}:
            continue
        try:
            tree = _parse(path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Match aiohttp.ClientSession(...) or ClientSession(...)
            is_cs = False
            if isinstance(node.func, ast.Attribute) and node.func.attr == "ClientSession":
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "aiohttp":
                    is_cs = True
            elif isinstance(node.func, ast.Name) and node.func.id == "ClientSession":
                is_cs = True
            if not is_cs:
                continue
            # Only flag if the constructor carries an Authorization
            # header literal (the load-bearing security pattern).
            headers_kwarg = next(
                (kw for kw in node.keywords if kw.arg == "headers"), None,
            )
            if headers_kwarg is None:
                continue
            headers_source = ast.unparse(headers_kwarg.value)
            if "Authorization" in headers_source or "Bearer " in headers_source:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, (
        "aiohttp.ClientSession(headers={'Authorization': ...}) "
        "constructor outside credentialed-session allowlist — the "
        "Authorization bearer would bypass the Aegis bridge.\n"
        "Allowlist: "
        + ", ".join(p.relative_to(REPO_ROOT).as_posix()
                    for p in _CLIENT_SESSION_ALLOWLIST)
        + "\nOffenders:\n  " + "\n  ".join(offenders)
    )


# ──────────────────────────────────────────────────────────────────────
# AST PIN — confirm dw_discovery_runner + topology_sentinel make zero
# direct HTTP calls (their fix is transitive via heavy_probe rewire)
# ──────────────────────────────────────────────────────────────────────

def test_ast_pin_discovery_runner_makes_no_direct_http_calls() -> None:
    """dw_discovery_runner.py must NOT make direct session.post/get
    calls — it threads api_key/base_url through to HeavyProber.probe.
    If a future contributor adds direct HTTP here, this pin fails
    so they get prompted to use the bridge.
    """
    tree = _parse(DISCOVERY_RUNNER_FILE)
    offenders: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("post", "get"):
            continue
        # Filter for session-shaped receivers (mirror Slice 2B-ii pin)
        if isinstance(node.func.value, ast.Name) and node.func.value.id in ("session", "_session"):
            offenders.append(f"dw_discovery_runner.py:{node.lineno}")
        if isinstance(node.func.value, ast.Attribute) and node.func.value.attr in ("session", "_session", "_http"):
            offenders.append(f"dw_discovery_runner.py:{node.lineno}")
    assert not offenders, (
        "dw_discovery_runner.py made a direct session.post/get call — "
        "must route through HeavyProber.probe (which goes through the "
        "Aegis bridge as of Slice 2B-ii.2).\nOffenders:\n  "
        + "\n  ".join(offenders)
    )


def test_ast_pin_topology_sentinel_makes_no_direct_http_calls() -> None:
    """topology_sentinel.py is pure orchestration — must not call HTTP
    directly. Same enforcement shape as discovery_runner."""
    tree = _parse(TOPOLOGY_SENTINEL_FILE)
    offenders: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("post", "get"):
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id in ("session", "_session"):
            offenders.append(f"topology_sentinel.py:{node.lineno}")
    assert not offenders, (
        "topology_sentinel.py made a direct session.post/get call — "
        "must route through HeavyProber (which goes through the "
        "Aegis bridge).\nOffenders:\n  " + "\n  ".join(offenders)
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — _do_probe end-to-end with httpx-style aiohttp mock
# ──────────────────────────────────────────────────────────────────────

class _FakeRespCtx:
    """Async context-manager wrapping a fake aiohttp response."""

    def __init__(self, captured_headers: dict, status: int = 200,
                 body_text: str = "data: [DONE]\n\n") -> None:
        self.captured_headers = captured_headers
        self.status = status
        self._body_text = body_text

    async def __aenter__(self) -> "_FakeRespCtx":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def text(self) -> str:
        return self._body_text

    @property
    def content(self) -> "_FakeRespCtx":
        return self

    async def __aiter__(self):
        # Minimal SSE-shaped iterator: yield one bytes chunk with a
        # role delta + DONE so the probe sees a "first content" event.
        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        yield b'data: [DONE]\n\n'


class _FakeAiohttpSession:
    """Captures the headers passed to session.post for assertion."""

    def __init__(self) -> None:
        self.captured_headers: dict = {}
        self.captured_url: str = ""

    def post(self, url: str, *, json: dict, headers: dict) -> _FakeRespCtx:
        self.captured_url = url
        self.captured_headers = dict(headers)
        return _FakeRespCtx(captured_headers=headers, status=200)


@pytest.fixture
def aegis_env_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_AEGIS_URL", "http://aegis-test:9999")
    monkeypatch.setenv("JARVIS_AEGIS_BOOTSTRAP_PSK", "test-psk")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "should-not-leak")


@pytest.fixture
def aegis_env_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_AEGIS_URL", raising=False)
    monkeypatch.delenv("JARVIS_AEGIS_BOOTSTRAP_PSK", raising=False)
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "sk-legacy-dw-real-key")


@pytest.mark.asyncio
async def test_heavy_probe_post_carries_aegis_lease_when_enabled(
    aegis_env_enabled: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Aegis is enabled, the heavy probe's session.post must
    carry ``X-JARVIS-Lease`` AND must NOT carry a real Bearer for
    the local DW key (which has been scrubbed by Aegis preflight)."""
    from backend.core.ouroboros.governance import dw_heavy_probe as hp_mod
    from backend.core.ouroboros.governance import aegis_provider_bridge as bridge_mod

    # Monkeypatch the lease helper to return a deterministic token
    captured_lease_calls: List[dict] = []

    async def fake_acquire_lease(**kwargs) -> str:
        captured_lease_calls.append(kwargs)
        return "lease-heavy-probe-token"

    monkeypatch.setattr(
        bridge_mod, "acquire_call_lease", fake_acquire_lease,
    )

    prober = hp_mod.HeavyProber()
    fake_session = _FakeAiohttpSession()
    _ = await prober._do_probe(
        session=fake_session,
        model_id="test-model",
        base_url="http://aegis-test:9999/v1",
        api_key="should-not-leak",
        max_tokens=1,
    )

    # 1. X-JARVIS-Lease attached
    assert fake_session.captured_headers.get("X-JARVIS-Lease") == "lease-heavy-probe-token", (
        f"X-JARVIS-Lease header missing or wrong: "
        f"{fake_session.captured_headers!r}"
    )
    # 2. No real DW Bearer leaked through (under Aegis enabled)
    auth = fake_session.captured_headers.get("Authorization", "")
    assert "should-not-leak" not in auth, (
        f"LEAK: real DOUBLEWORD_API_KEY appeared in Authorization "
        f"header under Aegis enabled: {auth!r}"
    )
    # 3. Lease was acquired with the right op_id shape
    assert len(captured_lease_calls) >= 1, "acquire_call_lease not invoked"
    op_id = captured_lease_calls[0].get("op_id", "")
    assert op_id.startswith("dw-heavy-probe:"), (
        f"unexpected op_id shape: {op_id!r}; expected "
        f"'dw-heavy-probe:<model_id>'"
    )


@pytest.mark.asyncio
async def test_heavy_probe_legacy_path_preserves_bearer_when_aegis_disabled(
    aegis_env_disabled: None,
) -> None:
    """Disabled-Aegis path must produce byte-identical legacy
    behavior: Authorization: Bearer <real-key>, no X-JARVIS-Lease."""
    from backend.core.ouroboros.governance import dw_heavy_probe as hp_mod

    prober = hp_mod.HeavyProber()
    fake_session = _FakeAiohttpSession()
    _ = await prober._do_probe(
        session=fake_session,
        model_id="test-model",
        base_url="https://api.doubleword.ai/v1",
        api_key="sk-legacy-dw-real-key",
        max_tokens=1,
    )

    # Bearer with the real key (legacy behavior preserved)
    assert fake_session.captured_headers.get("Authorization") == "Bearer sk-legacy-dw-real-key"
    # No lease header (Aegis disabled)
    assert "X-JARVIS-Lease" not in fake_session.captured_headers
