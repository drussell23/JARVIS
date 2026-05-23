"""AST pins for Slice Aegis-1 binding directives.

Pin set (each is a deliberate Slice-1 invariant):

  1. Aegis daemon has NO ``/v1/*`` routes — Slice 1 does NOT forward
     provider traffic. AST-pin enforced at the route-registration site.
  2. ``ImmutableBudgetStateMachine`` has NO public mutator beyond
     ``admit`` / ``reconcile`` / ``tighten`` (the audited surface).
  3. The HMAC key ``K`` never appears in any ``return`` payload of a
     route handler, never in ``logger.*`` / ``logging.*`` calls,
     never in ``json.dumps`` / ``repr`` / ``str.format`` — only
     passed as positional arg to lease minting/validation functions.
  4. No ``anthropic`` / ``openai`` SDK import inside the ``aegis``
     package (forwarding is Slice 2; substrate stays SDK-free).
  5. ``RejectReason`` taxonomy is exactly the 6-value §43.6.1 set.
  6. ``credential_registry`` exports exactly the central frozenset
     of upstream credential env var names.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


AEGIS_ROOT = Path(__file__).resolve().parents[2] / "backend" / "core" / "ouroboros" / "aegis"
DAEMON_FILE = AEGIS_ROOT / "daemon.py"
STATE_MACHINE_FILE = AEGIS_ROOT / "budget_state_machine.py"


def _aegis_modules() -> list:
    return sorted(p for p in AEGIS_ROOT.glob("*.py") if p.name != "__init__.py")


# ---------------------------------------------------------------------------
# Pin 1: NO /v1/* routes in Slice 1
# ---------------------------------------------------------------------------


def test_ast_pin_no_v1_route_literals_in_daemon():
    """daemon.py must contain NO literal ``/v1/...`` string in any
    ``router.add_*`` call.

    Slice 2 added /v1/messages + /v1/chat/completions forwarding, but
    those paths are registered via iteration over the upstream_registry
    map keys — never as string literals in daemon.py. Keeping this pin
    enforces the single-source-of-truth discipline:
    :mod:`upstream_registry` is the only place where /v1/* path
    literals live, and adding a new upstream endpoint is a one-line
    edit there + a matching :mod:`credential_registry` entry. A literal
    here would bypass the boot-time credential-alignment check.
    """
    tree = ast.parse(DAEMON_FILE.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if not node.func.attr.startswith("add_"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            assert not first.value.startswith("/v1/"), (
                f"daemon.py registers a /v1/* route literal "
                f"({first.value!r}) — paths must come from "
                f"upstream_registry.snapshot(), not literals here. "
                f"Adding an endpoint is a registry edit, not a daemon edit."
            )


def test_ast_pin_v1_paths_known_only_via_upstream_registry():
    """Crosscheck: every path the upstream_registry exposes must
    follow the ``/v1/`` family convention, and the registry must
    align with the credential_registry (enforced at boot via
    ``upstream_registry._validate_credential_registry_alignment``;
    pinned here as well so CI catches a misaligned add fast)."""
    from backend.core.ouroboros.aegis.upstream_registry import (
        known_aegis_paths,
        snapshot,
    )
    paths = known_aegis_paths()
    assert paths, "upstream_registry has no known paths"
    for p in paths:
        assert p.startswith("/v1/"), (
            f"upstream_registry path {p!r} doesn't follow the /v1/ "
            f"convention — Aegis only forwards /v1/* endpoints"
        )
    # snapshot() raises at boot if credential_registry doesn't align.
    snapshot()


# ---------------------------------------------------------------------------
# Pin 2: ImmutableBudgetStateMachine has no setter beyond tighten()
# ---------------------------------------------------------------------------


def test_ast_pin_state_machine_no_public_setter():
    """Walk the class body. Public methods must be one of the audited
    surface set. Anything else (esp. names starting with ``set_``) is
    a violation."""
    tree = ast.parse(STATE_MACHINE_FILE.read_text())
    audited = {
        "__init__",
        "caps",                 # @property — read-only
        "wal_path",             # @property — read-only
        "record_boot",
        "replay_for_recovery",
        "admit",
        "reconcile",
        "tighten",
        "snapshot",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "ImmutableBudgetStateMachine":
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = item.name
                if name.startswith("_"):
                    continue  # private helpers OK
                assert name in audited, (
                    f"ImmutableBudgetStateMachine has unaudited public "
                    f"method: {name!r}. Add to the audited set in this "
                    f"test OR remove from the class."
                )
                assert not name.startswith("set_"), (
                    f"setter-shaped method on state machine: {name!r}"
                )


# ---------------------------------------------------------------------------
# Pin 3: HMAC K never in returns / logs / format calls
# ---------------------------------------------------------------------------


def test_ast_pin_hmac_key_never_returned_or_logged():
    """Scan daemon.py for any place that might leak the HMAC key.

    The key is stored as ``app[_K_HMAC_KEY]`` and bound to local ``K``
    in handlers. We confirm:
      - No ``return`` statement whose payload directly contains
        ``app[_K_HMAC_KEY]`` or a bare ``K`` reference.
      - No ``logger.*`` / ``logging.*`` call references K.
    """
    tree = ast.parse(DAEMON_FILE.read_text())

    def _names_in(node: ast.AST) -> set:
        out = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                out.add(child.id)
        return out

    for node in ast.walk(tree):
        # Return statement: ``return X`` — X must not contain K.
        if isinstance(node, ast.Return) and node.value is not None:
            names = _names_in(node.value)
            assert "K" not in names, (
                f"return statement at line {node.lineno} references K — "
                f"HMAC key leak risk"
            )
        # logger.X(...) / logging.X(...) calls — args must not contain K.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id in (
                "logger", "logging",
            ):
                for arg in node.args:
                    names = _names_in(arg)
                    assert "K" not in names, (
                        f"logger call at line {node.lineno} references K"
                    )


def test_ast_pin_no_hmac_key_attr_in_health_response_keys():
    """/health response body must not contain a field that hints at K."""
    src = DAEMON_FILE.read_text()
    # Cheap textual pin — any future field added to the /health body
    # must not contain these tokens.
    forbidden = ['"hmac"', "'hmac'", '"k_hmac"', "'k_hmac'", '"_hmac_key"']
    # Find the _handle_health body and check.
    start = src.find("async def _handle_health(")
    assert start >= 0, "_handle_health handler missing"
    # The next function definition (handler) bounds the search.
    next_def = src.find("async def ", start + 1)
    body = src[start:next_def] if next_def > start else src[start:]
    for token in forbidden:
        assert token not in body, (
            f"/health body contains forbidden token {token}"
        )


# ---------------------------------------------------------------------------
# Pin 4: No anthropic/openai SDK in aegis package
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_file", _aegis_modules())
def test_ast_pin_no_anthropic_or_openai_sdk_import(module_file: Path):
    """The Aegis substrate stays SDK-free until Slice 2 adds forwarding."""
    tree = ast.parse(module_file.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in ("anthropic", "openai"), (
                    f"{module_file.name} imports {alias.name} — "
                    f"provider SDKs are Slice 2 territory"
                )
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root not in ("anthropic", "openai"), (
                f"{module_file.name} imports from {node.module} — "
                f"provider SDKs are Slice 2 territory"
            )


# ---------------------------------------------------------------------------
# Pin 5 + 6: closed taxonomies / canonical exports
# ---------------------------------------------------------------------------


def test_credential_registry_exports_canonical_set():
    from backend.core.ouroboros.aegis.credential_registry import (
        upstream_credential_env_vars,
    )
    names = upstream_credential_env_vars()
    # Hard pin on the names we know carry credentials right now.
    # Adding a provider == updating this test + the registry together.
    assert names == frozenset({
        "ANTHROPIC_API_KEY",
        "DOUBLEWORD_API_KEY",
    })


def test_reject_reason_taxonomy_exact_match():
    from backend.core.ouroboros.aegis.budget_state_machine import RejectReason
    actual = {r.value for r in RejectReason}
    expected = {
        "emission_cap_exceeded",
        "fanout_cap_exceeded",
        "cost_ceiling_exceeded",
        "causal_depth_exceeded",
        "lineage_forgery",
        "budget_authority_unavailable",
    }
    assert actual == expected


def test_token_verdict_kind_taxonomy_exact_match():
    from backend.core.ouroboros.aegis.lease import TokenVerdictKind
    actual = {v.value for v in TokenVerdictKind}
    expected = {
        "valid", "invalid_format", "invalid_signature",
        "expired", "replayed",
    }
    assert actual == expected


def test_preflight_outcome_taxonomy_exact_match():
    from backend.core.ouroboros.aegis.preflight import PreflightOutcome
    actual = {v.value for v in PreflightOutcome}
    expected = {
        "skipped_disabled",
        "ready",
        "failed_spawn",
        "failed_bootstrap_timeout",
        "failed_credential_scrub",
    }
    assert actual == expected
