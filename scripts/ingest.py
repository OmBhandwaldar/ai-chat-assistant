"""One-shot loader: JSONL dataset -> DuckDB + FTS + vector index.

Usage:
    python scripts/ingest.py [DATASET_ROOT] [--db-path PATH] [--chroma-path PATH]
                             [--skip-embeddings]

DATASET_ROOT is a directory containing fleet/ and assets/ exactly as described in
the dataset guide (the sample data, or the test dataset released in the final 2
hours: same layout, swap the path, nothing else changes). It defaults to the
DATASET_ROOT environment variable (or .env entry). Nothing is hardcoded to the
sample: any number of wells, any well ids, any well types.

Output locations (the real ones are the defaults; override both to rehearse):
    --db-path / env DB_PATH           default backend/data.duckdb
    --chroma-path / env CHROMA_PATH   default backend/chroma_db when --db-path is the
                                      default, else <db-path stem>_chroma next to it
                                      (so a rehearsal never touches the real index)

The database is built in "<db>.ingest-tmp" and swapped in only when the whole run
succeeded, so a crash never leaves you without a database. Idempotent: every run
rebuilds every table from scratch.

Robustness contract (the test dataset has a shape we have not seen):
  * unknown extra fields are ignored; missing optional fields become NULL;
  * missing / empty / BOM / CRLF files, blank lines, a last line without newline,
    malformed lines, JSON arrays in place of JSONL: tolerated;
  * timestamps with / without timezone and fractional seconds are normalised to
    naive UTC; numbers / booleans given as strings are coerced;
  * duplicate ids, rows without ids, unknown well types / areas / teams, report
    files that no maintenance row mentions (and vice versa): repaired or skipped
    with a counted warning, never a crash, and listed in the final summary
    (and in the `ingest_warnings` table).

See PLAN.md Phase 1.2 / 8.1, DECISIONS.md D13.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import posixpath
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "backend" / "db" / "schema.sql"
VIEWS_PATH = REPO_ROOT / "backend" / "db" / "views.sql"
DEFAULT_DB_PATH = REPO_ROOT / "backend" / "data.duckdb"
DEFAULT_CHROMA_PATH = REPO_ROOT / "backend" / "chroma_db"

# Fallback thresholds, used only when fleet/well-types/<TYPE>.md is absent or its
# producing-temperature / alarm sentences cannot be parsed. Values normally come
# from the manuals (see parse_manual_rules), so a changed manual in the test
# dataset is picked up without a code change.
DEFAULT_WELL_TYPE_RULES: dict[str, dict[str, float]] = {
    "T200": {"producing_threshold_c": 15.0, "max_wellhead_c": 85.0},
    "T350": {"producing_threshold_c": 20.0, "max_wellhead_c": 95.0},
}
DEFAULT_RCA_COUNT = 3
DEFAULT_RCA_LOOKBACK_DAYS = 365
# Used for a well type that has neither a manual nor a built-in default.
ASSUMED_RULE = DEFAULT_WELL_TYPE_RULES["T200"]

REPORT_SUFFIXES = {".md", ".markdown", ".txt"}
EMBED_MODEL_CACHE_DIR = "models--BAAI--bge-small-en-v1.5"

TABLE_COLUMNS = {
    "teams": 3,
    "areas": 5,
    "users": 4,
    "well_type_rules": 5,
    "wells": 11,
    "well_equipment": 4,
    "inspections": 11,
    "maintenance": 16,
    "maintenance_reports": 6,
    "orphan_reports": 3,
    "regulatory_events": 11,
    "context_notes": 10,
}
SUMMARY_TABLES = [
    "wells", "well_equipment", "inspections", "maintenance", "maintenance_reports",
    "orphan_reports", "regulatory_events", "context_notes", "areas", "teams", "users",
    "well_type_rules", "report_chunks",
]


class IngestError(RuntimeError):
    """Fatal problem: nothing sensible can be loaded (the old DB is left alone)."""


# ---------------------------------------------------------------------------
# Warning bookkeeping
# ---------------------------------------------------------------------------


class Warnings:
    """Counts warnings by category and keeps a few samples of each."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.samples: dict[str, list[str]] = defaultdict(list)

    def warn(self, category: str, detail: str = "") -> None:
        self.counts[category] += 1
        if detail and len(self.samples[category]) < 3:
            self.samples[category].append(detail)

    def total(self) -> int:
        return sum(self.counts.values())


# ---------------------------------------------------------------------------
# Tolerant file readers
# ---------------------------------------------------------------------------


def read_text(path: Path, W: Warnings) -> str | None:
    """Read a text file as UTF-8 (BOM tolerated; UTF-16 BOM honoured; bad bytes replaced)."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        W.warn("file_unreadable", f"{path}: {e}")
        return None
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace").replace("\x00", "")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        W.warn("file_bad_encoding", str(path))
        text = raw.decode("utf-8-sig", errors="replace")
    return text.replace("\x00", "")


def read_json(path: Path, W: Warnings) -> Any | None:
    """Parse a JSON file; None when missing, empty or malformed (warned, never raised)."""
    if not path.is_file():
        return None
    text = read_text(path, W)
    if text is None:
        return None
    if not text.strip():
        W.warn("json_file_empty", str(path))
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        W.warn("json_file_malformed", f"{path}: {e}")
        return None


def read_jsonl(path: Path, W: Warnings) -> list[tuple[int, dict]]:
    """Return (line_number, object) pairs. Blank lines, CRLF, a missing final newline,
    a leading BOM are fine; malformed or non-object lines are skipped with a warning.
    A file holding one JSON array instead of JSON lines is also accepted."""
    if not path.is_file():
        return []
    text = read_text(path, W)
    if text is None or not text.strip():
        return []
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
            if isinstance(data, list):
                W.warn("jsonl_was_json_array", str(path))
                return [(i + 1, o) for i, o in enumerate(data) if isinstance(o, dict)]
        except json.JSONDecodeError:
            pass
    rows: list[tuple[int, dict]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            W.warn("jsonl_line_malformed", f"{path.name}:{lineno}: {e}")
            continue
        if not isinstance(obj, dict):
            W.warn("jsonl_line_not_object", f"{path.name}:{lineno}")
            continue
        rows.append((lineno, obj))
    return rows


def as_dict_list(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def clean_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def keep_text(v: Any) -> str | None:
    """Free text stored exactly as given (including an empty string); only None stays NULL."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return clean_str(v)


def to_float(v: Any, W: Warnings, what: str) -> float | None:
    if v is None or isinstance(v, bool):
        if isinstance(v, bool):
            W.warn("value_not_numeric", f"{what}={v!r}")
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        s = str(v).strip().replace(",", "")
        if not s:
            return None
        try:
            f = float(s)
        except ValueError:
            W.warn("value_not_numeric", f"{what}={v!r}")
            return None
    if math.isnan(f) or math.isinf(f):
        W.warn("value_not_numeric", f"{what}={v!r}")
        return None
    return f


def to_bool(v: Any, W: Warnings, what: str) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    s = str(v).strip().lower()
    if s in {"true", "t", "yes", "y", "1"}:
        return True
    if s in {"false", "f", "no", "n", "0"}:
        return False
    if s in {"", "null", "none"}:
        return None
    W.warn("value_not_boolean", f"{what}={v!r}")
    return None


def parse_ts(v: Any) -> datetime | None:
    """Any ISO-ish timestamp -> naive UTC datetime (None if unparseable).

    Handles 'Z', explicit offsets, no timezone (taken as UTC), fractional seconds of
    any length, space instead of 'T', date-only, and epoch seconds / milliseconds."""
    if v is None or isinstance(v, bool):
        return None
    dt: datetime | None = None
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, date):
        dt = datetime(v.year, v.month, v.day)
    elif isinstance(v, (int, float)):
        secs = float(v) / 1000.0 if abs(float(v)) > 1e11 else float(v)
        try:
            dt = datetime.fromtimestamp(secs, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        s = str(v).strip()
        if not s:
            return None
        if s[-1] in "Zz":
            s = s[:-1] + "+00:00"
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            if re.fullmatch(r"-?\d+(\.\d+)?", s):
                return parse_ts(float(s))
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def to_ts(v: Any, W: Warnings, what: str) -> datetime | None:
    dt = parse_ts(v)
    if dt is None and v not in (None, ""):
        W.warn("timestamp_unparseable", f"{what}={v!r}")
    return dt


def to_date(v: Any, W: Warnings, what: str) -> date | None:
    dt = to_ts(v, W, what)
    return dt.date() if dt else None


def norm_rel_path(p: Any) -> str | None:
    """Normalise a report_file value: forward slashes, no ./ prefix, no .. segments."""
    s = clean_str(p)
    if not s:
        return None
    s = s.replace("\\", "/")
    s = posixpath.normpath(s)
    return s.lstrip("/") or None


# ---------------------------------------------------------------------------
# Collected rows
# ---------------------------------------------------------------------------


@dataclass
class Dataset:
    rows: dict[str, list[tuple]] = field(
        default_factory=lambda: {t: [] for t in TABLE_COLUMNS}
    )
    seen: dict[str, set] = field(default_factory=lambda: defaultdict(set))
    # roster from fleet/areas.json (asset_id -> area_id), used when metadata lacks area_id
    roster_area: dict[str, str] = field(default_factory=dict)
    area_team: dict[str, str | None] = field(default_factory=dict)
    well_area: dict[str, str | None] = field(default_factory=dict)
    well_team: dict[str, str | None] = field(default_factory=dict)

    def add(self, table: str, key: Any, row: tuple, W: Warnings) -> bool:
        """Append a row unless its primary key was already seen (first one wins)."""
        if key in self.seen[table]:
            W.warn(f"duplicate_{table}_key", str(key))
            return False
        self.seen[table].add(key)
        self.rows[table].append(row)
        return True

    def has(self, table: str, key: Any) -> bool:
        return key in self.seen[table]


# ---------------------------------------------------------------------------
# Fleet files
# ---------------------------------------------------------------------------


def load_fleet(ds: Dataset, fleet: Path, W: Warnings) -> None:
    if not fleet.is_dir():
        W.warn("fleet_dir_missing", str(fleet))
        return

    areas_doc = read_json(fleet / "areas.json", W)
    if isinstance(areas_doc, list):
        areas_doc = {"areas": areas_doc}
    if not isinstance(areas_doc, dict):
        if (fleet / "areas.json").exists():
            W.warn("areas_json_unusable", "areas.json is not an object")
        areas_doc = {}

    for team in as_dict_list(areas_doc.get("teams")):
        team_id = clean_str(team.get("team_id"))
        if not team_id:
            W.warn("team_missing_id")
            continue
        ds.add("teams", team_id, (team_id, clean_str(team.get("name")) or team_id,
                                  clean_str(team.get("lead_user_id"))), W)

    for area in as_dict_list(areas_doc.get("areas")):
        area_id = clean_str(area.get("area_id"))
        if not area_id:
            W.warn("area_missing_id")
            continue
        center = area.get("center") if isinstance(area.get("center"), dict) else {}
        team_id = clean_str(area.get("team_id"))
        lat = to_float(center.get("lat", area.get("lat")), W, f"area {area_id} lat")
        lon = to_float(center.get("lon", area.get("lon")), W, f"area {area_id} lon")
        if ds.add("areas", area_id,
                  (area_id, clean_str(area.get("name")) or area_id, team_id, lat, lon), W):
            ds.area_team[area_id] = team_id
            for aid in area.get("asset_ids") or []:
                if isinstance(aid, str):
                    ds.roster_area.setdefault(aid, area_id)

    users_doc = read_json(fleet / "users.json", W)
    if isinstance(users_doc, dict):
        users = as_dict_list(users_doc.get("users"))
    else:
        users = as_dict_list(users_doc)
    for user in users:
        user_id = clean_str(user.get("user_id"))
        if not user_id:
            W.warn("user_missing_id")
            continue
        ds.add("users", user_id, (user_id, clean_str(user.get("name")) or user_id,
                                  clean_str(user.get("role")) or "unknown",
                                  clean_str(user.get("team_id"))), W)


# ---------------------------------------------------------------------------
# Well-type rules (thresholds) from the manuals
# ---------------------------------------------------------------------------

_PRODUCING_RE = re.compile(
    r"at\s+least\s*\W{0,4}(\d+(?:\.\d+)?)\s*°?\s*C?\s*above\s+ambient", re.IGNORECASE)
_MAX_RE = re.compile(r"at\s+or\s+below\s*\W{0,4}(\d+(?:\.\d+)?)\s*°\s*C", re.IGNORECASE)


def parse_manual_rules(text: str) -> dict[str, float]:
    """Pull the producing threshold and temperature alarm out of a well-type manual."""
    out: dict[str, float] = {}
    m = _PRODUCING_RE.search(text)
    if m:
        out["producing_threshold_c"] = float(m.group(1))
    m = _MAX_RE.search(text)
    if m:
        out["max_wellhead_c"] = float(m.group(1))
    return out


def derive_well_type_rules(
    fleet: Path, types_in_data: set[str], W: Warnings
) -> dict[str, dict[str, float]]:
    rules: dict[str, dict[str, float]] = {}
    manuals: dict[str, dict[str, float]] = {}
    manual_dir = fleet / "well-types"
    if manual_dir.is_dir():
        for md in sorted(manual_dir.glob("*.md")):
            text = read_text(md, W)
            if text is None:
                continue
            parsed = parse_manual_rules(text)
            if parsed:
                manuals[md.stem.strip()] = parsed
            else:
                W.warn("manual_rules_unparsed", md.name)

    for well_type in sorted(set(DEFAULT_WELL_TYPE_RULES) | set(manuals) | types_in_data):
        base = DEFAULT_WELL_TYPE_RULES.get(well_type)
        parsed = manuals.get(well_type, {})
        if base is None and "producing_threshold_c" not in parsed:
            base = ASSUMED_RULE
            W.warn("well_type_rule_assumed",
                   f"{well_type}: no manual/default, assuming T200 thresholds")
        merged = {**(base or ASSUMED_RULE), **parsed}
        rules[well_type] = merged
    return rules


# ---------------------------------------------------------------------------
# One well folder
# ---------------------------------------------------------------------------


def _check_record_asset(rec: dict, asset_id: str, W: Warnings, where: str) -> None:
    other = clean_str(rec.get("asset_id"))
    if other and other != asset_id:
        W.warn("record_asset_id_mismatch", f"{where}: {other} in folder of {asset_id}")


def _cost(m: dict, W: Warnings, mid: str) -> tuple[float | None, float | None, float | None]:
    raw = m.get("cost_usd", m.get("cost"))
    if isinstance(raw, dict):
        return (to_float(raw.get("labor"), W, f"{mid} cost.labor"),
                to_float(raw.get("parts"), W, f"{mid} cost.parts"),
                to_float(raw.get("total"), W, f"{mid} cost.total"))
    if raw is not None:
        return None, None, to_float(raw, W, f"{mid} cost")
    return None, None, None


def load_well(ds: Dataset, dataset_root: Path, well_dir: Path, W: Warnings) -> bool:
    meta = read_json(well_dir / "metadata.json", W)
    if not isinstance(meta, dict):
        W.warn("well_skipped_no_metadata", well_dir.name)
        return False
    asset_id = clean_str(meta.get("asset_id")) or well_dir.name
    if ds.has("wells", asset_id):
        W.warn("well_skipped_duplicate_id", f"{well_dir.name} -> {asset_id}")
        return False

    well_type = clean_str(meta.get("well_type"))
    if not well_type:
        W.warn("well_missing_type", asset_id)
        well_type = "UNKNOWN"
    area_id = clean_str(meta.get("area_id")) or ds.roster_area.get(asset_id)
    team_id = clean_str(meta.get("team_id")) or (ds.area_team.get(area_id) if area_id else None)
    loc = meta.get("location") if isinstance(meta.get("location"), dict) else {}
    last_insp = meta.get("last_inspection") if isinstance(meta.get("last_inspection"), dict) else {}

    # ---- inspections
    insp_rows: list[tuple] = []
    latest: tuple[datetime, str] | None = None
    for lineno, r in read_jsonl(well_dir / "inspections.jsonl", W):
        _check_record_asset(r, asset_id, W, "inspections")
        iid = clean_str(r.get("inspection_id"))
        if not iid:
            iid = f"INS-{asset_id}-L{lineno}"
            W.warn("inspection_id_synthesized", iid)
        ts = to_ts(r.get("inspected_at"), W, f"{iid} inspected_at")
        if ts is None:
            W.warn("inspection_skipped_no_timestamp", iid)
            continue
        row = (iid, asset_id, ts, clean_str(r.get("inspector_user_id")),
               clean_str(r.get("method")),
               to_float(r.get("ambient_temp_c"), W, f"{iid} ambient_temp_c"),
               to_float(r.get("wellhead_temp_c"), W, f"{iid} wellhead_temp_c"),
               to_bool(r.get("spill_detected"), W, f"{iid} spill_detected"),
               to_float(r.get("spill_area_pct"), W, f"{iid} spill_area_pct"),
               to_float(r.get("spill_est_volume_bbl"), W, f"{iid} spill_est_volume_bbl"),
               keep_text(r.get("notes")))
        insp_rows.append((iid, row, ts))

    # ---- maintenance
    mnt_rows: list[tuple] = []  # (mid, row, report_file_norm)
    for lineno, m in read_jsonl(well_dir / "maintenance.jsonl", W):
        _check_record_asset(m, asset_id, W, "maintenance")
        mid = clean_str(m.get("maintenance_id"))
        if not mid:
            mid = f"MNT-{asset_id}-L{lineno}"
            W.warn("maintenance_id_synthesized", mid)
        labor, parts, total = _cost(m, W, mid)
        report_file = norm_rel_path(m.get("report_file"))
        row = (mid, asset_id, to_ts(m.get("started_at"), W, f"{mid} started_at"),
               to_ts(m.get("completed_at"), W, f"{mid} completed_at"),
               clean_str(m.get("maintenance_type")), clean_str(m.get("equipment_kind")),
               keep_text(m.get("issue")), keep_text(m.get("action_taken")),
               clean_str(m.get("technician_user_id")), clean_str(m.get("contractor")),
               to_float(m.get("downtime_hours"), W, f"{mid} downtime_hours"),
               labor, parts, total, clean_str(m.get("triggered_by_inspection_id")),
               report_file)
        mnt_rows.append((mid, row, report_file))

    # ---- regulatory events
    reg_rows: list[tuple] = []
    for lineno, e in read_jsonl(well_dir / "regulatory-events.jsonl", W):
        _check_record_asset(e, asset_id, W, "regulatory-events")
        eid = clean_str(e.get("event_id"))
        if not eid:
            eid = f"REG-{asset_id}-L{lineno}"
            W.warn("event_id_synthesized", eid)
        reg_rows.append((eid, (
            eid, asset_id, to_date(e.get("date"), W, f"{eid} date"), clean_str(e.get("agency")),
            clean_str(e.get("event_type")), clean_str(e.get("protocol_id")),
            clean_str(e.get("related_inspection_id")),
            to_float(e.get("hours_to_report"), W, f"{eid} hours_to_report"),
            keep_text(e.get("detail")), to_float(e.get("fine_usd"), W, f"{eid} fine_usd"),
            clean_str(e.get("status")))))

    # ---- context notes
    ctx_rows: list[tuple] = []
    for lineno, n in read_jsonl(well_dir / "context.jsonl", W):
        _check_record_asset(n, asset_id, W, "context")
        nid = clean_str(n.get("note_id"))
        if not nid:
            nid = f"CTX-{asset_id}-L{lineno}"
            W.warn("note_id_synthesized", nid)
        text = keep_text(n.get("text"))
        valid_from = to_date(n.get("valid_from"), W, f"{nid} valid_from")
        created = to_ts(n.get("created_at"), W, f"{nid} created_at")
        if created is None and valid_from is not None:
            created = datetime(valid_from.year, valid_from.month, valid_from.day)
            W.warn("note_created_at_from_valid_from", nid)
        if text is None or not text.strip() or created is None:
            W.warn("note_skipped_incomplete", nid)
            continue
        ctx_rows.append((nid, (
            nid, asset_id, created, clean_str(n.get("author_user_id")),
            clean_str(n.get("note_type")) or "operator_note",
            clean_str(n.get("inspection_id")), text, valid_from,
            to_date(n.get("valid_to"), W, f"{nid} valid_to"), "dataset")))

    # ---- commit this well's rows (only now: a skipped well leaves nothing half-loaded)
    latest_id = None
    for iid, row, ts in insp_rows:
        if ds.has("inspections", iid):
            W.warn("duplicate_inspections_key", iid)
            continue
        ds.add("inspections", iid, row, W)
        if latest is None or ts > latest[0]:
            latest = (ts, iid)
    if latest:
        latest_id = latest[1]

    installed = to_date(meta.get("installed_on"), W, f"{asset_id} installed_on")
    ds.add("wells", asset_id, (
        asset_id, clean_str(meta.get("name")) or asset_id, well_type, area_id, team_id,
        clean_str(meta.get("in_charge_user_id")),
        to_float(loc.get("lat", meta.get("lat")), W, f"{asset_id} lat"),
        to_float(loc.get("lon", meta.get("lon")), W, f"{asset_id} lon"),
        installed, clean_str(meta.get("recorded_status")),
        clean_str(last_insp.get("inspection_id")) or latest_id), W)
    ds.well_area[asset_id] = area_id
    ds.well_team[asset_id] = team_id

    for i, eq in enumerate(as_dict_list(meta.get("equipment")), 1):
        eid = clean_str(eq.get("equipment_id")) or f"{asset_id}-EQ{i}"
        ds.add("well_equipment", (asset_id, eid),
               (asset_id, eid, clean_str(eq.get("kind")) or "unknown", clean_str(eq.get("model"))),
               W)

    kept_mnt: dict[str, tuple[str | None, str | None]] = {}  # mid -> (report_file, started)
    for mid, row, report_file in mnt_rows:
        if ds.add("maintenance", mid, row, W):
            kept_mnt[mid] = (report_file, row[2])
    for eid, row in reg_rows:
        ds.add("regulatory_events", eid, row, W)
    for nid, row in ctx_rows:
        ds.add("context_notes", nid, row, W)

    _load_reports(ds, dataset_root, well_dir, asset_id, area_id, kept_mnt, W)
    return True


_REPORT_HEADER_ID_RE = re.compile(r"\b[A-Z]{2,5}-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b")


def _load_reports(
    ds: Dataset, dataset_root: Path, well_dir: Path, asset_id: str, area_id: str | None,
    kept_mnt: dict[str, tuple[str | None, Any]], W: Warnings,
) -> None:
    """Match report files to maintenance rows and store them.

    Matching order (the guide says file names vary, so report_file always wins):
      1. maintenance.report_file (normalised path relative to the well folder);
      2. same file name as a report_file whose directory part is different;
      3. file stem equals a maintenance_id that has no report yet;
      4. a maintenance_id of this well in the report's title line.
    Unmatched files go to orphan_reports; rows whose report_file matched no file
    are warned (the row itself is kept)."""
    reports_dir = well_dir / "maintenance-reports"
    by_path: dict[str, str] = {}
    by_base: dict[str, list[str]] = defaultdict(list)
    for mid, (rf, _) in kept_mnt.items():
        if rf:
            by_path.setdefault(rf, mid)
            by_base[posixpath.basename(rf)].append(mid)

    files: list[Path] = []
    if reports_dir.is_dir():
        files = sorted(p for p in reports_dir.rglob("*")
                       if p.is_file() and p.suffix.lower() in REPORT_SUFFIXES)

    claimed: dict[str, Path] = {}
    pending: list[tuple[Path, str, str]] = []  # (path, rel_to_root, body)
    for p in files:
        body = read_text(p, W)
        rel_root = p.relative_to(dataset_root).as_posix()
        if body is None or not body.strip():
            W.warn("report_file_empty", rel_root)
            continue
        body = body.replace("\r\n", "\n").replace("\r", "\n")
        rel_well = p.relative_to(well_dir).as_posix()
        mid = by_path.get(rel_well)
        if mid is None:
            cands = by_base.get(p.name, [])
            if len(cands) == 1:
                mid = cands[0]
        if mid is not None and mid in claimed:
            mid = None  # another file already took this maintenance row
        if mid is not None:
            claimed[mid] = p
            _store_report(ds, rel_root, asset_id, area_id, mid, kept_mnt[mid][1], body, W)
        else:
            pending.append((p, rel_root, body))

    for p, rel_root, body in pending:
        mid = None
        if p.stem in kept_mnt and p.stem not in claimed:
            mid = p.stem
        else:
            title = "\n".join(body.splitlines()[:5])
            for cand in _REPORT_HEADER_ID_RE.findall(title):
                if cand in kept_mnt and cand not in claimed:
                    mid = cand
                    break
        if mid is not None:
            claimed[mid] = p
            W.warn("report_linked_by_name_or_title", f"{rel_root} -> {mid}")
            _store_report(ds, rel_root, asset_id, area_id, mid, kept_mnt[mid][1], body, W)
        else:
            W.warn("report_without_maintenance_row", rel_root)
            ds.add("orphan_reports", rel_root, (rel_root, asset_id, body), W)

    for mid, (rf, _) in kept_mnt.items():
        if rf and mid not in claimed:
            W.warn("maintenance_report_file_missing", f"{mid} -> {rf}")


def _store_report(
    ds: Dataset, rel_root: str, asset_id: str, area_id: str | None, mid: str, started: Any,
    body: str, W: Warnings,
) -> None:
    ds.add("maintenance_reports", rel_root, (rel_root, asset_id, mid, area_id, started, body), W)


# ---------------------------------------------------------------------------
# Referential repair (DuckDB enforces the schema's foreign keys)
# ---------------------------------------------------------------------------


def repair_references(ds: Dataset, W: Warnings) -> None:
    """Create stub teams / areas for ids that are referenced but not defined, so a well
    in an unlisted area is loaded (and filterable by that area) instead of dropped."""
    for area_id, team_id in list(ds.area_team.items()):
        if team_id and not ds.has("teams", team_id):
            W.warn("team_stub_created", team_id)
            ds.add("teams", team_id, (team_id, team_id, None), W)
    for asset_id, area_id in ds.well_area.items():
        if area_id and not ds.has("areas", area_id):
            W.warn("area_stub_created", f"{area_id} (referenced by {asset_id})")
            team = ds.well_team.get(asset_id)
            ds.add("areas", area_id, (area_id, area_id, team, None, None), W)
            ds.area_team[area_id] = team
    teams_needed = {t for t in ds.well_team.values() if t}
    teams_needed |= {r[3] for r in ds.rows["users"] if r[3]}
    teams_needed |= {r[2] for r in ds.rows["areas"] if r[2]}
    for team_id in sorted(teams_needed):
        if not ds.has("teams", team_id):
            W.warn("team_stub_created", team_id)
            ds.add("teams", team_id, (team_id, team_id, None), W)


# ---------------------------------------------------------------------------
# DuckDB writing
# ---------------------------------------------------------------------------


_NULL_TOKEN = "\\N-ingest-null"


def _csv_cell(v: Any) -> str:
    if v is None:
        return _NULL_TOKEN
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _insert_rows_slow(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple],
                      chunk: int = 200) -> None:
    one = "(" + ",".join("?" * len(rows[0])) + ")"
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        flat = [v for r in part for v in r]
        con.execute(f"INSERT INTO {table} VALUES " + ",".join([one] * len(part)), flat)


def bulk_insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple],
                W: Warnings | None = None) -> None:
    """Insert rows through a temp CSV + read_csv (about 200x faster than bound
    parameters for the 40k-row inspections table); falls back to bound parameters
    if the CSV route fails for any reason."""
    if not rows:
        return
    cols = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position", [table],
    ).fetchall()
    spec = ", ".join(f"'{name}': '{dtype}'" for name, dtype in cols)
    fd, tmp = tempfile.mkstemp(suffix=".csv", prefix=f"ingest_{table}_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            for r in rows:
                writer.writerow([_csv_cell(v) for v in r])
        con.execute(
            f"INSERT INTO {table} SELECT * FROM read_csv(?, header=false, "
            f"nullstr=?, quote='\"', escape='\"', new_line='\\n', max_line_size=268435456, "
            f"columns={{{spec}}})", [tmp, _NULL_TOKEN])
    except Exception as e:  # noqa: BLE001
        if W is not None:
            W.warn("csv_bulk_load_fallback", f"{table}: {str(e)[:120]}")
        _insert_rows_slow(con, table, rows)
    finally:
        Path(tmp).unlink(missing_ok=True)


def load_extension(con: duckdb.DuckDBPyConnection, name: str, W: Warnings) -> bool:
    try:
        con.load_extension(name)
        return True
    except Exception:  # noqa: BLE001
        try:
            con.install_extension(name)
            con.load_extension(name)
            return True
        except Exception as e:  # noqa: BLE001
            W.warn(f"extension_{name}_unavailable", str(e)[:160])
            return False


def find_asset_dirs(assets_dir: Path, W: Warnings) -> list[Path]:
    """Every sub-folder of assets/ that holds a metadata.json is a well (no WELL-
    prefix assumption); other entries are noted and ignored."""
    dirs: list[Path] = []
    for entry in sorted(assets_dir.iterdir(), key=lambda p: p.name):
        if entry.is_dir() and (entry / "metadata.json").is_file():
            dirs.append(entry)
        elif entry.is_dir():
            W.warn("assets_folder_without_metadata", entry.name)
        elif not entry.name.startswith("."):
            W.warn("assets_stray_file", entry.name)
    return dirs


# ---------------------------------------------------------------------------
# Embedding / Chroma step
# ---------------------------------------------------------------------------


def _prefer_offline_model_cache() -> bool:
    """If bge-small is already in the local HF cache, don't hit the network for it
    (flaky venue wifi must not break the ingest). Must run before the first import of
    huggingface_hub / sentence_transformers. Returns True if it set HF_HUB_OFFLINE."""
    if os.environ.get("HF_HUB_OFFLINE"):
        return False
    hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    model_dir = hf_home / "hub" / EMBED_MODEL_CACHE_DIR / "snapshots"
    if model_dir.is_dir() and any(model_dir.rglob("model.safetensors")) \
            and any(model_dir.rglob("tokenizer.json")):
        os.environ["HF_HUB_OFFLINE"] = "1"
        return True
    return False


def build_vector_index(con: duckdb.DuckDBPyConnection, chroma_path: Path, W: Warnings) -> int:
    """Chunk + embed every linked report into report_chunks + the Chroma collection
    (the same function search_reports() uses lazily, so the two can never disagree)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    _prefer_offline_model_cache()
    from backend.tools.search import build_report_index

    chroma_path.mkdir(parents=True, exist_ok=True)
    return build_report_index(con, chroma_path=chroma_path, force=True)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass
class IngestResult:
    counts: dict[str, int]
    warnings: dict[str, int]
    samples: dict[str, list[str]]
    timings: dict[str, float]
    db_path: Path
    chroma_path: Path | None
    vector_count: int | None = None


def ingest(
    dataset_root: Path,
    db_path: Path,
    chroma_path: Path | None = None,
    skip_embeddings: bool = False,
    verbose: bool = True,
) -> IngestResult:
    dataset_root = Path(dataset_root)
    db_path = Path(db_path)
    log = print if verbose else (lambda *a, **k: None)
    W = Warnings()
    timings: dict[str, float] = {}

    assets_dir = dataset_root / "assets"
    if not dataset_root.is_dir():
        raise IngestError(f"dataset root does not exist: {dataset_root}")
    if not assets_dir.is_dir():
        raise IngestError(f"no assets/ folder under {dataset_root}")

    # ---- 1. read + normalise everything in memory (pure Python, no DB yet)
    t0 = time.perf_counter()
    ds = Dataset()
    fleet = dataset_root / "fleet"
    log(f"Loading fleet data from {fleet} ...")
    load_fleet(ds, fleet, W)

    well_dirs = find_asset_dirs(assets_dir, W)
    log(f"Loading {len(well_dirs)} well folders ...")
    for i, well_dir in enumerate(well_dirs, 1):
        try:
            load_well(ds, dataset_root, well_dir, W)
        except Exception as e:  # noqa: BLE001  # one broken folder must not sink the other wells
            W.warn("well_skipped_unexpected_error", f"{well_dir.name}: {type(e).__name__}: {e}")
        if i % 50 == 0 or i == len(well_dirs):
            log(f"  {i}/{len(well_dirs)} wells read")
    if not ds.rows["wells"]:
        raise IngestError(f"no wells could be loaded from {assets_dir} (existing DB untouched)")

    repair_references(ds, W)
    types_in_data = {r[2] for r in ds.rows["wells"]}
    rules = derive_well_type_rules(fleet, types_in_data, W)
    for well_type, r in rules.items():
        ds.rows["well_type_rules"].append((
            well_type, r["producing_threshold_c"], r["max_wellhead_c"],
            DEFAULT_RCA_COUNT, DEFAULT_RCA_LOOKBACK_DAYS))
    timings["read_files_s"] = time.perf_counter() - t0

    # ---- 2. build the DB in a temp file, swap in at the end
    t1 = time.perf_counter()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".ingest-tmp")
    for stale in (tmp_path, Path(str(tmp_path) + ".wal")):
        stale.unlink(missing_ok=True)

    con = duckdb.connect(str(tmp_path))
    try:
        load_extension(con, "json", W)
        have_fts = load_extension(con, "fts", W)
        con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

        insert_order = ["teams", "areas", "users", "well_type_rules", "wells", "well_equipment",
                        "inspections", "maintenance", "maintenance_reports", "orphan_reports",
                        "regulatory_events", "context_notes"]
        for table in insert_order:
            bulk_insert(con, table, ds.rows[table], W)
        con.execute(VIEWS_PATH.read_text(encoding="utf-8"))

        if have_fts and ds.rows["maintenance_reports"]:
            try:
                con.execute("PRAGMA create_fts_index('maintenance_reports', 'report_path', "
                            "'body', overwrite=1)")
            except Exception as e:  # noqa: BLE001
                W.warn("fts_index_failed", str(e)[:160])
        timings["sql_load_s"] = time.perf_counter() - t1

        # ---- 3. embeddings + Chroma (slow part, skippable)
        vector_count: int | None = None
        t2 = time.perf_counter()
        if chroma_path is None:
            chroma_path = (DEFAULT_CHROMA_PATH if db_path.resolve() == DEFAULT_DB_PATH.resolve()
                           else db_path.parent / f"{db_path.stem}_chroma")
        if skip_embeddings:
            log("Skipping embeddings (--skip-embeddings): search_reports() will build the "
                "vector index lazily on first use.")
        elif not ds.rows["maintenance_reports"]:
            log("No linked maintenance reports: nothing to embed.")
        else:
            log(f"Embedding report chunks into {chroma_path} ...")
            try:
                n_chunks = build_vector_index(con, chroma_path, W)
                vector_count = n_chunks
                log(f"  {n_chunks} chunks embedded")
            except Exception as e:  # noqa: BLE001
                W.warn("embedding_failed",
                       f"{type(e).__name__}: {str(e)[:200]} (search_reports will retry lazily)")
                con.execute("DELETE FROM report_chunks")
        timings["embeddings_s"] = time.perf_counter() - t2

        for category, n in sorted(W.counts.items()):
            sample = "; ".join(W.samples.get(category, []))[:300] or None
            con.execute("INSERT INTO ingest_warnings VALUES (?, ?, ?)", [category, n, sample])

        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in SUMMARY_TABLES}
        con.execute("CHECKPOINT")
    except BaseException:
        con.close()
        tmp_path.unlink(missing_ok=True)
        Path(str(tmp_path) + ".wal").unlink(missing_ok=True)
        raise
    con.close()

    Path(str(db_path) + ".wal").unlink(missing_ok=True)
    os.replace(tmp_path, db_path)
    Path(str(tmp_path) + ".wal").unlink(missing_ok=True)
    timings["total_s"] = time.perf_counter() - t0

    return IngestResult(counts=counts, warnings=dict(W.counts), samples=dict(W.samples),
                        timings=timings, db_path=db_path,
                        chroma_path=None if skip_embeddings else chroma_path,
                        vector_count=vector_count)


def print_summary(res: IngestResult) -> None:
    print("\nIngest complete:")
    for table, n in res.counts.items():
        print(f"  {table}: {n}")
    if res.vector_count is not None:
        print(f"  chroma vectors: {res.vector_count}")
    total = sum(res.warnings.values())
    print(f"\nWarnings: {total} in {len(res.warnings)} categories")
    for category, n in sorted(res.warnings.items()):
        sample = "; ".join(res.samples.get(category, [])[:2])
        print(f"  {category}: {n}" + (f"   e.g. {sample}" if sample else ""))
    t = res.timings
    print(f"\nTiming: read files {t['read_files_s']:.1f}s, SQL load + views + FTS "
          f"{t['sql_load_s']:.1f}s, embeddings/Chroma {t['embeddings_s']:.1f}s, "
          f"total {t['total_s']:.1f}s")
    print(f"Database written to {res.db_path}")
    if res.chroma_path is not None:
        print(f"Vector index at   {res.chroma_path}")


def _resolve_dataset_root(arg: Path | None) -> Path:
    if arg is None:
        env = os.environ.get("DATASET_ROOT")
        if not env:
            raise IngestError("no dataset root: pass it as an argument or set DATASET_ROOT")
        arg = Path(env)
    arg = arg.expanduser()
    if not arg.exists() and not arg.is_absolute() and (REPO_ROOT / arg).exists():
        arg = REPO_ROOT / arg  # DATASET_ROOT in .env is written relative to the repo
    return arg


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dataset_root", type=Path, nargs="?",
                        help="folder with fleet/ and assets/ (default: $DATASET_ROOT)")
    parser.add_argument("--db-path", "--db", dest="db_path", type=Path,
                        default=Path(os.environ["DB_PATH"]) if os.environ.get("DB_PATH")
                        else DEFAULT_DB_PATH,
                        help="output DuckDB file (default: $DB_PATH or backend/data.duckdb)")
    parser.add_argument("--chroma-path", type=Path,
                        default=Path(os.environ["CHROMA_PATH"]) if os.environ.get("CHROMA_PATH")
                        else None,
                        help="Chroma directory (default: backend/chroma_db for the default DB, "
                             "else next to --db-path)")
    parser.add_argument("--skip-embeddings", action="store_true",
                        help="SQL + FTS only; the vector index is built lazily by search_reports()")
    args = parser.parse_args(argv)

    try:
        root = _resolve_dataset_root(args.dataset_root)
        res = ingest(root, args.db_path, args.chroma_path, args.skip_embeddings)
    except IngestError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print_summary(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
