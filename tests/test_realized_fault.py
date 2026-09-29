"""The fault that took effect, against the fault that was drawn (1.8.0, D).

`FaultProxy._corrupt` and `_lose` leave a reply that already failed alone, so a
`malformed` or lost-reply fault drawn onto one is on the record and nothing was
done to the call. Until 1.8.0 every count of "faults injected" read the draw.
`Invocation.injected` keeps the draw -- it is frozen, and the schedule consumed
it -- and `Invocation.realized_fault` says what happened. Every count reads the
second.

The shipped example is where this first showed: `examples/mock-failing` drew a
`malformed` onto the failing mock's own timeout (sequence 77, `search#61`
attempt 1), published 28 faults injected, and 27 took effect. The regenerated
example is gated by `test_reconciliation`; these assert the arithmetic.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from click.testing import CliRunner

from ratemyagent.cli import cli
from ratemyagent.models import ErrorKind, FaultKind, Request, Response
from ratemyagent.targets.base import Target
from ratemyagent.targets.fault_proxy import FaultConfig, FaultProxy

#: `examples/README.md`'s command for the shipped example.
SHIPPED = [
    "scan", "--target", "mock", "--profile", "failing", "--requests", "40",
    "--concurrency", "16", "--fault-rate", "0.3", "--seed", "42",
]


@pytest.fixture
def proxies(monkeypatch):
    """Every `FaultProxy` a scan builds, so the draws can be read after it."""
    built: list[FaultProxy] = []
    original = FaultProxy.__init__

    def record(self, *args, **kwargs):
        original(self, *args, **kwargs)
        built.append(self)

    monkeypatch.setattr(FaultProxy, "__init__", record)
    return built


def _scan(tmp_path, *args: str) -> dict:
    out = tmp_path / "scan.json"
    result = CliRunner().invoke(cli, [*args, "--output", "scorecard", "--json-out", str(out)])
    assert result.exit_code in (0, 1), result.output
    data = json.loads(out.read_text())
    return {p["probe"]: p for p in data["probes"]}


class TestTheShippedExample:
    """K12: the registered diff, not equality with 1.7.5 (PREDICTIONS-1.8.0 1.1)."""

    def test_27_took_effect_of_28_drawn(self, tmp_path, proxies):
        probes = _scan(tmp_path, *SHIPPED)
        fault = probes["fault"]["metrics"]
        assert fault["calls"] == 119
        assert fault["injected"] == 27
        assert fault["injected_by_kind"] == {
            "connection_refused": 9, "rate_limit": 7, "server_error": 4,
            "timeout": 4, "malformed": 3,
        }
        assert fault["injection_rate"] == pytest.approx(27 / 119)
        (proxy,) = proxies
        drawn = [inv for inv in proxy.invocations if inv.injected is not None]
        assert len(drawn) == 28
        (untaken,) = [inv for inv in drawn if inv.realized_fault is None]
        assert untaken.injected is FaultKind.MALFORMED
        assert (untaken.trajectory_id, untaken.attempt) == ("search#61", 1)
        assert untaken.error_kind is ErrorKind.TIMEOUT

    def test_the_table_and_the_total_agree(self, tmp_path):
        """Both keys read the same field, so the report's rows sum to its total.

        The failing case: `injected_count` moved to `realized_fault` and
        `injected_by_kind` left on `injected` gives 27 against rows summing 28.
        """
        fault = _scan(tmp_path, *SHIPPED)["fault"]["metrics"]
        assert sum(fault["injected_by_kind"].values()) == fault["injected"]

    def test_the_behaviour_counts_move_with_it(self, tmp_path):
        behavior = _scan(tmp_path, *SHIPPED)["behavior"]
        metrics = behavior["metrics"]
        assert metrics["injected_faults_by_kind"]["malformed"] == 2
        assert metrics["unrecovered_by_fault_kind"]["malformed"] == 2
        assert any(
            "most often 4 after connection_refused, 4 after timeout, "
            "3 after rate_limit." in finding
            for finding in behavior["findings"]
        ), behavior["findings"]

    def test_nothing_scored_moved(self, tmp_path):
        """Score and checks are the shipped example's (1.7.5): D reads no score."""
        out = tmp_path / "scan.json"
        CliRunner().invoke(cli, [*SHIPPED, "--output", "scorecard", "--json-out", str(out)])
        data = json.loads(out.read_text())
        behavior = next(p for p in data["probes"] if p["probe"] == "behavior")
        assert (behavior["metrics"]["disrupted"], behavior["metrics"]["recovered"]) == (21, 4)


class TestOnDefaultFlags:
    """CLAUDE.md rule 5 for D: the failing mock with no seed and no rate."""

    def test_every_count_is_the_realized_one(self, tmp_path, proxies):
        fault = _scan(tmp_path, "scan", "--target", "mock", "--profile", "failing")[
            "fault"]["metrics"]
        (proxy,) = proxies
        realized = [inv for inv in proxy.invocations if inv.realized_fault is not None]
        assert fault["injected"] == len(realized) == sum(fault["injected_by_kind"].values())
        untaken = [
            inv for inv in proxy.invocations
            if inv.injected is not None and inv.realized_fault is None
        ]
        # In process a mock is never cleared to mutate, so no lost reply is
        # drawn: the only fault that can fail to take is a malformed one, onto
        # a reply the mock had already failed.
        for inv in untaken:
            assert inv.injected is FaultKind.MALFORMED
            assert inv.ok is False and inv.executed is not True


class _Answers(Target):
    """Answers ok, or fails, as told. Nothing else."""

    def __init__(self, ok: bool) -> None:
        self.ok = ok

    async def setup(self) -> None: ...

    async def teardown(self) -> None: ...

    def describe(self):  # pragma: no cover - not reached
        raise NotImplementedError

    async def invoke(self, request: Request) -> Response:
        if self.ok:
            return Response(ok=True, latency_s=0.01, output={"a": 1, "b": 2})
        return Response(ok=False, latency_s=0.01, error="own failure",
                        error_kind=ErrorKind.SERVER_ERROR)


def _one(kind: FaultKind, ok: bool):
    proxy = FaultProxy(_Answers(ok), FaultConfig(
        rates={}, schedule={("", "op", 1): kind},
    ))
    asyncio.run(proxy.invoke(Request(op="op", payload={})))
    return proxy.invocations[0]


class TestEachKindOnEachReply:
    """The rule `FaultProxy.invoke` sets, kind by kind."""

    @pytest.mark.parametrize("kind", [
        FaultKind.MALFORMED, FaultKind.RESPONSE_LOST,
        FaultKind.RESPONSE_LOST_THEN_CLOSED,
    ])
    def test_a_reply_damaging_fault_takes_only_on_an_ok_reply(self, kind):
        assert _one(kind, ok=True).realized_fault is kind
        failed = _one(kind, ok=False)
        assert (failed.injected, failed.realized_fault) == (kind, None)

    @pytest.mark.parametrize("kind", [
        FaultKind.TIMEOUT, FaultKind.RATE_LIMIT, FaultKind.SERVER_ERROR,
        FaultKind.CONNECTION_REFUSED,
    ])
    @pytest.mark.parametrize("ok", [True, False])
    def test_a_rejecting_fault_always_takes(self, kind, ok):
        assert _one(kind, ok=ok).realized_fault is kind

    def test_no_draw_is_no_fault(self):
        proxy = FaultProxy(_Answers(False), FaultConfig.off())
        asyncio.run(proxy.invoke(Request(op="op", payload={})))
        inv = proxy.invocations[0]
        assert (inv.injected, inv.realized_fault) == (None, None)
        assert proxy.injected_count == 0 and proxy.injected_by_kind() == {}
