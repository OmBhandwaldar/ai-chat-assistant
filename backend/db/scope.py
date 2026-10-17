"""Resolve an area or team argument to a canonical id.

Structured tools filter on `areas.area_id` / `teams.team_id`. Callers (and the
agent) also pass the human name ("Northeast Block 8") or a light variant of
either form ("NE-8", "ne 08", "Northeast Block 08"). This module is the one
place that turns those into the id stored on wells, or reports that nothing
matched — an unresolved scope must not look like a real empty result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import duckdb

_TOKEN = re.compile(r"[^a-z0-9]+")
_FILLER = frozenset({"block", "area", "field"})
# Compass words used in area/team names, and the codes stored as ids (NE-08).
_COMPASS = {
    "northeast": "ne",
    "northwest": "nw",
    "southeast": "se",
    "southwest": "sw",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
}


@dataclass(frozen=True)
class UnresolvedScope:
    kind: str  # "area" | "team"
    given: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "given": self.given, "message": self.message}


@dataclass(frozen=True)
class ResolvedScope:
    """Canonical ids to bind into SQL. `unresolved` is empty when every given value matched."""

    area_id: str | None = None
    team_id: str | None = None
    unresolved: tuple[UnresolvedScope, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.unresolved


def unresolved_fields(scope: ResolvedScope) -> dict:
    """Fields to merge into a tool payload so an unknown name is not a silent empty list."""
    return {
        "unresolved_scope": [item.as_dict() for item in scope.unresolved],
        "message": " ".join(item.message for item in scope.unresolved),
    }


def resolve_scope(
    conn: duckdb.DuckDBPyConnection,
    area_id: str | None = None,
    team_id: str | None = None,
) -> ResolvedScope:
    """Look up optional area_id and team_id arguments. None means 'no filter'."""
    unresolved: list[UnresolvedScope] = []
    resolved_area = _resolve_one(conn, "area", area_id, unresolved)
    resolved_team = _resolve_one(conn, "team", team_id, unresolved)
    return ResolvedScope(resolved_area, resolved_team, tuple(unresolved))


def _resolve_one(
    conn: duckdb.DuckDBPyConnection,
    kind: str,
    given: str | None,
    unresolved: list[UnresolvedScope],
) -> str | None:
    if given is None:
        return None
    if kind == "area":
        rows = conn.execute("SELECT area_id, name FROM areas").fetchall()
    else:
        rows = conn.execute("SELECT team_id, name FROM teams").fetchall()
    matched, ambiguous = _match(rows, given)
    if matched is None:
        unresolved.append(UnresolvedScope(kind, given, _message(kind, given, ambiguous)))
    return matched


def _message(kind: str, given: str, ambiguous: tuple[str, ...]) -> str:
    label = "area" if kind == "area" else "team"
    if ambiguous:
        return (
            f"'{given}' matches more than one {label} ({', '.join(ambiguous)}). "
            f"Pass one {label} id."
        )
    example = "NE-08" if kind == "area" else "TEAM-NE"
    return (
        f"No {label} matches '{given}'. This is not an empty result for that {label}: "
        f"the name or id did not resolve. Pass a {label} id (for example {example}) "
        f"or the {label}'s full name."
    )


def _match(rows: list[tuple], given: str) -> tuple[str | None, tuple[str, ...]]:
    """Exact id, then case-insensitive name, then shared variants of id or name.

    Returns (canonical_id, ambiguous_ids). A miss is (None, ()).
    """
    raw = given.strip()
    if not raw:
        return None, ()

    exact = [rid for rid, _name in rows if rid == raw]
    if len(exact) == 1:
        return exact[0], ()

    folded = raw.casefold()
    by_name = [rid for rid, name in rows if (name or "").casefold() == folded]
    if len(by_name) == 1:
        return by_name[0], ()
    if len(by_name) > 1:
        return None, tuple(by_name)

    by_id = [rid for rid, _name in rows if rid.casefold() == folded]
    if len(by_id) == 1:
        return by_id[0], ()
    if len(by_id) > 1:
        return None, tuple(by_id)

    want = _keys(raw)
    if not want:
        return None, ()
    hits = [rid for rid, name in rows if want & (_keys(rid) | _keys(name or ""))]
    if len(hits) == 1:
        return hits[0], ()
    if len(hits) > 1:
        return None, tuple(hits)
    return None, ()


def _keys(value: str) -> set[str]:
    """Comparable forms: case, punctuation, leading zeros, and compass abbreviations.

    'Northeast Block 8', 'NE-08', 'NE-8' and 'NE Block 08' all share 'ne 8'.
    """
    tokens: list[str] = []
    for tok in _TOKEN.split(value.casefold()):
        if not tok:
            continue
        tokens.append(str(int(tok)) if tok.isdigit() else tok)
    if not tokens:
        return set()
    base = " ".join(tokens)
    keys = {base, "".join(tokens)}
    abbr = _COMPASS.get(tokens[0])
    if abbr is None:
        return keys
    tail = tokens[1:]
    keys.add(" ".join([abbr, *tail]))
    keys.add(abbr + "".join(tail))
    compact = [tok for tok in tail if tok not in _FILLER]
    if compact:
        keys.add(" ".join([abbr, *compact]))
        keys.add(abbr + "".join(compact))
    return keys
