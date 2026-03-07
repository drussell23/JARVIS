"""Tests for ModelArtifactManifest -- multi-brain model governance."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest
from core.contracts.model_artifact_manifest import (
    ModelArtifactManifest,
    BrainCapability,
    is_compatible,
)


class TestModelArtifactManifest:
    def test_manifest_creation(self):
        manifest = ModelArtifactManifest(
            brain_id="email_triage",
            model_name="jarvis-triage-v3",
            capabilities=(BrainCapability.EMAIL_CLASSIFICATION,),
            schema_version="1.0",
            min_runtime_version="1.0.0",
            eval_scores={"accuracy": 0.92, "f1": 0.89},
        )
        assert manifest.brain_id == "email_triage"
        assert BrainCapability.EMAIL_CLASSIFICATION in manifest.capabilities

    def test_manifest_is_frozen(self):
        manifest = ModelArtifactManifest(
            brain_id="email_triage",
            model_name="jarvis-triage-v3",
            capabilities=(BrainCapability.EMAIL_CLASSIFICATION,),
            schema_version="1.0",
        )
        with pytest.raises(AttributeError):
            manifest.brain_id = "other"

    def test_compatibility_check_passes(self):
        manifest = ModelArtifactManifest(
            brain_id="email_triage",
            model_name="jarvis-triage-v3",
            capabilities=(BrainCapability.EMAIL_CLASSIFICATION,),
            schema_version="1.0",
            min_runtime_version="1.0.0",
        )
        assert is_compatible(manifest, runtime_version="1.2.0",
                             requested_capability=BrainCapability.EMAIL_CLASSIFICATION)

    def test_compatibility_check_fails_wrong_capability(self):
        manifest = ModelArtifactManifest(
            brain_id="email_triage",
            model_name="jarvis-triage-v3",
            capabilities=(BrainCapability.EMAIL_CLASSIFICATION,),
            schema_version="1.0",
        )
        assert not is_compatible(manifest, runtime_version="1.0.0",
                                 requested_capability=BrainCapability.VOICE_PROCESSING)

    def test_compatibility_check_fails_old_runtime(self):
        manifest = ModelArtifactManifest(
            brain_id="email_triage",
            model_name="jarvis-triage-v3",
            capabilities=(BrainCapability.EMAIL_CLASSIFICATION,),
            schema_version="1.0",
            min_runtime_version="2.0.0",
        )
        assert not is_compatible(manifest, runtime_version="1.5.0",
                                 requested_capability=BrainCapability.EMAIL_CLASSIFICATION)
