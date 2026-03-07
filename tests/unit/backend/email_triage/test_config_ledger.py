"""Test ledger config field."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))


def test_ledger_lease_default():
    from autonomy.email_triage.config import TriageConfig
    config = TriageConfig()
    assert config.ledger_lease_duration_s == 60.0


def test_ledger_lease_custom():
    from autonomy.email_triage.config import TriageConfig
    config = TriageConfig(ledger_lease_duration_s=120.0)
    assert config.ledger_lease_duration_s == 120.0
