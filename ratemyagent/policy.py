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

from .models import CheckResult, ProbeResult, ScanResult

logger = logging.getLogger(__name__)

DEFAULT_POLICY_PATH = Path(__file__).parent / "policies" / "production-default.yaml"

#: Score awarded for exactly meeting a threshold, and for beating it.
COMPLIANT_SCORE = 100.0


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

    @property
    def is_max(self) -> bool:
        return self.direction == "max"


THRESHOLD_SPECS: tuple[ThresholdSpec, ...] = (
    ThresholdSpec("p95_latency_ms", "latency", "p95_s", "max", "p95 latency", "ms", 1000),
    ThresholdSpec("p99_latency_ms", "latency", "p99_s", "max", "p99 latency", "ms", 1000),
    ThresholdSpec("error_rate_max", "latency", "error_rate", "max", "error rate", "rate"),
    ThresholdSpec(
        "recovery_rate_min", "behavior", "recovery_rate", "min", "recovery rate", "rate"
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


@dataclass
class Policy:
    """What "reliable enough" means for one project."""

    name: str = "unnamed"
    thresholds: dict[str, float] = field(default_factory=dict)
    pass_score: float = 75.0
    description: str = ""

    def __post_init__(self) -> None:
        if not 0 <= self.pass_score <= 100:
            raise PolicyError(f"pass_score must be between 0 and 100, got {self.pass_score}")

        unknown = set(self.thresholds) - set(SPECS_BY_NAME)
        if unknown:
            known = ", ".join(sorted(SPECS_BY_NAME))
            raise PolicyError(
                f"unknown threshold(s): {', '.join(sorted(unknown))}. Known keys: {known}"
            )

        for key, value in self.thresholds.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise PolicyError(f"threshold {key} must be a number, got {value!r}")
            if value < 0:
                raise PolicyError(f"threshold {key} cannot be negative, got {value}")

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

        return cls(
            name=str(data.get("name", "unnamed")),
            thresholds=dict(thresholds),
            pass_score=float(data.get("pass_score", 75.0)),
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
            "pass_score": self.pass_score,
        }


def score_check(spec: ThresholdSpec, threshold: float, observed: float | None) -> CheckResult:
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
        threshold = float(policy.thresholds[spec.name])
        probe = by_probe.get(spec.probe)
        observed = _observed(probe, spec)
        check = score_check(spec, threshold, observed)

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

    all_scored = [c for c in result.checks if not c.skipped]
    result.score = _mean(c.score for c in all_scored) if all_scored else None
    result.policy_name = policy.name
    result.pass_score = policy.pass_score
    result.passed = None if result.score is None else result.score >= policy.pass_score
    return result


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
