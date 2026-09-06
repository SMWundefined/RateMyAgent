#!/usr/bin/env python3
"""Standalone probe: does malformed input crash mcp-server-git's stdio transport?

This file deliberately does **not** import ratemyagent. It needs only the MCP
Python SDK and `uvx`, so a maintainer of modelcontextprotocol/servers can run it
without installing our scanner and without taking our word for anything:

    pip install mcp
    python mcp_server_git_repro.py

It builds a throwaway two-commit git repository, opens ONE stdio session to
`mcp-server-git`, and sends the six malformed payloads RateMyAgent's contract
probe sends, against the first three tools -- 18 calls in total. After every
malformed call it makes a known-good call on the same session. If the transport
had died, that liveness check would fail.

Background: RateMyAgent reported a 33-50% "contract crash rate" against this
server, filed upstream as modelcontextprotocol/servers#4754. This script exists
to reduce that to a minimal repro. It does not reproduce it. The server answers
all 18 calls with a well-formed `isError=True` result and the session stays up
throughout -- see the note at the bottom of this file.

Exit status: 0 if the transport survived every call, 1 if anything actually
crashed it (that is the outcome the upstream issue claims), 2 on a setup
problem.
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import subprocess
import sys
import tempfile
from datetime import timedelta
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
CALL_TIMEOUT = timedelta(seconds=30)


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


def result_text(result: Any) -> str:
    return " ".join(
        getattr(block, "text", "") for block in getattr(result, "content", [])
    ).replace("\n", " ")


async def probe(server: list[str], repo: Path) -> int:
    good = {"repo_path": str(repo)}
    params = StdioServerParameters(command=server[0], args=server[1:])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = [tool.name for tool in (await session.list_tools()).tools]
            print(f"server:     {init.serverInfo.name} {init.serverInfo.version}")
            print(f"repository: {repo}")
            print(f"probing:    {', '.join(tools[:TOOLS_TO_PROBE])}\n")

            sanity = await session.call_tool("git_status", good, CALL_TIMEOUT)
            if sanity.isError:
                print(f"setup failed: a valid call already errors: {result_text(sanity)}")
                return 2

            crashed: list[str] = []
            answered = 0

            for tool in tools[:TOOLS_TO_PROBE]:
                for case, arguments in CASES:
                    label = f"{tool}/{case}"
                    try:
                        result = await session.call_tool(tool, arguments, CALL_TIMEOUT)
                    except Exception as exc:
                        crashed.append(label)
                        print(f"  CRASH     {label:38} {type(exc).__name__}: {exc}")
                        continue

                    answered += 1
                    verdict = "rejected " if result.isError else "ACCEPTED "
                    print(f"  {verdict} {label:38} {result_text(result)[:88]}")

                    # The claim under test is that the transport dies. Ask the
                    # same session for something it answered a moment ago.
                    alive = await session.call_tool("git_status", good, CALL_TIMEOUT)
                    if alive.isError:
                        crashed.append(f"{label} (session degraded after)")
                        print(f"  DEGRADED  {label:38} {result_text(alive)[:88]}")

            total = len(CASES) * min(TOOLS_TO_PROBE, len(tools))
            print(f"\n{answered}/{total} malformed calls answered on a single session.")
            if crashed:
                print(f"{len(crashed)} killed or degraded the transport: {', '.join(crashed)}")
                return 1

            final = await session.call_tool("git_status", good, CALL_TIMEOUT)
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

# What this actually shows, measured against mcp-server-git 2026.8.18 and MCP
# SDK 1.29.1 -- all 18 calls answered, transport alive after every one:
#
#   null_required     Input validation error: None is not of type 'string'
#   wrong_type        Input validation error: 12345 is not of type 'string'
#   missing_required  Input validation error: 'repo_path' is a required property
#   empty_string      Repository path '.' is outside the allowed repository '<repo>'
#   very_long_string  Repository path 'AAAA...' is outside the allowed repository '<repo>'
#   extra_param       Repository path 'mcp probe' is outside the allowed repository '<repo>'
#
# All six are correct rejections. The three RateMyAgent grades as crashes are
# exactly the three whose message says "Repository path ... is outside the
# allowed repository" -- text that contains none of the substrings
# `_classify_tool_error()` looks for ("invalid", "validation", "schema", ...),
# so it falls through to ErrorKind.UNKNOWN, and contract.py's CRASH_KINDS counts
# UNKNOWN as a dead transport.
#
# So the "33.3% / 50% contract crash rate" is our classifier, not their server,
# and it explains the repository-dependence too: point the server at the repo it
# was started in and `repo_path: ""` resolves to "." *inside* the allowed root,
# which turns one crash per tool into one acceptance per tool -- 6/18 instead of
# 9/18. Upstream issue #4754 needs correcting.
