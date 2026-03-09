"""Sensor adapters for the Unified Intake Router (Phase 2C)."""
from .backlog_sensor import BacklogSensor, BacklogTask
from .test_failure_sensor import TestFailureSensor
from .voice_command_sensor import VoiceCommandSensor, VoiceCommandPayload
from .opportunity_miner_sensor import OpportunityMinerSensor, StaticCandidate

__all__ = [
    "BacklogSensor",
    "BacklogTask",
    "TestFailureSensor",
    "VoiceCommandSensor",
    "VoiceCommandPayload",
    "OpportunityMinerSensor",
    "StaticCandidate",
]
