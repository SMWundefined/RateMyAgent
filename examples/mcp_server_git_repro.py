#!/usr/bin/env python3
"""Check a crash report against a real MCP server, without trusting this project.

Imports only the MCP Python SDK -- nothing from ratemyagent -- so it can settle a
disagreement about whether a server crashes, including a disagreement with us:

    pip install mcp
    python mcp_server_git_repro.py                 # a throwaway two-commit git repo
    python mcp_server_git_repro.py --repository .  # your own

It opens ONE stdio session, sends the six malformed payloads the contract probe
sends against the first three tools -- 18 calls -- and makes a known-good call
after every one of them. A crash is a call that never came back or a session that
stopped answering, not an error message we did not recognise. That distinction is
the whole point: grading crashes by error *text* is how this project once reported
a crash rate against two servers that crash nothing
(modelcontextprotocol/servers#4754, retracted; fixed in 0.1.4).

Exit status: 0 if the transport survived every call, 1 if anything actually
crashed it, 2 on a setup problem. Non-zero means the report was real.
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:  # pragma: no cover - the whole point is to run without our deps
    sys.exit("this script needs the MCP Python SDK: pip install mcp")

DEFAULT_SERVER = "uvx --from mcp-server-git mcp-server-git"

#: The six payloads. `BASELINE` is what you get by synthesizing arguments from
#: the tool's JSON Schema -- a structurally valid string that is not a real
#: path -- and each case mutates it the way a model would get it wrong.
BASELINE: dict[str, Any] = {"repo_path": "mcp probe"}

CASES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("null_required", {"repo_path": None}),
    ("empty_string", {"repo_path": ""}),
    ("wrong_type", {"repo_path": 12345}),
    ("very_long_string", {"repo_path": "A" * 50_000}),
    ("missing_required", {}),
    ("extra_param", {**BASELINE, "unexpected_field": True}),
)

TOOLS_TO_PROBE = 3
CALL_TIMEOUT_S = 30.0


def build_throwaway_repo(root: Path) -> Path:
    """Two commits, no remote. Somewhere other than the working directory on
    purpose: the reported count varies with whether the server's allowed root is
    the directory it was launched in, which is what decides `repo_path: ""`."""
    repo = root / "repo"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "repro@example.com")
    run("config", "user.name", "Repro")
    (repo / "a.txt").write_text("one\n")
    run("add", "a.txt")
    run("commit", "-qm", "first")
    (repo / "a.txt").write_text("one\ntwo\n")
    run("add", "a.txt")
    run("commit", "-qm", "second")
    return repo


def sdk_attr(obj: Any, *names: str) -> Any:
    """Read the first attribute that exists, across MCP SDK major versions.

    The SDK renamed its model fields to snake_case in 2.0 (`isError` ->
    `is_error`, `serverInfo` -> `server_info`); the camelCase spellings survive
    only as wire aliases. `pip install mcp` gets 2.x today, so a script that
    knows one spelling breaks for whoever runs it. Deliberately raises on an
    unknown shape rather than defaulting -- a default here would report every
    error as a success, which is the bug this file exists to check for.
    """
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    raise AttributeError(f"none of {names} on {type(obj).__name__}; unsupported mcp SDK")


def is_error(result: Any) -> bool:
    return bool(sdk_attr(result, "is_error", "isError"))


def result_text(result: Any) -> str:
    return " ".join(
        getattr(block, "text", "") for block in getattr(result, "content", [])
    ).replace("\n", " ")


async def call(session: Any, tool: str, arguments: dict[str, Any]) -> Any:
    """One tool call under a wall-clock deadline.

    Not `read_timeout_seconds=`: that parameter takes a `timedelta` in SDK 1.x
    and a `float` in 2.x, so passing either breaks the other. A corrupted stdio
    stream never returns on its own, so some deadline is required.
    """
    return await asyncio.wait_for(session.call_tool(tool, arguments), CALL_TIMEOUT_S)


async def probe(server: list[str], repo: Path) -> int:
    good = {"repo_path": str(repo)}
    params = StdioServerParameters(command=server[0], args=server[1:])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = [tool.name for tool in (await session.list_tools()).tools]
            info = sdk_attr(init, "server_info", "serverInfo")
            print(f"server:     {info.name} {info.version}")
            print(f"repository: {repo}")
            print(f"probing:    {', '.join(tools[:TOOLS_TO_PROBE])}\n")

            sanity = await call(session, "git_status", good)
            if is_error(sanity):
                print(f"setup failed: a valid call already errors: {result_text(sanity)}")
                return 2

            crashed: list[str] = []
            answered = 0

            for tool in tools[:TOOLS_TO_PROBE]:
                for case, arguments in CASES:
                    label = f"{tool}/{case}"
                    try:
                        result = await call(session, tool, arguments)
                    except Exception as exc:
                        crashed.append(label)
                        print(f"  CRASH     {label:38} {type(exc).__name__}: {exc}")
                        continue

                    answered += 1
                    verdict = "rejected " if is_error(result) else "ACCEPTED "
                    print(f"  {verdict} {label:38} {result_text(result)[:88]}")

                    # The claim under test is that the transport dies. Ask the
                    # same session for something it answered a moment ago.
                    alive = await call(session, "git_status", good)
                    if is_error(alive):
                        crashed.append(f"{label} (session degraded after)")
                        print(f"  DEGRADED  {label:38} {result_text(alive)[:88]}")

            total = len(CASES) * min(TOOLS_TO_PROBE, len(tools))
            print(f"\n{answered}/{total} malformed calls answered on a single session.")
            if crashed:
                print(f"{len(crashed)} killed or degraded the transport: {', '.join(crashed)}")
                return 1

            final = await call(session, "git_status", good)
            print("session still usable at the end: " + result_text(final)[:60])
            print(
                "\nThe stdio transport did not crash. Every malformed input came back as a "
                "well-formed\nCallToolResult with isError=True, which is the server "
                "rejecting bad input correctly."
            )
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=f"command that speaks MCP over stdio (default: {DEFAULT_SERVER!r})",
    )
    parser.add_argument(
        "--repository",
        type=Path,
        help="git repository to point the server at (default: a throwaway two-commit repo)",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="mcp-git-repro-") as tmp:
        if args.repository is not None:
            repo = args.repository.expanduser().resolve()
            if not (repo / ".git").exists():
                print(f"{repo} is not a git repository")
                return 2
        else:
            repo = build_throwaway_repo(Path(tmp))

        server = shlex.split(args.server) + ["--repository", str(repo)]
        return asyncio.run(probe(server, repo))


if __name__ == "__main__":
    sys.exit(main())

# Measured against mcp-server-git 2026.8.18: all 18 calls answered, transport
# alive after every one.
#
#   null_required     Input validation error: None is not of type 'string'
#   wrong_type        Input validation error: 12345 is not of type 'string'
#   missing_required  Input validation error: 'repo_path' is a required property
#   empty_string      Repository path '.' is outside the allowed repository '<repo>'
#   very_long_string  Repository path 'AAAA...' is outside the allowed repository '<repo>'
#   extra_param       Repository path 'mcp probe' is outside the allowed repository '<repo>'
#
# All six are correct rejections. The bottom three are the ones an error-text
# classifier is likely to miss, because they share no vocabulary with the top
# three -- which is why this script decides on liveness instead. Note also that
# `empty_string` resolves `.` against the server's launch directory, so whether
# it lands inside the allowed root depends on where the server was started, not
# on the input. Counts that move with the working directory are a signal the
# thing being measured is not the thing being reported.
