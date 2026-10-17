"""Parses SOP/manual markdown into protocol-id -> text lookup.

Two source shapes in this dataset:
  1. SOP protocol tables: `| **SR-01** | requirement text... |` (optionally
     with a trailing evidence column, as in regulatory-compliance-sop.md).
  2. Well-type manual sections: `## 9. Engineering limits` and
     `### 9.3 Production temperature rule`, body = everything until the
     next heading of the same or higher level.

Both are indexed by a stable key (the protocol ID, or the section number)
so the SOP breach engine (Phase 2.3) and get_sop/get_manual_section tools
(Phase 3.4) can look up `rules["SR-01"]["text"]` without re-parsing.
See PLAN.md Phase 2.4.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict


class SopRule(TypedDict):
    text: str
    source_path: str


_PROTOCOL_ROW_RE = re.compile(
    r"^\|\s*\*\*(?P<id>[A-Z]{2,3}-\d{2,3})\*\*\s*\|(?P<rest>.*)\|\s*$",
    re.MULTILINE,
)

_SECTION_HEADING_RE = re.compile(
    r"^(?P<hashes>#{2,4})\s+(?P<num>\d+(?:\.\d+)*)\.?\s+(?P<title>.+)$",
    re.MULTILINE,
)


def _strip_markdown_emphasis(text: str) -> str:
    """Remove ** and ` markers but keep the words, so extract_threshold()
    regexes don't need to account for them."""
    text = text.replace("**", "")
    text = text.replace("`", "")
    return text.strip()


def _parse_protocol_rows(body: str, source_path: str) -> dict[str, SopRule]:
    rules: dict[str, SopRule] = {}
    for m in _PROTOCOL_ROW_RE.finditer(body):
        protocol_id = m.group("id")
        rest = m.group("rest")
        # rest may contain further "| col |" separators (e.g. RC's evidence
        # column) — join all cells with a space, drop empties.
        cells = [c.strip() for c in rest.split("|")]
        text = " ".join(c for c in cells if c)
        rules[protocol_id] = {
            "text": _strip_markdown_emphasis(text),
            "source_path": source_path,
        }
    return rules


def _parse_sections(body: str, source_path: str) -> dict[str, SopRule]:
    sections: dict[str, SopRule] = {}
    matches = list(_SECTION_HEADING_RE.finditer(body))
    for i, m in enumerate(matches):
        num = m.group("num")
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section_body = body[start:end].strip()
        title = m.group("title").strip()
        sections[num] = {
            "text": f"{title}\n\n{_strip_markdown_emphasis(section_body)}",
            "source_path": source_path,
        }
    return sections


def load_sop_rules(fleet_dir: Path | str) -> dict[str, SopRule]:
    """Load every protocol ID and manual section from fleet/sops/ and
    fleet/well-types/ into one flat {key: {text, source_path}} dict.

    Protocol IDs (SR-01, WS-05, RC-02, ...) and section numbers (9, 9.3,
    10, ...) share the same namespace; collisions are not expected since
    protocol IDs always contain a letter prefix and sections are numeric.
    """
    fleet_dir = Path(fleet_dir)
    rules: dict[str, SopRule] = {}

    sops_dir = fleet_dir / "sops"
    if sops_dir.exists():
        for path in sorted(sops_dir.glob("*.md")):
            body = path.read_text(encoding="utf-8")
            rel = str(path.relative_to(fleet_dir.parent)) if fleet_dir.parent in path.parents else str(path)
            rules.update(_parse_protocol_rows(body, rel))

    well_types_dir = fleet_dir / "well-types"
    if well_types_dir.exists():
        for path in sorted(well_types_dir.glob("*.md")):
            body = path.read_text(encoding="utf-8")
            rel = str(path.relative_to(fleet_dir.parent)) if fleet_dir.parent in path.parents else str(path)
            well_type = path.stem  # "T200" or "T350"
            section_rules = _parse_sections(body, rel)
            for num, rule in section_rules.items():
                rules[f"{well_type}#{num}"] = rule
                # Also index bare section numbers from the first manual seen,
                # so a lookup like get_manual_section("T200", "9.3") and a
                # bare "9.3" both resolve when unambiguous.
                rules.setdefault(num, rule)

    return rules


def extract_threshold(text: str, pattern: str) -> float | None:
    """Pull the first number matching `pattern` (a regex with one capture
    group around the number) out of `text`. Returns None if not found.

    Example: extract_threshold(rules["SR-01"]["text"], r"within\\s+(\\d+)\\s+hours")
    """
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (IndexError, ValueError):
        return None
