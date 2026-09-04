"""Core data models shared by targets, probes, and outputs.

Everything here is a plain dataclass or enum so that a full scan can be
serialized to JSON without a schema library.
"""

from __future__ import annotations

import hashlib
import json
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


class FaultKind(str, Enum):
    """A fault the FaultProxy can inject.

    Faults are what we *do* to the target; ErrorKind is what the caller
    *observes* as a result. They are deliberately separate: an injected
    MALFORMED response and a genuinely corrupt one both surface as
    ErrorKind.INVALID_RESPONSE, which is what makes a fault run comparable to a
    baseline run.
    """

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    MALFORMED = "malformed"
    CONNECTION_REFUSED = "connection_refused"

    @property
    def error_kind(self) -> "ErrorKind":
        return _FAULT_TO_ERROR[self]


_FAULT_TO_ERROR: dict["FaultKind", ErrorKind] = {
    FaultKind.TIMEOUT: ErrorKind.TIMEOUT,
    FaultKind.RATE_LIMIT: ErrorKind.RATE_LIMIT,
    FaultKind.SERVER_ERROR: ErrorKind.SERVER_ERROR,
    FaultKind.MALFORMED: ErrorKind.INVALID_RESPONSE,
    FaultKind.CONNECTION_REFUSED: ErrorKind.CONNECTION,
}


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
    trajectory_id: str | None = None

    @property
    def fingerprint(self) -> str:
        """Identifies "the same call" across attempts.

        Deliberately excludes `label`, which is unique per attempt: two retries
        of one logical operation must share a fingerprint, or duplicate and
        retry detection sees nothing.
        """
        payload = json.dumps(self.payload, sort_keys=True, default=str)
        digest = hashlib.sha256(f"{self.op}:{payload}".encode()).hexdigest()
        return f"{self.op}:{digest[:12]}"

    @property
    def trajectory_key(self) -> str:
        """Groups attempts of one logical operation."""
        return self.trajectory_id or self.label or self.op

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "payload": dict(self.payload),
            "timeout_s": self.timeout_s,
            "label": self.label,
            "trajectory_id": self.trajectory_id,
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
class Invocation:
    """One call observed at the FaultProxy boundary.

    `injected` records what we did to this call, which is what lets a
    trajectory distinguish "the target failed" from "we broke it on purpose".
    """

    sequence: int
    op: str
    fingerprint: str
    trajectory_id: str
    attempt: int
    ok: bool
    latency_s: float
    started_at: float
    error_kind: ErrorKind | None = None
    injected: FaultKind | None = None

    @property
    def finished_at(self) -> float:
        return self.started_at + self.latency_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "op": self.op,
            "fingerprint": self.fingerprint,
            "trajectory_id": self.trajectory_id,
            "attempt": self.attempt,
            "ok": self.ok,
            "latency_s": self.latency_s,
            "started_at": self.started_at,
            "error_kind": self.error_kind.value if self.error_kind else None,
            "injected": self.injected.value if self.injected else None,
        }


@dataclass
class Trajectory:
    """Every attempt at one logical operation, and what they add up to.

    This is the model that separates RateMyAgent from a load tester: not "did
    it work" but "what did it do when things went wrong". Derived values are
    properties rather than stored fields so a trajectory cannot go stale as
    invocations are appended.

    Observed at the proxy boundary, so these signals describe whoever is on the
    far side of the proxy. Wrapping an MCP server, they describe the caller's
    retry behavior against that server; wrapping an agent, they describe the
    agent's own behavior. The proxy reports what crossed the boundary and does
    not guess which.
    """

    trajectory_id: str
    invocations: list[Invocation] = field(default_factory=list)

    @property
    def attempts(self) -> int:
        return len(self.invocations)

    @property
    def retries(self) -> int:
        """Calls after the first for this operation."""
        return max(0, self.attempts - 1)

    @property
    def failures(self) -> int:
        return sum(1 for inv in self.invocations if not inv.ok)

    @property
    def recovered(self) -> bool:
        """Did a failure later turn into a success?"""
        seen_failure = False
        for inv in self.invocations:
            if not inv.ok:
                seen_failure = True
            elif seen_failure:
                return True
        return False

    @property
    def recovery_latency_s(self) -> float | None:
        """First failure to the success that resolved it."""
        first_failure: float | None = None
        for inv in self.invocations:
            if not inv.ok and first_failure is None:
                first_failure = inv.started_at
            elif inv.ok and first_failure is not None:
                return inv.finished_at - first_failure
        return None

    @property
    def duplicates(self) -> int:
        """Repeated *successful* calls with identical arguments.

        The dangerous case, and the reason this counts successes rather than
        attempts: a retry that succeeds twice has run the same mutation twice.
        """
        succeeded: set[str] = set()
        duplicates = 0
        for inv in self.invocations:
            if not inv.ok:
                continue
            if inv.fingerprint in succeeded:
                duplicates += 1
            succeeded.add(inv.fingerprint)
        return duplicates

    @property
    def loops_detected(self) -> bool:
        """Three or more attempts that never resolved.

        The spec phrases this as "same call pattern repeated 3+ times". Adding
        the unrecovered condition avoids flagging a retry that succeeded on the
        third try, which is a system working, not a system stuck.
        """
        return self.attempts >= 3 and not self.recovered

    @property
    def final_status(self) -> str:
        """success or failed, from the last attempt.

        The spec also lists "abandoned" and "incorrect". Neither is derivable
        here: abandoned needs the caller's intent, and incorrect needs a
        correctness oracle. They stay unreported rather than guessed at.
        """
        if not self.invocations:
            return "empty"
        return "success" if self.invocations[-1].ok else "failed"

    @property
    def injected_faults(self) -> list[FaultKind]:
        return [inv.injected for inv in self.invocations if inv.injected is not None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trajectory_id": self.trajectory_id,
            "attempts": self.attempts,
            "retries": self.retries,
            "failures": self.failures,
            "recovered": self.recovered,
            "recovery_latency_s": self.recovery_latency_s,
            "duplicates": self.duplicates,
            "loops_detected": self.loops_detected,
            "final_status": self.final_status,
            "injected_faults": [fault.value for fault in self.injected_faults],
            "invocations": [inv.to_dict() for inv in self.invocations],
        }


@dataclass
class ProbeResult:
    """What a probe produces. `grade` is filled in by Probe.grade()."""

    probe: str
    summary: str = ""
    grade: Grade | None = None
    phase: str = "baseline"
    #: False when the probe cannot meaningfully run against this target -- cost
    #: against a target that reports no tokens, contract against one with no
    #: tools. An inapplicable probe still reports findings but is left out of
    #: the overall grade, because "we could not measure this" is not a C.
    applicable: bool = True
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
            "phase": self.phase,
            "applicable": self.applicable,
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
        """Average of the probes that could actually measure something.

        Inapplicable probes are excluded rather than counted as mediocre: a
        cost probe against an MCP server that reports no tokens says nothing
        about the server's reliability, and averaging it in would.
        """
        return Grade.average(
            [p.grade for p in self.probes if p.grade is not None and p.applicable]
        )

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
