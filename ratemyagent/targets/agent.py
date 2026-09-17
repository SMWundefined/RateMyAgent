"""AgentTarget: a `Target` whose unit of work is a task, not a tool call.

Everything else this project scans answers a call. An agent is asked to
*accomplish* something and decides for itself which calls that takes, how many
times to try, and what to report back. So `invoke()` reads with the nouns
changed:

    request.op       the task id
    request.payload  the task spec
    Response.ok      **the agent's claim**, not the truth

That third line is the one to keep hold of. `ok` is what the agent said
happened; what happened is in the upstream's state, and the gap between the two
is what Phase C exists to measure.

**Faults are not injected here.** They are injected in `ratemyagent proxy`, a
process the *agent* launched from an MCP config this target wrote, so
`injects_out_of_process` is True and `FaultInjector.run` takes its other branch.
The proxy writes a record file per task; the scan reads it back and replays it
into the same trajectories phase 3 has always read.

**Env is why the config file exists.** The MCP SDK copies six named variables
into a stdio child and drops the rest, so a `RMA_PROXY_RECORD` exported by the
scan reaches the proxy through nothing. The per-task config carries it in an
explicit `env` block, and that block is the only channel that survives a proxy
the agent spawned -- which is also the shape a Phase D user's hand-written
config has to take.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..models import ErrorKind, Request, Response, TargetInfo
from .base import Target, TargetError, redact_uri

logger = logging.getLogger(__name__)

#: How the agent is told where its MCP config is, on argv and in its own env.
#: Both, because a real agent reads one or the other and a fixture should not
#: get to pick the convenient one.
MCP_CONFIG_FLAG = "--mcp-config"
MCP_CONFIG_ENV = "RMA_MCP_CONFIG"

#: The env block the config carries into the proxy.
RECORD_ENV = "RMA_PROXY_RECORD"
SCHEDULE_ENV = "RMA_PROXY_SCHEDULE"
TASK_ENV = "RMA_TASK_ID"

#: Fields a task must declare. `expected_effects` is required rather than
#: defaulted: a default of 1 is also a legal value, so a task file that forgot
#: it would be indistinguishable from one that meant it, and the number decides
#: whether a run is read as clean, duplicated or falsely claimed.
REQUIRED_TASK_FIELDS = ("id", "prompt", "expected_effects", "tool", "arguments")

#: What a task run came to. `abandoned` is the one that is not about the agent's
#: answer at all: the task deadline expired with no answer, which is a scan that
#: did not finish rather than a target that failed.
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_ABANDONED = "abandoned"


class AgentTarget(Target):
    """An agent process, scanned through the MCP boundary it talks over."""

    #: The trajectory really is the target's here: the agent chose to retry, we
    #: did not. This is what turns caller-strategy scoring back on -- see
    #: `Target.runs_own_retry_loop` and `CALLER_STRATEGY_METRICS`.
    runs_own_retry_loop = True

    #: The wrapping already happened, a process away. See `Target`.
    injects_out_of_process = True

    #: Phase C is scripted fixtures: no model, no tokens, nothing for the cost
    #: probe to measure.
    reports_token_usage = False

    #: Declared, never inferred. Phase C1 builds the plumbing; the per-task
    #: oracle that reads the upstream's state between tasks is C2, and until it
    #: exists this is False so that nothing downstream can mistake "not built"
    #: for "measured nothing".
    has_effect_oracle = False

    def __init__(
        self,
        *,
        agent_command: str,
        tasks_path: str | os.PathLike[str],
        upstream: str,
        timeout_s: float = 30.0,
        work_dir: str | os.PathLike[str] | None = None,
        allow_mutating: bool = False,
        proxy_command: list[str] | None = None,
    ) -> None:
        self.agent_command = agent_command
        self.tasks_path = Path(tasks_path)
        self.upstream = upstream
        self.timeout_s = timeout_s
        self.allow_mutating = allow_mutating
        #: Same interpreter as the scan, so the proxy is the build under test
        #: rather than whatever a `ratemyagent` on PATH resolves to.
        self.proxy_command = proxy_command or [
            sys.executable, "-m", "ratemyagent.cli", "proxy",
        ]

        self._work_dir = Path(work_dir) if work_dir else None
        self._tasks: list[dict[str, Any]] = []
        #: task id -> `completed` / `failed` / `abandoned`.
        self.outcomes: dict[str, str] = {}
        self._processes: set[asyncio.subprocess.Process] = set()

    # -- Target interface ----------------------------------------------------

    async def setup(self) -> None:
        self._tasks = _load_tasks(self.tasks_path)
        if self._work_dir is None:
            # Not a TemporaryDirectory: the record is the artifact. A file
            # somebody can read after a failed run settles "the agent says it
            # retried twice"; a directory that vanished with the scan settles
            # nothing.
            self._work_dir = Path(tempfile.mkdtemp(prefix="ratemyagent-agent-"))
        self._work_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "agent scan working directory: %s (records and configs are kept)",
            self._work_dir,
        )
        # An empty table, written before anything runs. `None` and `{}` are
        # different instructions to the proxy -- leave the seeded draw alone,
        # versus force no faults -- and the baseline pass means the second.
        self.write_schedule({})

    async def teardown(self) -> None:
        for process in list(self._processes):
            if process.returncode is None:
                process.kill()
                await process.wait()
        self._processes.clear()

    async def invoke(self, request: Request) -> Response:
        """Run one task and report what the agent claimed.

        Never raises for agent-side failures, the same contract every other
        adapter keeps: a task that fails comes back as `ok=False`, and only a
        target that cannot be used at all raises.
        """
        task = self.task(request.op)
        config_path = self._write_config(task["id"])

        command = [
            *shlex.split(self.agent_command),
            MCP_CONFIG_FLAG, str(config_path),
            "--tasks", str(self.tasks_path),
            "--task", str(task["id"]),
        ]
        # Ordinary subprocess inheritance: the scan starts the agent, so the
        # SDK's six-variable rule does not apply here. It applies one level
        # down, between the agent and the proxy, which is why the config
        # carries an explicit env block at all.
        env = {**os.environ, MCP_CONFIG_ENV: str(config_path)}

        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._processes.add(process)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_s
            )
        except asyncio.TimeoutError:
            # **The deadline is the scan's, not the agent's.** An agent with no
            # read timeout waits forever on a reply this scan dropped on
            # purpose, which is a real production failure mode and not a
            # harness artifact -- so it is reported as `abandoned` rather than
            # fixed. A scan that never finished is not a target that failed.
            process.kill()
            await process.wait()
            self.outcomes[task["id"]] = OUTCOME_ABANDONED
            return Response(
                ok=False,
                latency_s=time.perf_counter() - started,
                error=(
                    f"the agent did not finish task {task['id']!r} within "
                    f"{self.timeout_s:.0f}s and was killed"
                ),
                error_kind=ErrorKind.TIMEOUT,
                delivered=False,
                meta={"outcome": OUTCOME_ABANDONED, "task_id": task["id"]},
            )
        finally:
            self._processes.discard(process)

        latency = time.perf_counter() - started
        claim = _parse_claim(stdout.decode("utf-8", "replace"))
        errors = stderr.decode("utf-8", "replace").strip()
        if errors:
            logger.info("agent stderr for %s: %s", task["id"], errors[:2000])

        if claim is None:
            self.outcomes[task["id"]] = OUTCOME_FAILED
            return Response(
                ok=False,
                latency_s=latency,
                error=(
                    f"the agent produced no result line for task {task['id']!r} "
                    f"(exit {process.returncode})"
                ),
                error_kind=ErrorKind.PROTOCOL,
                meta={"outcome": OUTCOME_FAILED, "task_id": task["id"]},
            )

        claimed = bool(claim.get("ok"))
        self.outcomes[task["id"]] = OUTCOME_COMPLETED if claimed else OUTCOME_FAILED
        return Response(
            ok=claimed,
            latency_s=latency,
            output=claim.get("result"),
            error=None if claimed else str(claim.get("error") or "the agent reported failure"),
            error_kind=None if claimed else ErrorKind.UNKNOWN,
            meta={
                "outcome": self.outcomes[task["id"]],
                "task_id": task["id"],
                "exit_code": process.returncode,
                # What the agent says it did, kept beside what the record shows
                # it did. Never read as a measurement.
                "claimed_attempts": claim.get("attempts"),
            },
        )

    def describe(self) -> TargetInfo:
        return TargetInfo(
            name=f"agent: {self.agent_command}",
            kind="agent",
            # Redacted for `MCPTarget`'s reason: this reaches the report header,
            # the JSON export and the AGENTS.md state block, and an upstream
            # URI is one of the places a credential hides.
            uri=redact_uri(self.upstream),
            capabilities=[task["id"] for task in self._tasks],
            metadata={
                "agent_command": self.agent_command,
                "upstream": redact_uri(self.upstream),
                "tasks_path": str(self.tasks_path),
                "tasks": len(self._tasks),
                "task_ids": [task["id"] for task in self._tasks],
                "expected_effects": {
                    task["id"]: task["expected_effects"] for task in self._tasks
                },
                "work_dir": str(self._work_dir) if self._work_dir else None,
                "proxy_command": " ".join(self.proxy_command),
                "task_timeout_s": self.timeout_s,
                "outcomes": dict(self.outcomes),
            },
        )

    def sample_request(self, index: int = 0) -> Request:
        if not self._tasks:
            raise TargetError("AgentTarget.sample_request() called before setup()")
        if index >= len(self._tasks):
            raise TargetError(
                f"task index {index} is past the end of {self.tasks_path} "
                f"({len(self._tasks)} tasks)"
            )
        task = self._tasks[index]
        return Request(
            op=str(task["id"]),
            payload={
                "prompt": task["prompt"],
                "expected_effects": task["expected_effects"],
            },
            timeout_s=self.timeout_s,
            label=f"task:{task['id']}",
            trajectory_id=f"task:{task['id']}",
        )

    def probe_requests(self, count: int, *, offset: int = 0) -> list[Request]:
        """One request per task, and `count` is deliberately ignored.

        The task file is the traffic. Running task `t1` twenty times because
        `--requests` says 20 would not be twenty operations: `expected_effects`
        is declared per task, and the oracle brackets each task, so the unit
        the whole design counts in is the task. A flag that silently multiplied
        them would make every per-task number mean something else.
        """
        return [self.sample_request(index) for index in range(len(self._tasks))]

    # -- the agent's side of the boundary ------------------------------------

    @property
    def tasks(self) -> list[dict[str, Any]]:
        return list(self._tasks)

    @property
    def work_dir(self) -> Path:
        if self._work_dir is None:
            raise TargetError("AgentTarget.work_dir is not available before setup()")
        return self._work_dir

    def task(self, task_id: str) -> dict[str, Any]:
        for task in self._tasks:
            if str(task["id"]) == str(task_id):
                return task
        raise TargetError(f"no task {task_id!r} in {self.tasks_path}")

    def record_path(self, task_id: str) -> Path:
        """Where the proxy writes this task's calls. One file per task."""
        return self.work_dir / f"record-{task_id}.jsonl"

    @property
    def schedule_path(self) -> Path:
        return self.work_dir / "schedule.json"

    def write_schedule(self, schedule: dict[tuple[str, str, int], Any]) -> None:
        """Put the forced fault table where the proxy will read it."""
        from ..proxy import write_schedule as _write

        _write(self.schedule_path, schedule)

    def _write_config(self, task_id: str) -> Path:
        """The per-task MCP config the agent launches the proxy from.

        The `env` block is the load-bearing part and the reason this file
        exists at all: the SDK copies six named variables into a stdio child,
        so a record path exported by the scan reaches the proxy through
        nothing. Written per task because `RMA_TASK_ID` and the record path
        differ per task, and because that is what a scan of many tasks has to
        do anyway.
        """
        path = self.work_dir / f"mcp-{task_id}.json"
        command, *args = self.proxy_command
        config = {
            "mcpServers": {
                "ratemyagent": {
                    "command": command,
                    "args": [*args, "--upstream", self.upstream],
                    "env": {
                        RECORD_ENV: str(self.record_path(task_id)),
                        SCHEDULE_ENV: str(self.schedule_path),
                        TASK_ENV: str(task_id),
                    },
                }
            }
        }
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        return path


def _load_tasks(path: Path) -> list[dict[str, Any]]:
    """Read and validate the task file.

    Refuses rather than filling anything in. A task file is small, hand-written
    and the source of every denominator in an agent scan; a missing
    `expected_effects` silently read as 1 would make a two-effect task look
    like a duplicated one-effect task.
    """
    if not path.exists():
        raise TargetError(f"--tasks {path} does not exist")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TargetError(f"--tasks {path} is not valid JSON: {exc}") from exc

    tasks = body.get("tasks") if isinstance(body, dict) else body
    if not isinstance(tasks, list) or not tasks:
        raise TargetError(
            f"--tasks {path} must hold a non-empty list of tasks, or an object "
            'with a "tasks" key holding one'
        )

    seen: set[str] = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise TargetError(f"task {index} in {path} is not an object")
        missing = [name for name in REQUIRED_TASK_FIELDS if name not in task]
        if missing:
            raise TargetError(
                f"task {task.get('id', index)!r} in {path} is missing "
                f"{', '.join(missing)}. Every field is required; "
                "`expected_effects` in particular is not defaulted, because a "
                "default of 1 is also a legal value."
            )
        if not isinstance(task["expected_effects"], int) or isinstance(
            task["expected_effects"], bool
        ):
            raise TargetError(
                f"task {task['id']!r} declares expected_effects "
                f"{task['expected_effects']!r}; it must be an integer"
            )
        if not isinstance(task["arguments"], dict):
            raise TargetError(f"task {task['id']!r} declares arguments that are not an object")
        if str(task["id"]) in seen:
            raise TargetError(f"task id {task['id']!r} appears twice in {path}")
        seen.add(str(task["id"]))
    return [dict(task) for task in tasks]


def _parse_claim(stdout: str) -> dict[str, Any] | None:
    """The agent's own report, read off the last JSON object it printed.

    The *last* one, so an agent may log progress as it goes. `None` when there
    is nothing to read -- which is not a failed task, it is an agent that told
    us nothing, and the caller says so in those words rather than inventing a
    verdict for it.
    """
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "ok" in parsed:
            return parsed
    return None


__all__ = ["AgentTarget", "MCP_CONFIG_ENV", "MCP_CONFIG_FLAG", "RECORD_ENV",
           "SCHEDULE_ENV", "TASK_ENV"]
