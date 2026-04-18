"""Regression spine for the ``Attachment`` substrate on ``OperationContext``.

Pins every invariant enumerated in the VisionSensor + Visual VERIFY design
spec (``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``).
The I7 *export-ban* CI check (unauthorized readers of ``ctx.attachments``)
lives in ``test_attachment_export_ban.py`` — this file exclusively exercises
the data type and its integration with the hash-chained ``OperationContext``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import time
from datetime import datetime, timezone

import pytest

from backend.core.ouroboros.governance.op_context import (
    _ATTACHMENT_MAX_APP_ID_LEN,
    _ATTACHMENT_MAX_IMAGE_BYTES_DEFAULT,
    _ATTACHMENT_MAX_PATH_LEN,
    _ATTACHMENT_MAX_PER_CTX,
    _VALID_ATTACHMENT_KINDS,
    _VALID_ATTACHMENT_MIMES,
    Attachment,
    OperationContext,
    OperationPhase,
)


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _make_image(tmp_path, name: str = "frame.png", contents: bytes = _PNG_MAGIC) -> str:
    p = tmp_path / name
    p.write_bytes(contents)
    return str(p)


def _make_attachment(
    tmp_path,
    *,
    kind: str = "sensor_frame",
    app_id=None,
    contents: bytes = _PNG_MAGIC,
    name: str = "frame.png",
) -> Attachment:
    path = _make_image(tmp_path, name=name, contents=contents)
    return Attachment.from_file(path, kind=kind, app_id=app_id)


# ---------------------------------------------------------------------------
# Frozen + basic construction
# ---------------------------------------------------------------------------


def test_attachment_is_frozen(tmp_path):
    a = _make_attachment(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.kind = "pre_apply"  # type: ignore[misc]


def test_attachment_is_hashable(tmp_path):
    a = _make_attachment(tmp_path)
    # Frozen dataclasses with hashable fields are hashable by default.
    assert {a, a} == {a}


# ---------------------------------------------------------------------------
# kind whitelist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(_VALID_ATTACHMENT_KINDS))
def test_attachment_each_valid_kind_accepted(tmp_path, kind):
    path = _make_image(tmp_path)
    a = Attachment.from_file(path, kind=kind)
    assert a.kind == kind


@pytest.mark.parametrize("bad_kind", ["", "foo", "PRE_APPLY", "pre-apply", "screenshot"])
def test_attachment_invalid_kind_rejected(tmp_path, bad_kind):
    path = _make_image(tmp_path)
    with pytest.raises(ValueError, match="kind"):
        Attachment(
            kind=bad_kind,
            image_path=path,
            mime_type="image/png",
            hash8="abcd1234",
            ts=0.0,
        )


# ---------------------------------------------------------------------------
# image_path validation
# ---------------------------------------------------------------------------


def test_attachment_relative_path_rejected():
    with pytest.raises(ValueError, match="absolute"):
        Attachment(
            kind="sensor_frame",
            image_path="relative/path.png",
            mime_type="image/png",
            hash8="abcd1234",
            ts=0.0,
        )


def test_attachment_empty_path_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        Attachment(
            kind="sensor_frame",
            image_path="",
            mime_type="image/png",
            hash8="abcd1234",
            ts=0.0,
        )


def test_attachment_path_length_capped():
    long_path = "/" + "a" * _ATTACHMENT_MAX_PATH_LEN
    with pytest.raises(ValueError, match="exceeds"):
        Attachment(
            kind="sensor_frame",
            image_path=long_path,
            mime_type="image/png",
            hash8="abcd1234",
            ts=0.0,
        )


# ---------------------------------------------------------------------------
# mime_type whitelist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mime", list(_VALID_ATTACHMENT_MIMES))
def test_attachment_each_valid_mime_accepted(mime):
    a = Attachment(
        kind="sensor_frame",
        image_path="/tmp/x",
        mime_type=mime,
        hash8="abcd1234",
        ts=0.0,
    )
    assert a.mime_type == mime


@pytest.mark.parametrize(
    "bad_mime", ["image/gif", "application/pdf", "", "IMAGE/PNG", "image/jpg"]
)
def test_attachment_invalid_mime_rejected(bad_mime):
    with pytest.raises(ValueError, match="mime_type"):
        Attachment(
            kind="sensor_frame",
            image_path="/tmp/x",
            mime_type=bad_mime,
            hash8="abcd1234",
            ts=0.0,
        )


# ---------------------------------------------------------------------------
# hash8 validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_hash",
    [
        "",              # empty
        "short",         # too short
        "abcdef123",     # too long (9)
        "abcdefg1",      # non-hex
        "ABCD1234",      # uppercase
        "Abcd1234",      # mixed case
        " bcd1234",      # leading space
    ],
)
def test_attachment_hash8_invalid_rejected(bad_hash):
    with pytest.raises(ValueError, match="hash8"):
        Attachment(
            kind="sensor_frame",
            image_path="/tmp/x",
            mime_type="image/png",
            hash8=bad_hash,
            ts=0.0,
        )


def test_attachment_hash8_exactly_8_lowercase_hex_ok():
    a = Attachment(
        kind="sensor_frame",
        image_path="/tmp/x",
        mime_type="image/png",
        hash8="0123abcd",
        ts=0.0,
    )
    assert a.hash8 == "0123abcd"


# ---------------------------------------------------------------------------
# ts validation
# ---------------------------------------------------------------------------


def test_attachment_negative_ts_rejected():
    with pytest.raises(ValueError, match="ts"):
        Attachment(
            kind="sensor_frame",
            image_path="/tmp/x",
            mime_type="image/png",
            hash8="abcd1234",
            ts=-0.001,
        )


def test_attachment_zero_ts_accepted():
    a = Attachment(
        kind="sensor_frame",
        image_path="/tmp/x",
        mime_type="image/png",
        hash8="abcd1234",
        ts=0.0,
    )
    assert a.ts == 0.0


# ---------------------------------------------------------------------------
# app_id validation
# ---------------------------------------------------------------------------


def test_attachment_app_id_empty_string_rejected():
    with pytest.raises(ValueError, match="app_id"):
        Attachment(
            kind="sensor_frame",
            image_path="/tmp/x",
            mime_type="image/png",
            hash8="abcd1234",
            ts=0.0,
            app_id="",
        )


def test_attachment_app_id_length_capped():
    with pytest.raises(ValueError, match="app_id"):
        Attachment(
            kind="sensor_frame",
            image_path="/tmp/x",
            mime_type="image/png",
            hash8="abcd1234",
            ts=0.0,
            app_id="a" * (_ATTACHMENT_MAX_APP_ID_LEN + 1),
        )


def test_attachment_app_id_none_is_default():
    a = Attachment(
        kind="sensor_frame",
        image_path="/tmp/x",
        mime_type="image/png",
        hash8="abcd1234",
        ts=0.0,
    )
    assert a.app_id is None


# ---------------------------------------------------------------------------
# Redaction-safe repr / str
# ---------------------------------------------------------------------------


def test_attachment_repr_redacts_full_path(tmp_path):
    a = _make_attachment(tmp_path, name="sensitive_file_in_home.png")
    r = repr(a)
    # Full path not present; basename present under redacted prefix
    assert str(tmp_path) not in r
    assert "<redacted:basename=" in r
    assert "sensitive_file_in_home.png" in r


def test_attachment_str_redacts_full_path(tmp_path):
    a = _make_attachment(tmp_path, name="sensitive_file_in_home.png")
    # __str__ is aliased to __repr__ — same redaction contract
    assert str(a) == repr(a)
    assert str(tmp_path) not in str(a)


# ---------------------------------------------------------------------------
# from_file — canonical constructor
# ---------------------------------------------------------------------------


def test_from_file_computes_hash8_correctly(tmp_path):
    payload = b"deterministic bytes"
    path = _make_image(tmp_path, name="det.png", contents=payload)
    a = Attachment.from_file(path, kind="sensor_frame")
    expected = hashlib.sha256(payload).hexdigest()[:8]
    assert a.hash8 == expected


def test_from_file_infers_mime_jpeg(tmp_path):
    for ext in (".jpg", ".jpeg", ".JPG", ".JPEG"):
        path = _make_image(tmp_path, name=f"f{ext}", contents=_JPEG_MAGIC)
        a = Attachment.from_file(path, kind="sensor_frame")
        assert a.mime_type == "image/jpeg", ext


def test_from_file_infers_mime_png(tmp_path):
    for ext in (".png", ".PNG"):
        path = _make_image(tmp_path, name=f"f{ext}", contents=_PNG_MAGIC)
        a = Attachment.from_file(path, kind="sensor_frame")
        assert a.mime_type == "image/png", ext


def test_from_file_infers_mime_webp(tmp_path):
    path = _make_image(tmp_path, name="f.webp", contents=b"RIFFxxxxWEBP" + b"\x00" * 16)
    a = Attachment.from_file(path, kind="sensor_frame")
    assert a.mime_type == "image/webp"


def test_from_file_rejects_missing_path(tmp_path):
    with pytest.raises(FileNotFoundError, match="missing"):
        Attachment.from_file(str(tmp_path / "nope.png"), kind="sensor_frame")


def test_from_file_rejects_relative_path():
    with pytest.raises(ValueError, match="absolute"):
        Attachment.from_file("relative.png", kind="sensor_frame")


def test_from_file_rejects_empty_path():
    with pytest.raises(ValueError, match="non-empty"):
        Attachment.from_file("", kind="sensor_frame")


def test_from_file_rejects_unsupported_extension(tmp_path):
    path = _make_image(tmp_path, name="f.gif", contents=b"GIF89a")
    with pytest.raises(ValueError, match="unsupported extension"):
        Attachment.from_file(path, kind="sensor_frame")


def test_from_file_uses_monotonic_ts_default(tmp_path):
    path = _make_image(tmp_path)
    before = time.monotonic()
    a = Attachment.from_file(path, kind="sensor_frame")
    after = time.monotonic()
    assert before <= a.ts <= after


def test_from_file_accepts_explicit_ts(tmp_path):
    path = _make_image(tmp_path)
    a = Attachment.from_file(path, kind="sensor_frame", ts=123.456)
    assert a.ts == 123.456


def test_from_file_propagates_invalid_kind(tmp_path):
    path = _make_image(tmp_path)
    with pytest.raises(ValueError, match="kind"):
        Attachment.from_file(path, kind="bogus")


# ---------------------------------------------------------------------------
# read_bytes
# ---------------------------------------------------------------------------


def test_read_bytes_returns_expected_content(tmp_path):
    payload = b"hello world"
    path = _make_image(tmp_path, contents=payload)
    a = Attachment.from_file(path, kind="sensor_frame")
    assert a.read_bytes() == payload


def test_read_bytes_enforces_size_cap(tmp_path):
    payload = b"x" * 200
    path = _make_image(tmp_path, contents=payload)
    a = Attachment.from_file(path, kind="sensor_frame")
    with pytest.raises(ValueError, match="exceeds cap"):
        a.read_bytes(max_bytes=50)


def test_read_bytes_default_cap_is_10mib():
    # Sanity check module-level default matches spec
    assert _ATTACHMENT_MAX_IMAGE_BYTES_DEFAULT == 10 * 1024 * 1024


def test_read_bytes_raises_on_missing_file(tmp_path):
    path = _make_image(tmp_path)
    a = Attachment.from_file(path, kind="sensor_frame")
    os.unlink(path)
    with pytest.raises(FileNotFoundError, match="disappeared"):
        a.read_bytes()


def test_read_bytes_rejects_nonpositive_cap(tmp_path):
    path = _make_image(tmp_path)
    a = Attachment.from_file(path, kind="sensor_frame")
    with pytest.raises(ValueError, match="positive"):
        a.read_bytes(max_bytes=0)
    with pytest.raises(ValueError, match="positive"):
        a.read_bytes(max_bytes=-1)


# ---------------------------------------------------------------------------
# read_bytes_verified — integrity check
# ---------------------------------------------------------------------------


def test_read_bytes_verified_passes_when_unchanged(tmp_path):
    payload = b"stable bytes"
    path = _make_image(tmp_path, contents=payload)
    a = Attachment.from_file(path, kind="sensor_frame")
    assert a.read_bytes_verified() == payload


def test_read_bytes_verified_detects_file_rotation(tmp_path):
    payload = b"original"
    path = _make_image(tmp_path, contents=payload)
    a = Attachment.from_file(path, kind="sensor_frame")
    # Rotate file content — hash8 should no longer match
    with open(path, "wb") as fh:
        fh.write(b"tampered content")
    with pytest.raises(ValueError, match="integrity check failed"):
        a.read_bytes_verified()


# ---------------------------------------------------------------------------
# OperationContext integration — hash-chain invariants
# ---------------------------------------------------------------------------


def _make_ctx(attachments: tuple = ()) -> OperationContext:
    return OperationContext.create(
        target_files=("backend/x.py",),
        description="test op",
        attachments=attachments,
    )


def test_ctx_default_attachments_is_empty_tuple():
    ctx = _make_ctx()
    assert ctx.attachments == ()
    assert isinstance(ctx.attachments, tuple)


def test_ctx_create_accepts_attachments(tmp_path):
    a = _make_attachment(tmp_path)
    ctx = _make_ctx(attachments=(a,))
    assert ctx.attachments == (a,)


def test_ctx_create_rejects_too_many_attachments(tmp_path):
    atts = tuple(
        _make_attachment(tmp_path, name=f"f{i}.png") for i in range(_ATTACHMENT_MAX_PER_CTX + 1)
    )
    with pytest.raises(ValueError, match="at most"):
        _make_ctx(attachments=atts)


def test_ctx_with_attachments_hash_changes(tmp_path):
    ctx = _make_ctx()
    before_hash = ctx.context_hash
    a = _make_attachment(tmp_path)
    ctx2 = ctx.with_attachments((a,))
    assert ctx2.context_hash != before_hash
    assert ctx2.previous_hash == before_hash
    assert ctx2.attachments == (a,)


def test_ctx_with_attachments_phase_unchanged(tmp_path):
    ctx = _make_ctx()
    a = _make_attachment(tmp_path)
    ctx2 = ctx.with_attachments((a,))
    assert ctx2.phase == ctx.phase == OperationPhase.CLASSIFY


def test_ctx_with_attachments_rejects_too_many(tmp_path):
    ctx = _make_ctx()
    atts = tuple(
        _make_attachment(tmp_path, name=f"f{i}.png") for i in range(_ATTACHMENT_MAX_PER_CTX + 1)
    )
    with pytest.raises(ValueError, match="at most"):
        ctx.with_attachments(atts)


def test_ctx_with_attachments_rejects_wrong_type(tmp_path):
    ctx = _make_ctx()
    with pytest.raises(TypeError, match="must be Attachment"):
        ctx.with_attachments(("not_an_attachment",))  # type: ignore[arg-type]


def test_ctx_with_attachments_coerces_list_to_tuple(tmp_path):
    ctx = _make_ctx()
    a = _make_attachment(tmp_path)
    ctx2 = ctx.with_attachments([a])  # type: ignore[arg-type]
    assert ctx2.attachments == (a,)
    assert isinstance(ctx2.attachments, tuple)


def test_ctx_add_attachment_appends(tmp_path):
    ctx = _make_ctx()
    a1 = _make_attachment(tmp_path, name="f1.png")
    a2 = _make_attachment(tmp_path, name="f2.png")
    ctx2 = ctx.add_attachment(a1).add_attachment(a2)
    assert ctx2.attachments == (a1, a2)


def test_ctx_hash_stable_when_attachments_identical(tmp_path):
    a = _make_attachment(tmp_path)
    # Two contexts built identically (same timestamp) should hash identically,
    # including the attachments tuple contribution.
    now = datetime.now(tz=timezone.utc)
    ctx1 = OperationContext.create(
        target_files=("backend/x.py",),
        description="test op",
        op_id="op-fixed",
        _timestamp=now,
        attachments=(a,),
    )
    ctx2 = OperationContext.create(
        target_files=("backend/x.py",),
        description="test op",
        op_id="op-fixed",
        _timestamp=now,
        attachments=(a,),
    )
    assert ctx1.context_hash == ctx2.context_hash


def test_ctx_hash_differs_when_attachments_differ(tmp_path):
    a1 = _make_attachment(tmp_path, name="a.png", contents=b"one")
    a2 = _make_attachment(tmp_path, name="b.png", contents=b"two")
    now = datetime.now(tz=timezone.utc)
    ctx_a = OperationContext.create(
        target_files=("backend/x.py",),
        description="test op",
        op_id="op-fixed",
        _timestamp=now,
        attachments=(a1,),
    )
    ctx_b = OperationContext.create(
        target_files=("backend/x.py",),
        description="test op",
        op_id="op-fixed",
        _timestamp=now,
        attachments=(a2,),
    )
    assert ctx_a.context_hash != ctx_b.context_hash


def test_ctx_advance_preserves_attachments(tmp_path):
    from backend.core.ouroboros.governance.op_context import OperationPhase as P

    a = _make_attachment(tmp_path)
    ctx = _make_ctx(attachments=(a,))
    ctx2 = ctx.advance(P.ROUTE)
    assert ctx2.attachments == (a,)
