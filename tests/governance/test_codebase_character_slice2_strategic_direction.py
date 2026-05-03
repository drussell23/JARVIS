"""CodebaseCharacterDigest Slice 2 — StrategicDirection wire-up.

Pins:
  * ``_render_codebase_character_section`` exists on
    ``StrategicDirectionService`` and mirrors the posture-section
    discipline (fail-silent, ImportError-safe, advisory-only)
  * ``format_for_prompt`` appends the codebase-character block AFTER
    the posture block (additive — never displaces existing sections)
  * Master flag default-False at Slice 2 (graduates in Slice 3)
  * Master-off → empty string → no section appended → byte-stable
    against pre-Slice-2 prompt
  * SemanticIndex empty / never-built → empty string (STALE_INDEX
    outcome → fail-open)
  * SemanticIndex with ≥ min_clusters READY clusters → section
    appended with ``## Codebase Character`` heading
  * BUG-FIX REGRESSION PIN: format_for_prompt body MUST contain the
    codebase-character render call so a refactor cannot silently
    delete the wire-up
  * Char budget honored — section never exceeds 1500 chars
  * No semantic_index rebuild triggered from the prompt path
    (discipline check via spy on .build())
"""
from __future__ import annotations

import ast
import inspect
import time
from dataclasses import dataclass
from typing import Any, Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance import strategic_direction as sd_mod
from backend.core.ouroboros.governance.codebase_character import (
    DigestOutcome,
)
from backend.core.ouroboros.governance.strategic_direction import (
    StrategicDirectionService,
)


# ---------------------------------------------------------------------------
# Helpers — synthetic SemanticIndex with controllable clusters/stats
# ---------------------------------------------------------------------------


@dataclass
class _FakeStats:
    built_at: float
    corpus_n: int
    cluster_mode: str = "kmeans"


@dataclass
class _FakeCluster:
    cluster_id: int
    kind: str
    size: int
    nearest_item_text: str
    nearest_item_source: str
    source_composition: Tuple[Tuple[str, int], ...]
    centroid_hash8: str


class _FakeSemanticIndex:
    """Stand-in for ``semantic_index.get_default_index()``.

    Tests inject controllable ``.clusters`` and ``.stats()``. Records
    whether ``.build()`` was called so we can pin "no rebuild from
    prompt path" discipline.
    """

    def __init__(
        self,
        clusters: Tuple[_FakeCluster, ...] = (),
        built_at: float = 0.0,
        corpus_n: int = 0,
        cluster_mode: str = "kmeans",
    ) -> None:
        self.clusters = clusters
        self._stats = _FakeStats(
            built_at=built_at,
            corpus_n=corpus_n,
            cluster_mode=cluster_mode,
        )
        self.build_call_count = 0

    def stats(self) -> _FakeStats:
        return self._stats

    def build(self, *, force: bool = False) -> bool:
        self.build_call_count += 1
        return True


def _mk_cluster(
    cid: int = 1, kind: str = "goal", size: int = 5,
    text: str = "Voice biometric authentication primitive",
    source: str = "git_commit",
    comp: Tuple[Tuple[str, int], ...] = (("git_commit", 4), ("goal", 1)),
    hash8: str = "deadbeef",
) -> _FakeCluster:
    return _FakeCluster(
        cluster_id=cid,
        kind=kind,
        size=size,
        nearest_item_text=text,
        nearest_item_source=source,
        source_composition=comp,
        centroid_hash8=hash8,
    )


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED",
        "JARVIS_CODEBASE_CHARACTER_MIN_CLUSTERS",
        "JARVIS_CODEBASE_CHARACTER_STALE_AFTER_S",
        "JARVIS_CODEBASE_CHARACTER_MAX_CLUSTERS_IN_DIGEST",
        "JARVIS_CODEBASE_CHARACTER_EXCERPT_MAX_CHARS",
        "JARVIS_DIRECTION_INFERRER_ENABLED",
        "JARVIS_POSTURE_PROMPT_INJECTION_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _make_service_with_digest(digest: str = "Test digest content"):
    """Construct a service with a non-empty digest so format_for_prompt
    yields the body (otherwise it short-circuits to empty)."""
    svc = StrategicDirectionService.__new__(StrategicDirectionService)
    svc._digest = digest
    svc._principles = []
    svc._manifesto = ""
    svc._architecture = []
    svc._git_themes = []
    return svc


# ---------------------------------------------------------------------------
# §A — Method exists + matches discipline
# ---------------------------------------------------------------------------


class TestMethodExists:
    def test_render_codebase_character_section_method_present(self):
        assert hasattr(
            StrategicDirectionService,
            "_render_codebase_character_section",
        )

    def test_method_is_static(self):
        # Same shape as _render_posture_section.
        attr = StrategicDirectionService.__dict__[
            "_render_codebase_character_section"
        ]
        assert isinstance(attr, staticmethod)

    def test_master_off_returns_empty(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED",
            raising=False,
        )
        rendered = (
            StrategicDirectionService
            ._render_codebase_character_section()
        )
        assert rendered == ""

    def test_master_off_explicit_returns_empty(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "false",
        )
        rendered = (
            StrategicDirectionService
            ._render_codebase_character_section()
        )
        assert rendered == ""


# ---------------------------------------------------------------------------
# §B — ImportError-safe (mirror of posture-section discipline)
# ---------------------------------------------------------------------------


class TestImportErrorSafe:
    def test_codebase_character_import_error_returns_empty(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        # Force ImportError on the codebase_character module.
        import sys
        with mock.patch.dict(
            sys.modules,
            {
                "backend.core.ouroboros.governance.codebase_character": None,  # noqa: E501
            },
        ):
            rendered = (
                StrategicDirectionService
                ._render_codebase_character_section()
            )
        assert rendered == ""

    def test_semantic_index_import_error_returns_empty(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        import sys
        with mock.patch.dict(
            sys.modules,
            {
                "backend.core.ouroboros.governance.semantic_index": None,  # noqa: E501
            },
        ):
            rendered = (
                StrategicDirectionService
                ._render_codebase_character_section()
            )
        assert rendered == ""


# ---------------------------------------------------------------------------
# §C — Fail-silent on any exception
# ---------------------------------------------------------------------------


class TestFailSilent:
    def test_get_default_index_raises_returns_empty(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            side_effect=RuntimeError("boom"),
        ):
            rendered = (
                StrategicDirectionService
                ._render_codebase_character_section()
            )
        assert rendered == ""

    def test_clusters_property_raises_returns_empty(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )

        class _Bomb:
            @property
            def clusters(self):
                raise RuntimeError("clusters boom")

            def stats(self):
                return _FakeStats(
                    built_at=time.time(), corpus_n=10,
                )

        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=_Bomb(),
        ):
            rendered = (
                StrategicDirectionService
                ._render_codebase_character_section()
            )
        assert rendered == ""


# ---------------------------------------------------------------------------
# §D — Digest outcomes → rendered string
# ---------------------------------------------------------------------------


class TestDigestOutcomes:
    def test_stale_index_returns_empty(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        # built_at=0 → STALE_INDEX → empty prompt section.
        fake = _FakeSemanticIndex(
            clusters=(_mk_cluster(1), _mk_cluster(2)),
            built_at=0.0, corpus_n=10,
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake,
        ):
            rendered = (
                StrategicDirectionService
                ._render_codebase_character_section()
            )
        assert rendered == ""

    def test_insufficient_clusters_returns_empty(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        # 1 cluster < default min_clusters=2 → INSUFFICIENT.
        fake = _FakeSemanticIndex(
            clusters=(_mk_cluster(1),),
            built_at=time.time(), corpus_n=5,
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake,
        ):
            rendered = (
                StrategicDirectionService
                ._render_codebase_character_section()
            )
        assert rendered == ""

    def test_ready_renders_section(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        fake = _FakeSemanticIndex(
            clusters=(
                _mk_cluster(
                    1, "goal", 5,
                    "Voice biometric authentication primitive",
                ),
                _mk_cluster(
                    2, "conversation", 3,
                    "Ghost hands UI automation layer",
                    comp=(("conversation", 3),),
                ),
            ),
            built_at=time.time(), corpus_n=20,
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake,
        ):
            rendered = (
                StrategicDirectionService
                ._render_codebase_character_section()
            )
        assert rendered.startswith("## Codebase Character")
        assert "voice biometric" in rendered.lower()
        assert "ghost hands" in rendered.lower()


# ---------------------------------------------------------------------------
# §E — format_for_prompt integration
# ---------------------------------------------------------------------------


class TestFormatForPromptIntegration:
    def test_master_off_byte_stable_against_pre_slice_2(
        self, monkeypatch,
    ):
        # When master is off, format_for_prompt output must be
        # identical to what it produced before Slice 2 landed
        # (additive section is suppressed).
        monkeypatch.delenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED",
            raising=False,
        )
        svc = _make_service_with_digest("Test digest")
        with mock.patch.object(
            StrategicDirectionService,
            "_render_codebase_character_section",
            wraps=(
                StrategicDirectionService
                ._render_codebase_character_section
            ),
        ) as spy:
            body = svc.format_for_prompt()
        # Method called (Slice 2 wired it in) but returned "".
        assert spy.called
        assert "## Codebase Character" not in body

    def test_master_on_with_ready_snapshot_appends_section(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        svc = _make_service_with_digest("Test digest")
        fake = _FakeSemanticIndex(
            clusters=(
                _mk_cluster(
                    1, "goal", 5,
                    "Voice biometric authentication primitive",
                ),
                _mk_cluster(
                    2, "conversation", 3,
                    "Ghost hands UI automation",
                ),
            ),
            built_at=time.time(), corpus_n=20,
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake,
        ):
            body = svc.format_for_prompt()
        assert "## Codebase Character" in body
        # Original sections preserved.
        assert "## Strategic Direction (Manifesto v4)" in body
        assert "MANDATE: Structural repair, not patches" in body
        # Order: Codebase Character is AFTER the body and posture
        # block (additive append).
        idx_strategic = body.find(
            "## Strategic Direction (Manifesto v4)",
        )
        idx_codebase = body.find("## Codebase Character")
        assert idx_strategic < idx_codebase

    def test_no_digest_short_circuits_no_prompt_path(
        self, monkeypatch,
    ):
        # If _digest is empty, format_for_prompt returns "" — never
        # invokes the codebase-character path.
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        svc = _make_service_with_digest("")
        with mock.patch.object(
            StrategicDirectionService,
            "_render_codebase_character_section",
        ) as spy:
            body = svc.format_for_prompt()
        assert body == ""
        assert spy.called is False

    def test_codebase_section_under_char_budget(self, monkeypatch):
        # Even with many clusters, the char budget caps the section
        # at 1500 chars (per Slice 2 wire-up).
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        clusters = tuple(
            _mk_cluster(
                i, "goal", 10,
                f"theme cluster {i} " + ("filler text " * 30),
            )
            for i in range(8)
        )
        fake = _FakeSemanticIndex(
            clusters=clusters,
            built_at=time.time(), corpus_n=80,
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake,
        ):
            rendered = (
                StrategicDirectionService
                ._render_codebase_character_section()
            )
        assert rendered != ""
        # Budget enforced.
        assert len(rendered) <= 1500


# ---------------------------------------------------------------------------
# §F — No rebuild triggered from prompt path
# ---------------------------------------------------------------------------


class TestNoBuildSideEffect:
    def test_render_does_not_call_build(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        fake = _FakeSemanticIndex(
            clusters=(_mk_cluster(1), _mk_cluster(2)),
            built_at=time.time(), corpus_n=10,
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake,
        ):
            (
                StrategicDirectionService
                ._render_codebase_character_section()
            )
        # CRITICAL: prompt path must not trigger a rebuild. Async
        # build path owns refresh discipline.
        assert fake.build_call_count == 0


# ---------------------------------------------------------------------------
# §G — BUG-FIX REGRESSION PIN
# ---------------------------------------------------------------------------


class TestBugFixRegressionPin:
    def test_format_for_prompt_calls_render_codebase_character(self):
        # AST-level pin: format_for_prompt body must contain a call to
        # _render_codebase_character_section. A refactor that silently
        # drops the wire-up MUST be caught.
        import textwrap
        src = textwrap.dedent(
            inspect.getsource(
                StrategicDirectionService.format_for_prompt,
            )
        )
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                # Match self._render_codebase_character_section()
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr
                    == "_render_codebase_character_section"
                ):
                    found = True
                    break
        assert found, (
            "BUG-FIX regression pin violated: "
            "format_for_prompt no longer invokes "
            "_render_codebase_character_section — Slice 2 wire-up "
            "was removed; the codebase-character digest will never "
            "reach the prompt"
        )

    def test_format_for_prompt_appends_after_posture(self):
        # Pin: codebase-character call must appear AFTER the posture
        # render call in the source. Posture is the predecessor in
        # the additive chain.
        src = inspect.getsource(
            StrategicDirectionService.format_for_prompt,
        )
        idx_posture = src.find("_render_posture_section")
        idx_codebase = src.find("_render_codebase_character_section")
        assert idx_posture > 0
        assert idx_codebase > 0
        assert idx_posture < idx_codebase, (
            "Codebase-character render must follow posture render "
            "(additive append discipline)"
        )


# ---------------------------------------------------------------------------
# §H — Empty / never-built index
# ---------------------------------------------------------------------------


class TestEmptyIndex:
    def test_index_with_no_clusters_returns_empty(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        fake = _FakeSemanticIndex(
            clusters=(), built_at=time.time(), corpus_n=0,
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake,
        ):
            rendered = (
                StrategicDirectionService
                ._render_codebase_character_section()
            )
        # 0 clusters < min_clusters=2 → INSUFFICIENT → empty.
        assert rendered == ""

    def test_index_built_at_zero_returns_empty(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CODEBASE_CHARACTER_DIGEST_ENABLED", "true",
        )
        fake = _FakeSemanticIndex(
            clusters=(_mk_cluster(1), _mk_cluster(2)),
            built_at=0.0, corpus_n=10,
        )
        with mock.patch(
            "backend.core.ouroboros.governance.semantic_index.get_default_index",  # noqa: E501
            return_value=fake,
        ):
            rendered = (
                StrategicDirectionService
                ._render_codebase_character_section()
            )
        # built_at=0 → STALE_INDEX → empty.
        assert rendered == ""
