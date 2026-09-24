"""What the event twin advertises, per role -- the surface an agent can see.

**The confound this pins was in the fixture, and nothing asserted against it.**
The twin exposes one write tool (`event`) and two read tools (`effects`,
`effects_array`). The reads exist for the oracle, which reads state through its
own `--role oracle` copy. Until Gate S the agent's copy advertised them too, and
no test said otherwise, because no fixture agent cared: each is scripted and
calls what it was written to call.

Gate S compared three agent stacks on one fault and needed every arm to see the
same tool surface. Two SDK runners filtered to `event`; Claude Code's
`--tools ""` empties only its built-ins, so its model saw both reads and was
denied them at use time -- in 4 of 5 chaos runs, after a lost reply
(`assets/moat/GATE-S-PHASE2.md` 9.1). The arms were not seeing the same surface,
and the experiment's validity rested on that.

Pinned here:

1. under `--role agent`, `tools/list` is exactly `["event"]`, in both modes;
2. under `--role oracle`, it is all three -- the scan's setup check finds its
   verify tool there, and a fix that hid it would refuse every agent scan;
3. the change is advertising only: a read called BY NAME from the agent's copy
   is still served (`chatty_agent` relies on it; see NextSteps for why that is
   a realism gap and not a feature).

And the deliberate failing case: the same check, run against a copy of the twin
with the fix reverted, must fail.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TWIN = ROOT / "tests" / "fixtures" / "event_twin_mcp_server.py"

WRITE = ["event"]
ALL = ["event", "effects", "effects_array"]


def _talk(twin: Path, role: str, mode: str, messages: list[dict]) -> list[dict]:
    """One twin process, one conversation, then it exits. In-memory: no --state."""
    proc = subprocess.run(
        [sys.executable, str(twin), "--mode", mode, "--role", role],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True, text=True, timeout=30, check=True,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def advertised(twin: Path, role: str, mode: str = "append") -> list[str]:
    reply = _talk(twin, role, mode, [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    return [tool["name"] for tool in reply[0]["result"]["tools"]]


def assert_agent_surface(twin: Path) -> None:
    """The property itself, as one callable, so the failing case runs THIS."""
    for mode in ("append", "put"):
        got = advertised(twin, "agent", mode)
        assert got == WRITE, (
            f"--role agent --mode {mode} advertises {got}; the agent's copy must "
            f"advertise exactly {WRITE}, or restriction by omission does not hold "
            f"for a client that shows the model every tool the server lists"
        )


@pytest.mark.parametrize("mode", ["append", "put"])
def test_agent_copy_advertises_only_the_write_tool(mode):
    assert advertised(TWIN, "agent", mode) == WRITE


@pytest.mark.parametrize("mode", ["append", "put"])
def test_oracle_copy_advertises_every_tool(mode):
    assert advertised(TWIN, "oracle", mode) == ALL


def test_the_property_holds_for_the_shipped_fixture():
    assert_agent_surface(TWIN)


def test_a_read_called_by_name_is_still_served_to_the_agent():
    """Advertising only. The call path is unchanged."""
    reply = _talk(TWIN, "agent", "append", [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "event", "arguments": {"id": "a", "payload": "p"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "effects", "arguments": {}}},
    ])
    body = json.loads(reply[1]["result"]["content"][0]["text"])
    assert body == {"entries": ["a"]}
    assert not reply[1]["result"].get("isError")


class TestTheSurfaceCheckCanFail:
    """The deliberate failing case: revert the fix and the check must fail."""

    def test_a_twin_that_advertises_reads_to_the_agent_is_caught(self, tmp_path):
        source = TWIN.read_text()
        guard = (
            "    if ROLE == ROLE_AGENT:\n"
            '        return [t for t in tools if t["name"] == TOOL]\n'
        )
        assert source.count(guard) == 1, "the fix's guard moved; update this mutant"
        mutant = tmp_path / "event_twin_mcp_server.py"
        mutant.write_text(source.replace(guard, ""))

        # The mutant is live -- it really advertises all three to the agent --
        # so a pass below would not be a vacuous one.
        assert advertised(mutant, "agent") == ALL

        with pytest.raises(AssertionError, match="must advertise exactly"):
            assert_agent_surface(mutant)
