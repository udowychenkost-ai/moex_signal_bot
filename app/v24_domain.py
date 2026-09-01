from __future__ import annotations

from enum import StrEnum

STRATEGY_VERSION_V24 = "intraday_v2_4"


class JournalDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class SetupType(StrEnum):
    TREND_PULLBACK = "TREND_PULLBACK"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    MOMENTUM_CONTINUATION = "MOMENTUM_CONTINUATION"
    VWAP_RECLAIM = "VWAP_RECLAIM"
    VWAP_REJECT = "VWAP_REJECT"
    SUPPORT_BOUNCE = "SUPPORT_BOUNCE"
    RESISTANCE_REJECTION = "RESISTANCE_REJECTION"
    RELATIVE_STRENGTH = "RELATIVE_STRENGTH"
    RELATIVE_WEAKNESS = "RELATIVE_WEAKNESS"
    COMPRESSION_EXPANSION = "COMPRESSION_EXPANSION"
    OPENING_RANGE_BREAKOUT = "OPENING_RANGE_BREAKOUT"
    UNKNOWN = "UNKNOWN"


class GateResult(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class DataAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    DATA_NOT_AVAILABLE = "DATA_NOT_AVAILABLE"


class DataConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class ConfigurationStatus(StrEnum):
    CONFIGURED = "CONFIGURED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class DataSLAResult(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class MicrostructureStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    DATA_NOT_AVAILABLE = "DATA_NOT_AVAILABLE"


class SampleType(StrEnum):
    DEVELOPMENT = "DEVELOPMENT"
    OOS = "OOS"
    FORWARD = "FORWARD"
    SHADOW = "SHADOW"


class FillStatus(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    UNCERTAIN = "UNCERTAIN"
    NOT_FILLED = "NOT_FILLED"


class TradeEventType(StrEnum):
    ENTRY = "ENTRY"
    STOP_MOVE = "STOP_MOVE"
    TP_MOVE = "TP_MOVE"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    ADD = "ADD"
    REDUCE = "REDUCE"
    FULL_EXIT = "FULL_EXIT"
    THESIS_UPDATE = "THESIS_UPDATE"
    CANCEL = "CANCEL"
    OTHER = "OTHER"


class JournalStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    INCOMPLETE = "INCOMPLETE"


class StatisticalAdmissionStatus(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    PASS = "PASS"
    FAIL = "FAIL"


class CalibrationStatus(StrEnum):
    UNCALIBRATED = "UNCALIBRATED"
    PRELIMINARY = "PRELIMINARY"
    CALIBRATED = "CALIBRATED"


class ProbabilityStatus(StrEnum):
    NOT_RELIABLY_CALIBRATED = "NOT_RELIABLY_CALIBRATED"
    CALIBRATED = "CALIBRATED"


class FinalClassification(StrEnum):
    TRADEABLE = "TRADEABLE"
    WATCH_ONLY = "WATCH_ONLY"
    SIGNAL_VALID_EXECUTION_INVALID = "SIGNAL_VALID_EXECUTION_INVALID"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    REJECTED = "REJECTED"


class FinalDecision(StrEnum):
    ENTER_NOW = "ENTER_NOW"
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"
    CANCEL = "CANCEL"


class AdversarialResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WAIT = "WAIT"


class AuditStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class KillSwitchState(StrEnum):
    NORMAL = "NORMAL"
    CAPITAL_PRESERVATION = "CAPITAL_PRESERVATION"


class HistoricalEvidenceStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    MISSING_HISTORICAL_EVIDENCE = "MISSING_HISTORICAL_EVIDENCE"
