"""Model and grading arithmetic."""

from __future__ import annotations

import json

import pytest

from ratemyagent.models import (
    ErrorKind,
    FaultKind,
    Grade,
    Invocation,
    ProbeResult,
    Request,
    Response,
    ScanResult,
    TargetInfo,
    Trajectory,
)


class TestGrade:
    def test_points_descend_from_a_to_f(self):
        assert [g.points for g in (Grade.A, Grade.B, Grade.C, Grade.D, Grade.F)] == [4, 3, 2, 1, 0]

    def test_worst_picks_lowest_grade(self):
        assert Grade.worst([Grade.A, Grade.D, Grade.B]) is Grade.D

    def test_worst_of_empty_is_f(self):
        assert Grade.worst([]) is Grade.F

    def test_average_rounds_to_nearest_letter(self):
        assert Grade.average([Grade.A, Grade.C]) is Grade.B
        assert Grade.average([Grade.A, Grade.A, Grade.B]) is Grade.A

    def test_average_of_empty_is_f(self):
        assert Grade.average([]) is Grade.F

    def test_from_points_clamps_out_of_range(self):
        assert Grade.from_points(99) is Grade.A
        assert Grade.from_points(-5) is Grade.F

    def test_serializes_as_its_letter(self):
        assert json.dumps({"grade": Grade.B}) == '{"grade": "B"}'


class TestProbeResult:
    def test_failed_is_true_only_when_probe_errored(self):
        assert ProbeResult(probe="latency", grade=Grade.F).failed is False
        assert ProbeResult(probe="latency", error="boom").failed is True

    def test_to_dict_is_json_serializable(self):
        result = ProbeResult(probe="latency", grade=Grade.B, metrics={"p95_s": 3.2})
        assert json.loads(json.dumps(result.to_dict()))["grade"] == "B"

    def test_to_dict_handles_ungraded_result(self):
        assert ProbeResult(probe="latency").to_dict()["grade"] is None


class TestScanResult:
    def _result(self, *grades: Grade) -> ScanResult:
        return ScanResult(
            target=TargetInfo(name="t", kind="mock"),
            probes=[ProbeResult(probe=f"p{i}", grade=g) for i, g in enumerate(grades)],
        )

    def test_overall_grade_averages_probes(self):
        assert self._result(Grade.A, Grade.C).overall_grade is Grade.B

    def test_overall_grade_of_no_probes_is_f(self):
        assert self._result().overall_grade is Grade.F

    def test_probe_lookup_by_name(self):
        result = self._result(Grade.A)
        assert result.probe("p0") is not None
        assert result.probe("nope") is None

    def test_to_dict_round_trips_through_json(self):
        payload = json.loads(json.dumps(self._result(Grade.A, Grade.B).to_dict()))
        assert payload["overall_grade"] == "A"
        assert len(payload["probes"]) == 2
        assert payload["started_at"].endswith("+00:00")


class TestRequestResponse:
    def test_response_to_dict_flattens_error_kind(self):
        response = Response(ok=False, latency_s=1.0, error_kind=ErrorKind.RATE_LIMIT)
        assert response.to_dict()["error_kind"] == "rate_limit"

    def test_response_to_dict_keeps_none_error_kind(self):
        assert Response(ok=True, latency_s=1.0).to_dict()["error_kind"] is None

    def test_requests_do_not_share_payload_state(self):
        first, second = Request(op="a"), Request(op="b")
        first.payload["x"] = 1
        assert second.payload == {}


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_error_kinds_have_stable_string_values(kind: ErrorKind):
    assert isinstance(kind.value, str) and kind.value == kind.value.lower()


@pytest.mark.parametrize("fault", list(FaultKind))
def test_every_fault_maps_to_an_error_kind(fault: FaultKind):
    assert isinstance(fault.error_kind, ErrorKind)


class TestRequestIdentity:
    def test_same_arguments_share_a_fingerprint(self):
        a = Request(op="search", payload={"q": "x"}, label="search#1")
        b = Request(op="search", payload={"q": "x"}, label="search#2")
        assert a.fingerprint == b.fingerprint

    def test_key_order_does_not_change_the_fingerprint(self):
        a = Request(op="s", payload={"a": 1, "b": 2})
        b = Request(op="s", payload={"b": 2, "a": 1})
        assert a.fingerprint == b.fingerprint

    def test_different_arguments_differ(self):
        a = Request(op="search", payload={"q": "x"})
        b = Request(op="search", payload={"q": "y"})
        assert a.fingerprint != b.fingerprint

    def test_trajectory_key_prefers_explicit_id_then_label_then_op(self):
        assert Request(op="o", label="l", trajectory_id="t").trajectory_key == "t"
        assert Request(op="o", label="l").trajectory_key == "l"
        assert Request(op="o").trajectory_key == "o"


def _inv(sequence: int, ok: bool, *, started: float = 0.0, latency: float = 1.0,
         fingerprint: str = "op:aaa", trajectory: str = "t0", attempt: int | None = None,
         injected: FaultKind | None = None) -> Invocation:
    return Invocation(
        sequence=sequence,
        op="op",
        fingerprint=fingerprint,
        trajectory_id=trajectory,
        attempt=attempt if attempt is not None else sequence + 1,
        ok=ok,
        latency_s=latency,
        started_at=started,
        injected=injected,
    )


class TestTrajectory:
    def test_empty_trajectory_reports_nothing_happened(self):
        trajectory = Trajectory(trajectory_id="t0")
        assert trajectory.attempts == 0
        assert trajectory.retries == 0
        assert trajectory.recovered is False
        assert trajectory.final_status == "empty"

    def test_first_try_success_has_no_retries(self):
        trajectory = Trajectory("t0", [_inv(0, True)])
        assert trajectory.retries == 0
        assert trajectory.recovered is False
        assert trajectory.final_status == "success"

    def test_retries_count_calls_after_the_first(self):
        trajectory = Trajectory("t0", [_inv(0, False), _inv(1, False), _inv(2, True)])
        assert trajectory.attempts == 3
        assert trajectory.retries == 2
        assert trajectory.failures == 2

    def test_recovery_requires_a_success_after_a_failure(self):
        assert Trajectory("t0", [_inv(0, False), _inv(1, True)]).recovered is True
        assert Trajectory("t0", [_inv(0, True), _inv(1, False)]).recovered is False

    def test_recovery_latency_spans_first_failure_to_success(self):
        trajectory = Trajectory("t0", [
            _inv(0, False, started=0.0, latency=2.0),
            _inv(1, True, started=5.0, latency=1.0),
        ])
        assert trajectory.recovery_latency_s == pytest.approx(6.0)

    def test_recovery_latency_is_none_without_recovery(self):
        assert Trajectory("t0", [_inv(0, False)]).recovery_latency_s is None

    def test_duplicates_count_repeated_successes_only(self):
        """A retry that succeeds twice ran the same mutation twice."""
        trajectory = Trajectory("t0", [_inv(0, True), _inv(1, True)])
        assert trajectory.duplicates == 1

    def test_failures_are_not_duplicates(self):
        trajectory = Trajectory("t0", [_inv(0, False), _inv(1, False), _inv(2, True)])
        assert trajectory.duplicates == 0

    def test_different_arguments_are_not_duplicates(self):
        trajectory = Trajectory("t0", [
            _inv(0, True, fingerprint="op:aaa"),
            _inv(1, True, fingerprint="op:bbb"),
        ])
        assert trajectory.duplicates == 0

    def test_loop_needs_three_attempts_and_no_recovery(self):
        stuck = Trajectory("t0", [_inv(0, False), _inv(1, False), _inv(2, False)])
        assert stuck.loops_detected is True

    def test_a_retry_that_eventually_works_is_not_a_loop(self):
        """Three attempts ending in success is a system working, not a stuck one."""
        recovered = Trajectory("t0", [_inv(0, False), _inv(1, False), _inv(2, True)])
        assert recovered.loops_detected is False

    def test_two_failures_are_not_yet_a_loop(self):
        assert Trajectory("t0", [_inv(0, False), _inv(1, False)]).loops_detected is False

    def test_final_status_follows_the_last_attempt(self):
        assert Trajectory("t0", [_inv(0, True), _inv(1, False)]).final_status == "failed"
        assert Trajectory("t0", [_inv(0, False), _inv(1, True)]).final_status == "success"

    def test_injected_faults_are_listed(self):
        trajectory = Trajectory("t0", [
            _inv(0, False, injected=FaultKind.TIMEOUT),
            _inv(1, True),
        ])
        assert trajectory.injected_faults == [FaultKind.TIMEOUT]

    def test_to_dict_is_json_serializable(self):
        trajectory = Trajectory("t0", [
            _inv(0, False, injected=FaultKind.SERVER_ERROR),
            _inv(1, True),
        ])
        payload = json.loads(json.dumps(trajectory.to_dict()))

        assert payload["recovered"] is True
        assert payload["injected_faults"] == ["server_error"]
        assert len(payload["invocations"]) == 2

    def test_derived_values_track_appended_invocations(self):
        """Properties, not stored fields, so a trajectory cannot go stale."""
        trajectory = Trajectory("t0", [_inv(0, False)])
        assert trajectory.recovered is False

        trajectory.invocations.append(_inv(1, True))
        assert trajectory.recovered is True
