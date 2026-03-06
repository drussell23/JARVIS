"""Tests for durable publish cursor in the journal DB."""
from __future__ import annotations

import pytest

from backend.core.reactive_state.journal import AppendOnlyJournal


def _append_sample(
    journal: AppendOnlyJournal,
    *,
    key: str = "gcp.vm_ready",
    value: object = True,
    previous_value: object = None,
    version: int = 1,
    epoch: int = 0,
    writer: str = "supervisor",
    writer_session_id: str = "sess-abc-123",
    origin: str = "explicit",
    consistency_group: str | None = None,
):
    return journal.append(
        key=key,
        value=value,
        previous_value=previous_value,
        version=version,
        epoch=epoch,
        writer=writer,
        writer_session_id=writer_session_id,
        origin=origin,
        consistency_group=consistency_group,
    )


class TestPublishCursor:
    def test_initial_cursor_is_zero(self, tmp_path) -> None:
        j = AppendOnlyJournal(tmp_path / "j.db")
        j.open()
        try:
            assert j.get_publish_cursor() == 0
        finally:
            j.close()

    def test_advance_cursor(self, tmp_path) -> None:
        j = AppendOnlyJournal(tmp_path / "j.db")
        j.open()
        try:
            _append_sample(j, key="a", version=1)
            _append_sample(j, key="b", version=1)
            j.advance_publish_cursor(2)
            assert j.get_publish_cursor() == 2
        finally:
            j.close()

    def test_cursor_monotonic_rejects_backward(self, tmp_path) -> None:
        j = AppendOnlyJournal(tmp_path / "j.db")
        j.open()
        try:
            _append_sample(j, key="a", version=1)
            _append_sample(j, key="b", version=1)
            j.advance_publish_cursor(2)
            with pytest.raises(ValueError, match="monotonic"):
                j.advance_publish_cursor(1)
        finally:
            j.close()

    def test_cursor_persists_across_reopen(self, tmp_path) -> None:
        db = tmp_path / "j.db"
        j = AppendOnlyJournal(db)
        j.open()
        _append_sample(j, key="a", version=1)
        j.advance_publish_cursor(1)
        j.close()

        j2 = AppendOnlyJournal(db)
        j2.open()
        try:
            assert j2.get_publish_cursor() == 1
        finally:
            j2.close()

    def test_unpublished_entries(self, tmp_path) -> None:
        j = AppendOnlyJournal(tmp_path / "j.db")
        j.open()
        try:
            _append_sample(j, key="a", version=1)
            _append_sample(j, key="b", version=1)
            _append_sample(j, key="c", version=1)

            j.advance_publish_cursor(1)
            unpublished = j.read_unpublished()
            assert len(unpublished) == 2
            assert unpublished[0].global_revision == 2
            assert unpublished[1].global_revision == 3
        finally:
            j.close()

    def test_unpublished_entries_when_all_published(self, tmp_path) -> None:
        j = AppendOnlyJournal(tmp_path / "j.db")
        j.open()
        try:
            _append_sample(j, key="a", version=1)
            j.advance_publish_cursor(1)
            assert j.read_unpublished() == []
        finally:
            j.close()

    def test_unpublished_entries_when_none_published(self, tmp_path) -> None:
        j = AppendOnlyJournal(tmp_path / "j.db")
        j.open()
        try:
            _append_sample(j, key="a", version=1)
            _append_sample(j, key="b", version=1)
            unpublished = j.read_unpublished()
            assert len(unpublished) == 2
        finally:
            j.close()
