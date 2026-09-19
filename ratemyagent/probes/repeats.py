"""R runs of one task set, and how to report them without lying about them.

**Experimental (1.6.1), not frozen** -- see `docs/API-STABILITY.md`.

An agent is not deterministic, and one run of it supports fewer claims than it
looks like it does. From a single run you can report facts -- this run made 7
calls, 2 effects landed against an expected 1, the agent claimed success. What
you cannot report is a rate, a ratio, a shape, or any sentence with "the agent"
as its subject in the present tense. "The agent does not duplicate" from one
clean run bounds the duplication rate at roughly 95% at the top, which is not a
bound anyone should act on (`DESIGN-AGENT-D.md` (e)).

So repeats, and four presentation rules that are not style choices:

1. **Min-max with the run count, never a mean.** A mean of four runs is a number
   with no denominator attached to it, and it reads as a property of the agent
   rather than as four observations.
2. **An occurrence count for anything that is fundamentally yes/no.** "occurred
   in 2 of 5 runs" is the whole finding for a duplicate; "0-1" is not.
3. **Below n=3, the individual values and no range at all.** A "range" over two
   points is two points with a dash in it.
4. **Scoring takes the worst observed run, and the report says so.** A stated
   choice rather than arithmetic: a duplicate that happens in 1 run of 5 is a
   duplicate, the user's production traffic is not five runs, and a metric that
   averages it away is telling them the failure is smaller than it is.

**And runs are grouped by realized fault placement before any of that.** The
forced schedule is keyed `(task_id, tool, ordinal)`, so if the agent's call
sequence differs between runs the same seed faults a *different call*. Two runs
at one seed are then two different experiments that share a random number, and a
range across them is the spread of the schedule wearing the label of the spread
of the agent. See `probes.fault.realized_schedule`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Below this many runs, print the values rather than a range.
MIN_RUNS_FOR_RANGE = 3

#: Metrics that are really yes/no per run, and get an occurrence count as well
#: as a range. Each is a count whose interesting question is "did it happen at
#: all", not "how large was it on average".
OCCURRENCE_METRICS = (
    "duplicate_mutations",
    "lost_effects",
    "unsupported_claims",
    "lost_acknowledgements",
    "loops_detected",
)


@dataclass
class RunRecord:
    """One run's numbers, and where its faults actually landed."""

    metrics: dict[str, Any]
    placement: str = ""


@dataclass
class MetricRepeat:
    """One metric across R runs of one placement group."""

    metric: str
    values: list[Any] = field(default_factory=list)
    #: `"max"` when a larger value is worse, `"min"` when a smaller one is.
    #: Taken from `THRESHOLD_SPECS` rather than guessed -- see `summarize`.
    direction: str | None = None

    @property
    def measured(self) -> list[Any]:
        """Runs that produced a number. `None` is withheld, never zero."""
        return [v for v in self.values if isinstance(v, (int, float))
                and not isinstance(v, bool)]

    @property
    def runs(self) -> int:
        return len(self.values)

    @property
    def occurrences(self) -> int:
        """Runs in which this happened at all."""
        return sum(1 for v in self.measured if v)

    @property
    def worst(self) -> Any:
        """The run a score is taken from.

        **The worst observed, and only when the direction is known.** Without a
        direction there is no such thing as worst, and picking one arbitrarily
        is how a metric gets scored backwards. `None` then, and the caller
        withholds rather than scoring.
        """
        if not self.measured or self.direction is None:
            return None
        return max(self.measured) if self.direction == "max" else min(self.measured)

    def render(self) -> str:
        """The line a reader sees. Never a mean; see the module docstring."""
        if not self.measured:
            return f"n/a (n={self.runs}; no run produced a value)"

        n = self.runs
        low, high = min(self.measured), max(self.measured)

        if n < MIN_RUNS_FOR_RANGE:
            # Two points with a dash between them is not a range, and printing
            # one invites a reader to treat it as a spread.
            body = ", ".join(_number(v) for v in self.measured)
            text = f"{body} (n={n}; individual runs, too few for a range)"
        elif low == high:
            text = f"{_number(low)} (n={n}; every run)"
        else:
            text = f"{_number(low)}-{_number(high)} (n={n})"

        if self.metric in OCCURRENCE_METRICS:
            hit = self.occurrences
            text += f"; occurred in {hit} of {len(self.measured)} runs"
        if len(self.measured) < n:
            text += f"; {n - len(self.measured)} withheld"
        return text


@dataclass
class RepeatGroup:
    """Runs that faulted the same calls, and are therefore comparable."""

    placement: str
    runs: list[RunRecord] = field(default_factory=list)
    metrics: dict[str, MetricRepeat] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.runs)


def _number(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def group_by_placement(runs: list[RunRecord]) -> list[RepeatGroup]:
    """Split runs into comparable sets, in first-seen order.

    **Runs whose realized placement differs are reported separately.** They are
    not replicates: the schedule caught a different call in each, so the spread
    between them is partly ours. A single range with one seed printed above it
    is the kind of number this project has spent twenty releases learning not to
    print.
    """
    groups: dict[str, RepeatGroup] = {}
    for run in runs:
        group = groups.setdefault(run.placement, RepeatGroup(placement=run.placement))
        group.runs.append(run)
    return list(groups.values())


def summarize(
    runs: list[RunRecord],
    metrics: tuple[str, ...],
    *,
    directions: dict[str, str] | None = None,
) -> list[RepeatGroup]:
    """Group the runs, then summarize each metric within each group.

    `directions` says which way is worse per metric, and comes from
    `policy.THRESHOLD_SPECS` rather than from a table here: a second list of
    directions is a second thing to keep in step, and the scored ones already
    have one.
    """
    directions = directions or {}
    groups = group_by_placement(runs)
    for group in groups:
        for metric in metrics:
            group.metrics[metric] = MetricRepeat(
                metric=metric,
                values=[run.metrics.get(metric) for run in group.runs],
                direction=directions.get(metric),
            )
    return groups


def scored_metrics(group: RepeatGroup) -> dict[str, Any]:
    """The values a policy is evaluated against: the worst run, per metric."""
    return {
        name: repeat.worst
        for name, repeat in group.metrics.items()
        if repeat.worst is not None
    }


def describe_scoring(group: RepeatGroup) -> str:
    """The sentence that has to sit beside a score taken from repeats."""
    return (
        f"Scored against the worst of {group.n} "
        f"{'run' if group.n == 1 else 'runs'}, not the average: a failure that "
        f"happens in one run of {group.n} is a failure, and production traffic "
        f"is not {group.n} runs."
    )


def describe_placements(groups: list[RepeatGroup]) -> str | None:
    """Said out loud when the repeats were not replicates of one experiment."""
    if len(groups) < 2:
        return None
    detail = "; ".join(
        f"{group.n}x [{group.placement or 'no fault reached'}]" for group in groups
    )
    return (
        f"These runs did not all fault the same calls, so they are reported in "
        f"{len(groups)} groups rather than as one range: {detail}. The agent "
        f"chose a different number of calls between runs, so one seed placed "
        f"the fault differently -- a range across them would be the spread of "
        f"the schedule, not of the agent."
    )


__all__ = [
    "MIN_RUNS_FOR_RANGE",
    "OCCURRENCE_METRICS",
    "MetricRepeat",
    "RepeatGroup",
    "RunRecord",
    "describe_placements",
    "describe_scoring",
    "group_by_placement",
    "scored_metrics",
    "summarize",
]
