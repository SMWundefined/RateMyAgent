"""Shared helpers for the gates that read prose off disk.

Every gate in this repository that asserts a phrase is or is not present in a
document used to build its pattern with `re.escape(phrase)` -- the whole phrase,
spaces and all, as one literal. That cannot match an occurrence the author's
line wrapper split, and it is not a hypothetical: `"eight servers"` sat in
PROGRESS section 8 from week 6 until 2026-09-09 wrapped as

    ...which three of the eight
    servers rejected outright...

and `test_no_surface_says_nine_servers`, whose entire job is catching that
claim, read green for twenty releases. Section 8b of PROGRESS has the writeup;
it is the sixth instance of a check that was itself unchecked.

The fix lives here rather than in each gate so there is one place to get it
wrong. Route every prose gate through `contains_phrase` or `find_phrases`.

**Not for exact-format assertions.** `phrase.split()` collapses runs of
whitespace, so `"Phase 1  baseline"` becomes `Phase\\s+1\\s+baseline` and would
accept the single-spaced misalignment the gate exists to reject. Column and
table-alignment checks -- `"Phase 1  baseline"`, `"| measurement | actual |
target | status |"` -- must keep using `in`. Widening those would be the same
defect pointed the other way: a check that stops checking.
"""

from __future__ import annotations

import re

#: What may separate two words of a phrase in a wrapped document.
#:
#: Not plain `\s+`. A markdown wrapper puts a leader at the start of a
#: continuation line, and `>` is not whitespace -- so a phrase wrapped inside a
#: blockquote is invisible to `\s+`, and both notes files are full of
#: blockquotes. `#`, `*` and `-` are deliberately absent: they only ever begin a
#: block, never continue one, and including them would let a phrase match across
#: a heading boundary.
#:
#: The cost is that a literal `foo > bar` reads as the phrase "foo bar". That is
#: the safe direction for both gate polarities: a positive gate that over-matches
#: passes when it should pass anyway, and a negative gate that over-matches
#: reports a hit for a human to dismiss. Silence is the failure mode worth
#: engineering against here.
SEPARATOR = r"[\s>]+"


def phrase_pattern(phrase: str, flags: int = 0) -> re.Pattern[str]:
    """Compile `phrase` so it matches across a line break.

    Each word is escaped individually and the words are joined with
    `SEPARATOR`, rather than escaping the phrase as one literal.
    """
    words = phrase.split()
    if not words:
        raise ValueError("phrase_pattern needs at least one word")
    return re.compile(SEPARATOR.join(re.escape(word) for word in words), flags)


def find_phrases(text: str, phrase: str, flags: int = 0) -> list[re.Match[str]]:
    """Every occurrence of `phrase` in `text`, wrapped or not."""
    return list(phrase_pattern(phrase, flags).finditer(text))


def contains_phrase(text: str, phrase: str, flags: int = 0) -> bool:
    """True if `phrase` appears in `text`, wrapped or not."""
    return phrase_pattern(phrase, flags).search(text) is not None


#: Spans in which an occurrence of a phrase is a *mention* of it, not a claim.
#:
#: Anchored the way the `SUPERSEDED-PENDING-RESCAN` gate was: a live marker and
#: a description of one are different strings. That gate matched the HTML
#: comment form, so `` `SUPERSEDED-PENDING-RESCAN` `` in prose could explain the
#: mechanism without tripping it. A banned *phrase* has no comment form -- prose
#: is where it lives -- so the equivalent anchor is the code span. Backtick it
#: and you are naming it; write it bare and you are claiming it.
#:
#: Two span kinds plus one adjacency rule:
#:
#: 1. fenced blocks, ``` ... ```
#: 2. backtick spans, which may cross a line break but not a blank line, so a
#:    mention the author wrapped is still a mention and a stray backtick cannot
#:    silence the rest of the document
#: 3. the phrase sitting immediately inside a matched pair of double quotes
#:
#: **Indented blocks are deliberately not a span kind.** Treating four-space
#: indentation as code would also swallow every list continuation in these
#: files, and a gate that goes quiet inside list items is the coverage hole this
#: entry is about. The one indented demonstration in section 8b is backticked
#: instead, which is the anchor rather than a dodge.
#:
#: **Double quotes are an adjacency test, not a span.** Pairing them across a
#: document does not work: in `... servers", never "nine servers".` the stray
#: closing quote pairs with the opening quote of the mention, consumes both, and
#: leaves the phrase bare. Asking whether the phrase itself is wrapped needs no
#: pairing and cannot be thrown off by one unbalanced quote elsewhere.
#:
#: `'` is not a delimiter at all. Prose is full of "don't" and "server's", and
#: admitting apostrophes would let one contraction silence everything after it.
_FENCED = re.compile(r"^[ \t]*```.*?^[ \t]*```", re.M | re.S)
_BACKTICK = re.compile(r"`(?:[^`\n]|\n(?![ \t]*\n))*?`")


def mention_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every span in which a phrase is quoted, not claimed."""
    spans = [m.span() for m in _FENCED.finditer(text)]
    spans += [m.span() for m in _BACKTICK.finditer(text)]
    return spans


def is_mention(text: str, start: int, end: int, spans=None) -> bool:
    """True if [start, end) is a mention of the phrase rather than a claim."""
    if spans is None:
        spans = mention_spans(text)
    if any(s <= start and end <= e for s, e in spans):
        return True
    # Immediately wrapped in double quotes: `"nine servers"`.
    return text[start - 1 : start] == '"' and text[end : end + 1] == '"'
