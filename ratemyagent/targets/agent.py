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

**The oracle is the upstream's own state, read per task** (C2). With
`--verify-tool` the scan opens its own connection to the upstream -- never
through the proxy, so a verify call cannot be faulted or recorded -- and reads
the state before and after each task. `E_t = after - before`, and the unit of
attribution is the task window, because the agent chooses its own arguments
and the scanner has nowhere to plant an `{op_id}`. That is also why tasks run
one at a time and the target refuses a second one in flight: with two windows
open, each contains the other's effects.

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
import re
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

#: The argv appended to `--agent`, as a template. The default is 1.5.1's fixed
#: argv written out, so a scan that passes no `--agent-command` builds exactly
#: the command line it always did and the three fixtures keep working.
#:
#: **It exists because the fixed argv cannot launch a real agent.** Every hosted
#: CLI validates its flags before it runs, and `--tasks`/`--task` are not flags
#: any of them has: the Phase D spike had to put a shim in front of Claude Code
#: purely to strip them. A template is the smallest thing that removes the shim.
DEFAULT_AGENT_ARGV = f"{MCP_CONFIG_FLAG} {{config}} --tasks {{tasks}} --task {{task_id}}"

#: Placeholders a template may use.
TEMPLATE_FIELDS = ("config", "prompt", "task_id", "tasks")

#: What a user-supplied template must contain. `{config}` because without it the
#: agent never reaches the proxy and the scan measures a direct connection to the
#: upstream without noticing; `{prompt}` because an agent launched by a template
#: is not reading our task file, so the prompt has nowhere else to come from.
#:
#: **Not checked against `DEFAULT_AGENT_ARGV`**, which carries `{tasks}` and
#: `{task_id}` instead of `{prompt}`: the fixtures read the prompt out of the
#: task file themselves, which is exactly the arrangement the default preserves.
#: The rule is about templates a user wrote, so it runs only on those.
REQUIRED_TEMPLATE_FIELDS = ("config", "prompt")

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
#: How this target's scans earn a verdict, declared rather than inferred and
#: carried in `describe().metadata` so the policy can read it off the result.
#: See `policy.agent_verdict_blocker`.
COVERAGE_RULE = "agent_behavior"

#: The unit a `duplicate_mutations` count is attributed to on this target.
#: `op_id` on a server scan; here the agent picks its own arguments, so the
#: task window is the only unit there is.
EFFECT_ATTRIBUTION = "task_window"

#: The pass a record belongs to before any probe names one. Direct `invoke()`
#: use -- the tests, a Python caller -- lands here.
DEFAULT_PASS = "run"

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

    #: No `{op_id}` is ever generated on this path: the agent writes its own
    #: arguments and the proxy relays them byte-identical. Declared so
    #: `recovery_op_ids` and the staleness check have nothing to register.
    uses_op_id = False

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
        verify_tool: str | None = None,
        verify_args: dict[str, Any] | None = None,
        verify_count: str | None = None,
        agent_argv: str | None = None,
        claim_path: str | None = None,
    ) -> None:
        self.agent_command = agent_command
        #: `None` means the default argv, and the distinction is kept rather
        #: than collapsed: the required-placeholder check applies to a template
        #: the user wrote and not to the one this file ships.
        self.agent_argv = agent_argv
        #: Dotted path to the claim object inside a single JSON document on
        #: stdout. `None` keeps 1.5.1's rule: the last JSON line.
        self.claim_path = claim_path
        self.tasks_path = Path(tasks_path)
        self.upstream = upstream
        self.timeout_s = timeout_s
        self.allow_mutating = allow_mutating
        #: Same interpreter as the scan, so the proxy is the build under test
        #: rather than whatever a `ratemyagent` on PATH resolves to.
        self.proxy_command = proxy_command or [
            sys.executable, "-m", "ratemyagent.cli", "proxy",
        ]

        self._verify_tool = verify_tool
        self._verify_args = dict(verify_args) if verify_args else {}
        self._verify_count = verify_count

        self._work_dir = Path(work_dir) if work_dir else None
        self._tasks: list[dict[str, Any]] = []
        #: task id -> `completed` / `failed` / `abandoned`.
        self.outcomes: dict[str, str] = {}
        self._processes: set[asyncio.subprocess.Process] = set()
        #: Which pass the records and configs below belong to. One file per
        #: pass per task: the baseline and the chaos pass writing to the same
        #: record made the clean call attempt 1 of every chaos trajectory, so a
        #: task whose every chaos call failed read as never disrupted (C2).
        self._pass = DEFAULT_PASS
        #: The task currently running, if any. Two at once is refused.
        self._in_flight: str | None = None

    @property
    def has_effect_oracle(self) -> bool:
        """True only when `--verify-tool` was given. Declared, never inferred."""
        return self._verify_tool is not None

    # -- Target interface ----------------------------------------------------

    async def setup(self) -> None:
        _check_template(self.agent_argv)
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
        if self.has_effect_oracle:
            await self._check_oracle()

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
        if self._in_flight is not None:
            # **Enforced, not assumed.** The oracle's window for a task is the
            # time between two reads of the upstream; a second task running
            # inside it puts its effects in the first one's count, and no
            # arithmetic afterwards takes them out. The probes already run tasks
            # one at a time -- this is what makes that a property of the target
            # rather than of whoever happens to be calling it.
            raise TargetError(
                f"task {task['id']!r} was started while task "
                f"{self._in_flight!r} is still running. An agent target runs "
                f"one task at a time: effects are attributed per task window, "
                f"and overlapping windows cannot be separated."
            )
        self._in_flight = str(task["id"])
        try:
            return await self._run_task(task)
        finally:
            self._in_flight = None

    async def _run_task(self, task: dict[str, Any]) -> Response:
        config_path = self._write_config(task["id"])

        command = [
            *shlex.split(self.agent_command),
            *_render_argv(
                self.agent_argv or DEFAULT_AGENT_ARGV,
                config=str(config_path),
                prompt=str(task["prompt"]),
                task_id=str(task["id"]),
                tasks=str(self.tasks_path),
            ),
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
        text = stdout.decode("utf-8", "replace")
        claim = (
            _claim_at(text, self.claim_path) if self.claim_path
            else _parse_claim(text)
        )
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
                "coverage_rule": COVERAGE_RULE,
                "effect_attribution": EFFECT_ATTRIBUTION,
                # Read by `verify_not_measured` and the agent verdict rule to
                # tell "no oracle was asked for" from "one was and failed".
                "verify_tool": self._verify_tool,
                "verify_count": self._verify_count,
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

    @property
    def current_pass(self) -> str:
        return self._pass

    def start_pass(self, name: str) -> None:
        """Point records, configs and the schedule at a fresh set of files.

        Called by each probe before it runs tasks. **Separate files per pass
        are what keep the passes apart**: the proxy restores its ordinal
        counter from the record (A4) and the replay groups rows by fingerprint,
        so one file shared by the baseline and the chaos pass started the chaos
        schedule one call late and folded the clean call into the chaos
        trajectory as a successful first attempt.
        """
        if not name or any(sep in name for sep in "/\\"):
            raise TargetError(f"pass name {name!r} is not a plain word")
        self._pass = name

    def record_path(self, task_id: str) -> Path:
        """Where the proxy writes this task's calls. One file per task per pass."""
        return self.work_dir / f"record-{self._pass}-{task_id}.jsonl"

    @property
    def schedule_path(self) -> Path:
        return self.work_dir / f"schedule-{self._pass}.json"

    # -- the oracle ------------------------------------------------------------

    def _oracle_connection(self) -> Any:
        from .mcp import MCPTarget

        return MCPTarget(
            self.upstream,
            timeout_s=self.timeout_s,
            verify_tool=self._verify_tool,
            verify_args=self._verify_args,
            verify_count=self._verify_count,
            # Reads only. No probe tool, no preflight: the oracle must not write
            # into the state it counts.
            probe_traffic=False,
            allow_mutating=self.allow_mutating,
        )

    async def _check_oracle(self) -> None:
        """Refuse a verify tool that is not known read-only, or does not read.

        The same two refusals a server scan makes at setup, before any task
        runs: an oracle that writes changes the number it defines, and a
        `--verify-count` path that does not resolve is better found now than as
        `failed` on every task.
        """
        from .mutability import Mutability, classify, describe_verify_refusal

        oracle = self._oracle_connection()
        await oracle.setup()
        try:
            tools = oracle.list_tools()
            chosen = next((t for t in tools if t.name == self._verify_tool), None)
            if chosen is None:
                available = ", ".join(t.name for t in tools if t.name) or "none"
                raise TargetError(
                    f"verify tool {self._verify_tool!r} not found on the upstream; "
                    f"available: {available}"
                )
            if classify(chosen) is not Mutability.READ_ONLY:
                raise TargetError(describe_verify_refusal(tools, chosen))
            if not self.allow_mutating:
                raise TargetError(
                    "--verify-tool needs --allow-mutating: an agent scan exists to "
                    "watch an agent write, and the tasks will write."
                )
            if await oracle.read_effect_entries() is None:
                raise TargetError(
                    f"verify tool {self._verify_tool!r} did not answer at setup, "
                    f"so no task could be measured. Check the tool and "
                    f"--verify-count."
                )
        finally:
            await oracle.teardown()

    async def read_effect_entries(self) -> list | int | None:
        """The upstream's state, on a connection of its own, or None.

        **A fresh connection per read.** A stdio upstream is a process, and the
        agent's proxy starts its own copy per task; a long-lived oracle process
        would read its own memory rather than the state the agent's copy wrote.
        Opening one per read makes the oracle see what is on disk -- or behind
        the URL -- at that moment, which is the only state there is.

        None when the read failed, which the probe records as `failed` for that
        task. Never zero: "we could not look" is not "nothing was applied".
        """
        if not self.has_effect_oracle:
            return None
        oracle = self._oracle_connection()
        try:
            await oracle.setup()
        except TargetError as exc:
            logger.warning("verify connection failed: %s", exc)
            return None
        try:
            return await oracle.read_effect_entries()
        except TargetError as exc:
            logger.warning("verify read failed: %s", exc)
            return None
        finally:
            await oracle.teardown()

    def write_schedule(
        self,
        schedule: dict[tuple[str, str, int], Any],
        *,
        close_after_s: float | None = None,
    ) -> None:
        """Put the forced fault table where the proxy will read it."""
        from ..proxy import write_schedule as _write

        _write(self.schedule_path, schedule, close_after_s=close_after_s)

    def config_path(self, task_id: str) -> Path:
        """The MCP config the agent is handed for this task in this pass."""
        return self.work_dir / f"mcp-{self._pass}-{task_id}.json"

    def _write_config(self, task_id: str) -> Path:
        """The per-task MCP config the agent launches the proxy from.

        The `env` block is the load-bearing part and the reason this file
        exists at all: the SDK copies six named variables into a stdio child,
        so a record path exported by the scan reaches the proxy through
        nothing. Written per task because `RMA_TASK_ID` and the record path
        differ per task, and because that is what a scan of many tasks has to
        do anyway.
        """
        path = self.config_path(task_id)
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


def _check_template(template: str | None) -> None:
    """Refuse a user template that cannot produce a working launch.

    Named placeholders, not a count: "missing {prompt}" is something a user can
    act on and "expected 2 placeholders" is not.
    """
    if template is None:
        return
    missing = [
        field for field in REQUIRED_TEMPLATE_FIELDS
        if "{" + field + "}" not in template
    ]
    if missing:
        raise TargetError(
            "--agent-command is missing "
            + " and ".join("{" + field + "}" for field in missing)
            + f". The template was: {template!r}\n"
            "{config} is the per-task MCP config; without it the agent never "
            "reaches the proxy and the scan would measure a direct connection "
            "to the upstream. {prompt} is the task text; an agent launched by a "
            "template is not reading the task file, so nothing else carries it."
        )
    unknown = sorted(
        set(re.findall(r"\{([a-z_]+)\}", template)) - set(TEMPLATE_FIELDS)
    )
    if unknown:
        raise TargetError(
            "--agent-command uses placeholders this scan cannot fill: "
            + ", ".join("{" + field + "}" for field in unknown)
            + ". Available: "
            + ", ".join("{" + field + "}" for field in TEMPLATE_FIELDS)
        )


def _render_argv(template: str, **fields: str) -> list[str]:
    """Split the template, then substitute -- in that order, deliberately.

    A prompt is a sentence with spaces in it, and substituting before splitting
    would let it become five arguments. Splitting first means one placeholder is
    one argv entry whatever it contains, and no quoting rule is imposed on
    whoever writes the task file.
    """
    rendered: list[str] = []
    for token in shlex.split(template):
        for field, value in fields.items():
            token = token.replace("{" + field + "}", value)
        rendered.append(token)
    return rendered


def _claim_at(stdout: str, path: str) -> dict[str, Any] | None:
    """The claim object at a dotted path inside a single JSON document.

    The smallest pointer syntax that does the job: dot-separated keys, objects
    only. Not JSONPath and not JSON Pointer -- both are specifications this would
    then owe a conformant implementation of, to address `structured_output`,
    which is two levels deep at worst.

    **Refuses rather than reporting a failed task.** Unparseable stdout, or a
    path that is not there, means the scan was told where to look and the
    instruction was wrong. Returning `ok=False` would record that as an agent
    that failed its task, which is a measurement invented out of a
    misconfiguration -- the shape section 8b keeps cataloguing.
    """
    body = stdout.strip()
    if not body:
        raise TargetError(
            f"--claim-path {path!r} was given and the agent printed nothing on "
            "stdout. There is no document to read the claim out of."
        )
    try:
        document: Any = json.loads(body)
    except json.JSONDecodeError as exc:
        raise TargetError(
            f"--claim-path {path!r} needs stdout to be one JSON document and it "
            f"is not ({exc}). First 200 characters: {body[:200]!r}"
        ) from None

    walked: list[str] = []
    for key in path.split("."):
        if not isinstance(document, dict) or key not in document:
            where = ".".join(walked) or "the top level"
            available = (
                ", ".join(sorted(document)) if isinstance(document, dict)
                else f"a {type(document).__name__}, not an object"
            )
            raise TargetError(
                f"--claim-path {path!r}: no {key!r} at {where}. Found: {available}"
            )
        document = document[key]
        walked.append(key)

    if not isinstance(document, dict) or "ok" not in document:
        # Built outside the f-string: a nested quote inside one is a syntax
        # error before Python 3.12, and this package supports 3.10.
        found = (
            'an object with no "ok" key' if isinstance(document, dict)
            else type(document).__name__
        )
        raise TargetError(
            f"--claim-path {path!r} points at {found}. The claim has to be an "
            "object carrying `ok`, the agent's own report of whether it "
            "succeeded."
        )
    return document


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


def effect_count(entries: list | int | None) -> int | None:
    """How many effects a verify read reports: list length, or the number."""
    if entries is None or isinstance(entries, bool):
        return None
    if isinstance(entries, list):
        return len(entries)
    if isinstance(entries, int):
        return entries
    return None


__all__ = ["AgentTarget", "COVERAGE_RULE", "EFFECT_ATTRIBUTION", "MCP_CONFIG_ENV",
           "MCP_CONFIG_FLAG", "RECORD_ENV", "SCHEDULE_ENV", "TASK_ENV", "effect_count"]
