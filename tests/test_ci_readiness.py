"""The CI readiness probe, exercised under the shell CI actually uses.

**Why this file exists, and it is not really about SSE.**

The readiness check for the SSE step polled an event stream with a bare `GET`.
`/sse` answers 200 and holds the connection open, so curl never returned, the
loop never reached its second iteration, and the step ran until GitHub's six-hour
ceiling. Five releases -- including the 1.0 API freeze -- were tagged and pushed
on jobs that timed out rather than passed, because a run still in progress reads
as slow rather than broken.

The fix for that hang then shipped a second defect, and the second one is the
reason for this file. The replacement loop called curl as a bare command:

    curl -s --max-time 2 -o /dev/null "$URL"
    case $? in 28|0) ready=1; break ;; esac

GitHub runs `run:` steps as `bash -e {0}`. A bare command that exits non-zero
kills the script, so `case` never executed and the loop never retried -- and
connection-refused is the *normal* first observation when the step starts the
server itself. It failed by construction on every run.

It was verified before shipping, in both directions, and both results were real
and irrelevant: they ran under `bash`, not `bash -e`. The same file, two shells:

    plain bash :  ::error::… never became ready   exit=1
    bash -e    :  (no output)                     exit=7

Nothing in the code showed the difference. So:

    **Verify a CI step body under the shell CI uses, not the one your terminal
    hands you.**

That rule needs somewhere to live or it is a habit. It lives here: the loop is
now `ci/wait-for-endpoint.sh`, the workflow calls that script, and these tests
run that same file under `bash -e`. The artifact CI executes and the artifact the
suite executes are one object, so they cannot drift.
"""

from __future__ import annotations

import http.server
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ci" / "wait-for-endpoint.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


def run(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Invoke the script the way GitHub invokes a `run:` step body.

    `bash -e` is the whole point of this helper. Calling it any other way tests
    a program CI does not run.
    """
    # A short poll interval, so exercising the retry path repeatedly does not
    # cost a second per attempt. CI takes the script's default of 1s.
    return subprocess.run(
        ["bash", "-e", str(SCRIPT), *args, "0.05"],
        capture_output=True, text=True, timeout=timeout, cwd=ROOT,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Streaming(http.server.BaseHTTPRequestHandler):
    """An endpoint that answers and never closes, like a real SSE route."""

    def do_GET(self):  # noqa: N802 - stdlib naming
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(0.2)

    def log_message(self, *args):
        pass


class _Responding(_Streaming):
    """An endpoint that answers a bare GET with 400 and closes, like /mcp."""

    def do_GET(self):  # noqa: N802 - stdlib naming
        self.send_response(400)
        self.end_headers()
        self.wfile.write(b"bad request")


@pytest.fixture
def serving():
    servers = []

    def start(handler) -> str:
        port = _free_port()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{port}/"

    yield start
    for server in servers:
        server.shutdown()


class TestItSurvivesErrexit:
    """The defect this file was written for.

    Every test here calls the script under `bash -e`. A version that treats a
    failed curl as fatal dies on the first connection-refused, which is what the
    shipped one did.
    """

    def test_a_refused_connection_is_retried_not_fatal(self):
        """Connection-refused is the normal first observation, not an error.

        The step starts the server itself, so the first poll always races it. On
        a cold start this is eighteen consecutive refusals before the endpoint
        answers. Under the shipped version, the first one ended the step.
        """
        result = run("stream", f"http://127.0.0.1:{_free_port()}/sse", "3")

        assert result.returncode == 1, (
            f"expected the script's own failure, got exit {result.returncode} -- "
            "a bare curl under `bash -e` exits with curl's code and never "
            "reaches the assertion"
        )
        assert result.stdout.count("attempt ") == 3, "every attempt must be tried"
        assert "::error::" in result.stdout

    def test_the_same_holds_for_the_responding_mode(self):
        result = run("respond", f"http://127.0.0.1:{_free_port()}/mcp", "3")

        assert result.returncode == 1
        assert result.stdout.count("attempt ") == 3

    def test_an_unknown_mode_fails_distinctly(self):
        result = run("nonsense", "http://127.0.0.1:1/x", "1")
        assert result.returncode == 2
        assert "unknown mode" in result.stdout


class TestItReportsWhatItSaw:
    """`gave up after 30` says nothing; the exit codes say what happened.

    The same argument as `n` beside a metric: a number without the observations
    behind it cannot be acted on, and a failure that cannot describe itself makes
    the next person start over.
    """

    def test_every_attempt_logs_its_exit_code(self):
        result = run("stream", f"http://127.0.0.1:{_free_port()}/sse", "3")

        assert "curl_exit=7" in result.stdout, "connection refused, by its code"
        for n in (1, 2, 3):
            assert f"attempt {n}/3" in result.stdout

    def test_the_failure_names_the_url_and_the_elapsed_time(self):
        url = f"http://127.0.0.1:{_free_port()}/sse"
        result = run("stream", url, "2")

        assert url in result.stdout
        assert "never became ready after 2 attempts" in result.stdout

    def test_the_responding_mode_logs_the_status(self, serving):
        result = run("respond", serving(_Responding), "5")
        assert "http_status=400" in result.stdout


class TestReadinessIsTheRightObservation:
    def test_a_streaming_endpoint_is_ready_when_it_streams(self, serving):
        """Exit 28 -- timed out with bytes received -- is the healthy answer.

        A bare GET can never return here, which is the original hang. Readiness
        has to be the *timeout*, and that also proves the endpoint is serving
        rather than that the port is merely open.
        """
        result = run("stream", serving(_Streaming), "10")

        assert result.returncode == 0
        assert "curl_exit=28" in result.stdout
        assert "ready: streaming" in result.stdout

    def test_a_responding_endpoint_is_ready_on_a_400(self, serving):
        """Up and objecting to a bare GET is enough, and `-f` would reject it."""
        result = run("respond", serving(_Responding), "10")

        assert result.returncode == 0
        assert "answered 400" in result.stdout

    def test_stream_mode_does_not_hang_on_a_streaming_endpoint(self, serving):
        """The six-hour failure, asserted as a bound rather than described."""
        started = time.monotonic()
        run("stream", serving(_Streaming), "10")
        assert time.monotonic() - started < 15


class TestTheWorkflowUsesThisScript:
    """Otherwise the tested artifact and the CI text drift apart again.

    A gate on a copy of the loop would be the same defect one level up: the
    thing verified and the thing that runs would be different objects.
    """

    def test_both_readiness_steps_call_it(self):
        workflow = WORKFLOW.read_text()
        assert "ci/wait-for-endpoint.sh respond http://localhost:3001/mcp" in workflow
        assert "ci/wait-for-endpoint.sh stream http://localhost:3005/sse" in workflow

    def test_no_step_polls_an_endpoint_by_hand(self):
        """The pattern that hung, kept out by name rather than by memory."""
        for line in WORKFLOW.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "curl" not in stripped:
                continue
            pytest.fail(
                f"readiness polling belongs in ci/wait-for-endpoint.sh: {stripped}"
            )

    def test_every_job_is_bounded(self):
        """A run with no timeout has no verdict, and 360 minutes is not a bound."""
        import yaml

        jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        for name, job in jobs.items():
            bound = job.get("timeout-minutes")
            assert bound is not None, f"job {name!r} has no timeout-minutes"
            assert bound <= 30, f"job {name!r} is bounded at {bound}m, too loose to notice"

    def test_the_script_is_executable(self):
        assert SCRIPT.stat().st_mode & 0o111, "CI calls it directly"
