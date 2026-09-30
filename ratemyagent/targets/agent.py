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
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import ErrorKind, Request, Response, TargetInfo
from .base import Target, TargetError, redact_command, redact_uri, walk_dotted

logger = logging.getLogger(__name__)

#: How the agent is told where its MCP config is, on argv and in its own env.
#: Both, because a real agent reads one or the other and a fixture should not
#: get to pick the convenient one.
MCP_CONFIG_FLAG = "--mcp-config"
MCP_CONFIG_ENV = "RMA_MCP_CONFIG"

#: The one placeholder `--upstream` understands, and what it resolves to for
#: each of the two consumers.
#:
#: **Why the upstream needs one at all.** A scan of an agent launches the
#: upstream server twice over, in two roles that must not be confused: the
#: agent's copy, behind `ratemyagent proxy`, and the scan's own read of the
#: state, on a connection of its own (`_oracle_connection`). One string is
#: authored, so a server that behaves differently in the two roles -- an
#: upstream that scopes idempotency keys to an operation, say -- has no way to
#: be told which one it is. It used to guess from its parent process and
#: guessed wrong on some machines, silently.
#:
#: **Substituted at each point of consumption, never at storage.** `self.upstream`
#: stays the string the user wrote, so the report, the export and the record
#: show what was asked for rather than one of the two things that ran.
#:
#: A command with no `{role}` is passed through byte-identical, so every
#: upstream that worked before this existed still does.
ROLE_PLACEHOLDER = "{role}"
ROLE_ORACLE = "oracle"
ROLE_AGENT = "agent"


def substitute_role(upstream: str, role: str) -> str:
    """`upstream` with `{role}` resolved, or unchanged when it carries none."""
    return upstream.replace(ROLE_PLACEHOLDER, role)

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

#: What is on the other end of `--agent`, **declared rather than inferred**.
#:
#: The distinction decides which metrics are scored: three of Phase C's six
#: readings are properties of a *retry loop*, and a model deciding does not have
#: one (`DESIGN-AGENT-D.md` (c)). A scripted fixture with a real retry loop and
#: real sleeps produces a meaningful `backoff_shape`; an LLM produces a gap that
#: is one inference round trip, and nothing tells the two apart after the fact.
#:
#: **Declared, not sniffed**, and the same rule as `has_effect_oracle` and
#: `coverage_rule` for the same reason. Every observable candidate is a
#: threshold on a continuum -- wall clock per task, inter-call gaps, whether
#: repeats differ -- with no gap that survives a loaded machine, a fixture that
#: sleeps, or a small local model. A threshold quietly deciding which metrics
#: get scored is `concurrency_min` again, and "it was fast, therefore it is a
#: script" is a lookup that cannot fail wired to a default that looks like a
#: real answer (PROGRESS 8b, opening).
#:
#: The default is `scripted` because that is what every existing scan is, and a
#: default that silently unscored the shipped fixtures would change Phase C's
#: results without anyone asking for it.
AGENT_KIND_SCRIPTED = "scripted"
AGENT_KIND_LLM = "llm"
AGENT_KINDS = (AGENT_KIND_SCRIPTED, AGENT_KIND_LLM)

OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_ABANDONED = "abandoned"

#: How much of the agent's stderr is logged when it ends without a result line
#: (1.9.0). **The tail, not the head**: a traceback's last lines are the ones
#: that name the exception, and the head of a long log is start-up noise. Every
#: agent run used to log only the first 2,000 characters, at INFO, and a task
#: killed at its deadline logged nothing at all -- so the parallel-CI flake's
#: second failure could only be inferred (FLAKE-1.8.0).
STDERR_TAIL_CHARS = 4000
#: Lines of that tail carried into a refusal's text, where a refusal follows.
STDERR_REFUSAL_LINES = 10
#: How long to keep reading the agent's pipes after killing it. A proxy the
#: agent started inherits its stderr and can hold the pipe open, so reading to
#: EOF could wait on a process this scan does not own.
STDERR_DRAIN_AFTER_KILL_S = 2.0

#: The shell `--verify-command` runs under (1.9.0). `/bin/sh -c` on the string
#: exactly as typed, so what the user ran at their prompt is what runs here.
#: POSIX only.
VERIFY_SHELL = "/bin/sh"
#: How much of a failed verify command's stderr a setup refusal prints.
VERIFY_STDERR_BYTES = 500

#: What `--key-path` means when it is not given: the one name the proxy has
#: always recorded (`proxy.IDEMPOTENCY_ARG`). Duplicated as a literal rather
#: than imported, because `proxy` imports this package's `mcp` module and
#: importing it here would make a cycle; `tests/test_verify_command.py` pins
#: that the two agree.
DEFAULT_KEY_PATH = "idempotency_key"


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
        hold_reply_s: float | None = None,
        agent_kind: str = AGENT_KIND_SCRIPTED,
        verify_command: str | None = None,
        key_path: str | None = None,
    ) -> None:
        if verify_tool is not None and verify_command is not None:
            # Two oracles are two denominators, and preferring one quietly
            # would choose the experiment (DESIGN-TIER-1 1.4). The CLI refuses
            # first, as a usage error; this is the Python caller's copy.
            raise TargetError(
                "--verify-tool and --verify-command are mutually exclusive: each "
                "is a complete effect oracle, and two would be two denominators."
            )
        if verify_command is not None and verify_args:
            raise TargetError(
                "--verify-args means something only to an MCP verify tool; a "
                "--verify-command takes its arguments in the command itself."
            )
        if key_path is not None and not all(key_path.split(".")):
            raise TargetError(
                f"--key-path {key_path!r} is not a dotted path of keys, e.g. "
                "idempotency_key or options.key"
            )
        self.agent_command = agent_command
        #: `None` means the default argv, and the distinction is kept rather
        #: than collapsed: the required-placeholder check applies to a template
        #: the user wrote and not to the one this file ships.
        self.agent_argv = agent_argv
        #: Dotted path to the claim object inside a single JSON document on
        #: stdout. `None` keeps 1.5.1's rule: the last JSON line.
        self.claim_path = claim_path
        #: Hold one reply for this long in an extra clean-pass task, to observe
        #: the agent's own read timeout. `None` means the task does not run and
        #: the scan is exactly 1.6.0's. See `probes.agent_deadline`.
        self.hold_reply_s = hold_reply_s
        if agent_kind not in AGENT_KINDS:
            raise TargetError(
                f"agent kind {agent_kind!r} is not one of {', '.join(AGENT_KINDS)}"
            )
        #: `scripted` or `llm`. See `AGENT_KIND_SCRIPTED`.
        self.agent_kind = agent_kind
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
        #: The shell command that reads the upstream's state (1.9.0), run as
        #: typed under `VERIFY_SHELL`. Never written down: it may carry an
        #: inlined password, so the export carries `verify_command_digest`.
        self._verify_command = verify_command
        #: Where the agent's retry key sits in a tool's arguments (1.9.0).
        #: `None` means `--key-path` was not given; the proxy then reads
        #: `DEFAULT_KEY_PATH`, exactly as it always has, and the schedule file
        #: is byte-identical to 1.8.0's.
        self.key_path = key_path

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
        """True when `--verify-tool` or `--verify-command` was given.

        Declared, never inferred.
        """
        return self._verify_tool is not None or self._verify_command is not None

    @property
    def oracle_name(self) -> str:
        """The oracle as the user named it, for messages that must say which."""
        return "--verify-command" if self._verify_command is not None else "--verify-tool"

    # -- Target interface ----------------------------------------------------

    async def setup(self) -> None:
        from .mcp import _refuse_unquoted_space

        # The 1.5.1 rule, applied to `--agent` (1.9.0): refused only on proof
        # that the split broke a path that exists, with the quoted form printed.
        _refuse_unquoted_space(
            _split_command(self.agent_command), flag="--agent", prefix="",
            what="--agent",
        )
        _check_template(self.agent_argv)
        self._tasks = _load_tasks(self.tasks_path)
        if self._work_dir is None:
            # Not a TemporaryDirectory: the record is the artifact. A file
            # somebody can read after a failed run settles "the agent says it
            # retried twice"; a directory that vanished with the scan settles
            # nothing.
            self._work_dir = Path(tempfile.mkdtemp(prefix="ratemyagent-agent-"))
        self._work_dir.mkdir(parents=True, exist_ok=True)
        _refuse_used_work_dir(self._work_dir)
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
            *_split_command(self.agent_command),
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
        # **Read into buffers, not through `communicate()`** (1.9.0). A
        # cancelled `communicate()` discards what it had read, so the deadline
        # path had no stderr to log: a task killed at its deadline left no
        # traceback. The buffers keep every byte read before the kill.
        out, err = bytearray(), bytearray()
        readers = [
            asyncio.ensure_future(_drain(process.stdout, out)),
            asyncio.ensure_future(_drain(process.stderr, err)),
        ]
        waiter = asyncio.ensure_future(process.wait())
        try:
            _, pending = await asyncio.wait([*readers, waiter], timeout=self.timeout_s)
            if pending:
                # **The deadline is the scan's, not the agent's.** An agent
                # with no read timeout waits forever on a reply this scan
                # dropped on purpose, which is a real production failure mode
                # and not a harness artifact -- so it is reported as
                # `abandoned` rather than fixed. A scan that never finished is
                # not a target that failed.
                if process.returncode is None:
                    process.kill()
                await process.wait()
                await asyncio.wait(readers, timeout=STDERR_DRAIN_AFTER_KILL_S)
                tail = _log_stderr_tail(
                    task["id"], err, f"killed at the {self.timeout_s:.0f}s deadline"
                )
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
                    meta={
                        "outcome": OUTCOME_ABANDONED,
                        "task_id": task["id"],
                        # Wall clock, on the record's own clock. `latency_s` is
                        # `perf_counter` and shares no origin with the record's
                        # `time.time()` stamps, so it cannot be compared
                        # against them -- and the deadline probe's whole
                        # measurement is exactly that comparison.
                        "finished_at": time.time(),
                        "stderr_tail": _last_lines(tail),
                    },
                )
        finally:
            for pending_task in (*readers, waiter):
                if not pending_task.done():
                    pending_task.cancel()
            self._processes.discard(process)

        latency = time.perf_counter() - started
        finished_at = time.time()
        text = bytes(out).decode("utf-8", "replace")
        if self.claim_path:
            try:
                claim = _claim_at(text, self.claim_path)
            except TargetError as exc:
                # A refusal, and one about the agent's output: its stderr is
                # the likeliest account of why stdout was not the document.
                tail = _log_stderr_tail(
                    task["id"], err, f"exit {process.returncode}, no claim at "
                    f"--claim-path {self.claim_path!r}",
                )
                raise TargetError(str(exc) + _stderr_sentence(tail)) from None
        else:
            claim = _parse_claim(text)

        if claim is None:
            tail = _log_stderr_tail(
                task["id"], err, f"exit {process.returncode}, no result line"
            )
            self.outcomes[task["id"]] = OUTCOME_FAILED
            return Response(
                ok=False,
                latency_s=latency,
                error=(
                    f"the agent produced no result line for task {task['id']!r} "
                    f"(exit {process.returncode})"
                ),
                error_kind=ErrorKind.PROTOCOL,
                meta={
                    "outcome": OUTCOME_FAILED,
                    "task_id": task["id"],
                    # Beside the one in the error text, so a refusal that has
                    # to explain an unrecorded task can say how the agent ended
                    # without parsing a sentence (`explain_unrecorded`).
                    "exit_code": process.returncode,
                    "finished_at": finished_at,
                    # The last lines, for any refusal that follows (1.9.0).
                    "stderr_tail": _last_lines(tail),
                },
            )

        errors = bytes(err).decode("utf-8", "replace").strip()
        if errors:
            logger.info("agent stderr for %s: %s", task["id"], errors[:2000])

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
                "finished_at": finished_at,
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
                # The interpreter's directory is dropped and everything that
                # says what ran is kept. `sys.executable` is the default here,
                # so without this every agent scan's `--json-out` carried the
                # user's home directory -- see `redact_command`.
                "proxy_command": redact_command(self.proxy_command),
                "task_timeout_s": self.timeout_s,
                "outcomes": dict(self.outcomes),
                "coverage_rule": COVERAGE_RULE,
                # Which metrics this scan is entitled to score, carried into
                # the export so a consumer can tell a run whose timing metrics
                # were withheld from one that had none to report.
                "agent_kind": self.agent_kind,
                "effect_attribution": EFFECT_ATTRIBUTION,
                # Read by `verify_not_measured` and the agent verdict rule to
                # tell "no oracle was asked for" from "one was and failed".
                "verify_tool": self._verify_tool,
                "verify_count": self._verify_count,
                # The command's identity, never its text (1.9.0): the first
                # word and a hash prefix. The text may carry an inlined
                # password, and this reaches `--json-out` -- the `--header`
                # rule (SCANNING.md). `None` when no command was given.
                "verify_command_digest": verify_command_digest(self._verify_command),
                # Where the retry key was read, when `--key-path` was given.
                # `None` means the default, `idempotency_key`.
                "key_path": self.key_path,
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
            substitute_role(self.upstream, ROLE_ORACLE),
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
        if self._verify_command is not None:
            await self._check_command()
            return

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

    async def _check_command(self) -> None:
        """The command's setup refusals (DESIGN-TIER-1 1.2), before any task runs.

        **Read twice, and refused if the two differ.** A shell command cannot
        be classified read-only the way a tool's annotations can; two readings
        of a store nothing else is writing must agree, and one that moves on
        its own is either written by something else or writes itself. That is
        the stand-in for the read-only check, and it is only a stand-in -- the
        docs say so. The clean-pass persistence check is the stronger one.
        """
        if not self.allow_mutating:
            raise TargetError(
                "--verify-command needs --allow-mutating: an agent scan exists "
                "to watch an agent write, and the tasks will write."
            )
        first = await self._run_verify_command()
        if first.value is None:
            raise TargetError(
                "--verify-command did not measure at setup, so no task could be "
                "measured. " + first.describe() + "\n\n" + VERIFY_OUTPUT_RULE
            )
        second = await self._run_verify_command()
        if second.value is None:
            raise TargetError(
                "--verify-command measured once at setup and then did not. "
                + second.describe() + "\n\n" + VERIFY_OUTPUT_RULE
            )
        if first.value != second.value:
            raise TargetError(
                f"--verify-command read {_reading(first.value)} and then "
                f"{_reading(second.value)} at setup, with no task running. A "
                f"reading that moves on its own cannot bracket a task: something "
                f"else writes to the store it reads, or the command writes. "
                f"Point it at a store only the agent's upstream writes."
            )

    async def _run_verify_command(self) -> "VerifyRead":
        assert self._verify_command is not None
        return await run_verify_command(
            self._verify_command, timeout_s=self.timeout_s,
            count_path=self._verify_count,
        )

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
        if self._verify_command is not None:
            # A failed read is `None` here exactly as a failed MCP read is
            # (1.9.0): the task is `failed`, never counted as zero.
            read = await self._run_verify_command()
            if read.value is None:
                logger.warning("verify command read failed: %s", read.describe())
            return read.value
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
        hold_s: float | None = None,
    ) -> None:
        """Put the forced fault table where the proxy will read it.

        Every pass carries `--key-path` when it was given (1.9.0), the clean
        pass included: `_shares_key` reads the key on the clean pass's rows.
        """
        from ..proxy import write_schedule as _write

        _write(
            self.schedule_path, schedule,
            close_after_s=close_after_s, hold_s=hold_s, key_path=self.key_path,
        )

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
                    # The agent's copy. `self.upstream` is what the user
                    # wrote; this is the one of its two readings that belongs
                    # behind the proxy.
                    "args": [
                        *args, "--upstream",
                        substitute_role(self.upstream, ROLE_AGENT),
                    ],
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
        if "expected_entries" in task:
            _check_entries(task)
        if str(task["id"]) in seen:
            raise TargetError(f"task id {task['id']!r} appears twice in {path}")
        seen.add(str(task["id"]))
    _check_tokens_apart(tasks, path)
    return [dict(task) for task in tasks]


def _check_entries(task: dict[str, Any]) -> None:
    """`expected_entries`: the oracle-visible tokens a task applies (1.7.5).

    **Optional, and absent means exactly the 1.7.4 reading.** When present it is
    what the oracle should show after the task, one token per effect, read as a
    multiset -- a token may repeat when the task deliberately writes one entry
    twice. So its length is `expected_effects`, and a list that disagrees is two
    denominators for one task: refused rather than resolved in favour of either.

    Not `writes` or `arguments`: those are the agent's *input*, and a count
    derived from the input can never disagree with the behaviour it describes.
    """
    entries = task["expected_entries"]
    if not isinstance(entries, list) or not all(
        isinstance(token, str) and token for token in entries
    ):
        raise TargetError(
            f"task {task['id']!r} declares expected_entries {entries!r}; it must be "
            "a list of non-empty strings, each one a token the verify tool's "
            "entries contain"
        )
    if len(entries) != task["expected_effects"]:
        raise TargetError(
            f"task {task['id']!r} declares {len(entries)} expected_entries and "
            f"expected_effects {task['expected_effects']}; one entry per effect, "
            "so the two must agree"
        )


def _check_tokens_apart(tasks: list[Any], path: Path) -> None:
    """No declared token may be a substring of a different one in the file.

    An entry is matched to a token by containment in its JSON (`count_matching`,
    the rule the server path trusts for `{op_id}`), which is what lets a token
    match a bare id and a server row with generated columns alike. It is sound
    only if one token can never be found inside another: `e1` would match every
    entry of `e10`. Equal tokens are allowed -- a repeated write is declared
    that way -- so only proper substrings are refused.
    """
    tokens = sorted({
        token for task in tasks if isinstance(task, dict)
        for token in task.get("expected_entries") or ()
    })
    for inner in tokens:
        for outer in tokens:
            if inner != outer and inner in outer:
                raise TargetError(
                    f"--tasks {path} declares expected_entries {inner!r} and "
                    f"{outer!r}; the first is inside the second, so an entry of "
                    f"{outer!r} would also count as {inner!r}. Make every token "
                    "distinct from every other token's substrings."
                )


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

    found, document, walked = walk_dotted(document, path)
    if not found:
        # The walker reports; this caller refuses. `--key-path` walks the same
        # way and records `None` on a miss instead (`walk_dotted`).
        key = path.split(".")[len(walked)]
        where = ".".join(walked) or "the top level"
        available = (
            ", ".join(sorted(document)) if isinstance(document, dict)
            else f"a {type(document).__name__}, not an object"
        )
        raise TargetError(
            f"--claim-path {path!r}: no {key!r} at {where}. Found: {available}"
        )

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


def _refuse_used_work_dir(work_dir: Path) -> None:
    """Refuse a `--work-dir` that already holds an earlier scan's records (1.9.0).

    **The proxy continues a record; it never starts one over.** That is how a
    reconnecting agent keeps its place in the forced schedule (A4), and it is
    right within a scan. Across two scans it is wrong in two ways at once: the
    second scan's faults start where the first scan's calls stopped, so the
    fault its seed places at ordinal 1 lands on nothing; and its clean-call
    denominator includes the first scan's calls. Found building the 1.9.0
    walkthrough, whose step 6 re-runs with the seed a NO VERDICT names: into
    the same `./rma-work`, the re-run realized no fault and named the same
    seed again.

    Refused on proof -- a record file exists before any task ran -- and never
    repaired: the records are the earlier scan's evidence, and moving or
    truncating them is the user's call. The default work directory is new on
    every scan, so this cannot fire there.
    """
    used = sorted(path.name for path in work_dir.glob("record-*.jsonl"))
    if used:
        shown = ", ".join(used[:4]) + (f" and {len(used) - 4} more" if len(used) > 4 else "")
        raise TargetError(
            f"refusing to scan: --work-dir {work_dir} already holds "
            f"{len(used)} record file(s) from an earlier scan ({shown}). The "
            f"proxy continues a record rather than starting one -- that is how a "
            f"reconnecting agent keeps its place in the fault schedule -- so this "
            f"scan's faults would start where that scan's calls stopped, and its "
            f"clean-path call counts would include them. Point --work-dir at a "
            f"new directory, or move those records out."
        )


def _split_command(command: str) -> list[str]:
    """`--agent` as argv, or a refusal that says why it could not be split."""
    try:
        return shlex.split(command)
    except ValueError as exc:
        raise TargetError(f"--agent {command!r} cannot be split: {exc}") from None


async def _drain(stream: Any, sink: bytearray) -> None:
    """Append everything `stream` yields to `sink`, until EOF or cancellation.

    The bytes land in `sink` as they arrive, so a cancelled read loses nothing
    that was already read -- which is the property `communicate()` lacks.
    """
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        sink.extend(chunk)


def _log_stderr_tail(task_id: Any, raw: bytes | bytearray, how: str) -> str:
    """Log the last `STDERR_TAIL_CHARS` of the agent's stderr at WARNING.

    Called on every agent exit without a result line (1.9.0), the deadline
    kill included. Returns the tail so a refusal can quote its last lines. An
    empty stderr is logged as empty: "it said nothing" is also an account.
    """
    text = bytes(raw).decode("utf-8", "replace").rstrip()
    tail = text[-STDERR_TAIL_CHARS:]
    if tail:
        logger.warning(
            "agent ended task %s without a result (%s); the last %d characters "
            "of its stderr:\n%s", task_id, how, len(tail), tail,
        )
    else:
        logger.warning(
            "agent ended task %s without a result (%s) and wrote nothing to stderr",
            task_id, how,
        )
    return tail


def _last_lines(tail: str, count: int = STDERR_REFUSAL_LINES) -> list[str]:
    return [line for line in tail.splitlines() if line.strip()][-count:]


def _stderr_sentence(tail: str) -> str:
    """The last stderr lines, as a paragraph to append to a refusal."""
    lines = _last_lines(tail)
    if not lines:
        return "\n\nThe agent wrote nothing to stderr."
    return "\n\nThe agent's stderr ended:\n" + "\n".join(f"  {line}" for line in lines)


#: What a verify command has to print, said once for every refusal that needs it.
VERIFY_OUTPUT_RULE = (
    "The command must exit 0 and print a non-negative integer (a count) or a "
    "JSON list (the entries); with --verify-count, a JSON object holding one of "
    "those at that path. Empty output is not an empty list: use an aggregate "
    "that prints [] when there are no rows, e.g. sqlite3's json_group_array. "
    "Run it at your own prompt until it does."
)


@dataclass(frozen=True)
class VerifyRead:
    """One run of `--verify-command`: the reading, and how it was obtained."""

    value: list | int | None
    exit_code: int | None
    stderr_tail: str
    first_line: str
    timed_out: bool = False
    timeout_s: float | None = None

    def describe(self) -> str:
        """Exit code, stderr tail and first stdout line -- what setup prints."""
        if self.timed_out:
            head = (
                f"It did not finish within --timeout {self.timeout_s:g}s, and "
                f"its process group was killed."
            )
        elif self.exit_code != 0:
            head = f"It exited {self.exit_code}, so its output was not read."
        else:
            head = "It exited 0 and printed no count and no list."
        stderr = self.stderr_tail.strip()
        return (
            f"{head} Exit code: {self.exit_code}. "
            f"Last {VERIFY_STDERR_BYTES} bytes of stderr: {stderr!r}. "
            f"First line of stdout: {self.first_line!r}."
        )


async def run_verify_command(
    command: str, *, timeout_s: float, count_path: str | None = None,
) -> VerifyRead:
    """Run `--verify-command` once and parse its reading (DESIGN-TIER-1 1.1-1.2).

    `/bin/sh -c` on the string exactly as typed, stdin `/dev/null` so a client
    that would prompt fails instead of hanging, the scan's whole environment
    inherited (so `$DATABASE_URL` expands in the shell and stays out of the
    text), and its own session, so a timeout kills the process group and not
    just the shell. Nothing is substituted into the command.
    """
    process = await asyncio.create_subprocess_exec(
        VERIFY_SHELL, "-c", command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        _kill_group(process)
        await process.wait()
        return VerifyRead(
            value=None, exit_code=process.returncode, stderr_tail="",
            first_line="", timed_out=True, timeout_s=timeout_s,
        )
    except asyncio.CancelledError:
        _kill_group(process)
        raise
    text = stdout.decode("utf-8", "replace")
    stripped = text.strip()
    first_line = stripped.splitlines()[0] if stripped else ""
    return VerifyRead(
        # **A non-zero exit is `None`, and stdout is not read.** A `psql` that
        # printed `0` and exited 2 did not measure.
        value=parse_verify_output(text, count_path) if process.returncode == 0 else None,
        exit_code=process.returncode,
        stderr_tail=stderr[-VERIFY_STDERR_BYTES:].decode("utf-8", "replace"),
        first_line=first_line,
    )


def _kill_group(process: Any) -> None:
    """Kill the command's whole process group; the shell alone is not enough."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        logger.debug("verify command's process group was already gone")


def parse_verify_output(stdout: str, count_path: str | None = None) -> list | int | None:
    """A verify command's stdout as a reading, or `None`. **Never a zero.**

    Stripped and parsed as JSON; with `--verify-count`, the value at that path
    (`entries_at_path`, the verify tool's own walker). A non-negative integer
    that is not a bool is a count, a list is the entries, and everything else is
    `None`: empty output, a float (even `3.0`), a negative number, a string,
    an object with no path. **Empty stdout is not `[]`** -- `sqlite3 -json`
    prints nothing for zero rows, and reading that as an empty store would turn
    a broken command into a clean one.
    """
    from .mcp import entries_at_path

    text = stdout.strip()
    if not text:
        return None
    try:
        value: Any = json.loads(text)
    except ValueError:
        return None
    if count_path:
        try:
            value = entries_at_path(text, count_path)
        except TargetError:
            return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, list):
        return value
    return None


def verify_command_digest(command: str | None) -> str | None:
    """The command's identity for the export: first word and a sha256 prefix.

    **Never the text.** The first word is the program, with leading `NAME=value`
    assignments skipped -- `PGPASSWORD=... psql` would otherwise export the
    password as the "first word" -- and reduced to its name, so a path through a
    home directory does not travel either. Two scans with the same digest ran
    the same command; the digest says nothing else about it.
    """
    if command is None:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    program = next(
        (word for word in words if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word)),
        "",
    )
    name = program.rstrip("/").rsplit("/", 1)[-1]
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:12]
    return f"{name} sha256:{digest}" if name else f"sha256:{digest}"


def _reading(value: list | int | None) -> str:
    if isinstance(value, list):
        return f"{len(value)} entries"
    return str(value)


def effect_count(entries: list | int | None) -> int | None:
    """How many effects a verify read reports: list length, or the number."""
    if entries is None or isinstance(entries, bool):
        return None
    if isinstance(entries, list):
        return len(entries)
    if isinstance(entries, int):
        return entries
    return None


__all__ = ["AGENT_KINDS", "AGENT_KIND_LLM", "AGENT_KIND_SCRIPTED",
           "AgentTarget", "COVERAGE_RULE", "DEFAULT_KEY_PATH", "EFFECT_ATTRIBUTION",
           "MCP_CONFIG_ENV", "MCP_CONFIG_FLAG", "RECORD_ENV", "SCHEDULE_ENV",
           "TASK_ENV", "VerifyRead", "effect_count", "parse_verify_output",
           "run_verify_command", "verify_command_digest"]
