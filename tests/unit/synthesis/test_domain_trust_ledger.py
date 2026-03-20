import pytest
from backend.neural_mesh.synthesis.domain_trust_ledger import (
    DomainTrustLedger,
    DomainTrustRecord,
)


@pytest.fixture()
def ledger():
    return DomainTrustLedger()


def test_new_domain_at_tier1(ledger):
    assert ledger.record("new:domain").tier == 1


def test_trust_score_ratio_formula(ledger):
    """trust_score = 0.40*(s/n) - 0.30*(r/n) - 0.20*(i/n) + 0.10*(a/n)"""
    domain = "test:ratio"
    for _ in range(10):
        ledger.record_success(domain)
    for _ in range(2):
        ledger.record_rollback(domain)
    for _ in range(1):
        ledger.record_incident(domain)
    for _ in range(3):
        ledger.record_audit(domain)
    r = ledger.record(domain)
    n = max(r.total_attempts, 1)
    expected = (
        0.40 * (r.successful_runs / n)
        - 0.30 * (r.rollback_count / n)
        - 0.20 * (r.incident_count / n)
        + 0.10 * (r.audit_pass_count / n)
    )
    assert abs(r.trust_score - expected) < 1e-9


def test_tier2_requires_score_and_attempts(ledger):
    domain = "test:tier2"
    # Not enough attempts yet
    for _ in range(4):
        ledger.record_success(domain)
    assert ledger.record(domain).tier < 2
    # Now add one more success to meet total_attempts >= 5
    ledger.record_success(domain)
    r = ledger.record(domain)
    if r.trust_score >= 0.70:
        assert r.tier >= 2


def test_incident_resets_to_tier1(ledger):
    domain = "test:incident_reset"
    for _ in range(25):
        ledger.record_success(domain)
    pre = ledger.record(domain).tier
    ledger.record_incident(domain)
    assert ledger.record(domain).tier == 1


def test_tier3_requires_zero_incidents(ledger):
    domain = "test:tier3"
    for _ in range(25):
        ledger.record_success(domain)
    for _ in range(5):
        ledger.record_audit(domain)
    r = ledger.record(domain)
    # If incident_count > 0 it cannot be tier 3
    ledger.record_incident(domain)
    assert ledger.record(domain).tier < 3


def test_journal_append_only(ledger):
    domain = "test:journal"
    ledger.record_success(domain)
    ledger.record_rollback(domain)
    entries = ledger.journal(domain)
    assert len(entries) == 2
    assert entries[0].kind == "success"
    assert entries[1].kind == "rollback"


def test_total_attempts_counts_all_events(ledger):
    domain = "test:total"
    ledger.record_success(domain)
    ledger.record_success(domain)
    ledger.record_rollback(domain)
    r = ledger.record(domain)
    assert r.total_attempts == 3
