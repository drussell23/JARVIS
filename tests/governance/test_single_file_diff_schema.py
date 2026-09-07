"""Root-cause regression for the full_content docstring ``\"\"\"`` mangle.

Commit ``c7b518aabb`` hardcoded ``_single_file_task = False`` in
``providers._build_codegen_prompt`` — a GLOBAL kill of the 2b.1-diff schema
("force full_content") added when every served model produced unappliable
diffs. Slice 235 later built the capability + size gate
(``resolve_force_full_content``) to re-enable diff for diff-capable brains on
large files, but never removed the hardcode, so its computed
``force_full_content=False`` verdict was silently ignored and the diff branch
stayed dead. Re-emitting a whole file verbatim is exactly where a mid-size
model drops a closing ``\"\"\"`` — a diff never reproduces the docstring, so it
cannot mangle it.

These tests pin the ONE shared predicate ``single_file_diff_requested`` that
both the prompt builder (which schema to EMIT) and the response parser (whether
a diff reply is EXPECTED vs schema drift) consult, so the two can never drift
apart, and the master switch's OFF path stays byte-identical to the legacy.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import providers


class _RI:
    """Minimal stand-in for RoutingIntentTelemetry."""

    def __init__(self, schema_capability: str = "full_content_only") -> None:
        self.schema_capability = schema_capability


class _Tel:
    def __init__(self, ri: _RI) -> None:
        self.routing_intent = ri


class _Ctx:
    """Minimal OpContext duck-type for the pure predicate."""

    def __init__(
        self,
        *,
        target_files=(),
        cross_repo: bool = False,
        schema_capability: str = "full_content_only",
    ) -> None:
        self.target_files = tuple(target_files)
        self.cross_repo = cross_repo
        self.telemetry = _Tel(_RI(schema_capability))


def _single_file_ctx(**kw):
    kw.setdefault("target_files", ("backend/core/x.py",))
    kw.setdefault("schema_capability", "full_content_and_diff")
    return _Ctx(**kw)


# ---------------------------------------------------------------------------
# Master switch semantics
# ---------------------------------------------------------------------------


def test_switch_defaults_off(monkeypatch):
    monkeypatch.delenv(providers._SINGLE_FILE_DIFF_SCHEMA_ENV, raising=False)
    assert providers.single_file_diff_schema_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", " On "])
def test_switch_on_truthy(monkeypatch, val):
    monkeypatch.setenv(providers._SINGLE_FILE_DIFF_SCHEMA_ENV, val)
    assert providers.single_file_diff_schema_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "bogus"])
def test_switch_off_falsy(monkeypatch, val):
    monkeypatch.setenv(providers._SINGLE_FILE_DIFF_SCHEMA_ENV, val)
    assert providers.single_file_diff_schema_enabled() is False


# ---------------------------------------------------------------------------
# OFF path is byte-identical to the hardcoded-off legacy: NEVER requests a diff
# ---------------------------------------------------------------------------


def test_off_never_requests_diff_even_when_favorable(monkeypatch):
    monkeypatch.delenv(providers._SINGLE_FILE_DIFF_SCHEMA_ENV, raising=False)
    ctx = _single_file_ctx()
    # Even with the capability+size gate saying diff is fine, OFF ⇒ full_content.
    assert providers.single_file_diff_requested(ctx, force_full_content=False) is False


# ---------------------------------------------------------------------------
# ON path: the capability+size gate is authoritative
# ---------------------------------------------------------------------------


def test_on_requests_diff_when_gate_allows(monkeypatch):
    monkeypatch.setenv(providers._SINGLE_FILE_DIFF_SCHEMA_ENV, "true")
    ctx = _single_file_ctx()
    # force_full_content=False is Slice 235's verdict: capable brain + large file.
    assert providers.single_file_diff_requested(ctx, force_full_content=False) is True


def test_on_forces_full_when_gate_says_so(monkeypatch):
    """A weak brain or a small file ⇒ force_full_content=True ⇒ full_content,
    even with the switch ON. This is the guard that kept the diff-disable safe."""
    monkeypatch.setenv(providers._SINGLE_FILE_DIFF_SCHEMA_ENV, "true")
    ctx = _single_file_ctx()
    assert providers.single_file_diff_requested(ctx, force_full_content=True) is False


def test_on_multi_file_never_diffs(monkeypatch):
    monkeypatch.setenv(providers._SINGLE_FILE_DIFF_SCHEMA_ENV, "true")
    ctx = _single_file_ctx(target_files=("a.py", "b.py"))
    assert providers.single_file_diff_requested(ctx, force_full_content=False) is False


def test_on_zero_file_never_diffs(monkeypatch):
    monkeypatch.setenv(providers._SINGLE_FILE_DIFF_SCHEMA_ENV, "true")
    ctx = _single_file_ctx(target_files=())
    assert providers.single_file_diff_requested(ctx, force_full_content=False) is False


def test_on_cross_repo_never_diffs(monkeypatch):
    monkeypatch.setenv(providers._SINGLE_FILE_DIFF_SCHEMA_ENV, "true")
    ctx = _single_file_ctx(cross_repo=True)
    assert providers.single_file_diff_requested(ctx, force_full_content=False) is False


# ---------------------------------------------------------------------------
# Fail-soft: a malformed ctx never raises out of the predicate
# ---------------------------------------------------------------------------


def test_predicate_failsoft_on_broken_ctx(monkeypatch):
    monkeypatch.setenv(providers._SINGLE_FILE_DIFF_SCHEMA_ENV, "true")

    class _Broken:
        @property
        def target_files(self):  # noqa: D401 — raises on access
            raise RuntimeError("boom")

    assert providers.single_file_diff_requested(_Broken(), force_full_content=False) is False


# ---------------------------------------------------------------------------
# schema_capability reader
# ---------------------------------------------------------------------------


def test_ctx_schema_capability_reads_routing_intent():
    ctx = _Ctx(schema_capability="full_content_and_diff")
    assert providers._ctx_schema_capability(ctx) == "full_content_and_diff"


def test_ctx_schema_capability_defaults_conservative():
    assert providers._ctx_schema_capability(object()) == "full_content_only"
