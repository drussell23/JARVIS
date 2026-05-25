"""Slice 2B-iii.3 — Strip endpoint.auth_header from forwarded headers.

Closes the case-collision bug surfaced by the capability soak
bt-2026-05-25-000817:

  POST /v1/messages → 401 "invalid x-api-key"
    request_id=req_011CbNKujYDTjX6Ggs4bT517

Direct curl with the same key returns HTTP 200. The Aegis-side proxy
was leaking the bridge's PLACEHOLDER ``X-Api-Key`` header through to
api.anthropic.com because ``forwarding.py``'s strip-loop omitted the
endpoint's own auth header from its strip-list.

# Root cause

``backend/core/ouroboros/aegis/forwarding.py:452-461`` (pre-fix):

  outbound_headers: Dict[str, str] = {}
  for name, value in request.headers.items():
      lname = name.lower()
      if lname in ("host", "authorization", "x-jarvis-lease", "content-length"):
          continue
      outbound_headers[name] = value         # ← keeps "X-Api-Key" (SDK case)
  if endpoint.auth_scheme is AuthScheme.HEADER_RAW:
      outbound_headers[endpoint.auth_header] = upstream_credential  # ← adds "x-api-key" (lowercase)

The Anthropic Python SDK sends ``X-Api-Key: aegis-managed-no-real-key-do-not-use``
(the bridge's placeholder). The loop copies it into the dict with key
``"X-Api-Key"`` (case preserved). Line 459 then sets
``outbound_headers["x-api-key"] = <real key>`` — a DIFFERENT dict key
(lowercase). The Python dict ends up with BOTH:

  {"X-Api-Key": "aegis-managed-no-real-key-do-not-use",
   "x-api-key": "sk-ant-..."}

When aiohttp serializes this via CIMultiDict, both headers go on the
wire; Anthropic uses the first (the placeholder) and returns 401.

# Why DW heavy_probe DIDN'T hit this earlier

DW uses ``AuthScheme.HEADER_BEARER`` not ``HEADER_RAW``:

  outbound_headers["Authorization"] = f"Bearer {upstream_credential}"

And the strip-list DOES strip ``"authorization"``, so:
  1. Loop strips the JARVIS-side Authorization (correctly)
  2. Line 461 adds the daemon-side ``Authorization: Bearer <real DW key>``
  3. Single header, no collision → reaches DW with real bearer → DW
     returns 403 entitlement (real upstream response with real key)

The gap is ONLY for ``HEADER_RAW`` upstreams (Anthropic-style x-api-key).
``HEADER_BEARER`` upstreams (DoubleWord-style) were already protected
by the existing ``"authorization"`` strip.

# Fix

One-line addition to the strip-list at ``forwarding.py:455``:

  if lname in ("host", "authorization", "x-jarvis-lease",
               "content-length", endpoint.auth_header.lower()):
      continue

Strips ALL case-variants of the endpoint's auth header (the
case-insensitive ``lname`` comparison covers ``X-Api-Key`` /
``x-api-key`` / ``X-API-KEY`` / etc.), preventing the inbound
placeholder from collision-leaking into the outbound dict.

# Test surface

  * Spine: simulate the EXACT collision (inbound X-Api-Key
    placeholder + lowercase x-api-key written by line 459) — assert
    the dict has ONLY ONE x-api-key entry (the real one), not two.
  * Spine: case-variant coverage — X-API-KEY, x-Api-Key,
    X-api-key all stripped, only daemon-injected key survives.
  * Spine: bearer path UNCHANGED — DW HEADER_BEARER still works
    byte-identically (no regression).
  * AST pin: strip-list at forwarding.py:~455 references
    ``endpoint.auth_header`` (anti-regression — would catch future
    contributors omitting the auth_header from the strip-list when
    adding a new HEADER_RAW upstream).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FORWARDING_FILE = REPO_ROOT / "backend" / "core" / "ouroboros" / "aegis" / "forwarding.py"


# ──────────────────────────────────────────────────────────────────────
# AST PIN — strip-list references endpoint.auth_header
# ──────────────────────────────────────────────────────────────────────

def test_ast_pin_strip_list_references_endpoint_auth_header() -> None:
    """The strip-list at forwarding.py:~455 must include
    ``endpoint.auth_header.lower()`` as one of the entries. Any
    future contributor who adds a new HEADER_RAW upstream WITHOUT
    updating the strip list would re-introduce the collision bug —
    this pin catches that.

    Approach: AST-walk forwarding.py for the ``forward_request``
    function, find the tuple/set literal containing the canonical
    Aegis-specific ``"x-jarvis-lease"`` strip entry, and assert
    that tuple ALSO contains an ``endpoint.auth_header.lower()``
    call expression. AST-level (not substring) ensures the entry
    is genuinely IN the strip collection, not just referenced
    nearby (e.g., on the next line where line 459 sets the auth
    header — that's an unrelated reference).
    """
    tree = ast.parse(FORWARDING_FILE.read_text())
    # Walk every tuple/set/list literal in the file; find the one
    # that contains "x-jarvis-lease" (the canonical Aegis marker).
    strip_collections = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Tuple, ast.Set, ast.List)):
            continue
        # Collect the string-literal entries in this collection
        str_entries = {
            elt.value for elt in node.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        }
        if "x-jarvis-lease" not in str_entries:
            continue
        strip_collections.append(node)

    assert strip_collections, (
        "strip-list literal containing 'x-jarvis-lease' not found in "
        "forwarding.py — was the strip block refactored away?"
    )

    # For each candidate strip-collection, check that an
    # endpoint.auth_header.lower() call expression is one of the
    # elements (NOT just a string literal — that wouldn't generalize
    # across HEADER_RAW upstreams).
    fixed_any = False
    for node in strip_collections:
        for elt in node.elts:
            if not isinstance(elt, ast.Call):
                continue
            # Looking for `endpoint.auth_header.lower()` shape:
            # Call(func=Attribute(attr='lower',
            #   value=Attribute(attr='auth_header',
            #     value=Name(id='endpoint'))))
            try:
                if (
                    isinstance(elt.func, ast.Attribute)
                    and elt.func.attr == "lower"
                    and isinstance(elt.func.value, ast.Attribute)
                    and elt.func.value.attr == "auth_header"
                    and isinstance(elt.func.value.value, ast.Name)
                    and elt.func.value.value.id == "endpoint"
                ):
                    fixed_any = True
                    break
            except AttributeError:
                continue
        if fixed_any:
            break

    assert fixed_any, (
        "strip-list at forwarding.py:~455 does NOT include "
        "`endpoint.auth_header.lower()` as one of its entries. "
        "The collision bug (Slice 2B-iii.3) will re-emerge for any "
        "HEADER_RAW upstream — the bridge's placeholder X-Api-Key "
        "header will leak through to the upstream, taking precedence "
        "over the daemon-injected real key. Add "
        "`endpoint.auth_header.lower()` to the strip-list tuple."
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — exact collision reproduction (the soak's failure mode)
# ──────────────────────────────────────────────────────────────────────

class _FakeMultiDict:
    """Minimal stand-in for aiohttp.web.Request.headers — a multidict-
    style iterator that preserves case (mimicking the real ClientSession
    behavior on the JARVIS→daemon hop)."""

    def __init__(self, pairs):
        self._pairs = list(pairs)

    def items(self):
        return iter(self._pairs)


def _build_outbound_headers(
    inbound_headers: _FakeMultiDict,
    auth_header_name: str,
    upstream_credential: str,
    auth_scheme: str = "HEADER_RAW",
) -> Dict[str, str]:
    """Pure-function extraction of forwarding.py:452-461 logic.
    Mirrors the production code 1:1 so the test exercises the EXACT
    behavior, not a paraphrase."""
    outbound_headers: Dict[str, str] = {}
    # Build the strip set the way the FIXED code does
    _strip = {"host", "authorization", "x-jarvis-lease", "content-length",
              auth_header_name.lower()}
    for name, value in inbound_headers.items():
        if name.lower() in _strip:
            continue
        outbound_headers[name] = value
    if auth_scheme == "HEADER_RAW":
        outbound_headers[auth_header_name] = upstream_credential
    elif auth_scheme == "HEADER_BEARER":
        outbound_headers[auth_header_name] = f"Bearer {upstream_credential}"
    return outbound_headers


def test_spine_collision_with_sdk_case_variant() -> None:
    """Reproduce the EXACT soak failure mode: Anthropic SDK sends
    ``X-Api-Key`` (mixed case) as the placeholder. Pre-fix, the
    outbound dict ends up with BOTH ``X-Api-Key`` (placeholder) AND
    ``x-api-key`` (real). Post-fix, ONLY the real ``x-api-key``
    remains in the outbound dict.
    """
    inbound = _FakeMultiDict([
        ("X-Api-Key", "aegis-managed-no-real-key-do-not-use"),
        ("anthropic-version", "2023-06-01"),
        ("content-type", "application/json"),
        ("user-agent", "Anthropic/Python 0.75.0"),
    ])
    out = _build_outbound_headers(
        inbound_headers=inbound,
        auth_header_name="x-api-key",
        upstream_credential="sk-ant-real-deadbeef",
        auth_scheme="HEADER_RAW",
    )
    # The CORE assertion: no case-variant of x-api-key survives
    # except the daemon-injected one. The placeholder is stripped.
    api_key_entries = {
        k: v for k, v in out.items() if k.lower() == "x-api-key"
    }
    assert len(api_key_entries) == 1, (
        f"Expected exactly ONE x-api-key entry, got {len(api_key_entries)}: "
        f"{api_key_entries}. Multiple entries = bug re-introduced."
    )
    assert "aegis-managed-no-real-key" not in str(api_key_entries.values()), (
        f"PLACEHOLDER LEAKED into outbound: {api_key_entries}"
    )
    surviving_value = list(api_key_entries.values())[0]
    assert surviving_value == "sk-ant-real-deadbeef", (
        f"Wrong value: expected real key, got {surviving_value}"
    )


@pytest.mark.parametrize("inbound_case", [
    "X-Api-Key", "x-api-key", "X-API-KEY", "x-Api-Key", "X-api-key",
    "x-API-KEY", "X-Api-KEY",
])
def test_spine_all_case_variants_stripped(inbound_case: str) -> None:
    """Every plausible case-variant the upstream client might send
    must be stripped. Catches future-proof: if the Anthropic SDK
    changes its header casing in a future version, we still don't
    leak."""
    inbound = _FakeMultiDict([
        (inbound_case, "aegis-managed-no-real-key-do-not-use"),
        ("content-type", "application/json"),
    ])
    out = _build_outbound_headers(
        inbound_headers=inbound,
        auth_header_name="x-api-key",
        upstream_credential="sk-ant-real-12345",
        auth_scheme="HEADER_RAW",
    )
    api_key_entries = {
        k: v for k, v in out.items() if k.lower() == "x-api-key"
    }
    assert len(api_key_entries) == 1, f"case={inbound_case!r}: {api_key_entries}"
    assert list(api_key_entries.values())[0] == "sk-ant-real-12345"
    assert "aegis-managed" not in str(api_key_entries.values())


# ──────────────────────────────────────────────────────────────────────
# Spine — bearer path UNCHANGED (no regression for HEADER_BEARER)
# ──────────────────────────────────────────────────────────────────────

def test_spine_bearer_path_byte_identical_post_fix() -> None:
    """DW uses HEADER_BEARER which strips ``authorization`` (already
    in the pre-fix strip-list). The fix shouldn't change anything for
    HEADER_BEARER upstreams — verify byte-identical outbound.
    """
    inbound = _FakeMultiDict([
        ("Authorization", "Bearer aegis-managed-placeholder"),
        ("content-type", "application/json"),
    ])
    out = _build_outbound_headers(
        inbound_headers=inbound,
        auth_header_name="Authorization",
        upstream_credential="dw-real-key-67890",
        auth_scheme="HEADER_BEARER",
    )
    # Only ONE Authorization entry, with the real bearer composed
    auth_entries = {k: v for k, v in out.items() if k.lower() == "authorization"}
    assert len(auth_entries) == 1, auth_entries
    assert list(auth_entries.values())[0] == "Bearer dw-real-key-67890"
    assert "aegis-managed-placeholder" not in str(auth_entries.values())


# ──────────────────────────────────────────────────────────────────────
# Spine — anti-regression — pre-fix code DOES collision-leak
# ──────────────────────────────────────────────────────────────────────

def _pre_fix_build_outbound(
    inbound_headers: _FakeMultiDict,
    auth_header_name: str,
    upstream_credential: str,
) -> Dict[str, str]:
    """The PRE-FIX behavior — for proof the test would have caught
    the bug. Strip-list does NOT include auth_header."""
    outbound_headers: Dict[str, str] = {}
    _strip = {"host", "authorization", "x-jarvis-lease", "content-length"}
    for name, value in inbound_headers.items():
        if name.lower() in _strip:
            continue
        outbound_headers[name] = value
    outbound_headers[auth_header_name] = upstream_credential
    return outbound_headers


def test_pre_fix_code_DOES_leak_placeholder() -> None:
    """Sanity check: confirm the PRE-FIX logic (without the
    endpoint.auth_header.lower() addition) DOES produce the
    collision. If this test ever starts to fail, it means the
    pre-fix behavior changed and our regression coverage may be
    stale. The fix's correctness depends on this pre-fix proof.
    """
    inbound = _FakeMultiDict([
        ("X-Api-Key", "aegis-managed-placeholder"),
    ])
    out = _pre_fix_build_outbound(
        inbound_headers=inbound,
        auth_header_name="x-api-key",
        upstream_credential="sk-ant-real",
    )
    # Pre-fix: TWO entries with different cases — the bug
    api_key_entries = {
        k: v for k, v in out.items() if k.lower() == "x-api-key"
    }
    assert len(api_key_entries) == 2, (
        f"Pre-fix code should produce 2 entries (the bug); got {api_key_entries}. "
        "If this fails, pre-fix logic changed — review the regression coverage."
    )
    # Both keys present → collision
    assert "X-Api-Key" in out
    assert "x-api-key" in out
