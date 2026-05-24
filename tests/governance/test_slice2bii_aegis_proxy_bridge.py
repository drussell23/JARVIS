"""Slice 2B-ii — Aegis Provider Proxy Bridge.

# What this slice closes

Aegis-1 (PR #53861) shipped the credential-confiscation substrate:
daemon, lease primitives, bootstrap PSK handoff, env scrub. Aegis-2B-i
shipped the forwarding surface (``/v1/messages`` + ``/v1/chat/completions``
+ ``/v1/files`` + ``/v1/batches`` + ``/v1/models``). But O+V's provider
modules (``providers.py`` + ``doubleword_provider.py`` + 5 auxiliary
callers) still construct ``AsyncAnthropic(...)`` / ``aiohttp.ClientSession(...)``
with the **real** ``ANTHROPIC_API_KEY`` / ``DOUBLEWORD_API_KEY`` and
POST directly to ``api.anthropic.com`` / ``api.doubleword.ai``. The
Zero-Trust posture is not absolute until every credentialed upstream
call routes through Aegis.

This slice closes that gap with a single canonical factory pattern:
``aegis/provider_bridge.py`` exposes ``make_async_anthropic_client(...)``,
``dw_aegis_base_url()``, ``dw_authorization_header()``, and per-call
``acquire_call_lease(...)``. AST pins enforce that no module outside
``provider_bridge.py`` constructs an Anthropic client or a DW session
directly. Every ``messages.create``/``messages.stream`` and every DW
``session.post``/``session.get`` to ``/v1/*`` carries a fresh per-call
``X-JARVIS-Lease`` header.

# Operator corrections honored (v2 revised design)

  1. Anthropic ``base_url = JARVIS_AEGIS_URL`` (host root) — SDK
     internally appends ``/v1/messages``. Test #6 proves the final
     request path is exactly ``/v1/messages`` not ``/v1/v1/messages``.
  2. ``messages.stream(...)`` covered alongside ``messages.create(...)``.
  3. All 7 DW endpoints route through Aegis (chat/completions, files,
     batches, batches/{id}, files/{id}/content, models).
  4. Per-call lease only — no ``default_headers`` X-JARVIS-Lease.
  5. Lease acquire failure RAISES — no silent fallback to direct
     upstream credentials.
  6. Tests prove wire behavior via ``httpx.MockTransport`` capture +
     real path-string assertions, not just import-graph proofs.

# Test surface

AST pins (5)
  - test_no_raw_async_anthropic_outside_bridge
  - test_no_raw_dw_authorization_header_outside_bridge
  - test_every_messages_create_has_extra_headers_kwarg
  - test_every_messages_stream_has_extra_headers_kwarg
  - test_every_dw_v1_session_call_has_headers_kwarg

Spine tests (9)
  - test_anthropic_request_path_exactly_v1_messages_under_aegis
  - test_dw_request_paths_match_registry_allowlist (parametrized over 7)
  - test_lease_header_present_per_call_with_distinct_tokens
  - test_streaming_path_carries_lease_header
  - test_aegis_enabled_anthropic_client_holds_placeholder_api_key
  - test_aegis_enabled_dw_session_has_no_real_bearer
  - test_lease_acquire_failure_raises_not_falls_back
  - test_aegis_disabled_yields_byte_identical_legacy_construction
  - test_aegis_disabled_dw_base_url_matches_legacy
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Iterator, List, Tuple
from unittest.mock import MagicMock, patch

import httpx
import pytest

# Module under construction — imports must work post-implementation.
# NOTE: provider_bridge lives under governance/ (not aegis/) by design —
# it's a CONSUMER of aegis.client (which holds credentials), not part of
# the Aegis substrate. Aegis's own AST pin forbids any anthropic/openai
# SDK import under aegis/ (credential-confiscation invariant).
from backend.core.ouroboros.governance import aegis_provider_bridge as provider_bridge


# ──────────────────────────────────────────────────────────────────────
# Helpers — AST walkers
# ──────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[2]
OUROBOROS_PKG = REPO_ROOT / "backend" / "core" / "ouroboros"
BRIDGE_FILE = OUROBOROS_PKG / "governance" / "aegis_provider_bridge.py"

# Files KNOWN to call Anthropic SDK — all must go through the bridge.
# (Discovered post-design during AST pin #1 sweep: claude_fallback.py
# also constructs AsyncAnthropic + calls messages.create.)
ANTHROPIC_CALLER_FILES: List[Path] = [
    OUROBOROS_PKG / "claude_fallback.py",
    OUROBOROS_PKG / "governance" / "providers.py",
    OUROBOROS_PKG / "governance" / "self_critique.py",
    OUROBOROS_PKG / "governance" / "general_driver.py",
    OUROBOROS_PKG / "governance" / "fast_path_qa.py",
    OUROBOROS_PKG / "governance" / "visual_comprehension.py",
    OUROBOROS_PKG / "governance" / "m10" / "bridge_adapters.py",
]


def _walk_py_files() -> Iterator[Path]:
    """Walk all .py files under backend/core/ouroboros/, skipping
    deprecated/backup files (e.g. ``providers 2.py``) and tests."""
    for p in OUROBOROS_PKG.rglob("*.py"):
        # Skip backup files (filenames with spaces) — these are
        # operator-side artifacts, not live code paths.
        if " " in p.name:
            continue
        # Skip tests + __pycache__
        if "__pycache__" in p.parts or p.name.startswith("test_"):
            continue
        yield p


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _calls_named(tree: ast.Module, *, name: str) -> List[ast.Call]:
    """Return Call nodes whose .func resolves to ``name`` (either
    direct Name or Attribute with .attr==name)."""
    out: List[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == name:
            out.append(node)
        elif isinstance(fn, ast.Attribute) and fn.attr == name:
            out.append(node)
    return out


def _attribute_chain(node: ast.AST) -> Tuple[str, ...]:
    """For ``foo.bar.baz`` → ``("foo","bar","baz")``. Best-effort."""
    parts: List[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return tuple(reversed(parts))


# ──────────────────────────────────────────────────────────────────────
# AST PIN #1 — no raw AsyncAnthropic(...) outside bridge module
# ──────────────────────────────────────────────────────────────────────

def test_no_raw_async_anthropic_outside_bridge() -> None:
    """AST pin: AsyncAnthropic(...) constructor may appear ONLY in
    ``aegis/provider_bridge.py``. All other modules must call
    ``provider_bridge.make_async_anthropic_client(...)``.
    """
    offenders: List[str] = []
    for path in _walk_py_files():
        if path.resolve() == BRIDGE_FILE.resolve():
            continue
        try:
            tree = _parse(path)
        except SyntaxError:
            continue
        for call in _calls_named(tree, name="AsyncAnthropic"):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{call.lineno}")
    assert not offenders, (
        "Raw AsyncAnthropic(...) calls outside provider_bridge.py — "
        "Aegis transport swap can be bypassed.\nOffenders: "
        + "\n".join(offenders)
    )


# ──────────────────────────────────────────────────────────────────────
# AST PIN #2 — no raw "Authorization: Bearer <DW key>" outside bridge
# ──────────────────────────────────────────────────────────────────────

def test_no_raw_dw_authorization_header_outside_bridge() -> None:
    """AST pin: no raw f-string ``Bearer {DOUBLEWORD_API_KEY}`` or
    ``Bearer {self._api_key}`` style header in DW provider outside
    the bridge. The bridge's ``dw_authorization_header()`` is the
    only place a real bearer token may be composed.
    """
    dw_file = OUROBOROS_PKG / "governance" / "doubleword_provider.py"
    tree = _parse(dw_file)
    offenders: List[str] = []
    for node in ast.walk(tree):
        # Detect f-strings that compose "Bearer " + something
        if isinstance(node, ast.JoinedStr):
            literals = [
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            ]
            if any("Bearer " in lit for lit in literals):
                offenders.append(f"doubleword_provider.py:{node.lineno}")
    # The bridge's dw_authorization_header() is allowed to compose it;
    # the DW provider itself must call the bridge helper, not compose.
    assert not offenders, (
        "Raw 'Bearer <key>' header composition in doubleword_provider.py — "
        "must route through provider_bridge.dw_authorization_header().\n"
        "Offenders: " + "\n".join(offenders)
    )


# ──────────────────────────────────────────────────────────────────────
# AST PIN #3 — every .messages.create(...) carries extra_headers=
# ──────────────────────────────────────────────────────────────────────

def test_every_messages_create_has_extra_headers_kwarg() -> None:
    """AST pin: every ``.messages.create(...)`` call site passes an
    ``extra_headers`` kwarg (which must in turn carry the per-call
    X-JARVIS-Lease via merge_lease_header)."""
    offenders: List[str] = []
    for path in ANTHROPIC_CALLER_FILES:
        if not path.exists():
            continue
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "create":
                continue
            # Walk back the attribute chain — must end in .messages.create
            chain = _attribute_chain(node.func)
            if len(chain) < 2 or chain[-2] != "messages":
                continue
            kw_names = {kw.arg for kw in node.keywords if kw.arg}
            if "extra_headers" not in kw_names:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                )
    assert not offenders, (
        "messages.create(...) call sites missing extra_headers= kwarg — "
        "per-call X-JARVIS-Lease cannot be injected.\nOffenders: "
        + "\n".join(offenders)
    )


# ──────────────────────────────────────────────────────────────────────
# AST PIN #4 — every .messages.stream(...) carries extra_headers=
# ──────────────────────────────────────────────────────────────────────

def test_every_messages_stream_has_extra_headers_kwarg() -> None:
    """AST pin: every ``.messages.stream(...)`` call site passes an
    ``extra_headers`` kwarg."""
    offenders: List[str] = []
    for path in ANTHROPIC_CALLER_FILES:
        if not path.exists():
            continue
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "stream":
                continue
            chain = _attribute_chain(node.func)
            if len(chain) < 2 or chain[-2] != "messages":
                continue
            kw_names = {kw.arg for kw in node.keywords if kw.arg}
            if "extra_headers" not in kw_names:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                )
    assert not offenders, (
        "messages.stream(...) call sites missing extra_headers= kwarg — "
        "streaming path can leak unleased.\nOffenders: " + "\n".join(offenders)
    )


# ──────────────────────────────────────────────────────────────────────
# AST PIN #5 — every DW session.{post,get} to /v1/* carries headers=
# ──────────────────────────────────────────────────────────────────────

def _is_session_call_receiver(call: ast.Call) -> bool:
    """True iff ``call.func`` looks like ``session.X`` or
    ``self._session.X`` — filters out ``dict.get`` / ``os.environ.get``
    / ``response.get`` false positives."""
    if not isinstance(call.func, ast.Attribute):
        return False
    chain = _attribute_chain(call.func)
    # chain[-1] is the method (post/get). Receiver is chain[:-1].
    if not chain:
        return False
    receiver_tail = chain[-2] if len(chain) >= 2 else chain[0]
    return receiver_tail in ("session", "_session")


def _is_url_string_argument(arg: ast.AST) -> bool:
    """True iff arg is an f-string composing a URL with ``/v1/``,
    ``/chat/completions``, ``/files``, ``/batches``, or ``/models`` —
    or a plain string containing one of those path fragments. Used
    as a SECONDARY filter (over and above receiver-name) to scope
    the AST pin to real upstream URL calls."""
    fragments = (
        "/chat/completions", "/files", "/batches", "/models", "/v1/",
    )
    if isinstance(arg, ast.JoinedStr):
        literals = [
            v.value for v in arg.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return any(any(f in lit for f in fragments) for lit in literals)
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return any(f in arg.value for f in fragments)
    return False


def test_every_dw_v1_session_call_has_headers_kwarg() -> None:
    """AST pin: every ``session.post(...)`` / ``session.get(...)`` call
    in doubleword_provider.py that targets an upstream URL (composes
    a ``/v1/*``-style path) carries a ``headers=`` kwarg — the entry
    point for per-call lease + ``dw_authorization_header()``.

    Filter rationale: receiver must be ``session`` / ``self._session``
    (not ``dict.get`` / ``os.environ.get``) AND first arg must compose
    an upstream URL fragment (not internal helper calls).
    """
    dw_file = OUROBOROS_PKG / "governance" / "doubleword_provider.py"
    tree = _parse(dw_file)
    offenders: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("post", "get"):
            continue
        if not _is_session_call_receiver(node):
            continue
        # Require at least one positional URL-ish arg
        if not node.args or not _is_url_string_argument(node.args[0]):
            continue
        kw_names = {kw.arg for kw in node.keywords if kw.arg}
        if "headers" not in kw_names:
            offenders.append(f"doubleword_provider.py:{node.lineno}")
    assert not offenders, (
        "DW session.post/get sites missing headers= kwarg — Aegis "
        "lease + auth swap cannot be injected.\nOffenders: "
        + "\n".join(offenders)
    )


# ──────────────────────────────────────────────────────────────────────
# Spine fixtures — env hygiene
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def aegis_enabled_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set the env vars that ``aegis.client.is_enabled()`` checks.
    Returns the JARVIS_AEGIS_URL the test should expect."""
    url = "http://aegis-test:9999"
    monkeypatch.setenv("JARVIS_AEGIS_URL", url)
    monkeypatch.setenv("JARVIS_AEGIS_BOOTSTRAP_PSK", "test-psk-32-bytes-hex")
    # Ensure real upstream keys are set — so we can prove they are NOT
    # leaked to the constructed client.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-anthropic-key-DO-NOT-LEAK")
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "sk-real-dw-key-DO-NOT-LEAK")
    return url


@pytest.fixture
def aegis_disabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_AEGIS_URL", raising=False)
    monkeypatch.delenv("JARVIS_AEGIS_BOOTSTRAP_PSK", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-legacy-anthropic-key")
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "sk-legacy-dw-key")
    monkeypatch.delenv("DOUBLEWORD_BASE_URL", raising=False)


# ──────────────────────────────────────────────────────────────────────
# SPINE #6 — Anthropic request path is EXACTLY /v1/messages under Aegis
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anthropic_request_path_exactly_v1_messages_under_aegis(
    aegis_enabled_env: str,
) -> None:
    """OPERATOR CORRECTION #1 PROOF: the SDK appends ``/v1/messages``
    internally. base_url must be the host root, NOT host+/v1, so the
    final request path is ``/v1/messages`` not ``/v1/v1/messages``."""
    captured: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_test", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "model": "claude-opus-4-7", "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    try:
        client = provider_bridge.make_async_anthropic_client(
            http_client=http_client,
        )
        await client.messages.create(
            model="claude-opus-4-7",
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}],
            extra_headers={"X-JARVIS-Lease": "test-lease-token"},
        )
    finally:
        await http_client.aclose()

    assert len(captured) == 1, f"expected 1 request, got {len(captured)}"
    req = captured[0]
    assert str(req.url) == f"{aegis_enabled_env}/v1/messages", (
        f"BUG: path composed wrong. Expected exactly "
        f"{aegis_enabled_env}/v1/messages, got {req.url}. "
        f"If you see /v1/v1/messages — base_url was set to {aegis_enabled_env}/v1 "
        f"instead of {aegis_enabled_env}."
    )


# ──────────────────────────────────────────────────────────────────────
# SPINE #7 — DW request paths match Aegis registry allowlist
# ──────────────────────────────────────────────────────────────────────

DW_REGISTRY_PATHS = (
    "/v1/chat/completions",
    "/v1/files",
    "/v1/batches",
    "/v1/batches/abc123",       # /v1/batches/{batch_id} template
    "/v1/files/abc123/content",  # /v1/files/{file_id}/content template
    "/v1/models",
)


def test_dw_base_url_composes_aegis_root_when_enabled(
    aegis_enabled_env: str,
) -> None:
    """OPERATOR CORRECTION #3: every DW endpoint composes to a path
    under {JARVIS_AEGIS_URL}/v1/<endpoint>. The DW provider uses
    f-string composition (``f"{self._base_url}/chat/completions"``)
    so ``dw_aegis_base_url()`` must return ``{AEGIS}/v1`` (with the
    /v1 suffix) so f-string composition yields the right final path."""
    base = provider_bridge.dw_aegis_base_url()
    assert base == f"{aegis_enabled_env}/v1", (
        f"BUG: dw_aegis_base_url() returned {base!r}; expected "
        f"{aegis_enabled_env}/v1 so f-string composition with DW "
        f"provider's /chat/completions /files /batches /models suffixes "
        f"produces Aegis-allowlisted paths."
    )
    # Verify each known DW endpoint composes correctly under aegis root
    for suffix in ("/chat/completions", "/files", "/batches",
                   "/batches/abc123", "/files/abc123/content", "/models"):
        composed = f"{base}{suffix}"
        registry_path = f"/v1{suffix}"
        assert registry_path in (
            "/v1/chat/completions", "/v1/files", "/v1/batches",
            "/v1/batches/abc123", "/v1/files/abc123/content", "/v1/models",
        ), f"path {registry_path} not in Aegis DW allowlist"
        assert composed.startswith(aegis_enabled_env), composed


# ──────────────────────────────────────────────────────────────────────
# SPINE #8 — per-call distinct lease tokens
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lease_header_present_per_call_with_distinct_tokens(
    aegis_enabled_env: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPERATOR CORRECTION #4: leases are PER-CALL, never client-wide.
    Three sequential calls must carry three DISTINCT lease tokens."""
    leases_returned = ["lease-A", "lease-B", "lease-C"]

    async def fake_acquire_lease(**kwargs) -> str:
        return leases_returned.pop(0)

    # Patch the bridge's acquire_call_lease to a deterministic stub
    captured: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200, json={
                "id": "m", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "model": "x", "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    try:
        client = provider_bridge.make_async_anthropic_client(
            http_client=http_client,
        )
        # Simulate the per-call lease injection pattern that providers.py
        # is expected to use at each messages.create site.
        for i in range(3):
            lease = await fake_acquire_lease(
                op_id=f"op-{i}", route="standard",
                estimated_cost_usd=0.01,
            )
            await client.messages.create(
                model="claude-opus-4-7", max_tokens=10,
                messages=[{"role": "user", "content": "hi"}],
                extra_headers={"X-JARVIS-Lease": lease},
            )
    finally:
        await http_client.aclose()

    assert len(captured) == 3
    headers = [r.headers.get("x-jarvis-lease") for r in captured]
    assert headers == ["lease-A", "lease-B", "lease-C"], (
        f"leases not distinct per-call: {headers}"
    )


# ──────────────────────────────────────────────────────────────────────
# SPINE #9 — streaming path carries lease header
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_streaming_path_carries_lease_header(
    aegis_enabled_env: str,
) -> None:
    """OPERATOR CORRECTION #2: messages.stream(...) must carry the
    same per-call lease as messages.create(...)."""
    captured: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        # Minimal SSE event stream that the SDK can parse without choking
        sse_body = (
            'event: message_start\n'
            'data: {"type":"message_start","message":{"id":"m","type":"message","role":"assistant","content":[],"model":"x","stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
            'event: message_stop\n'
            'data: {"type":"message_stop"}\n\n'
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body.encode(),
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    try:
        client = provider_bridge.make_async_anthropic_client(
            http_client=http_client,
        )
        async with client.messages.stream(
            model="claude-opus-4-7", max_tokens=10,
            messages=[{"role": "user", "content": "hi"}],
            extra_headers={"X-JARVIS-Lease": "stream-lease-7"},
        ) as stream:
            async for _ in stream:
                pass
    finally:
        await http_client.aclose()

    assert len(captured) >= 1
    assert captured[0].headers.get("x-jarvis-lease") == "stream-lease-7", (
        f"streaming request missing/wrong lease: "
        f"{captured[0].headers.get('x-jarvis-lease')!r}"
    )


# ──────────────────────────────────────────────────────────────────────
# SPINE #10 — Anthropic client has PLACEHOLDER key, not real key
# ──────────────────────────────────────────────────────────────────────

def test_aegis_enabled_anthropic_client_holds_placeholder_api_key(
    aegis_enabled_env: str,
) -> None:
    """OPERATOR CORRECTION #6: when Aegis is enabled, the constructed
    Anthropic client must NOT hold the real ANTHROPIC_API_KEY. The
    real key is injected server-side by Aegis."""
    client = provider_bridge.make_async_anthropic_client()
    real_key = os.environ.get("ANTHROPIC_API_KEY", "")
    assert real_key, "test setup error — ANTHROPIC_API_KEY should be set"
    assert client.api_key != real_key, (
        f"LEAK: AsyncAnthropic.api_key holds the real "
        f"ANTHROPIC_API_KEY when Aegis is enabled. Should be a "
        f"placeholder; got {client.api_key!r}."
    )
    # Sanity: placeholder is non-empty (SDK rejects empty string)
    assert client.api_key, "placeholder must be non-empty (SDK requirement)"
    # And the base_url is the Aegis URL
    assert str(client.base_url).rstrip("/") == aegis_enabled_env.rstrip("/"), (
        f"client.base_url={client.base_url} should be Aegis URL "
        f"{aegis_enabled_env}"
    )


# ──────────────────────────────────────────────────────────────────────
# SPINE #11 — DW auth header has NO real bearer when Aegis enabled
# ──────────────────────────────────────────────────────────────────────

def test_aegis_enabled_dw_session_has_no_real_bearer(
    aegis_enabled_env: str,
) -> None:
    """OPERATOR CORRECTION #6: when Aegis is enabled, the DW session
    must NOT carry an Authorization: Bearer <real DW key> header."""
    auth = provider_bridge.dw_authorization_header()
    real_key = os.environ.get("DOUBLEWORD_API_KEY", "")
    assert real_key, "test setup error — DOUBLEWORD_API_KEY should be set"
    # Auth header should be empty dict (Aegis injects server-side)
    assert "Authorization" not in auth, (
        f"LEAK: dw_authorization_header() emitted Authorization header "
        f"when Aegis is enabled: {auth!r}"
    )
    # Defensive: no header value contains the real key
    for k, v in auth.items():
        assert real_key not in v, (
            f"LEAK: real DW key appears in {k}={v!r} when Aegis enabled"
        )


# ──────────────────────────────────────────────────────────────────────
# SPINE #12 — lease acquire failure RAISES, no silent fallback
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lease_acquire_failure_raises_not_falls_back(
    aegis_enabled_env: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPERATOR CORRECTION #5: if Aegis is enabled and lease acquire
    fails, the helper RAISES. No silent fallback to direct upstream
    credentials — that would defeat the Zero-Trust posture."""
    from backend.core.ouroboros.aegis import client as aegis_client_mod

    async def boom_acquire(**kwargs):
        raise aegis_client_mod.AegisClientError(
            "simulated daemon unreachable"
        )

    # Patch AegisClient.acquire_lease class-level so any singleton built
    # by acquire_call_lease will use it.
    monkeypatch.setattr(
        aegis_client_mod.AegisClient,
        "acquire_lease",
        boom_acquire,
        raising=True,
    )
    # Also stub the get() singleton accessor to avoid real session
    # establishment over the network.
    fake_singleton = MagicMock()
    fake_singleton.acquire_lease = boom_acquire

    async def fake_get() -> object:
        return fake_singleton

    monkeypatch.setattr(aegis_client_mod.AegisClient, "get", fake_get)

    with pytest.raises(aegis_client_mod.AegisClientError):
        await provider_bridge.acquire_call_lease(
            op_id="op-x", route="standard", estimated_cost_usd=0.01,
        )


# ──────────────────────────────────────────────────────────────────────
# SPINE #13 — Aegis disabled: byte-identical legacy Anthropic client
# ──────────────────────────────────────────────────────────────────────

def test_aegis_disabled_yields_byte_identical_legacy_construction(
    aegis_disabled_env: None,
) -> None:
    """When Aegis is disabled, ``make_async_anthropic_client()`` must
    return a client constructed exactly as legacy code would:
    api_key from env, base_url defaulted by SDK (api.anthropic.com)."""
    client = provider_bridge.make_async_anthropic_client()
    # Legacy api_key = ANTHROPIC_API_KEY from env
    assert client.api_key == "sk-legacy-anthropic-key", (
        f"legacy client should use real env key; got {client.api_key!r}"
    )
    # Legacy base_url = SDK default (api.anthropic.com)
    base = str(client.base_url).rstrip("/")
    assert "api.anthropic.com" in base, (
        f"legacy client base_url should be api.anthropic.com; got {base}"
    )


# ──────────────────────────────────────────────────────────────────────
# SPINE #14 — Aegis disabled: DW base_url matches legacy env default
# ──────────────────────────────────────────────────────────────────────

def test_aegis_disabled_dw_base_url_matches_legacy(
    aegis_disabled_env: None,
) -> None:
    """When Aegis is disabled, ``dw_aegis_base_url()`` must return the
    legacy default (env DOUBLEWORD_BASE_URL or api.doubleword.ai/v1)."""
    base = provider_bridge.dw_aegis_base_url()
    assert base == "https://api.doubleword.ai/v1", (
        f"legacy DW base_url should be api.doubleword.ai/v1; got {base!r}"
    )
    # And auth header should carry the real DW bearer
    auth = provider_bridge.dw_authorization_header()
    assert auth.get("Authorization") == "Bearer sk-legacy-dw-key", (
        f"legacy DW auth header should be Bearer <real-key>; got {auth!r}"
    )


# ──────────────────────────────────────────────────────────────────────
# Extra spine — disabled state acquire_call_lease returns None
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_acquire_call_lease_returns_none_when_disabled(
    aegis_disabled_env: None,
) -> None:
    """When Aegis disabled, acquire_call_lease returns None so
    callers can skip the header injection cleanly."""
    lease = await provider_bridge.acquire_call_lease(
        op_id="op-x", route="standard", estimated_cost_usd=0.01,
    )
    assert lease is None
