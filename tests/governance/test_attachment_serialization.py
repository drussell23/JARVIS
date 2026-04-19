"""Regression spine for ``providers._serialize_attachments``.

Task 7 of the VisionSensor + Visual VERIFY implementation plan. Pins:

* **Purpose gate (I7 defense-in-depth)** — only ``sensor_classify`` /
  ``visual_verify`` unlock walking ``ctx.attachments``; every other
  purpose returns an empty list.
* **Route gate** — BG / SPEC routes strip attachments regardless of
  purpose (cost + correctness optimization; those routes target
  text-only models).
* **Provider-specific serialization** — Claude uses the Messages API
  ``{"type": "image", "source": {"type": "base64", ...}}`` block;
  DoubleWord and J-Prime use the OpenAI-compatible ``image_url`` +
  ``data:<mime>;base64,<b64>`` schema.
* **Per-attachment read gate** — missing / oversized / broken
  attachments are dropped individually (WARNING log) without taking
  down the whole call.
* **Back-compat** — callers that don't pass ``purpose=`` default to
  ``"generate"`` which is NOT in the allowed set → empty list.

Spec: ``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
§Invariant I7 + §Shared Substrate → Provider serialization.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.op_context import (
    Attachment,
    OperationContext,
)
from backend.core.ouroboros.governance.providers import (
    _ATTACHMENT_PROVIDER_KINDS,
    _ATTACHMENT_PURPOSES_ALLOWED,
    _ATTACHMENT_STRIPPED_ROUTES,
    _serialize_attachments,
)


_PAYLOAD = b"hello world"
_PAYLOAD_B64 = base64.b64encode(_PAYLOAD).decode("ascii")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_image(tmp_path: Path, name: str = "frame.png", payload: bytes = _PAYLOAD) -> str:
    p = tmp_path / name
    p.write_bytes(payload)
    return str(p)


def _make_ctx(
    *,
    tmp_path: Path,
    attachment_kind: str = "sensor_frame",
    attachment_count: int = 1,
    route: str = "",
) -> OperationContext:
    attachments = tuple(
        Attachment.from_file(
            _make_image(tmp_path, name=f"frame_{i}.png"),
            kind=attachment_kind,
        )
        for i in range(attachment_count)
    )
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="test op",
        attachments=attachments,
    )
    if route:
        # provider_route is stamped at ROUTE phase normally; for tests we
        # use dataclasses.replace via advance() semantics. Easier: just
        # construct with a manual dataclasses.replace to inject.
        import dataclasses
        ctx = dataclasses.replace(ctx, provider_route=route)
    return ctx


# ---------------------------------------------------------------------------
# Module-level constants pinned
# ---------------------------------------------------------------------------


def test_allowed_purposes_are_exactly_sensor_classify_and_visual_verify():
    assert _ATTACHMENT_PURPOSES_ALLOWED == frozenset(
        {"sensor_classify", "visual_verify"}
    )


def test_stripped_routes_are_bg_and_spec():
    assert _ATTACHMENT_STRIPPED_ROUTES == frozenset({"background", "speculative"})


def test_provider_kinds_pinned():
    assert _ATTACHMENT_PROVIDER_KINDS == frozenset(
        {"claude", "doubleword", "jprime"}
    )


# ---------------------------------------------------------------------------
# Purpose gate — I7 defense-in-depth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("purpose", sorted(_ATTACHMENT_PURPOSES_ALLOWED))
def test_allowed_purpose_materializes_attachments(tmp_path, purpose):
    ctx = _make_ctx(tmp_path=tmp_path)
    out = _serialize_attachments(ctx, provider_kind="claude", purpose=purpose)
    assert len(out) == 1


@pytest.mark.parametrize(
    "bad_purpose",
    [
        "generate",           # default GENERATE path
        "plan",               # PLAN phase
        "tool_round",         # Venom tool loop
        "validate",           # VALIDATE phase
        "approve",            # APPROVE phase
        "",                   # empty string
        "SENSOR_CLASSIFY",    # case mismatch — allowed set is lowercase exact
        "sensor_classify ",   # trailing whitespace
        "random_purpose",     # fabricated
    ],
)
def test_disallowed_purpose_returns_empty_list(tmp_path, bad_purpose):
    ctx = _make_ctx(tmp_path=tmp_path)
    out = _serialize_attachments(ctx, provider_kind="claude", purpose=bad_purpose)
    assert out == []


def test_default_purpose_is_generate_and_returns_empty(tmp_path):
    """Back-compat: callers that don't pass ``purpose=`` default to
    ``"generate"`` which is NOT in the allowed set, so attachments
    stay invisible."""
    ctx = _make_ctx(tmp_path=tmp_path)
    out = _serialize_attachments(ctx, provider_kind="claude")
    assert out == []


# ---------------------------------------------------------------------------
# Route gate — BG / SPEC strip regardless of purpose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", sorted(_ATTACHMENT_STRIPPED_ROUTES))
def test_bg_spec_routes_strip_attachments_even_with_allowed_purpose(tmp_path, route):
    ctx = _make_ctx(tmp_path=tmp_path, route=route)
    out = _serialize_attachments(
        ctx, provider_kind="claude", purpose="sensor_classify",
    )
    assert out == []


def test_route_matching_is_case_insensitive(tmp_path):
    ctx = _make_ctx(tmp_path=tmp_path, route="BACKGROUND")
    out = _serialize_attachments(
        ctx, provider_kind="claude", purpose="visual_verify",
    )
    assert out == []


@pytest.mark.parametrize("route", ["immediate", "standard", "complex"])
def test_non_stripped_routes_pass_attachments_through(tmp_path, route):
    ctx = _make_ctx(tmp_path=tmp_path, route=route)
    out = _serialize_attachments(
        ctx, provider_kind="claude", purpose="sensor_classify",
    )
    assert len(out) == 1


# ---------------------------------------------------------------------------
# Empty-attachments fast path
# ---------------------------------------------------------------------------


def test_empty_attachments_returns_empty_list(tmp_path):
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="no attachments",
    )
    assert ctx.attachments == ()
    out = _serialize_attachments(ctx, provider_kind="claude", purpose="sensor_classify")
    assert out == []


# ---------------------------------------------------------------------------
# Claude serialization shape
# ---------------------------------------------------------------------------


def test_claude_block_shape_png(tmp_path):
    ctx = _make_ctx(tmp_path=tmp_path)
    out = _serialize_attachments(
        ctx, provider_kind="claude", purpose="sensor_classify",
    )
    assert len(out) == 1
    block = out[0]
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    assert block["source"]["data"] == _PAYLOAD_B64


def test_claude_block_shape_jpeg(tmp_path):
    path = _make_image(tmp_path, name="photo.jpg", payload=_PAYLOAD)
    att = Attachment.from_file(path, kind="sensor_frame")
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="jpeg test",
        attachments=(att,),
    )
    out = _serialize_attachments(
        ctx, provider_kind="claude", purpose="visual_verify",
    )
    assert out[0]["source"]["media_type"] == "image/jpeg"


def test_claude_case_insensitive_provider_kind(tmp_path):
    ctx = _make_ctx(tmp_path=tmp_path)
    out = _serialize_attachments(
        ctx, provider_kind="CLAUDE", purpose="sensor_classify",
    )
    assert len(out) == 1
    assert out[0]["type"] == "image"


# ---------------------------------------------------------------------------
# DoubleWord / J-Prime serialization shape — OpenAI-compatible image_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["doubleword", "jprime"])
def test_openai_compatible_image_url_shape(tmp_path, provider):
    ctx = _make_ctx(tmp_path=tmp_path)
    out = _serialize_attachments(
        ctx, provider_kind=provider, purpose="sensor_classify",
    )
    assert len(out) == 1
    block = out[0]
    assert block["type"] == "image_url"
    assert block["image_url"]["url"] == f"data:image/png;base64,{_PAYLOAD_B64}"


@pytest.mark.parametrize("provider", ["DOUBLEWORD", "JPrime", " doubleword "])
def test_openai_compatible_provider_kind_normalization(tmp_path, provider):
    ctx = _make_ctx(tmp_path=tmp_path)
    out = _serialize_attachments(
        ctx, provider_kind=provider, purpose="visual_verify",
    )
    assert len(out) == 1
    assert out[0]["type"] == "image_url"


# ---------------------------------------------------------------------------
# Unknown provider kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["openai", "gpt-4", "", "claude.vision", "llava"])
def test_unknown_provider_kind_returns_empty(tmp_path, provider):
    ctx = _make_ctx(tmp_path=tmp_path)
    out = _serialize_attachments(
        ctx, provider_kind=provider, purpose="sensor_classify",
    )
    assert out == []


# ---------------------------------------------------------------------------
# Multiple attachments — preserved order
# ---------------------------------------------------------------------------


def test_multiple_attachments_serialized_in_order(tmp_path):
    a1 = Attachment.from_file(
        _make_image(tmp_path, "pre.png", payload=b"pre_apply_bytes"),
        kind="pre_apply",
    )
    a2 = Attachment.from_file(
        _make_image(tmp_path, "post.png", payload=b"post_apply_bytes"),
        kind="post_apply",
    )
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="pre+post",
        attachments=(a1, a2),
    )
    out = _serialize_attachments(
        ctx, provider_kind="claude", purpose="visual_verify",
    )
    assert len(out) == 2
    assert out[0]["source"]["data"] == base64.b64encode(b"pre_apply_bytes").decode("ascii")
    assert out[1]["source"]["data"] == base64.b64encode(b"post_apply_bytes").decode("ascii")


# ---------------------------------------------------------------------------
# Per-attachment read gate — broken attachment drops, rest survive
# ---------------------------------------------------------------------------


def test_missing_attachment_file_is_dropped_but_others_survive(tmp_path, caplog):
    """If a frame file is unlinked between construction and serialization,
    that single attachment is dropped (with a WARNING log) and the rest
    of the batch still makes it through.
    """
    # Two attachments: the first's file is about to disappear.
    a1 = Attachment.from_file(
        _make_image(tmp_path, "gone.png", payload=b"will_vanish"),
        kind="sensor_frame",
    )
    a2 = Attachment.from_file(
        _make_image(tmp_path, "survivor.png", payload=b"still_here"),
        kind="sensor_frame",
    )
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="broken attachment test",
        attachments=(a1, a2),
    )
    # Remove the first file to force FileNotFoundError on read.
    import os
    os.unlink(a1.image_path)

    with caplog.at_level("WARNING"):
        out = _serialize_attachments(
            ctx, provider_kind="claude", purpose="sensor_classify",
        )

    assert len(out) == 1
    assert out[0]["source"]["data"] == base64.b64encode(b"still_here").decode("ascii")
    # Warning log names the dropped hash8.
    assert any("drop attachment hash8=" in rec.message for rec in caplog.records)


def test_oversized_attachment_is_dropped(tmp_path):
    """An attachment whose file grew past the 10 MiB read cap drops
    gracefully — the rest of the batch (if any) survives."""
    att = Attachment.from_file(
        _make_image(tmp_path, "ok.png", payload=_PAYLOAD),
        kind="sensor_frame",
    )
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="overflow test",
        attachments=(att,),
    )
    # Blow the file up past the default 10 MiB cap.
    with open(att.image_path, "wb") as fh:
        fh.write(b"\x00" * (11 * 1024 * 1024))

    out = _serialize_attachments(
        ctx, provider_kind="claude", purpose="sensor_classify",
    )
    assert out == []


# ---------------------------------------------------------------------------
# Round-trip — decode base64 back to original bytes
# ---------------------------------------------------------------------------


def test_roundtrip_claude_block_decodes_to_original(tmp_path):
    payload = b"round trip me!"
    path = _make_image(tmp_path, "rt.png", payload=payload)
    att = Attachment.from_file(path, kind="sensor_frame")
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="roundtrip",
        attachments=(att,),
    )
    out = _serialize_attachments(
        ctx, provider_kind="claude", purpose="sensor_classify",
    )
    decoded = base64.b64decode(out[0]["source"]["data"])
    assert decoded == payload


def test_roundtrip_doubleword_data_uri_decodes_to_original(tmp_path):
    payload = b"doubleword roundtrip"
    path = _make_image(tmp_path, "dw.png", payload=payload)
    att = Attachment.from_file(path, kind="sensor_frame")
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="roundtrip",
        attachments=(att,),
    )
    out = _serialize_attachments(
        ctx, provider_kind="doubleword", purpose="sensor_classify",
    )
    url = out[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    b64 = url.split(",", 1)[1]
    assert base64.b64decode(b64) == payload
