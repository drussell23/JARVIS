"""
DomainTrustLedger — append-only per-domain reliability journal.

Trust formula (ratio-based, Goodhart-resistant):
  trust_score = (
      0.40 * (successful_runs  / max(total_attempts, 1))
    - 0.30 * (rollback_count   / max(total_attempts, 1))
    - 0.20 * (incident_count   / max(total_attempts, 1))
    + 0.10 * (audit_pass_count / max(total_attempts, 1))
  )

Tier graduation gates:
  tier_0: risk_class=critical OR compensation_strategy.strategy_type="manual" — never graduates
  tier_1: default for new domains — human approves each synthesis
  tier_2: trust_score >= 0.70 AND total_attempts >= 5
  tier_3: trust_score >= 0.90 AND total_attempts >= 20 AND incident_count == 0

Any incident resets to tier_1 immediately.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class TrustJournalEntry:
    kind: str   # "success" | "rollback" | "incident" | "audit"
    timestamp_ms: int


@dataclass
class DomainTrustRecord:
    domain_id: str
    tier: int
    trust_score: float
    total_attempts: int
    successful_runs: int
    rollback_count: int
    incident_count: int
    audit_pass_count: int
    last_updated_ms: int
    journal: List[TrustJournalEntry]


def _compute_tier(r: DomainTrustRecord) -> int:
    if r.incident_count > 0:
        return 1
    if r.trust_score >= 0.90 and r.total_attempts >= 20 and r.incident_count == 0:
        return 3
    if r.trust_score >= 0.70 and r.total_attempts >= 5:
        return 2
    return 1


def _compute_score(r: DomainTrustRecord) -> float:
    n = max(r.total_attempts, 1)
    return (
        0.40 * (r.successful_runs / n)
        - 0.30 * (r.rollback_count / n)
        - 0.20 * (r.incident_count / n)
        + 0.10 * (r.audit_pass_count / n)
    )


class DomainTrustLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, DomainTrustRecord] = {}

    def _get_or_create(self, domain: str) -> DomainTrustRecord:
        if domain not in self._records:
            self._records[domain] = DomainTrustRecord(
                domain_id=domain,
                tier=1,
                trust_score=0.0,
                total_attempts=0,
                successful_runs=0,
                rollback_count=0,
                incident_count=0,
                audit_pass_count=0,
                last_updated_ms=int(time.time() * 1000),
                journal=[],
            )
        return self._records[domain]

    def _append(self, domain: str, kind: str) -> None:
        now_ms = int(time.time() * 1000)
        with self._lock:
            r = self._get_or_create(domain)
            r.journal.append(TrustJournalEntry(kind=kind, timestamp_ms=now_ms))
            r.total_attempts += 1
            if kind == "success":
                r.successful_runs += 1
            elif kind == "rollback":
                r.rollback_count += 1
            elif kind == "incident":
                r.incident_count += 1
            elif kind == "audit":
                r.audit_pass_count += 1
            r.trust_score = _compute_score(r)
            r.tier = _compute_tier(r)
            r.last_updated_ms = now_ms

    def record_success(self, domain: str) -> None:
        self._append(domain, "success")

    def record_rollback(self, domain: str) -> None:
        self._append(domain, "rollback")

    def record_incident(self, domain: str) -> None:
        self._append(domain, "incident")

    def record_audit(self, domain: str) -> None:
        self._append(domain, "audit")

    def record(self, domain: str) -> DomainTrustRecord:
        with self._lock:
            r = self._get_or_create(domain)
            # Return a snapshot (shallow copy, journal is a new list)
            return DomainTrustRecord(
                domain_id=r.domain_id,
                tier=r.tier,
                trust_score=r.trust_score,
                total_attempts=r.total_attempts,
                successful_runs=r.successful_runs,
                rollback_count=r.rollback_count,
                incident_count=r.incident_count,
                audit_pass_count=r.audit_pass_count,
                last_updated_ms=r.last_updated_ms,
                journal=list(r.journal),
            )

    def journal(self, domain: str) -> List[TrustJournalEntry]:
        with self._lock:
            return list(self._get_or_create(domain).journal)
