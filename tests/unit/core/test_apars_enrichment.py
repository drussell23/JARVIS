"""Tests for APARS enrichment middleware readiness isolation."""
import re
from pathlib import Path


def _extract_middleware_class(src: str) -> str:
    """Extract APARSEnrichmentMiddleware class source from the embedded Python heredoc."""
    # The class is inside a heredoc in _generate_golden_startup_script.
    # Find the class definition and extract until the next top-level definition or end of heredoc.
    match = re.search(
        r"(class APARSEnrichmentMiddleware:.*?)(?=\n# ---|^\w|\nEOFLAUNCHER)",
        src,
        re.DOTALL | re.MULTILINE,
    )
    assert match, "APARSEnrichmentMiddleware class not found in source"
    return match.group(1)


class TestAPARSEnrichmentReadiness:
    def test_middleware_does_not_set_readiness_fields(self):
        """APARSEnrichmentMiddleware must NOT inject model_loaded or ready_for_inference."""
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        class_src = _extract_middleware_class(src)
        assert "ready_for_inference" not in class_src, \
            "APARSEnrichmentMiddleware must not touch ready_for_inference"
        assert 'setdefault("model_loaded"' not in class_src, \
            "APARSEnrichmentMiddleware must not set model_loaded"

    def test_middleware_still_injects_apars_payload(self):
        """Middleware must still inject APARS progress data (just not readiness)."""
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        class_src = _extract_middleware_class(src)
        assert '"apars"' in class_src or "'apars'" in class_src, \
            "Middleware must still inject APARS data"
