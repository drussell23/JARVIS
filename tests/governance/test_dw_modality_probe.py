"""Phase 12 Slice G — modality probe + metadata extraction tests.

Pins:
  §1 Metadata extraction — explicit endpoints field
  §2 Metadata extraction — capabilities token in chat list
  §3 Metadata extraction — token in non-chat list
  §4 Metadata extraction — both signals → ambiguous (None)
  §5 Metadata extraction — no recognized fields → None
  §6 Metadata extraction — NEVER reads model_id (§12 Slice F invariant)
  §7 Metadata extraction — dict-shaped capabilities ({chat: true})
  §8 Micro-probe — 200 → CHAT_CAPABLE
  §9 Micro-probe — 4xx + modality marker → NON_CHAT
  §10 Micro-probe — 4xx without marker → UNKNOWN (don't permanently exclude)
  §11 Micro-probe — 401/403 → UNKNOWN auth_failure
  §12 Micro-probe — 5xx/timeout/transport → UNKNOWN transient
  §13 Micro-probe — body excerpts captured for audit
  §14 verify_catalog_modalities end-to-end — metadata + probe split
  §15 verify_catalog_modalities — skips already-classified models
  §16 verify_catalog_modalities — catalog refresh resets stale verdicts
  §17 Source-level pin — no model_id regex anywhere in probe code
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, List, Optional  # noqa: F401
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.dw_catalog_client import (
    CatalogSnapshot, ModelCard,
)
from backend.core.ouroboros.governance.dw_modality_ledger import (
    ModalityLedger,
    VERDICT_CHAT_CAPABLE,
    VERDICT_NON_CHAT,
    VERDICT_UNKNOWN,
)
from backend.core.ouroboros.governance import dw_modality_probe as dmp
from backend.core.ouroboros.governance.dw_modality_probe import (
    ProbeResult,
    extract_metadata_verdict,
    micro_probe,
    verify_catalog_modalities,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_ledger(tmp_path: Path,
                    monkeypatch: pytest.MonkeyPatch) -> ModalityLedger:
    monkeypatch.setenv(
        "JARVIS_DW_MODALITY_LEDGER_PATH",
        str(tmp_path / "modality.json"),
    )
    return ModalityLedger()


def _card(model_id: str = "vendor/m-7B",
          *,
          raw: Optional[dict] = None,
          params_b: float = 7.0,
          out_price: float = 0.10) -> ModelCard:
    return ModelCard(
        model_id=model_id,
        family=model_id.split("/")[0] if "/" in model_id else "unknown",
        parameter_count_b=params_b,
        context_window=None,
        pricing_in_per_m_usd=None,
        pricing_out_per_m_usd=out_price,
        supports_streaming=True,
        raw_metadata_json=json.dumps(raw or {}),
    )


def _snapshot(*models: ModelCard) -> CatalogSnapshot:
    return CatalogSnapshot(fetched_at_unix=1.0, models=tuple(models))


def _mock_session(json_body: Any = None, status: int = 200,
                  raise_exc: Optional[Exception] = None,
                  text_body: Optional[str] = None) -> Any:
    session = MagicMock()

    class _Resp:
        def __init__(self) -> None:
            self.status = status

        async def __aenter__(self) -> "_Resp":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def text(self) -> str:
            if text_body is not None:
                return text_body
            if isinstance(json_body, str):
                return json_body
            return json.dumps(json_body or {})

        async def json(self) -> Any:
            return json_body

    def _post(url: str, **kwargs: Any) -> Any:  # noqa: ARG001
        if raise_exc is not None:
            raise raise_exc
        return _Resp()

    session.post = _post
    return session


# ---------------------------------------------------------------------------
# §1 — Endpoints field
# ---------------------------------------------------------------------------


def test_metadata_endpoints_chat_completions_explicit() -> None:
    card = _card(raw={"endpoints": ["/v1/chat/completions"]})
    assert extract_metadata_verdict(card) is True


def test_metadata_endpoints_short_chat_string() -> None:
    card = _card(raw={"endpoints": ["chat"]})
    assert extract_metadata_verdict(card) is True


# ---------------------------------------------------------------------------
# §2 — Chat-capable token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", [
    "chat", "chat_completion", "text-generation",
    "conversational", "instruct",
])
def test_metadata_capabilities_chat_tokens(token: str) -> None:
    card = _card(raw={"capabilities": [token]})
    assert extract_metadata_verdict(card) is True


@pytest.mark.parametrize("field", ["task", "pipeline_tag"])
def test_metadata_task_field_text_generation(field: str) -> None:
    card = _card(raw={field: "text-generation"})
    assert extract_metadata_verdict(card) is True


# ---------------------------------------------------------------------------
# §3 — Non-chat tokens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", [
    "embedding", "embeddings", "feature-extraction",
    "image-classification", "ocr", "vision-only",
    "speech-recognition", "fill-mask",
])
def test_metadata_non_chat_tokens(token: str) -> None:
    card = _card(raw={"capabilities": [token]})
    assert extract_metadata_verdict(card) is False


# ---------------------------------------------------------------------------
# §4 — Ambiguous (both signals)
# ---------------------------------------------------------------------------


def test_metadata_both_signals_returns_none() -> None:
    """When metadata says both 'chat' AND 'embedding', defer to probe."""
    card = _card(raw={"capabilities": ["chat", "embedding"]})
    assert extract_metadata_verdict(card) is None


# ---------------------------------------------------------------------------
# §5 — No recognized fields
# ---------------------------------------------------------------------------


def test_metadata_empty_returns_none() -> None:
    card = _card(raw={})
    assert extract_metadata_verdict(card) is None


def test_metadata_unknown_fields_returns_none() -> None:
    card = _card(raw={"random_field": "weird_value"})
    assert extract_metadata_verdict(card) is None


def test_metadata_invalid_json_returns_none() -> None:
    card = ModelCard(
        model_id="vendor/m-7B", family="vendor",
        parameter_count_b=7.0, context_window=None,
        pricing_in_per_m_usd=None, pricing_out_per_m_usd=None,
        supports_streaming=True,
        raw_metadata_json="{invalid json",
    )
    assert extract_metadata_verdict(card) is None


# ---------------------------------------------------------------------------
# §6 — NEVER reads model_id
# ---------------------------------------------------------------------------


def test_extract_metadata_does_not_read_model_id() -> None:
    """Source-level pin: extract_metadata_verdict must NOT read
    card.model_id for inference. Operator-rejected Zero-Order shortcut."""
    src = inspect.getsource(extract_metadata_verdict)
    assert "card.model_id" not in src, (
        "extract_metadata_verdict must NOT read card.model_id — "
        "verdict comes from raw_metadata_json fields only"
    )


def test_metadata_id_with_embedding_substring_returns_none_when_metadata_clean() -> None:
    """Even when model_id contains 'embedding', if metadata says
    chat capability, verdict is True. The id is NEVER the signal."""
    card = _card(
        model_id="vendor/embedding-helper-7B",
        raw={"capabilities": ["chat"]},
    )
    assert extract_metadata_verdict(card) is True


def test_metadata_id_chat_substring_returns_false_when_metadata_says_embedding() -> None:
    """Inverse: model_id has 'chat' but metadata says embedding.
    Metadata wins."""
    card = _card(
        model_id="vendor/chat-friendly-embedding-8B",
        raw={"task": "feature-extraction"},
    )
    assert extract_metadata_verdict(card) is False


# ---------------------------------------------------------------------------
# §7 — Dict-shaped capabilities
# ---------------------------------------------------------------------------


def test_metadata_dict_capabilities_chat_true() -> None:
    card = _card(raw={"capabilities": {"chat": True, "embedding": False}})
    assert extract_metadata_verdict(card) is True


def test_metadata_dict_capabilities_only_embedding() -> None:
    card = _card(raw={"capabilities": {"embedding": True}})
    assert extract_metadata_verdict(card) is False


# ---------------------------------------------------------------------------
# §8 — Micro-probe: 200 → CHAT_CAPABLE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_micro_probe_200_chat_capable() -> None:
    session = _mock_session(json_body={"choices": [{"message": {"content": "1"}}]})
    result = await micro_probe(
        session=session,
        base_url="https://api.example.com",
        api_key="test",
        model_id="vendor/m-7B",
    )
    assert result.verdict == VERDICT_CHAT_CAPABLE
    assert result.status_code == 200
    assert result.failure_reason is None


# ---------------------------------------------------------------------------
# §9 — 4xx + marker → NON_CHAT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 422])
@pytest.mark.parametrize("marker", [
    "model does not support chat",
    "embedding only",
    "task mismatch",
])
async def test_micro_probe_4xx_with_marker_non_chat(
    status: int, marker: str,
) -> None:
    session = _mock_session(
        text_body=f'{{"error": "{marker}"}}',
        status=status,
    )
    result = await micro_probe(
        session=session,
        base_url="https://api.example.com",
        api_key="test",
        model_id="vendor/embed-8B",
    )
    assert result.verdict == VERDICT_NON_CHAT
    assert result.status_code == status
    assert marker in result.response_body_excerpt


# ---------------------------------------------------------------------------
# §10 — 4xx without marker → UNKNOWN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_micro_probe_400_without_marker_unknown() -> None:
    """A 400 about bad max_tokens should NOT be classified as NON_CHAT —
    that would permanently kill an otherwise-healthy model."""
    session = _mock_session(
        text_body='{"error": "max_tokens must be <= 8192"}',
        status=400,
    )
    result = await micro_probe(
        session=session,
        base_url="https://api.example.com",
        api_key="test",
        model_id="vendor/m-7B",
    )
    assert result.verdict == VERDICT_UNKNOWN
    assert result.failure_reason and "no_marker" in result.failure_reason


# ---------------------------------------------------------------------------
# §11 — Auth failures → UNKNOWN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_micro_probe_auth_unknown(status: int) -> None:
    """Auth failure = our credential problem, not the model's fault."""
    session = _mock_session(
        text_body='{"error": "unauthorized"}',
        status=status,
    )
    result = await micro_probe(
        session=session,
        base_url="https://api.example.com",
        api_key="bad-key",
        model_id="vendor/m-7B",
    )
    assert result.verdict == VERDICT_UNKNOWN
    assert result.failure_reason == f"auth_{status}"


# ---------------------------------------------------------------------------
# §12 — Transient failures → UNKNOWN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_micro_probe_transient_unknown(status: int) -> None:
    session = _mock_session(text_body="server error", status=status)
    result = await micro_probe(
        session=session,
        base_url="https://api.example.com",
        api_key="test",
        model_id="vendor/m-7B",
    )
    assert result.verdict == VERDICT_UNKNOWN
    assert result.failure_reason and "transient" in result.failure_reason


@pytest.mark.asyncio
async def test_micro_probe_timeout_unknown() -> None:
    session = _mock_session(raise_exc=asyncio.TimeoutError())
    result = await micro_probe(
        session=session,
        base_url="https://api.example.com",
        api_key="test",
        model_id="vendor/m-7B",
    )
    assert result.verdict == VERDICT_UNKNOWN
    assert result.failure_reason == "timeout"


@pytest.mark.asyncio
async def test_micro_probe_transport_exception_unknown() -> None:
    session = _mock_session(raise_exc=RuntimeError("conn refused"))
    result = await micro_probe(
        session=session,
        base_url="https://api.example.com",
        api_key="test",
        model_id="vendor/m-7B",
    )
    assert result.verdict == VERDICT_UNKNOWN
    assert "RuntimeError" in (result.failure_reason or "")


# ---------------------------------------------------------------------------
# §13 — Body excerpts captured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_micro_probe_captures_body_excerpt() -> None:
    body = "model does not support chat — try /v1/embeddings"
    session = _mock_session(text_body=body, status=400)
    result = await micro_probe(
        session=session,
        base_url="https://api.example.com",
        api_key="test",
        model_id="vendor/embed-8B",
    )
    assert "does not support chat" in result.response_body_excerpt


# ---------------------------------------------------------------------------
# §14 — End-to-end verify_catalog_modalities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_catalog_metadata_split(
    isolated_ledger: ModalityLedger,
) -> None:
    """Catalog with mixed signals: some models decided by metadata,
    others probed."""
    snap = _snapshot(
        # Metadata says chat → no probe needed
        _card("v/chat-7B", raw={"capabilities": ["chat"]}),
        # Metadata says embedding → no probe needed
        _card("v/embed-8B", raw={"task": "feature-extraction"}),
        # Ambiguous → needs probe
        _card("v/unknown-3B", raw={}),
    )
    # Probe path: returns 200 → CHAT_CAPABLE
    session = _mock_session(json_body={"choices": []})
    result = await verify_catalog_modalities(
        snapshot=snap,
        ledger=isolated_ledger,
        session=session,
        base_url="https://api.example.com",
        api_key="test",
        catalog_snapshot_id="snapshot-A",
    )
    assert result.metadata_verdicts == 2
    assert result.probes_fired == 1
    assert result.probes_succeeded == 1

    assert isolated_ledger.is_chat_capable("v/chat-7B")
    assert isolated_ledger.is_non_chat("v/embed-8B")
    assert isolated_ledger.is_chat_capable("v/unknown-3B")


# ---------------------------------------------------------------------------
# §15 — Skips already-classified
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_skips_already_classified(
    isolated_ledger: ModalityLedger,
) -> None:
    """If ledger already has a verdict for this snapshot_id, don't
    re-probe."""
    isolated_ledger.record_metadata_verdict(
        "v/chat-7B", is_chat_capable=True,
        catalog_snapshot_id="snapshot-A",
    )
    snap = _snapshot(_card("v/chat-7B", raw={}))
    session = _mock_session(json_body={"choices": []})
    result = await verify_catalog_modalities(
        snapshot=snap,
        ledger=isolated_ledger,
        session=session,
        base_url="https://api.example.com",
        api_key="test",
        catalog_snapshot_id="snapshot-A",
    )
    assert result.skipped_already_known == 1
    assert result.probes_fired == 0


# ---------------------------------------------------------------------------
# §16 — Catalog refresh resets stale verdicts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_catalog_refresh_resets_stale_verdicts(
    isolated_ledger: ModalityLedger,
) -> None:
    """Running verify with a NEW snapshot_id drops stale verdicts —
    the ambiguous model gets re-probed."""
    isolated_ledger.record_metadata_verdict(
        "v/m-7B", is_chat_capable=True,
        catalog_snapshot_id="snapshot-A",
    )
    snap = _snapshot(_card("v/m-7B", raw={}))  # ambiguous metadata now
    session = _mock_session(
        text_body='{"error": "model does not support chat"}',
        status=400,
    )
    result = await verify_catalog_modalities(
        snapshot=snap,
        ledger=isolated_ledger,
        session=session,
        base_url="https://api.example.com",
        api_key="test",
        catalog_snapshot_id="snapshot-B",  # NEW snapshot
    )
    # Stale verdict was dropped, then ambiguous metadata triggered probe,
    # which returned NON_CHAT
    assert result.probes_rejected == 1
    assert isolated_ledger.is_non_chat("v/m-7B")


# ---------------------------------------------------------------------------
# §17 — Source-level pin: no model_id regex anywhere
# ---------------------------------------------------------------------------


def test_no_regex_module_imports_in_probe() -> None:
    """Operator-mandated Slice G: capability/modality decisions MUST
    NOT pattern-match on model_id. The probe module must not import
    re or fnmatch — verdicts come from metadata fields + server
    responses, not regex on ids.

    Allowed: model_id is passed THROUGH as a parameter (the request
    URL has to include it). What's banned is regex/substring-matching
    against the id to derive verdicts."""
    src = inspect.getsource(dmp)
    # Strip docstrings + comments via simple line-level filter so the
    # check focuses on executable code
    code_lines = []
    in_docstring = False
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            quote_count = stripped.count('"""') + stripped.count("'''")
            if quote_count == 1:
                in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        if stripped.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)
    # The regex module must not be imported
    assert "import re\n" not in code and "import re " not in code, (
        "dw_modality_probe must not import 're' — verdicts come from "
        "metadata fields and server responses, not regex on ids"
    )
    assert "import fnmatch" not in code


def test_extract_metadata_does_not_use_model_id_for_inference() -> None:
    """The inference function specifically must not read model_id
    for verdict logic. (It's fine to receive a card argument; the
    pin is that model_id strings aren't substring-matched.)"""
    src = inspect.getsource(extract_metadata_verdict)
    # No regex on identifier
    assert "import re" not in src
    assert "fnmatch" not in src
    # No comparing the id to known patterns
    assert ".model_id ==" not in src
    assert ".model_id.endswith" not in src
    assert ".model_id.startswith" not in src
    assert ".model_id.lower()" not in src