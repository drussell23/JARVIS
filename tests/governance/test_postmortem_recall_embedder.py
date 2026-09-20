"""PostmortemRecall can actually embed.

It built its embedder as ``_Embedder(model_name=_embedder_name())`` -- and
``_embedder_name()`` returns the embedder MODE ("fastembed"/"stdlib"), not a
model. fastembed was asked for a model called "fastembed" and refused, on every
call since the module was written. Recall's only possible answer was
``matched=0``, and the loader's log line ("... until dep installed") sent two
investigations after a missing package that was installed all along.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import semantic_index as si
from backend.core.ouroboros.governance.postmortem_recall import PostmortemRecallService


def _service(tmp_path):
    return PostmortemRecallService(
        sessions_dir=tmp_path / "sessions", semantic_index=None,
        ledger_path=tmp_path / "recall.jsonl",
    )


def test_the_embedder_is_built_by_the_factory_not_from_the_mode_string(
    tmp_path, monkeypatch,
):
    asked_for = []

    class _Spy:
        disabled = False

        def embed(self, texts):
            return [[1.0] for _ in texts]

    def _factory(*args, **kwargs):
        asked_for.append((args, kwargs))
        return _Spy()

    monkeypatch.setattr(si, "_embedder_factory", _factory)
    emb = _service(tmp_path)._ensure_embedder()
    assert emb is not None and asked_for, "recall bypassed the embedder factory"
    assert "fastembed" not in str(asked_for), (
        "the embedder MODE was passed along as if it were a model name"
    )


def test_recall_embeds_with_no_network_and_no_fastembed(tmp_path, monkeypatch):
    """The stdlib mode is the offline contract: it must yield real vectors."""
    monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "stdlib")
    emb = _service(tmp_path)._ensure_embedder()
    assert emb is not None
    vectors = emb.embed(["AttributeError: module has no attribute foo"])
    assert vectors and len(vectors[0]) > 0


@pytest.mark.parametrize("mode", ["fastembed", "stdlib", "martian"])
def test_no_mode_string_is_ever_used_as_a_model_name(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", mode)
    seen = []
    real = si._Embedder.__init__

    def _init(self, model_name="BAAI/bge-small-en-v1.5"):
        seen.append(model_name)
        real(self, model_name)

    monkeypatch.setattr(si._Embedder, "__init__", _init)
    _service(tmp_path)._ensure_embedder()
    assert mode not in seen
