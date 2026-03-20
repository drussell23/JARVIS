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
    """trust_score = 0.40*(s/n) - 0.30*(r/n) - 0.20*(i/n) + 0.10*(a/n)
    where n = total_attempts (audits don't count as attempts)"""
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
    n = max(r.total_attempts, 1)  # = 13 (audits excluded)
    expected = (
        0.40 * (r.successful_runs / n)
        - 0.30 * (r.rollback_count / n)
        - 0.20 * (r.incident_count / n)
        + 0.10 * (r.audit_pass_count / n)
    )
    assert abs(r.trust_score - expected) < 1e-9


def test_tier2_requires_score_and_attempts(ledger):
    domain = "test:tier2"
    # Gate 1: fewer than 5 attempts → tier 1
    for _ in range(4):
        ledger.record_success(domain)
    assert ledger.record(domain).tier == 1

    # Now meet both gates: 5 attempts + score >= 0.70 via audits
    # 5 successes (n=5), 15 audits → score = 0.40*(5/5) + 0.10*(15/5) = 0.70
    ledger.record_success(domain)          # 5th attempt
    for _ in range(15):
        ledger.record_audit(domain)        # boosts score without increasing n
    r = ledger.record(domain)
    assert r.total_attempts == 5
    assert abs(r.trust_score - 0.70) < 1e-9
    assert r.tier == 2


def test_incident_resets_to_tier1(ledger):
    domain = "test:incident_reset"
    for _ in range(25):
        ledger.record_success(domain)
    ledger.record_incident(domain)
    assert ledger.record(domain).tier == 1


def test_tier3_requires_zero_incidents(ledger):
    domain = "test:tier3"
    for _ in range(25):
        ledger.record_success(domain)
    for _ in range(5):
        ledger.record_audit(domain)
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
    ledger.record_rollback(domain)
    ledger.record_incident(domain)
    ledger.record_audit(domain)  # audit does NOT count as attempt
    r = ledger.record(domain)
    assert r.total_attempts == 3   # only success + rollback + incident
    assert r.audit_pass_count == 1  # audit still tracked
