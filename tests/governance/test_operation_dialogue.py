# [Ouroboros] Modified by Ouroboros (op=op-01a07d1b-) at 2026-09-07 18:24 UTC
# Reason: First-order proof #3: author a real unit test for the untested operation_dialogue store  AUTHOR a new pytest test file a

import os
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional
from unittest.mock import patch

current_dir = Path(__file__).parent
project_root = current_dir.parent.parent

# Import the real module
from backend.core.ouroboros.governance.operation_dialogue import (
    OperationDialogueStore,
    OperationDialogueRecord,
    DialogueEntry
)


def test_start_dialogue_returns_record(tmp_path: Path):
    """Test that start_dialogue returns an OperationDialogueRecord."""
    store = OperationDialogueStore(persistence_dir=tmp_path)
    
    record = store.start_dialogue(
        op_id="test-op-1",
        domain_key="test-domain",
        description="Test operation",
        target_files=("file1.txt", "file2.txt")
    )
    
    assert isinstance(record, OperationDialogueRecord)
    assert record.op_id == "test-op-1"
    assert record.domain_key == "test-domain"
    assert record.description == "Test operation"
    assert record.target_files == ("file1.txt", "file2.txt")
    assert len(record.entries) == 0
    assert record.outcome == ""
    assert record.completed_at == 0.0


def test_get_active_returns_record(tmp_path: Path):
    """Test that get_active returns the record started by start_dialogue."""
    store = OperationDialogueStore(persistence_dir=tmp_path)
    
    record = store.start_dialogue(
        op_id="test-op-1",
        domain_key="test-domain",
        description="Test operation",
        target_files=("file1.txt", "file2.txt")
    )
    
    retrieved_record = store.get_active("test-op-1")
    assert retrieved_record is not None
    assert retrieved_record.op_id == record.op_id
    assert retrieved_record.domain_key == record.domain_key


def test_add_entry_appends_dialogue_entry(tmp_path: Path):
    """Test that add_entry appends a DialogueEntry with given phase, reasoning and data."""
    store = OperationDialogueStore(persistence_dir=tmp_path)
    
    record = store.start_dialogue(
        op_id="test-op-1",
        domain_key="test-domain",
        description="Test operation",
        target_files=("file1.txt", "file2.txt")
    )
    
    # Add an entry
    record.add_entry(
        phase="CLASSIFY",
        reasoning="Classified as test operation",
        category="test-category",
        confidence=0.95
    )
    
    assert len(record.entries) == 1
    entry = record.entries[0]
    assert entry.phase == "CLASSIFY"
    assert entry.reasoning == "Classified as test operation"
    assert entry.data["category"] == "test-category"
    assert entry.data["confidence"] == 0.95


def test_complete_dialogue_sets_outcome_and_completed_at(tmp_path: Path):
    """Test that complete_dialogue sets record.outcome and completed_at."""
    store = OperationDialogueStore(persistence_dir=tmp_path)
    
    record = store.start_dialogue(
        op_id="test-op-1",
        domain_key="test-domain",
        description="Test operation",
        target_files=("file1.txt", "file2.txt")
    )
    
    # Complete the dialogue
    store.complete_dialogue("test-op-1", "success")
    
    # Check that record is no longer active
    assert store.get_active("test-op-1") is None
    
    # Check that the record has been archived
    past_dialogues = store.get_past_dialogues("test-domain")
    assert len(past_dialogues) == 1
    archived_record = past_dialogues[0]
    assert archived_record.outcome == "success"
    assert archived_record.completed_at != 0.0


def test_complete_dialogue_removes_from_active_and_adds_to_past(tmp_path: Path):
    """Test that complete_dialogue removes from active and adds to past dialogues."""
    store = OperationDialogueStore(persistence_dir=tmp_path)
    
    record1 = store.start_dialogue(
        op_id="test-op-1",
        domain_key="test-domain",
        description="Test operation 1",
        target_files=("file1.txt", "file2.txt")
    )
    
    record2 = store.start_dialogue(
        op_id="test-op-2",
        domain_key="test-domain",
        description="Test operation 2",
        target_files=("file3.txt", "file4.txt")
    )
    
    # Complete both dialogues
    store.complete_dialogue("test-op-1", "success")
    store.complete_dialogue("test-op-2", "failure")
    
    # Check that both are no longer active
    assert store.get_active("test-op-1") is None
    assert store.get_active("test-op-2") is None
    
    # Check that both are in past dialogues
    past_dialogues = store.get_past_dialogues("test-domain")
    assert len(past_dialogues) == 2
    
    # Check order (most recent first)
    assert past_dialogues[0].op_id == "test-op-2"
    assert past_dialogues[1].op_id == "test-op-1"


def test_complete_dialogue_unknown_op_id_is_noop(tmp_path: Path):
    """Test that complete_dialogue for unknown op_id is a no-op."""
    store = OperationDialogueStore(persistence_dir=tmp_path)
    
    # Try to complete a dialogue that never existed
    store.complete_dialogue("non-existent-op", "success")
    
    # Should not crash and should have no past dialogues
    assert len(store.get_past_dialogues("test-domain")) == 0


def test_format_for_prompt_empty_domain(tmp_path: Path):
    """Test that format_for_prompt returns empty string for domain with no dialogues."""
    store = OperationDialogueStore(persistence_dir=tmp_path)
    
    result = store.format_for_prompt("empty-domain")
    assert result == ""


def test_format_for_prompt_with_completed_dialogue(tmp_path: Path):
    """Test that format_for_prompt returns formatted string with completed dialogue."""
    store = OperationDialogueStore(persistence_dir=tmp_path)
    
    # Start and complete a dialogue
    record = store.start_dialogue(
        op_id="test-op-1",
        domain_key="test-domain",
        description="Test operation",
        target_files=("file1.txt", "file2.txt")
    )
    
    record.add_entry(
        phase="CLASSIFY",
        reasoning="Classified as test operation",
        category="test-category"
    )
    
    store.complete_dialogue("test-op-1", "success")
    
    # Format for prompt
    result = store.format_for_prompt("test-domain")
    
    assert "## Past Operation Reasoning for Domain: test-domain" in result
    assert "OK" in result  # Success outcome should show OK marker
    assert "CLASSIFY" in result
    assert "Classified as test operation" in result


def test_format_for_prompt_with_failure_outcome(tmp_path: Path):
    """Test that format_for_prompt shows FAIL marker for failure outcome."""
    store = OperationDialogueStore(persistence_dir=tmp_path)
    
    # Start and complete a dialogue with failure
    record = store.start_dialogue(
        op_id="test-op-1",
        domain_key="test-domain",
        description="Test operation",
        target_files=("file1.txt", "file2.txt")
    )
    
    record.add_entry(
        phase="CLASSIFY",
        reasoning="Classified as test operation",
        category="test-category"
    )
    
    store.complete_dialogue("test-op-1", "failure")
    
    # Format for prompt
    result = store.format_for_prompt("test-domain")
    
    assert "## Past Operation Reasoning for Domain: test-domain" in result
    assert "FAIL" in result  # Failure outcome should show FAIL marker
    assert "CLASSIFY" in result


def test_persistence_roundtrip(tmp_path: Path):
    """Test that persistence round-trips correctly."""
    store1 = OperationDialogueStore(persistence_dir=tmp_path)
    
    # Start and complete a dialogue
    record = store1.start_dialogue(
        op_id="test-op-1",
        domain_key="test-domain",
        description="Test operation",
        target_files=("file1.txt", "file2.txt")
    )
    
    record.add_entry(
        phase="CLASSIFY",
        reasoning="Classified as test operation",
        category="test-category"
    )
    
    store1.complete_dialogue("test-op-1", "success")
    
    # Create a new store with same persistence directory
    store2 = OperationDialogueStore(persistence_dir=tmp_path)
    
    # Should see the same past dialogue
    past_dialogues = store2.get_past_dialogues("test-domain")
    assert len(past_dialogues) == 1
    assert past_dialogues[0].op_id == "test-op-1"
    assert past_dialogues[0].outcome == "success"
    assert len(past_dialogues[0].entries) == 1
    assert past_dialogues[0].entries[0].phase == "CLASSIFY"


def test_get_past_dialogues_limit(tmp_path: Path):
    """Test that get_past_dialogues respects the limit parameter."""
    store = OperationDialogueStore(persistence_dir=tmp_path)
    
    # Start and complete multiple dialogues
    for i in range(5):
        record = store.start_dialogue(
            op_id=f"test-op-{i}",
            domain_key="test-domain",
            description=f"Test operation {i}",
            target_files=(f"file{i}.txt",)
        )
        store.complete_dialogue(f"test-op-{i}", "success")
    
    # Get past dialogues with limit 3
    past_dialogues = store.get_past_dialogues("test-domain", limit=3)
    assert len(past_dialogues) == 3
    
    # Should be most recent first
    assert past_dialogues[0].op_id == "test-op-4"
    assert past_dialogues[1].op_id == "test-op-3"
    assert past_dialogues[2].op_id == "test-op-2"
