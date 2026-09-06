"""Does calling this tool change anything?

Probing invokes a tool for real, once per request, and again under fault
injection. Against a read-only tool that is a measurement. Against a write tool
it is a hundred writes.

This exists because auto-selection picked `create_entities` on
`@modelcontextprotocol/server-memory` and called it 100 times under fault
injection with nothing but a warning -- while the server's own discovery
response carried `readOnlyHint=False` on that tool. The signal was there and
nothing read it.

Three values, not two. `UNKNOWN` is a real answer: a tool whose annotations are
absent and whose name says nothing is not thereby safe, and collapsing it into
either bucket is the mistake this project keeps making. Auto-selection requires
a positive `READ_ONLY`; both `MUTATING` and `UNKNOWN` refuse.
"""

from __future__ import annotations

import logging
import re
from enum import Enum

from ..models import ToolInfo

logger = logging.getLogger(__name__)


class Mutability(str, Enum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    UNKNOWN = "unknown"


#: Word stems that mean "this call changes state". Matched against any token in
#: the tool name, and checked before the read-only set, so `get_or_create` reads
#: as mutating rather than read-only.
MUTATING_WORDS = frozenset({
    "create", "delete", "write", "update", "add", "remove", "set", "insert",
    "drop", "move", "rename", "execute", "run", "send", "push", "put", "patch",
    "post", "clear", "reset", "upload", "commit", "apply", "install",
    "uninstall", "kill", "stop", "start", "restart", "modify", "edit", "append",
    "truncate", "purge", "revoke", "grant", "publish", "deploy", "merge",
})

#: Word stems that mean "this call only observes".
READ_ONLY_WORDS = frozenset({
    "get", "list", "read", "search", "find", "query", "describe", "show",
    "fetch", "status", "diff", "log", "head", "count", "exists", "check",
    "view", "inspect", "summarize", "summarise", "resolve", "lookup", "stat",
})

_SPLIT = re.compile(r"[^a-zA-Z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def tokenize(name: str) -> list[str]:
    """Split a tool name into lowercase words.

    Handles `create_entities`, `create-entities` and `createEntities` alike, and
    looks at every token rather than the first: `git_status` leads with a noun.
    """
    return [part.lower() for part in _SPLIT.split(name or "") if part]


def classify_by_name(name: str) -> Mutability:
    """Fallback when the server declares nothing. Mutating wins ties."""
    tokens = set(tokenize(name))
    if tokens & MUTATING_WORDS:
        return Mutability.MUTATING
    if tokens & READ_ONLY_WORDS:
        return Mutability.READ_ONLY
    return Mutability.UNKNOWN


def classify(tool: ToolInfo) -> Mutability:
    """Classify one tool, preferring what the server declared.

    `readOnlyHint` is the server's own statement about its own tool, so it wins
    over a guess made from the name. When the two disagree the guess is still
    worth surfacing -- a tool called `delete_everything` that declares itself
    read-only is either mislabelled or lying, and either way somebody should
    look -- so the disagreement is logged rather than silently resolved.
    """
    declared = tool.read_only
    guessed = classify_by_name(tool.name)

    if declared is None:
        return guessed

    annotated = Mutability.READ_ONLY if declared else Mutability.MUTATING
    if guessed is not Mutability.UNKNOWN and guessed is not annotated:
        logger.warning(
            "tool %r declares readOnlyHint=%s but its name suggests %s; "
            "trusting the declaration",
            tool.name, declared, guessed.value,
        )
    return annotated


def read_only_tools(tools: list[ToolInfo]) -> list[ToolInfo]:
    return [tool for tool in tools if classify(tool) is Mutability.READ_ONLY]


def describe_refusal(tools: list[ToolInfo], chosen: ToolInfo) -> str:
    """The message a user gets instead of a hundred writes.

    Says what was refused, why, and the exact command that proceeds -- a refusal
    that does not tell you how to continue just moves the problem.

    Only reached when *no* tool on the server classifies as read-only, since
    auto-selection returns the first one that does. So this never has a safe
    alternative to suggest, and pretending otherwise would be a branch nothing
    can execute.
    """
    verdict = classify(chosen)
    if verdict is Mutability.MUTATING:
        because = (
            "it declares readOnlyHint=false"
            if chosen.read_only is False
            else "its name suggests it modifies state"
        )
    else:
        because = (
            "nothing declares whether it modifies state, and an unclassified tool "
            "is not the same as a safe one"
        )

    listed = ", ".join(tool.name for tool in tools[:8])
    if len(tools) > 8:
        listed += f", and {len(tools) - 8} more"

    return "\n".join([
        f"refusing to auto-select {chosen.name!r}: {because}.",
        "",
        "Probing calls the chosen tool once per request, and again under fault",
        "injection. No tool on this server is known to be read-only, so there is",
        "nothing safe to fall back to.",
        "",
        f"  tools here: {listed}",
        "",
        "Choose one yourself, and point the scan at something disposable:",
        "  ratemyagent scan ... --tool <name> --allow-mutating",
    ])
