"""Core data models shared by targets, probes, and outputs.

Everything here is a plain dataclass or enum so that a full scan can be
serialized to JSON without a schema library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable


class Grade(str, Enum):
    """Letter grade for a single probe or for a whole scan."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"

    @property
    def points(self) -> int:
        """4.0-scale value, used to average grades across probes."""
        return _GRADE_POINTS[self.value]

    @classmethod
    def from_points(cls, points: float) -> "Grade":
        rounded = max(0, min(4, int(round(points))))
        return _POINTS_TO_GRADE[rounded]

    @classmethod
    def worst(cls, grades: Iterable["Grade"]) -> "Grade":
        """Lowest grade in the set. Empty set grades F."""
        collected = list(grades)
        if not collected:
            return cls.F
        return min(collected, key=lambda grade: grade.points)

    @classmethod
    def average(cls, grades: Iterable["Grade"]) -> "Grade":
        """Mean grade, rounded to the nearest letter. Empty set grades F."""
        collected = list(grades)
        if not collected:
            return cls.F
        return cls.from_points(sum(grade.points for grade in collected) / len(collected))


_GRADE_POINTS: dict[str, int] = {"A": 4, "B": 3, "C": 2, "D": 1, "F": 0}
_POINTS_TO_GRADE: dict[int, Grade] = {
    4: Grade.A,
    3: Grade.B,
    2: Grade.C,
    1: Grade.D,
    0: Grade.F,
}


class ErrorKind(str, Enum):
    """Failure taxonomy.

    The ErrorClassifier probe (week 4) builds its frequency table from these,
    but targets tag every failed Response as it happens so that error rates are
    meaningful from the first probe onward.
    """

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    CONNECTION = "connection"
    PROTOCOL = "protocol"
    INVALID_RESPONSE = "invalid_response"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass
class ToolInfo:
    """One capability a target exposes.

    MCP targets fill this from tool discovery; the contract probe (week 3)
    fuzzes against `input_schema`.
    """

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }


@dataclass
class TargetInfo:
    """Metadata a target reports about itself after setup()."""

    name: str
    kind: str
    uri: str | None = None
    capabilities: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "uri": self.uri,
            "capabilities": list(self.capabilities),
            "metadata": dict(self.metadata),
        }


@dataclass
class Request:
    """One unit of work to send at a target.

    `op` is the tool name for MCP targets and the prompt label for LLM targets.
    `label` is a stable per-request identifier; mock targets derive deterministic
    behavior from it, so it must be unique within a probe run.
    """

    op: str
    payload: dict[str, Any] = field(default_factory=dict)
    timeout_s: float | None = None
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "payload": dict(self.payload),
            "timeout_s": self.timeout_s,
            "label": self.label,
        }


@dataclass
class Response:
    """Result of a single invoke().

    `latency_s` is wall clock for real targets and simulated for mock targets;
    probes treat both the same way. `server_time_s` is the target's own reported
    execution time when it exposes one, which is what makes tool call overhead
    computable rather than guessed.
    """

    ok: bool
    latency_s: float
    ttft_s: float | None = None
    server_time_s: float | None = None
    output: Any = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "latency_s": self.latency_s,
            "ttft_s": self.ttft_s,
            "server_time_s": self.server_time_s,
            "error": self.error,
            "error_kind": self.error_kind.value if self.error_kind else None,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "meta": dict(self.meta),
        }


@dataclass
class ProbeResult:
    """What a probe produces. `grade` is filled in by Probe.grade()."""

    probe: str
    summary: str = ""
    grade: Grade | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    sample_count: int = 0
    error_rate: float = 0.0
    duration_s: float = 0.0
    error: str | None = None

    @property
    def failed(self) -> bool:
        """True when the probe itself blew up, as opposed to grading badly."""
        return self.error is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe": self.probe,
            "grade": self.grade.value if self.grade else None,
            "summary": self.summary,
            "metrics": dict(self.metrics),
            "findings": list(self.findings),
            "sample_count": self.sample_count,
            "error_rate": self.error_rate,
            "duration_s": self.duration_s,
            "error": self.error,
        }


@dataclass
class ScanResult:
    """Aggregate of every probe run against one target."""

    target: TargetInfo
    probes: list[ProbeResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    duration_s: float = 0.0
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def overall_grade(self) -> Grade:
        return Grade.average([p.grade for p in self.probes if p.grade is not None])

    def probe(self, name: str) -> ProbeResult | None:
        for result in self.probes:
            if result.probe == name:
                return result
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "overall_grade": self.overall_grade.value,
            "started_at": self.started_at.isoformat(),
            "duration_s": self.duration_s,
            "config": dict(self.config),
            "probes": [p.to_dict() for p in self.probes],
        }
