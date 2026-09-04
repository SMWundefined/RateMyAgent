"""Model and grading arithmetic."""

from __future__ import annotations

import json

import pytest

from ratemyagent.models import (
    ErrorKind,
    Grade,
    ProbeResult,
    Request,
    Response,
    ScanResult,
    TargetInfo,
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
