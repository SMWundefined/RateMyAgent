"""Reliability policy: thresholds in, a 0-100 score out.

Replaces the A-F grades used through week 3. The difference that matters is not
the numbers but who owns them: probes now only *measure*, and the policy decides
what those measurements are worth. That is what makes the bar configurable per
project instead of baked into each probe.

Scoring rule, in full:

- **Meeting the threshold scores 100 for that check.** A threshold is a limit,
  not a target -- being ten times under it is not ten times better, it is the
  same "within policy".
- **Missing it decays linearly to 0 at twice the limit** (for a `max` threshold)
  or at zero (for a `min` threshold), so a near miss and a catastrophe are not
  scored the same.
- **A `max` threshold of 0 is absolute**: any violation scores 0. That is what
  `duplicate_mutation_max: 0` is for.
- **A metric the scan could not produce is skipped**, not scored zero. Missing
  evidence is not a failure, and counting it as one would punish scanning an
  MCP server for having no token costs.

The overall score is the mean of every check that ran. Every check reports its
own number and the sentence explaining it, so the total can always be taken
apart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .models import CheckResult, DimensionScore, ProbeResult, ScanResult

logger = logging.getLogger(__name__)

DEFAULT_POLICY_PATH = Path(__file__).parent / "policies" / "production-default.yaml"

#: Score awarded for exactly meeting a threshold, and for beating it.
COMPLIANT_SCORE = 100.0

#: How much of the 100 points each dimension is worth. Behaviour carries the
#: largest share because recovery, amplification and duplicate mutations are the
#: questions this tool exists to answer -- a fast target that loses work under
#: failure is not a reliable one. `fault` has no weight: it is the injector, not
#: a judged dimension; what it produced is scored under `behavior`.
#: How `behavior`'s 35 points divide once both halves are measurable.
#:
#: Nested inside the dimension rather than split into two peer dimensions, and
#: that is deliberate: peers would change the denominator. Behaviour is 35 of
#: the 85 points available on an MCP target, 41% of its score. As peers
#: (survivability 20, caller strategy 15) the caller half renormalises out and
#: survivability becomes 20 of 70, 29% -- silently reweighting every server scan
#: on top of the change actually being made.
#:
#: Nothing observes this today. Caller strategy is inapplicable for every target
#: type that exists, so survivability carries all 35 and these numbers cannot
#: change any score. They are constants rather than policy keys for exactly that
#: reason: a YAML knob no target can exercise is configuration nothing can test.
#: Move them into the policy file in the release that lands `AgentTarget`, where
#: both halves apply and the split becomes observable.
SURVIVABILITY_SHARE = 20.0
CALLER_STRATEGY_SHARE = 15.0

DEFAULT_WEIGHTS: dict[str, float] = {
    "latency": 20.0,
    "cost": 15.0,
    "concurrency": 15.0,
    "contract": 15.0,
    "behavior": 35.0,
}

DIMENSION_LABELS: dict[str, str] = {
    "latency": "latency",
    "cost": "cost",
    "concurrency": "concurrency",
    "contract": "contract",
    "behavior": "behavior",
    "fault": "fault injection",
}


class PolicyError(ValueError):
    """A policy file that cannot be loaded or does not make sense."""


@dataclass(frozen=True)
class ThresholdSpec:
    """Wires one policy key to one probe metric.

    `scale` converts the probe's unit into the policy's: probes measure latency
    in seconds, policies are written in milliseconds because that is how SLOs
    are written.
    """

    name: str
    probe: str
    metric: str
    direction: str
    label: str
    units: str = ""
    scale: float = 1.0
    #: A metric on the same probe that supplies this threshold at runtime,
    #: overriding the policy file's literal value.
    #:
    #: Exists for one check and is deliberately general anyway: a threshold that
    #: is a property of the scan's own flags rather than of the org's standards
    #: does not belong in a YAML file an org edits. `recovery_rate_min` is the
    #: case -- the injector produces `1 - fault_rate**max_retries` against a
    #: target that never fails, so a fixed 0.90 grades `--fault-rate` and not
    #: the target.
    #:
    #: The policy value stays as the fallback for scans where the derivation is
    #: unavailable (no fault phase, or an unknown retry budget). Both the value
    #: used and where it came from are reported, because two scans at different
    #: fault rates are not comparable and the report has to say so.
    derived_from: str | None = None

    @property
    def is_max(self) -> bool:
        return self.direction == "max"


THRESHOLD_SPECS: tuple[ThresholdSpec, ...] = (
    ThresholdSpec("p95_latency_ms", "latency", "p95_s", "max", "p95 latency", "ms", 1000),
    ThresholdSpec("p99_latency_ms", "latency", "p99_s", "max", "p99 latency", "ms", 1000),
    ThresholdSpec("error_rate_max", "latency", "error_rate", "max", "error rate", "rate"),
    ThresholdSpec(
        "recovery_rate_min", "behavior", "recovery_rate", "min", "recovery rate", "rate",
        derived_from="recovery_floor",
    ),
    ThresholdSpec(
        "retry_amplification_max", "behavior", "retry_amplification", "max",
        "retry amplification", "x",
    ),
    ThresholdSpec(
        "duplicate_mutation_max", "behavior", "duplicate_mutations", "max",
        "duplicate mutations", "",
    ),
    ThresholdSpec(
        "cost_per_request_max", "cost", "cost_per_request", "max", "cost per request", "$"
    ),
    ThresholdSpec(
        "concurrency_min", "concurrency", "max_sustained_concurrency", "min",
        "sustained concurrency", "",
    ),
    ThresholdSpec(
        "contract_crash_rate_max", "contract", "crash_rate", "max",
        "contract crash rate", "rate",
    ),
    ThresholdSpec(
        "contract_invalid_accepted_max", "contract", "accepted_invalid", "max",
        "invalid inputs accepted", "",
    ),
)

SPECS_BY_NAME: dict[str, ThresholdSpec] = {spec.name: spec for spec in THRESHOLD_SPECS}

#: Thresholds that are still accepted in a policy file but no longer scored.
#:
#: `concurrency_min` passed only when the ramp reached the configured ceiling,
#: which means it compared `--concurrency` against itself. Scanning a healthy
#: hosted server with `--concurrency 3` failed it, capped the composite at 89 and
#: printed FAIL -- while the probe's own finding said "the configured ceiling ...
#: a floor set by the test, not a measurement of the target". The 0.1.8 caps did
#: not create that; they made it loud enough to notice.
#:
#: Saturation point, latency knee and goodput are still reported as findings.
#: They are worth reading and were never a policy question. A ramp that
#: genuinely saturates *below* its ceiling would be a real measurement and could
#: be scored -- as a differently named check, once a server produces one.
#:
#: Kept in `SPECS_BY_NAME` rather than deleted so an existing policy file still
#: loads: validation rejects unknown keys, and silently breaking every policy in
#: the wild is a worse outcome than an ignored line.
DEPRECATED_THRESHOLDS: frozenset[str] = frozenset({"concurrency_min"})


@dataclass
class Policy:
    """What "reliable enough" means for one project."""

    name: str = "unnamed"
    thresholds: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    pass_score: float = 75.0
    #: Ceilings a failed check imposes on the composite. The score is a weighted
    #: mean, so a failure can be averaged away to nothing: recovery at 85.7%
    #: against a 90% floor scored 95.2, diluted across three behaviour checks,
    #: and cost 0.6 points out of 100. These make the minimum cost of any
    #: failure "cannot score in the 90s". In the policy file, not hardcoded,
    #: because how much a failure should hurt is a project's call.
    fail_cap: float = 89.0
    absolute_fail_cap: float = 49.0
    description: str = ""

    def __post_init__(self) -> None:
        if not 0 <= self.pass_score <= 100:
            raise PolicyError(f"pass_score must be between 0 and 100, got {self.pass_score}")

        for name in ("fail_cap", "absolute_fail_cap"):
            value = getattr(self, name)
            if not 0 <= value <= 100:
                raise PolicyError(f"{name} must be between 0 and 100, got {value}")
        if self.absolute_fail_cap > self.fail_cap:
            raise PolicyError(
                f"absolute_fail_cap ({self.absolute_fail_cap}) must not exceed fail_cap "
                f"({self.fail_cap}): breaking an absolute rule cannot score better than "
                "missing a graded threshold"
            )

        unknown = set(self.thresholds) - set(SPECS_BY_NAME)
        if unknown:
            known = ", ".join(sorted(SPECS_BY_NAME))
            raise PolicyError(
                f"unknown threshold(s): {', '.join(sorted(unknown))}. Known keys: {known}"
            )

        retired = sorted(set(self.thresholds) & DEPRECATED_THRESHOLDS)
        for key in retired:
            logger.warning(
                "policy sets %r, which is no longer scored: it compared the "
                "--concurrency ceiling against itself. Saturation point and "
                "latency knee are still reported as findings.", key,
            )
            del self.thresholds[key]

        for key, value in self.thresholds.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise PolicyError(f"threshold {key} must be a number, got {value!r}")
            if value < 0:
                raise PolicyError(f"threshold {key} cannot be negative, got {value}")

        for key, value in self.weights.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise PolicyError(f"weight {key} must be a number, got {value!r}")
            if value < 0:
                raise PolicyError(f"weight {key} cannot be negative, got {value}")
        if self.weights and sum(self.weights.values()) <= 0:
            raise PolicyError("weights must not all be zero")

    @property
    def specs(self) -> list[ThresholdSpec]:
        """Threshold specs this policy actually sets, in declaration order."""
        return [spec for spec in THRESHOLD_SPECS if spec.name in self.thresholds]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Policy":
        if not isinstance(data, dict):
            raise PolicyError(f"policy must be a mapping, got {type(data).__name__}")

        thresholds = data.get("thresholds") or {}
        if not isinstance(thresholds, dict):
            raise PolicyError("policy 'thresholds' must be a mapping")
        if not thresholds:
            raise PolicyError("policy defines no thresholds, so nothing can be scored")

        weights = data.get("weights")
        if weights is not None and not isinstance(weights, dict):
            raise PolicyError("policy 'weights' must be a mapping")

        return cls(
            name=str(data.get("name", "unnamed")),
            thresholds=dict(thresholds),
            weights=dict(weights) if weights else dict(DEFAULT_WEIGHTS),
            pass_score=float(data.get("pass_score", 75.0)),
            fail_cap=float(data.get("fail_cap", 89.0)),
            absolute_fail_cap=float(data.get("absolute_fail_cap", 49.0)),
            description=str(data.get("description", "")),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        """Read a policy from a YAML file."""
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - yaml is a hard dependency
            raise PolicyError("reading policy files needs PyYAML") from exc

        resolved = Path(path)
        if not resolved.exists():
            raise PolicyError(f"policy file not found: {resolved}")

        try:
            data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PolicyError(f"{resolved} is not valid YAML: {exc}") from exc

        if data is None:
            raise PolicyError(f"{resolved} is empty")
        return cls.from_dict(data)

    @classmethod
    def default(cls) -> "Policy":
        """The shipped production-default policy."""
        return cls.load(DEFAULT_POLICY_PATH)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "thresholds": dict(self.thresholds),
            "weights": dict(self.weights),
            "pass_score": self.pass_score,
        }


def score_check(
    spec: ThresholdSpec,
    threshold: float,
    observed: float | None,
    threshold_source: str = "policy",
) -> CheckResult:
    """Turn one metric into one 0-100 check."""
    if observed is None:
        return CheckResult(
            name=spec.name,
            probe=spec.probe,
            metric=spec.metric,
            direction=spec.direction,
            threshold=threshold,
            observed=None,
            score=0.0,
            passed=True,
            units=spec.units,
            threshold_source=threshold_source,
            reason=(
                f"skipped: the {spec.probe} probe reported no {spec.label}, "
                "so this threshold could not be evaluated"
            ),
        )

    scaled = observed * spec.scale
    score, passed = _score_value(spec, threshold, scaled)

    comparison = "at most" if spec.is_max else "at least"
    reason = (
        f"{spec.label} was {_fmt(scaled, spec.units)}, policy allows "
        f"{comparison} {_fmt(threshold, spec.units)}"
    )

    return CheckResult(
        name=spec.name,
        probe=spec.probe,
        metric=spec.metric,
        direction=spec.direction,
        threshold=threshold,
        observed=scaled,
        score=score,
        passed=passed,
        units=spec.units,
        threshold_source=threshold_source,
        reason=reason,
    )


def _score_value(spec: ThresholdSpec, threshold: float, observed: float) -> tuple[float, bool]:
    """The scoring curve. See this module's docstring."""
    if spec.is_max:
        if observed <= threshold:
            return COMPLIANT_SCORE, True
        if threshold == 0:
            # An absolute rule: "no duplicate mutations" has no partial credit.
            return 0.0, False
        overshoot = (observed - threshold) / threshold
        return max(0.0, COMPLIANT_SCORE * (1.0 - overshoot)), False

    if observed >= threshold:
        return COMPLIANT_SCORE, True
    if threshold == 0:
        # Nothing was required.
        return COMPLIANT_SCORE, True
    return max(0.0, COMPLIANT_SCORE * (observed / threshold)), False


def _threshold_for(
    spec: ThresholdSpec, policy: Policy, probe: ProbeResult | None
) -> tuple[float, str]:
    """The threshold to score against, and where it came from.

    A spec with `derived_from` prefers the value the probe measured, because for
    that check the policy file cannot know the right number: it depends on the
    flags this particular scan ran with. Falls back to the policy literal when
    the probe did not run or could not derive one -- an org's written-down
    standard is a better default than nothing, and the fallback is labelled so
    nobody has to guess which applied.
    """
    literal = float(policy.thresholds[spec.name])
    if spec.derived_from is None or probe is None:
        return literal, "policy"

    derived = probe.metrics.get(spec.derived_from)
    if derived is None or isinstance(derived, bool) or not isinstance(derived, (int, float)):
        return literal, "policy"
    return float(derived), spec.derived_from


def evaluate(result: ScanResult, policy: Policy) -> ScanResult:
    """Score a completed scan against a policy, in place.

    Attaches each check to the probe that supplied its metric, gives every probe
    the mean of its own checks, and sets the scan's overall score, pass flag and
    policy name.
    """
    by_probe: dict[str, ProbeResult] = {p.probe: p for p in result.probes}
    for probe in result.probes:
        probe.checks = []
        probe.score = None

    result.unmeasured_checks = []
    for spec in policy.specs:
        probe = by_probe.get(spec.probe)
        threshold, source = _threshold_for(spec, policy, probe)
        observed = _observed(probe, spec)
        check = score_check(spec, threshold, observed, threshold_source=source)

        if probe is not None:
            probe.checks.append(check)
        else:
            # The probe never ran. Record the skip on the scan rather than
            # inventing a probe result for it: `probes` must stay a list of what
            # actually ran.
            logger.debug("no %s probe in this scan; %s skipped", spec.probe, spec.name)
            result.unmeasured_checks.append(check)

    for probe in result.probes:
        scored = [c for c in probe.checks if not c.skipped]
        probe.score = _mean(c.score for c in scored) if scored else None

    result.breakdown = _breakdown(result, policy)
    result.uncapped_score = _weighted_score(result.breakdown)
    result.cap_reason = None
    result.score = _apply_caps(result.uncapped_score, result, policy)
    result.policy_name = policy.name
    result.pass_score = policy.pass_score
    # A conjunction, not a threshold. The composite is a weighted mean, so a
    # failed check can be averaged away to almost nothing: a recovery rate of
    # 85.7% against a 90% floor scores 95.2, dilutes across three behaviour
    # checks, and costs 0.6 points -- a scan reporting 99/100 with FAIL beside
    # recovery rate in its own table. The score summarises; the verdict must not
    # contradict the evidence printed underneath it.
    if result.score is None:
        result.passed = None
    else:
        failed = [c for c in result.checks if not c.passed and not c.skipped]
        result.passed = result.score >= policy.pass_score and not failed
    return result


def _breakdown(result: ScanResult, policy: Policy) -> list[DimensionScore]:
    """Points per dimension, in weight order.

    A dimension the scan could not measure keeps its row -- "not measured" is
    worth showing -- but drops out of the denominator, so an MCP server with no
    token costs is not capped below 100 for it.
    """
    dimensions: list[DimensionScore] = []

    for probe_name, weight in policy.weights.items():
        if weight <= 0:
            continue
        probe = result.probe(probe_name)
        dimensions.append(
            DimensionScore(
                probe=probe_name,
                label=DIMENSION_LABELS.get(probe_name, probe_name),
                score=probe.score if probe else None,
                weight=float(weight),
                note=_dimension_note(probe, result, probe_name),
            )
        )
    return dimensions


def _dimension_note(probe: ProbeResult | None, result: ScanResult, name: str) -> str:
    """A short reason, shown beside the points."""
    if probe is None:
        return "probe did not run"
    if not probe.applicable:
        return "not measured against this target"
    if probe.score is None:
        return "no policy threshold reads it"

    failed = [c for c in probe.checks if not c.passed and not c.skipped]
    if not failed:
        return ""
    worst = min(failed, key=lambda c: c.score)
    return worst.reason


#: Thresholds where any failure is categorical rather than a matter of degree.
#: Both are limits of zero, so there is no "slightly over": a duplicated mutation
#: happened or it did not, and a crashed transport crashed.
ABSOLUTE_CHECKS = frozenset({"duplicate_mutation_max", "contract_crash_rate_max"})


def _apply_caps(score: float | None, result: ScanResult, policy: Policy) -> float | None:
    """Bound the composite by what failed, not just by the weighted mean.

    The mean lets a healthy dimension pay for a broken one, which is wrong for
    reliability: a service that is fast, cheap, and loses 14% of its retries is
    not 99% reliable. Two bands, both from the policy file.
    """
    if score is None:
        return None

    failed = [c for c in result.checks if not c.passed and not c.skipped]
    if not failed:
        return score

    absolute = [c for c in failed if c.name in ABSOLUTE_CHECKS]
    cap = policy.absolute_fail_cap if absolute else policy.fail_cap
    if score <= cap:
        return score

    # Raw check names, not display labels: policy must not import from outputs.
    named = ", ".join(c.name for c in (absolute or failed)[:2])
    result.cap_reason = (
        f"capped at {cap:g} from {score:.0f}: "
        f"{'absolute rule broken' if absolute else 'check failed'} ({named})"
    )
    return cap


def _weighted_score(breakdown: list[DimensionScore]) -> float | None:
    """Weighted mean over the dimensions that were actually measured."""
    measured = [dim for dim in breakdown if dim.measured and dim.weight > 0]
    if not measured:
        return None

    earned = sum(dim.points or 0.0 for dim in measured)
    available = sum(dim.weight for dim in measured)
    return (earned / available) * 100.0 if available else None


def _observed(probe: ProbeResult | None, spec: ThresholdSpec) -> float | None:
    """Read a metric off a probe, or None when it is unavailable."""
    if probe is None or not probe.applicable:
        return None
    value = probe.metrics.get(spec.metric)
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    return float(value)


def _mean(values: Iterable[float]) -> float:
    collected = list(values)
    return sum(collected) / len(collected) if collected else 0.0


def _fmt(value: float, units: str) -> str:
    if units == "ms":
        return f"{value:,.0f}ms"
    if units == "rate":
        return f"{value:.1%}"
    if units == "$":
        return f"${value:.4f}"
    if units == "x":
        return f"{value:.2f}x"
    if float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.2f}"


__all__ = [
    "COMPLIANT_SCORE",
    "DEFAULT_POLICY_PATH",
    "SPECS_BY_NAME",
    "THRESHOLD_SPECS",
    "Policy",
    "PolicyError",
    "ThresholdSpec",
    "evaluate",
    "score_check",
]
