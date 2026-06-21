from __future__ import annotations
from backend.core.ouroboros.governance.op_context import OperationContext


def test_prefetch_manifest_field_exists_with_tuple_default():
    fields = OperationContext.__dataclass_fields__
    assert "prefetch_manifest" in fields
    assert fields["prefetch_manifest"].default_factory is tuple


def test_prefetch_manifest_default_is_empty_tuple():
    """Freshly constructed context has prefetch_manifest == ()."""
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="test prefetch manifest default",
    )
    assert ctx.prefetch_manifest == ()
