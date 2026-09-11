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
from typing import Any


@dataclass
class CheckResult:
    """One policy threshold measured against one probe metric.

    This is the unit the whole score is built from, and it carries everything
    needed to explain itself: what was required, what was seen, and how the
    two turned into a number. A score nobody can audit is a score nobody will
    act on.
    """

    name: str
    probe: str
    metric: str
    direction: str
    threshold: float
    observed: float | None
    score: float
    passed: bool
    reason: str
    units: str = ""
    #: Where `threshold` came from: "policy" for the YAML literal, or the name
    #: of the metric it was derived from at runtime. A reader comparing two
    #: scans has to be able to see that the bar moved, and why.
    threshold_source: str = "policy"

    @property
    def skipped(self) -> bool:
        """True when the metric was unavailable, so this check did not count."""
        return self.observed is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "probe": self.probe,
            "metric": self.metric,
            "direction": self.direction,
            "threshold": self.threshold,
            "observed": self.observed,
            "score": self.score,
            "passed": self.passed,
            "skipped": self.skipped,
            "reason": self.reason,
            "units": self.units,
            "threshold_source": self.threshold_source,
        }


@dataclass
class DimensionScore:
    """One probe's contribution to the overall score.

    Carries points out of a weight rather than a bare percentage, because
    "contract 0/15" tells an engineer how much of the total is at stake and
    "contract 0%" does not.
    """

    probe: str
    label: str
    score: float | None
    weight: float
    note: str = ""
    #: Why this dimension carries no score, as a category rather than prose.
    #: `None` when it was scored.
    #:
    #: Five values, because "not scored" has been five different things wearing
    #: one string. Two of them are facts about the **target**, three are facts
    #: about the **command**, and collapsing them is how a scan that measured
    #: 15% of the policy came to print `100/100 PASS`:
    #:
    #: - `not_selected`   -- absent from `--probes`
    #: - `phase_excluded` -- selected, but no active phase would run it
    #: - `did_not_run`    -- expected, and produced no result
    #: - `not_applicable` -- ran, and cannot measure this target
    #: - `no_threshold`   -- ran, and no policy threshold reads it
    #:
    #: `note` stays as the human sentence. This is the machine-readable half,
    #: added because the only way to tell these apart downstream was to
    #: substring-match English -- which is section 8b entry 1, relocated into
    #: the JSON export.
    not_scored: str | None = None

    @property
    def points(self) -> float | None:
        """Weighted points earned, or None when the dimension was not scored."""
        return None if self.score is None else self.score / 100.0 * self.weight

    @property
    def measured(self) -> bool:
        return self.score is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe": self.probe,
            "label": self.label,
            "score": self.score,
            "not_scored": self.not_scored,
            "weight": self.weight,
            "points": self.points,
            "note": self.note,
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

    #: The target ran the call and the reply was thrown away. Added in 1.3.0 and
    #: **deliberately not in `ALL_FAULTS`**: `FaultConfig.uniform` divides the
    #: total rate by the kind count, and `_choose_fault` walks cumulative
    #: thresholds, so a sixth member in the default set moves every boundary and
    #: re-assigns every seeded draw in every scan ever recorded. A capability
    #: addition does not get to invalidate the corpus.
    #:
    #: It is also only meaningful where it is dangerous. Losing the reply to a
    #: read-only call is noise; losing the reply to a mutation is the failure
    #: this exists to find, so the fault probe enables it exactly when the scan
    #: is cleared to mutate.
    RESPONSE_LOST = "response_lost"

    @property
    def error_kind(self) -> "ErrorKind":
        return _FAULT_TO_ERROR[self]


_FAULT_TO_ERROR: dict["FaultKind", ErrorKind] = {
    FaultKind.TIMEOUT: ErrorKind.TIMEOUT,
    FaultKind.RATE_LIMIT: ErrorKind.RATE_LIMIT,
    FaultKind.SERVER_ERROR: ErrorKind.SERVER_ERROR,
    FaultKind.MALFORMED: ErrorKind.INVALID_RESPONSE,
    FaultKind.CONNECTION_REFUSED: ErrorKind.CONNECTION,
    # What a lost reply looks like from outside, which is the difficulty:
    # indistinguishable from a call that never ran.
    FaultKind.RESPONSE_LOST: ErrorKind.TIMEOUT,
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

    #: From the server's own `annotations`, when it publishes them. Tri-state on
    #: purpose: None means "the server said nothing", which is not the same as
    #: False and must not be collapsed into it. `destructive` is recorded but
    #: nothing gates on it yet -- it distinguishes `delete_entities` from
    #: `create_entities`, which a future rule will want.
    read_only: bool | None = None
    destructive: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "read_only": self.read_only,
            "destructive": self.destructive,
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

    #: Did the target answer at all? False only when this Response was built
    #: from a raised exception rather than from something the target sent back.
    #:
    #: This is the crash signal. It is a structural fact -- a message arrived,
    #: or it did not -- and deliberately not a judgement about the message's
    #: contents. Grading crashes by error *text* is what produced the retracted
    #: 33-50% contract crash rate against two MCP servers that crash nothing:
    #: any correct rejection whose wording the substring table did not
    #: recognise was scored as a dead transport. `delivered` cannot make that
    #: mistake, because it never reads the message.
    #:
    #: True is the default because answering is the normal case; the two
    #: simulating targets (MockTarget, FaultProxy) must declare it explicitly,
    #: since for them it is part of what they are pretending to be.
    delivered: bool = True

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
            "delivered": self.delivered,
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
    #: Did the **target** run this call, as distinct from whether the **caller**
    #: saw success? `ok` conflates the two, and the gap between them is where
    #: duplicate work comes from: a mutation that executed, whose reply was lost
    #: or damaged, is retried and executes again.
    #:
    #: **Three values, and `None` is the one that matters.** `True` we know it
    #: ran; `False` we know it did not -- the FaultProxy rejected the call
    #: without reaching the target. `None` is *unknown*, and it is the honest
    #: answer for a real failure from a real server: a timeout is precisely the
    #: case where the caller cannot tell whether the work happened. That is the
    #: distributed-systems problem itself, not a gap in this record.
    #:
    #: A boolean would have to pick one, and either choice asserts something
    #: false about every real failure. `Trajectory.duplicates` therefore tests
    #: `is True` rather than truthiness: a duplicate this tool reports is one it
    #: can account for, and under-counting is the right direction to be wrong in.
    executed: bool | None = None

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
            "executed": self.executed,
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
        """Repeated *executions* of identical arguments, not repeated successes.

        It counted successes until 1.3.0, and that made it structurally zero:
        the retry loop breaks on the first success, so a trajectory has at most
        one `ok=True` invocation and there was never a second one to find. An
        absolute check, capping the composite at 49, that could not fail.

        The condition it should always have tested is the one that produces
        duplicate work -- **the target ran the call and the caller did not see
        it succeed**, so the caller retried and it ran again. Measured on the
        existing corpus, that had been happening all along: an injected
        MALFORMED fault damages a reply the target produced successfully, and 60
        operations at a 0.4 fault rate contained four of them. All four scored
        zero.

        `is True` rather than truthiness, so an `executed` of `None` is never
        counted. Unknown is not a duplicate, and a duplicate this reports is one
        the proxy can account for.
        """
        executed: set[str] = set()
        duplicates = 0
        for inv in self.invocations:
            if inv.executed is not True:
                continue
            if inv.fingerprint in executed:
                duplicates += 1
            executed.add(inv.fingerprint)
        return duplicates

    @property
    def duplicate_opportunities(self) -> int:
        """Calls the target ran whose success the caller did not see.

        The denominator for `duplicates`, and the reason it exists: zero
        duplicates out of zero opportunities is the absence of evidence, and
        zero out of eleven is a target that is idempotent under retry. They are
        not the same result and were reported as the same number.
        """
        return sum(
            1 for inv in self.invocations if inv.executed is True and not inv.ok
        )

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
class Caveat:
    """A statement about our evidence, not about the target.

    Roughly a third of this project's findings were never findings: "only 8
    operations were disrupted, which bounds the rate rather than measuring it"
    is a fact about the run, and it was sitting in a list of facts about the
    server, taking its severity from whichever dimension it happened to land in.
    The identical sentence rendered CRITICAL from `behavior` and unmarked from
    `fault`, because `CRITICAL_CHECKS` maps to `contract` and `behavior` only.

    **There is deliberately no severity field.** Every caveat is the same speech
    act -- this number is weaker than it looks -- and a severity field is an
    invitation for the surrounding dimension to fill it in again. Caveats render
    in their own register, beside the number they qualify, and the critical
    marker never sees them because they are no longer findings.

    `metrics` is a set rather than one name: contract's no-cases-run suppresses
    `crash_rate` *and* `accepted_invalid`, and its coverage caveat qualifies
    every number the probe reports. Forcing one name would mean duplicating the
    record or picking an arbitrary metric to blame.

    **The twenty-two of these in the codebase are a per-scan maximum, not a
    per-scan expectation, and many are mutually exclusive by construction.** A
    tool with no required fields cannot also emit the multi-field caveat; a
    session that stopped answering cannot also emit the flaky-control one;
    latency's all-requests-failed suppression and its small-sample annotation
    exclude each other. Counting the constructors and concluding the output is
    drowning is a mistake -- the number to look at is how many fire on one scan,
    which is where a cap or a watering-down would do real damage.

    This generalises `CheckResult.skipped` plus its `reason`, which was the same
    idea built once at the policy layer for one kind of caveat;
    `ScanResult.unmeasured_checks` becomes a producer rather than a parallel
    concept.
    """

    #: Which probe observed it.
    probe: str
    #: Metric names this qualifies. Empty only when `scope` says "probe".
    #:
    #: An empty tuple used to mean "everything this probe reported", which is
    #: absence read as presence -- a forgotten argument would have taken the
    #: broadest possible scope from a mistake. Nothing could actually reach that
    #: state (the field has no default, so every site must pass something), but
    #: the meaning was overloaded and this project has spent a month removing
    #: exactly that shape. Probe-wide is now stated.
    metrics: tuple[str, ...]
    #: `suppress` -- the metric is not scored and must not be read as a result.
    #: `annotate` -- it stands, and means less than it looks.
    #: `inapplicable` -- the probe does not apply to this target at all.
    effect: str
    #: One sentence, stating the limit. No severity language.
    reason: str
    #: The flag or change that would remove it, when there is one.
    remedy: str | None = None
    #: "metric" -- qualifies the names in `metrics`.
    #: "probe"  -- qualifies everything this probe reported.
    scope: str = "metric"

    def __post_init__(self) -> None:
        if self.scope not in ("metric", "probe"):
            raise ValueError(f"unknown caveat scope {self.scope!r}")
        if self.scope == "metric" and not self.metrics:
            raise ValueError(
                "a metric-scoped caveat must name at least one metric; pass "
                "scope='probe' to qualify everything the probe reported"
            )
        if self.scope == "probe" and self.metrics:
            raise ValueError(
                f"a probe-scoped caveat cannot also name metrics: {self.metrics}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe": self.probe,
            "metrics": list(self.metrics),
            "scope": self.scope,
            "effect": self.effect,
            "reason": self.reason,
            "remedy": self.remedy,
        }


@dataclass
class ProbeResult:
    """What a probe produces. `grade` is filled in by Probe.grade()."""

    probe: str
    summary: str = ""
    #: 0-100, assigned by the policy engine from this probe's checks. None when
    #: no policy threshold reads anything this probe measures.
    score: float | None = None
    checks: list[CheckResult] = field(default_factory=list)
    phase: str = "baseline"
    #: False when the probe cannot meaningfully run against this target -- cost
    #: against a target that reports no tokens, contract against one with no
    #: tools. An inapplicable probe still reports findings but is left out of
    #: the overall grade, because "we could not measure this" is not a C.
    applicable: bool = True
    metrics: dict[str, Any] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    #: Statements about this measurement rather than about the target. Kept out
    #: of `findings` so they cannot inherit a severity from the dimension they
    #: sit in, and so a renderer can put them beside the number they qualify.
    caveats: list[Caveat] = field(default_factory=list)
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
            "score": self.score,
            "checks": [check.to_dict() for check in self.checks],
            "phase": self.phase,
            "applicable": self.applicable,
            "summary": self.summary,
            "metrics": dict(self.metrics),
            "findings": list(self.findings),
            # Exported alongside findings, not folded into them: a programmatic
            # consumer needs the same "this number is weaker than it looks"
            # signal the three human renderers get, and it is the only consumer
            # that cannot read a dim grey line and infer anything.
            "caveats": [caveat.to_dict() for caveat in self.caveats],
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

    #: 0-100 reliability score, filled in by the policy engine. None when no
    #: policy threshold could be evaluated against this scan at all.
    score: float | None = None
    #: The weighted mean before any failure cap was applied. Equal to `score`
    #: when nothing failed. Kept because the breakdown column sums to *this*,
    #: not to `score`, and a reader who adds the column and gets a different
    #: total stops trusting the report -- correctly.
    uncapped_score: float | None = None
    #: Which cap bit, for the renderers to explain. None when none did.
    cap_reason: str | None = None
    passed: bool | None = None
    policy_name: str | None = None
    pass_score: float | None = None
    #: Total weight of the dimensions this policy actually asks about, stamped
    #: on by `evaluate()`. The denominator `score` is a percentage *of*, and the
    #: one the coverage rule divides by.
    #:
    #: Here rather than recomputed by each renderer because it cannot be derived
    #: from the breakdown: a dimension reports `no_threshold` only if its probe
    #: ran, so a scan that skipped it cannot tell "the policy is silent about
    #: this" from "I did not look". The first verdict line written without it
    #: said "15 of 100" while the rule divided by 85.
    graded_weight: float | None = None

    #: Checks whose probe did not run in this scan. They are skipped, but still
    #: worth showing -- "we never measured this" is information. Kept here
    #: rather than as placeholder probe entries, so `probes` stays an honest
    #: list of what actually ran.
    unmeasured_checks: list[CheckResult] = field(default_factory=list)

    #: Per-dimension points, filled in by the policy engine. Sums to `score`.
    breakdown: list[DimensionScore] = field(default_factory=list)

    @property
    def biggest_gaps(self) -> list[DimensionScore]:
        """Measured dimensions losing the most points, worst first.

        This is what a CI verdict names: an engineer reading a red build wants
        to know which dimension to open, not that the number went down.
        """
        losing = [
            dim for dim in self.breakdown
            if dim.measured and dim.points is not None and dim.points < dim.weight
        ]
        return sorted(losing, key=lambda dim: dim.points - dim.weight)

    @property
    def checks(self) -> list[CheckResult]:
        """Every policy check across every probe, in probe order."""
        return [
            check for probe in self.probes for check in probe.checks
        ] + list(self.unmeasured_checks)

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and not c.skipped]

    def caveats(self) -> list[Caveat]:
        """Every statement about this scan's evidence, from all producers.

        Probe-emitted caveats first, then any policy check that skipped without
        one already covering its metric -- `unmeasured_checks` and `skipped`
        were the same idea built at the policy layer for one case, and they feed
        this rather than sitting beside it. A metric already spoken for is not
        described twice.
        """
        collected = [caveat for probe in self.probes for caveat in probe.caveats]

        # Two probes can observe the same limit on the same number: `fault` and
        # `behavior` both measure recovery, so both emit the thin-sample caveat,
        # and the first render of this channel printed it twice in slightly
        # different words. The metric belongs to whichever probe the policy
        # reads it from -- `recovery_rate_min` reads `behavior.recovery_rate` --
        # so that probe's caveat is the one that survives.
        owner = {check.metric: check.probe for check in self.checks}
        deduped: dict[tuple, Caveat] = {}
        for caveat in collected:
            key = (caveat.metrics, caveat.effect)
            first = caveat.metrics[0] if caveat.metrics else ""
            if key not in deduped or caveat.probe == owner.get(first):
                deduped[key] = caveat
        collected = list(deduped.values())

        spoken_for = {metric for c in collected for metric in c.metrics}

        for check in self.checks:
            if not check.skipped or check.metric in spoken_for:
                continue
            collected.append(Caveat(
                probe=check.probe,
                metrics=(check.metric,),
                effect="suppress",
                # The marker already says it was skipped; the sentence should
                # say why, not repeat the label.
                reason=(check.reason or "this metric was not measured").removeprefix(
                    "skipped: "
                ),
            ))
        return collected

    def probe(self, name: str) -> ProbeResult | None:
        for result in self.probes:
            if result.probe == name:
                return result
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "score": self.score,
            # Additive. The breakdown column sums to `uncapped_score`, not to
            # `score`, so an export carrying only the final number cannot be
            # reconciled against its own rows.
            "uncapped_score": self.uncapped_score,
            "cap_reason": self.cap_reason,
            "breakdown": [dim.to_dict() for dim in self.breakdown],
            "passed": self.passed,
            "policy": self.policy_name,
            "pass_score": self.pass_score,
            "graded_weight": self.graded_weight,
            "started_at": self.started_at.isoformat(),
            "duration_s": self.duration_s,
            "config": dict(self.config),
            "probes": [p.to_dict() for p in self.probes],
        }
