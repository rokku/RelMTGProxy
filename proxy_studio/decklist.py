"""Deck-list parsing.

Supports the plain-text formats real exporters (Moxfield, Archidekt, MTGO)
produce. See spec §3 for grammar. Every unparseable line is reported with its
line number — never silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DeckEntry:
    quantity: int
    name: str
    set_code: str | None = None      # lowercase, no brackets
    collector_number: str | None = None  # kept as string — some CNs are "★" or "12b"


class DecklistError(ValueError):
    """One or more lines couldn't be parsed."""

    def __init__(self, failures: list[tuple[int, str]]):
        self.failures = failures
        msg = "Could not parse deck-list lines:\n" + "\n".join(
            f"  line {n}: {text!r}" for n, text in failures
        )
        super().__init__(msg)


# --- Grammar ----------------------------------------------------------------
# Examples we must handle:
#   1 Mazirek, Kraul Death Priest
#   1x Sol Ring
#   4 Llanowar Elves (M19) 314
#   1 Fabled Passage [ELD] 244
#   2 Swamp <foil>
#
# Structure:
#   <qty>[x] <name> [ (SET) | [SET] ] [ collector_number ] [ <decoration> ]
#
# Decoration tags in <angle brackets> or *…* (Moxfield foil marker) are stripped.

_LINE_RE = re.compile(
    r"""^
        \s*
        (?P<qty>\d+)\s*x?\s+                              # quantity, optional x
        (?P<name>[^\(\[\<*][^\(\[\<*]*?)                  # name — anything not a bracket
        (?:\s+
            (?:\((?P<set_paren>[A-Za-z0-9_]{2,6})\)
             |\[(?P<set_brack>[A-Za-z0-9_]{2,6})\])
            (?:\s+(?P<cn>[A-Za-z0-9★\-]+))?
        )?
        \s*$
    """,
    re.VERBOSE,
)

# Fallback for a line that has been fully de-decorated but still has trailing
# punctuation / annotations we don't recognise.
_QTY_NAME_RE = re.compile(r"^\s*(?P<qty>\d+)\s*x?\s+(?P<name>.+?)\s*$")

_DECORATION_RES = [
    re.compile(r"<[^>]*>"),      # <foil>
    re.compile(r"\*[A-Za-z]+\*"),  # *F* (Moxfield foil)
    re.compile(r"#!.*$"),        # trailing "#!Commander" etc. (Archidekt)
]

_SECTION_HEADERS = {
    "sideboard", "sideboard:", "commander", "commander:", "companion",
    "companion:", "mainboard", "mainboard:", "deck", "deck:", "maybeboard",
    "maybeboard:", "tokens", "tokens:",
}


def _strip_decorations(line: str) -> str:
    for pat in _DECORATION_RES:
        line = pat.sub("", line)
    return line.strip()


def parse_line(raw: str) -> DeckEntry | None:
    """Return a DeckEntry, or None if the line should be skipped (blank /
    comment / section header). Raises ValueError if the line is
    non-empty-non-skippable but unparseable.
    """
    stripped = raw.strip()
    if not stripped:
        return None
    if stripped.startswith(("//", "#")):
        return None
    if stripped.lower() in _SECTION_HEADERS:
        return None

    cleaned = _strip_decorations(stripped)
    if not cleaned:
        return None

    m = _LINE_RE.match(cleaned)
    if m:
        set_code = m.group("set_paren") or m.group("set_brack")
        return DeckEntry(
            quantity=int(m.group("qty")),
            name=m.group("name").strip(),
            set_code=set_code.lower() if set_code else None,
            collector_number=m.group("cn"),
        )

    # Fallback: `<qty> <name>` with unrecognised trailing junk (still useful).
    m = _QTY_NAME_RE.match(cleaned)
    if m and not any(ch in cleaned for ch in "([<"):
        return DeckEntry(quantity=int(m.group("qty")), name=m.group("name").strip())

    raise ValueError(cleaned)


def parse_text(text: str) -> list[DeckEntry]:
    """Parse a full decklist. Merges duplicates only if set/CN also match."""
    entries: list[DeckEntry] = []
    failures: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        try:
            entry = parse_line(raw)
        except ValueError:
            failures.append((lineno, raw.strip()))
            continue
        if entry is None:
            continue
        entries.append(entry)

    if failures:
        raise DecklistError(failures)

    return _merge_duplicates(entries)


def parse_file(path: str | Path) -> list[DeckEntry]:
    text = Path(path).read_text(encoding="utf-8")
    return parse_text(text)


def _merge_duplicates(entries: list[DeckEntry]) -> list[DeckEntry]:
    """Combine identical (name, set, cn) rows by summing quantities.

    Different printings of the same card stay separate — that's the whole
    point of pinning a set/CN.
    """
    seen: dict[tuple[str, str | None, str | None], int] = {}
    order: list[tuple[str, str | None, str | None]] = []
    for e in entries:
        key = (e.name, e.set_code, e.collector_number)
        if key not in seen:
            seen[key] = 0
            order.append(key)
        seen[key] += e.quantity
    return [
        DeckEntry(quantity=seen[k], name=k[0], set_code=k[1], collector_number=k[2])
        for k in order
    ]
