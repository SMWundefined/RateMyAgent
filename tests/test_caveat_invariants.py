"""`Caveat.effect` is a claim about the scan; these are what make it checkable.

The field had no consumer in `policy.py` and twelve constructors passing
`effect="suppress"`. Every one of them relied on the metric already being
`None` somewhere upstream -- none of them caused suppression -- so the field was
decorative in exactly the way section 8b keeps cataloguing: a label describing
an invariant that nothing asserted.

It is kept rather than deleted, because `ScanResult.caveats` already runs the
inverse mapping (a skipped check becomes a synthesized `suppress` caveat) and
two representations that are meant to agree should be made to. These tests are
that agreement, in both directions:

- **suppress -> None.** A caveat saying a metric is not scored, beside a metric
  that *is* scored, is a report contradicting itself.
- **skipped -> caveat.** A metric silently dropped from the score with nothing
  saying why is the failure the caveat channel was built to end.

Run across every mock profile and both fault settings, because the interesting
caveats only fire in particular corners: no faults injected, nothing completed,
a sample too thin to decide, a model with no published price.
"""

from __future__ import annotations

import pytest

from ratemyagent import Policy, scan
from ratemyagent.probes import ProbeConfig
from ratemyagent.targets import MockTarget

PROFILES = ("healthy", "degraded", "failing", "saturating", "bloated")
#: (requests, fault_rate). 0.0 exercises the no-faults suppression; 0.9 pushes
#: recovery far enough that the undecidable-interval path fires on a real run.
SETTINGS = ((20, 0.0), (20, 0.3), (40, 0.9))


async def _scan(profile: str, requests: int, fault_rate: float):
    async with getattr(MockTarget, profile)() as target:
        return await scan(
            target,
            config=ProbeConfig(
                requests=requests, warmup=0, extra={"fault_rate": fault_rate}
            ),
            policy=Policy.default(),
        )


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("requests,fault_rate", SETTINGS)
class TestSuppressCaveatsMatchUnscoredMetrics:
    async def test_every_suppress_caveat_names_a_metric_that_is_none(
        self, profile, requests, fault_rate
    ):
        """Direction 1: a suppress caveat must be telling the truth.

        Checked against the probe that emitted it, not against the scan, so a
        caveat cannot be satisfied by some *other* probe happening to leave the
        same metric name unset.
        """
        result = await _scan(profile, requests, fault_rate)

        for probe in result.probes:
            for caveat in probe.caveats:
                if caveat.effect != "suppress":
                    continue
                for metric in caveat.metrics:
                    assert metric in probe.metrics, (
                        f"{probe.probe} suppresses {metric!r}, which it does not "
                        "report at all -- the caveat names nothing"
                    )
                    assert probe.metrics[metric] is None, (
                        f"{probe.probe} says {metric!r} is suppressed but reports "
                        f"{probe.metrics[metric]!r}. A caveat claiming a number is "
                        "not scored, beside a number that is, is a report "
                        "contradicting itself."
                    )

    async def test_every_skipped_check_has_a_caveat_explaining_it(
        self, profile, requests, fault_rate
    ):
        """Direction 2: nothing leaves the score silently.

        `ScanResult.caveats()` back-fills these, so this asserts the back-fill
        actually covers every skip rather than trusting that it does.

        Note `checks` is a property and `caveats()` is a method -- an asymmetry
        in the public API that this test tripped over on its first run. Left
        alone deliberately: renaming either is a breaking change and the API
        freeze in NextSteps section 0.3 is where that decision belongs.
        """
        result = await _scan(profile, requests, fault_rate)
        spoken_for = {m for caveat in result.caveats() for m in caveat.metrics}

        for check in result.checks:
            if not check.skipped:
                continue
            assert check.metric in spoken_for, (
                f"{check.name} was skipped, so it left both sides of the score, "
                "and no caveat says why. That is the silence this channel exists "
                "to end."
            )

    async def test_no_scored_metric_is_described_as_suppressed(
        self, profile, requests, fault_rate
    ):
        """The same invariant from the policy side rather than the probe side.

        Direction 1 checks probe-local consistency. This checks that nothing the
        *score* actually used is described anywhere as suppressed -- the case
        where two probes report the same metric name and only one nulls it.
        """
        result = await _scan(profile, requests, fault_rate)
        suppressed = {
            metric
            for caveat in result.caveats()
            if caveat.effect == "suppress"
            for metric in caveat.metrics
        }

        for check in result.checks:
            if check.skipped:
                continue
            assert check.metric not in suppressed, (
                f"{check.name} scored {check.observed!r} for {check.metric!r} "
                "while a caveat calls that metric suppressed"
            )


class TestTheInvariantCanFail:
    """The deliberate failing case: these tests must be able to catch a break.

    A consistency test that passes because both sides are empty is the shape
    section 8b lists six times. So: prove the corpus actually contains suppress
    caveats and skipped checks, and prove a hand-broken pair is caught.
    """

    async def test_the_corpus_actually_exercises_both_directions(self):
        seen_suppress = seen_skipped = 0
        for profile in PROFILES:
            for requests, fault_rate in SETTINGS:
                result = await _scan(profile, requests, fault_rate)
                seen_suppress += sum(
                    1
                    for probe in result.probes
                    for caveat in probe.caveats
                    if caveat.effect == "suppress"
                )
                seen_skipped += sum(1 for check in result.checks if check.skipped)

        assert seen_suppress > 0, "no suppress caveat fired anywhere; direction 1 is vacuous"
        assert seen_skipped > 0, "no check skipped anywhere; direction 2 is vacuous"

    async def test_a_metric_that_is_scored_while_called_suppressed_is_caught(self):
        """Break it on purpose: restore a suppressed metric and re-check."""
        result = await _scan("healthy", 20, 0.0)

        behavior = result.probe("behavior")
        suppressed = [
            metric
            for caveat in behavior.caveats
            if caveat.effect == "suppress"
            for metric in caveat.metrics
        ]
        assert suppressed, "this fixture no longer suppresses anything"

        behavior.metrics[suppressed[0]] = 0.99

        with pytest.raises(AssertionError, match="contradicting itself"):
            for probe in result.probes:
                for caveat in probe.caveats:
                    if caveat.effect != "suppress":
                        continue
                    for metric in caveat.metrics:
                        assert probe.metrics.get(metric) is None, (
                            "a caveat claiming a number is not scored, beside a "
                            "number that is, is a report contradicting itself."
                        )
