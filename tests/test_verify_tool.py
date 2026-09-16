"""`--verify-tool`: counting applied effects, per operation.

1.3.1 withheld `duplicate_mutations` on every scan, because the scanner saw
deliveries and not effects. 1.4.0 reads the target's own state through a
read-only tool, before and after the retried operations, and counts per
operation.

**Why per operation, and not an aggregate.** With `E` effects against `S`
successes, one duplicated operation and one lost effect cancel: `E == S`, both
metrics zero, and a clean report over a target that both double-applied one call
and dropped another. `test_a_duplicate_and_a_loss_do_not_cancel` is that case,
and it is the reason the arithmetic is per id.

**Why a window diff, and not the after count.** Counting only the after
snapshot assumes the window started empty. Ids are derived from `--seed`, so a
second run of the same seeded scan against a persistent target starts with its
own leftovers in place: `test_a_repeat_run_is_stale` pins that as `stale`,
never as a pass.

**Why no delivered gate.** Gating duplicates on an acknowledged delivery would
drop the case that matters most -- a real timeout the proxy cannot confirm,
followed by a retry that applies the work again.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from ratemyagent import Policy, scan
from ratemyagent.models import FaultKind
from ratemyagent.probes import ProbeConfig
from ratemyagent.probes import fault as fault_probe
from ratemyagent.probes.concurrency import _ladder
from ratemyagent.targets import MCPTarget, MockTarget
from ratemyagent.targets.base import TargetError
from ratemyagent.targets.fault_proxy import FaultProxy

ROOT = Path(__file__).resolve().parents[1]
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"
TOOL = "event"
VERIFY = "effects"
ARGS = {"id": "{op_id}", "payload": "p"}
FLAT_ARGS = {"id": "evt-1", "payload": "p"}


def _uri(state: Path, *extra: str, mode: str = "append") -> str:
    return "stdio://" + shlex.join(
        [sys.executable, str(TWIN), "--mode", mode, "--state", str(state), *extra]
    )


def _target(
    state: Path, *extra: str, args=None, verify=VERIFY, count="entries",
    mode: str = "append",
):
    return MCPTarget(
        _uri(state, *extra, mode=mode),
        tool=TOOL,
        tool_args=dict(args if args is not None else ARGS),
        allow_mutating=True,
        timeout_s=20,
        verify_tool=verify,
        verify_count=count,
    )


class ScheduledProxy(FaultProxy):
    """Faults exactly the (operation, attempt) pairs named, and nothing else."""

    schedule: dict[tuple[str, int], FaultKind] = {}

    def _choose_fault(self, request, attempt):
        return self.schedule.get((request.trajectory_key, attempt))


def _scheduled(monkeypatch, schedule):
    def build(inner, faults):
        proxy = ScheduledProxy(inner, faults)
        proxy.schedule = dict(schedule)
        return proxy

    monkeypatch.setattr(fault_probe, "FaultProxy", build)


async def _scan(target, *, requests=2, seed=7):
    return await scan(
        target,
        probes=["latency", "fault", "behavior"],
        config=ProbeConfig(
            requests=requests, warmup=0, timeout_s=20.0, seed=seed,
            extra={"fault_rate": 0.3},
        ),
        policy=Policy.default(),
    )


#: The shipped defaults. Arms that pass no `seed` test the configuration a user
#: actually gets, and that is the one the suite missed: the default `--seed`
#: equals `MCPTarget`'s constructor salt, so before the `recovery:` namespace
#: the concurrency ramp had already sent every id the oracle went on to
#: register, and a first run on clean state reported `stale`.
DEFAULTS = ProbeConfig()


async def _scan_defaults(target, *, requests=3):
    """A scan on default flags: default seed, default warmup, default ramp.

    The concurrency probe is included on purpose -- it is the phase whose
    indices covered the recovery window -- and no `seed` is passed, because
    naming one is exactly what hid the collision.
    """
    return await scan(
        target,
        probes=["latency", "concurrency", "fault", "behavior"],
        config=ProbeConfig(
            requests=requests, timeout_s=20.0, extra={"fault_rate": 0.3},
        ),
        policy=Policy.default(),
    )


def _bare_target(state: Path):
    """An adapter that never connects: only the id derivation is under test."""
    target = _target(state)
    target._probe_tool = TOOL
    target._probe_args = dict(ARGS)
    return target


def _ids(target, salt, count: int, offset: int) -> set[str]:
    """The ids a probe sends, built by the adapter's own request builder."""
    target._op_id_salt = salt
    return {
        request.payload["id"]
        for request in target.probe_requests(count, offset=offset)
    }


def _sent_before_recovery(
    target, *, warmup: int, concurrency: int, requests: int, seed, baseline_salt
) -> set[str]:
    """Every id the scan sends before the oracle's window opens.

    Each phase is called the way its own probe calls it, and the ramp uses the
    real `_ladder`, so the offsets are not arithmetic repeated here -- which is
    the arithmetic that was wrong.
    """
    ids: set[str] = set()

    # Preflight, at setup: `MCPTarget._preflight` sends `sample_request(0)`.
    target._op_id_salt = baseline_salt
    ids.add(target.sample_request(0).payload["id"])

    # Latency (latency.py): warmup, then the measured run at `offset=warmup`.
    ids |= _ids(target, baseline_salt, warmup, 0)
    ids |= _ids(target, baseline_salt, requests, warmup)

    # The concurrency ramp (concurrency.py): restarts at 0, `requests` a level.
    offset = 0
    for _level in _ladder(concurrency):
        ids |= _ids(target, baseline_salt, requests, offset)
        offset += requests

    # The degradation pass re-runs the baseline probes under the chaos salt.
    ids |= _ids(target, seed, warmup, 0)
    ids |= _ids(target, seed, requests, warmup)
    return ids


async def _registered_by_the_probe(
    state: Path, monkeypatch, *, seed, warmup: int, requests: int
) -> set[str]:
    """What `_recovery_pass` actually registered, by running it.

    The sweep below derives the registered set itself, and that cannot notice a
    *call site* which stops asking for the namespace: reverting one line in
    `_recovery_pass` left the sweep comparing against a namespace nothing
    writes, and it stayed green. So the pass is run here, in process, and asked.
    """
    from ratemyagent.models import Response
    from ratemyagent.targets.fault_proxy import FaultConfig

    async def answers(self, request):
        return Response(ok=True, latency_s=0.0)

    async def empty(self):
        return []

    monkeypatch.setattr(MCPTarget, "invoke", answers)
    monkeypatch.setattr(MCPTarget, "read_effect_entries", empty)

    injector = fault_probe.FaultInjector()
    await injector._recovery_pass(
        FaultProxy(_bare_target(state), FaultConfig()),
        ProbeConfig(requests=requests, warmup=warmup, seed=seed, timeout_s=5.0),
    )
    return set(injector._oracle._ids.values())


def _behavior(result) -> dict:
    return result.probe("behavior").metrics


def _caveat_reasons(result, metric: str) -> list[str]:
    return [
        caveat.reason for caveat in result.probe("behavior").caveats
        if metric in caveat.metrics
    ]


class TestPerOperationCounting:
    @pytest.mark.parametrize("seed", [7, DEFAULTS.seed])
    async def test_a_duplicate_and_a_loss_do_not_cancel(
        self, tmp_path, monkeypatch, seed
    ):
        """The mixed window: aggregates report a clean run, per-op does not.

        Operation `event#2` is applied twice -- its reply is dropped, the retry
        lands -- and `event#3` is acknowledged without ever being applied. An
        aggregate `effects - successes` is zero. The truth is one of each.

        Run at the default seed as well: the counting must not depend on which
        seed was named, and every arm here used to name one.
        """
        _scheduled(monkeypatch, {(f"{TOOL}#2", 1): FaultKind.RESPONSE_LOST})
        result = await _scan(
            _target(tmp_path / "state.jsonl", "--swallow-every", "2"), seed=seed
        )
        metrics = _behavior(result)

        assert metrics["effect_oracle_status"] == "ok"
        assert metrics["duplicate_mutations"] == 1
        assert metrics["lost_effects"] == 1
        # Which operation, not just how many: the scored number is auditable.
        assert sorted(metrics["effects_by_op"].values()) == [0, 2]

    async def test_the_aggregate_would_have_reported_a_clean_run(
        self, tmp_path, monkeypatch
    ):
        """The same window, with the false pass in the open."""
        _scheduled(monkeypatch, {(f"{TOOL}#2", 1): FaultKind.RESPONSE_LOST})
        result = await _scan(_target(tmp_path / "state.jsonl", "--swallow-every", "2"))
        metrics = _behavior(result)

        effects = sum(metrics["effects_by_op"].values())
        assert effects == metrics["operations_succeeded"], (
            "this window is exactly the case an aggregate cannot see"
        )

    async def test_two_lost_replies_count_as_repeat_deliveries(
        self, tmp_path, monkeypatch
    ):
        """Two dropped replies, so the target applied the call on both attempts.

        **This is not the unacknowledged case**, and it was named as though it
        were until 1.4.0's mutation matrix said otherwise: `RESPONSE_LOST`
        reaches the target, so the proxy records `executed is True` and a
        delivered gate would still count these. The case that needs no gate --
        work applied where the caller cannot confirm it -- is in
        `TestAnUnacknowledgedApply`, and adding it is what killed that mutation.
        """
        _scheduled(monkeypatch, {
            (f"{TOOL}#2", 1): FaultKind.RESPONSE_LOST,
            (f"{TOOL}#2", 2): FaultKind.RESPONSE_LOST,
        })
        result = await _scan(_target(tmp_path / "state.jsonl"))
        metrics = _behavior(result)

        assert metrics["duplicate_mutations"] >= 1
        assert metrics["effects_without_acknowledged_delivery"] >= 0

    @pytest.mark.parametrize("seed", [7, DEFAULTS.seed])
    async def test_a_clean_run_scores_zero_over_a_stated_denominator(
        self, tmp_path, seed
    ):
        result = await _scan(_target(tmp_path / "state.jsonl"), seed=seed)
        metrics = _behavior(result)

        assert metrics["effect_oracle_status"] == "ok"
        assert metrics["duplicate_mutations"] == 0
        assert "duplicate_opportunities" in metrics
        if not metrics["duplicate_opportunities"]:
            assert any(
                "chance to duplicate" in reason
                for reason in _caveat_reasons(result, "duplicate_mutations")
            )


class TestTheDiffIsTheCount:
    """Written because a mutation to after-only counting survived the suite.

    Stale detection normally catches a before-snapshot that already holds a
    registered id, so the two arithmetics agree on every scan the suite ran --
    which left the diff untested. This exercises the oracle directly, with the
    status forced, so the before term has to be subtracted.
    """

    def test_a_preexisting_entry_is_not_counted_as_an_effect(self):
        from ratemyagent.probes.fault import _EffectOracle

        class Stub:
            has_effect_oracle = True
            uses_op_id = True

            def op_id(self, index):
                return f"rma-{index:012d}"

            async def read_effect_entries(self):
                return []

        class Forced(_EffectOracle):
            def status(self):
                return "ok"

        request = type("R", (), {"trajectory_key": "event#3"})()
        oracle = Forced(Stub(), [request], 3)
        oracle._before = [{"name": "rma-000000000003"}]
        oracle._after = [{"name": "rma-000000000003"}]

        assert oracle.metrics()["effects_by_op"] == {"event#3": 0}, (
            "counting the after snapshot alone reports a pre-existing entry as "
            "an effect this window applied"
        )

    def test_an_effect_added_in_the_window_is_counted(self):
        from ratemyagent.probes.fault import _EffectOracle

        class Stub:
            has_effect_oracle = True
            uses_op_id = True

            def op_id(self, index):
                return f"rma-{index:012d}"

            async def read_effect_entries(self):
                return []

        class Forced(_EffectOracle):
            def status(self):
                return "ok"

        request = type("R", (), {"trajectory_key": "event#3"})()
        oracle = Forced(Stub(), [request], 3)
        oracle._before = [{"name": "rma-000000000003"}]
        oracle._after = [{"name": "rma-000000000003"}, {"name": "rma-000000000003"}]

        assert oracle.metrics()["effects_by_op"] == {"event#3": 1}


class TestAnUnacknowledgedApply:
    """Written because gating duplicates on `executed is True` survived.

    Every injected fault that reaches the target leaves `executed` True, so the
    suite had no case where the work happened and the proxy could not confirm
    it. This one does: the server applies the call and *then* returns an error,
    which is what a real timeout-after-completion looks like from outside.
    """

    async def test_a_double_apply_counts_without_an_acknowledged_delivery(
        self, tmp_path
    ):
        result = await _scan(
            _target(tmp_path / "state.jsonl", "--error-every", "1"), requests=1
        )
        metrics = _behavior(result)

        assert metrics["effect_oracle_status"] == "ok"
        applied = list(metrics["effects_by_op"].values())
        assert applied and max(applied) >= 2, metrics["effects_by_op"]
        assert metrics["duplicate_mutations"] >= 1, (
            "gating on an acknowledged delivery drops the work that happened "
            "without the caller being able to confirm it"
        )
        assert metrics["effects_without_acknowledged_delivery"] >= 1


class TestStaleState:
    @pytest.mark.parametrize("seed", [7, DEFAULTS.seed])
    async def test_a_repeat_run_is_stale(self, tmp_path, seed):
        """Ids come from the seed, so a repeat collides with its own leftovers.

        At the default seed too: namespacing the registered ids away from the
        rest of the scan must not cost `stale` its meaning. The namespace is
        still derived from the seed, so a repeat of one collides with itself.
        """
        state = tmp_path / "state.jsonl"
        first = await _scan(_target(state), seed=seed)
        second = await _scan(_target(state), seed=seed)

        assert _behavior(first)["effect_oracle_status"] == "ok"
        assert _behavior(second)["effect_oracle_status"] == "stale"
        assert _behavior(second)["duplicate_mutations"] is None
        assert _behavior(second)["lost_effects"] is None
        assert any(
            "previous scan with this seed" in reason
            for reason in _caveat_reasons(second, "duplicate_mutations")
        )

    async def test_a_different_seed_is_not_stale(self, tmp_path):
        state = tmp_path / "state.jsonl"
        await _scan(_target(state), seed=7)
        again = await _scan(_target(state), seed=8)

        assert _behavior(again)["effect_oracle_status"] == "ok"


class TestWithoutOpId:
    async def test_both_metrics_are_withheld(self, tmp_path):
        """Identical arguments per operation: effects cannot be attributed."""
        result = await _scan(_target(tmp_path / "state.jsonl", args=FLAT_ARGS))
        metrics = _behavior(result)

        assert metrics["effect_oracle_status"] == "unattributed"
        assert metrics["duplicate_mutations"] is None
        assert metrics["lost_effects"] is None
        assert any(
            "cancel" in reason
            for reason in _caveat_reasons(result, "duplicate_mutations")
        )

    async def test_the_aggregate_is_still_reported(self, tmp_path):
        result = await _scan(_target(tmp_path / "state.jsonl", args=FLAT_ARGS))
        assert "observed_effects" in _behavior(result)


class TestOracleAbsentAndFailed:
    async def test_no_verify_tool_is_absent_not_failed(self, tmp_path):
        """The 1.3.1 path, unchanged, and the state nothing used to reach.

        `MCPTarget` always defines `read_effect_entries`, so an oracle detected
        by the presence of the method reported "the verify tool did not answer"
        on every scan that never asked for one.
        """
        result = await _scan(
            _target(tmp_path / "state.jsonl", args=FLAT_ARGS, verify=None, count=None)
        )
        metrics = _behavior(result)

        assert metrics["effect_oracle_status"] == "absent"
        assert metrics["duplicate_mutations"] is None
        assert any(
            "delivered calls, not applied effects" in reason
            for reason in _caveat_reasons(result, "duplicate_mutations")
        )

    async def test_a_mock_target_is_absent(self):
        async with MockTarget.healthy() as target:
            result = await _scan(target, requests=3)

        assert _behavior(result)["effect_oracle_status"] == "absent"

    async def test_a_bad_verify_count_path_refuses_at_setup(self, tmp_path):
        """One verify read at setup, so the flag is named before a scan is spent.

        Discovered mid-scan this is `failed` -- honest, and a whole scan to find
        out the path was wrong.
        """
        target = _target(tmp_path / "state.jsonl", count="nope")
        with pytest.raises(TargetError) as excinfo:
            await _scan(target)

        assert "verify-count" in str(excinfo.value)

    async def test_a_verify_call_that_fails_mid_scan_is_na_never_zero(
        self, tmp_path, monkeypatch
    ):
        """The oracle answered at setup and stopped answering afterwards.

        `None` from the reader means "we could not look", and it must not be
        scored as "nothing was applied" -- the collapse this release undoes.
        """
        target = _target(tmp_path / "state.jsonl")

        original = MCPTarget.read_effect_entries
        calls = {"n": 0}

        async def flaky(self):
            calls["n"] += 1
            if calls["n"] == 1:          # the setup read still succeeds
                return await original(self)
            return None

        monkeypatch.setattr(MCPTarget, "read_effect_entries", flaky)
        result = await _scan(target)
        metrics = _behavior(result)

        assert metrics["effect_oracle_status"] == "failed"
        assert metrics["duplicate_mutations"] is None
        assert metrics["lost_effects"] is None
        assert any(
            "did not answer" in reason
            for reason in _caveat_reasons(result, "duplicate_mutations")
        )


class TestGating:
    async def test_a_mutating_verify_tool_is_refused(self, tmp_path):
        target = _target(tmp_path / "state.jsonl", verify=TOOL)
        with pytest.raises(TargetError) as excinfo:
            await _scan(target)

        message = str(excinfo.value)
        assert "refusing 'event' as a verify tool" in message
        assert "read-only tools here" in message

    async def test_a_missing_verify_tool_is_refused(self, tmp_path):
        target = _target(tmp_path / "state.jsonl", verify="nosuchtool")
        with pytest.raises(TargetError) as excinfo:
            await _scan(target)

        assert "not found" in str(excinfo.value)

    async def test_a_read_only_probe_tool_is_refused(self, tmp_path):
        """A read-only probe tool applies nothing, so a zero would mean
        "nothing was asked" rather than "no duplicates"."""
        target = MCPTarget(
            _uri(tmp_path / "state.jsonl"),
            tool=VERIFY, tool_args={}, allow_mutating=True, timeout_s=20,
            verify_tool=VERIFY, verify_count="entries",
        )
        with pytest.raises(TargetError) as excinfo:
            await _scan(target)

        assert "not one" in str(excinfo.value)

    async def test_allow_mutating_is_required(self, tmp_path):
        target = MCPTarget(
            _uri(tmp_path / "state.jsonl"),
            tool=TOOL, tool_args=dict(ARGS), allow_mutating=False, timeout_s=20,
            verify_tool=VERIFY, verify_count="entries",
        )
        with pytest.raises(TargetError) as excinfo:
            await _scan(target)

        assert "--allow-mutating" in str(excinfo.value)


class TestTheVerifyCallIsNeverFaulted:
    async def test_no_verify_call_reaches_the_proxy(self, tmp_path, monkeypatch):
        """Three legs, because this is an assertion about an absence.

        A tripwire proxy that raises on a verify call; the recorded invocations;
        and a schedule that faults every proxied call, so a verify call routed
        through the proxy would lose its reply and move the metric.
        """
        seen: list[str] = []

        class Tripwire(FaultProxy):
            def _choose_fault(self, request, attempt):
                return FaultKind.RESPONSE_LOST if attempt == 1 else None

            async def invoke(self, request):
                seen.append(request.op)
                assert request.op != VERIFY, "a verify call went through the proxy"
                return await super().invoke(request)

        monkeypatch.setattr(fault_probe, "FaultProxy", Tripwire)
        result = await _scan(_target(tmp_path / "state.jsonl"))

        assert VERIFY not in seen
        assert _behavior(result)["effect_oracle_status"] == "ok"
        assert _behavior(result)["duplicate_mutations"] >= 1


class TestTheJsonExportCarriesIt:
    async def test_status_and_per_op_effects_are_exported(self, tmp_path):
        result = await _scan(_target(tmp_path / "state.jsonl"))
        exported = json.loads(json.dumps(result.to_dict()))
        behavior = next(p for p in exported["probes"] if p["probe"] == "behavior")

        assert behavior["metrics"]["effect_oracle_status"] == "ok"
        assert isinstance(behavior["metrics"]["effects_by_op"], dict)


class TestDefaultFlags:
    """The configuration nothing ran until it was measured.

    Every other arm in this file pins `--seed 7` or `8`. The default seed is
    the one case where `MCPTarget`'s constructor salt and `ProbeConfig.seed`
    are the same number, so before the `recovery:` namespace the concurrency
    ramp -- which restarts at index 0 and walks `requests` indices per level --
    had already sent every id the oracle registered. A first run on clean state
    reported `stale`, withholding the metric this release exists to produce.
    The suite was green throughout, because a pinned seed made the two salts
    differ.

    So: **every new feature gets one test on default flags.**
    """

    @pytest.mark.parametrize("seed", [DEFAULTS.seed, 7, 0])
    @pytest.mark.parametrize("warmup", [0, 1, 2, 3])
    async def test_no_id_sent_before_the_window_is_ever_registered(
        self, tmp_path, monkeypatch, seed, warmup
    ):
        """Swept over the ramp and the request count, not just the defaults.

        Two legs, because either alone can be fooled. The **call site** is
        pinned by running the real recovery pass and asking what it registered
        -- collapsing the namespace in `_recovery_salt` or dropping the call
        both show up here. The **sweep** then covers ramp and request counts
        the defaults alone would not reach.
        """
        state = tmp_path / "state.jsonl"
        # A fresh adapter, before anything reassigns it: the salt the baseline
        # probes and the preflight actually use.
        baseline_salt = _bare_target(state)._op_id_salt
        target = _bare_target(state)

        registered_for_real = await _registered_by_the_probe(
            state, monkeypatch, seed=seed, warmup=warmup, requests=3
        )
        assert registered_for_real == _ids(
            target, fault_probe._recovery_salt(seed), 3, warmup + 3
        ), (
            "the recovery pass registered ids from a namespace this sweep does "
            "not check, so the sweep proves nothing about what it registers"
        )

        for concurrency in range(1, 17):
            for requests in range(1, 121):
                registered = _ids(
                    target,
                    fault_probe._recovery_salt(seed),
                    requests,
                    warmup + requests,
                )
                assert len(registered) == requests, "ids must be unique per op"

                sent = _sent_before_recovery(
                    target,
                    warmup=warmup,
                    concurrency=concurrency,
                    requests=requests,
                    seed=seed,
                    baseline_salt=baseline_salt,
                )
                overlap = sent & registered
                assert not overlap, (
                    f"seed={seed} warmup={warmup} concurrency={concurrency} "
                    f"requests={requests}: {len(overlap)} registered id(s) were "
                    f"already sent before the window opened, so a first run on "
                    f"clean state reads them as leftovers -- e.g. "
                    f"{sorted(overlap)[0]}"
                )

    async def test_a_first_run_on_clean_state_is_ok_and_a_repeat_is_stale(
        self, tmp_path
    ):
        """End to end, no `--seed`, on the idempotent twin.

        `put_event` stores by id, so a correct scan of it finds zero duplicates
        -- and `stale` would withhold that zero instead of reporting it. The
        second run must still be `stale`: the point is a namespace, not the
        removal of a check.
        """
        state = tmp_path / "state.jsonl"
        first = await _scan_defaults(_target(state, mode="put"))
        metrics = _behavior(first)

        assert metrics["effect_oracle_status"] == "ok", (
            "a first run on clean state reported leftovers from a scan that "
            "never happened"
        )
        assert metrics["duplicate_mutations"] == 0

        again = await _scan_defaults(_target(state, mode="put"))
        assert _behavior(again)["effect_oracle_status"] == "stale"
