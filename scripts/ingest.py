"""One-shot loader: JSONL dataset -> DuckDB + FTS + vector indices.

Usage: python scripts/ingest.py <DATASET_ROOT> [--db-path backend/data.duckdb]

<DATASET_ROOT> is a directory containing fleet/ and assets/ exactly as
described in the dataset guide (e.g. .../sample-data or the test dataset
released in the final 2 hours — same layout, swap the path, nothing else
changes). See PLAN.md Phase 1.2, TECH_STACK.md section 10.

Idempotent: drops and recreates every table on each run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

SCHEMA_PATH = Path(__file__).parent.parent / "backend" / "db" / "schema.sql"
VIEWS_PATH = Path(__file__).parent.parent / "backend" / "db" / "views.sql"

# Thresholds transcribed from fleet/well-types/T200.md section 9.3/9.4 and
# T350.md sections 3/4. If a future dataset ships different manuals, these
# become wrong silently — Phase 2.4's sop_rules.py parser is the long-term
# fix; this is the fast path for now.
WELL_TYPE_RULES = {
    "T200": {"producing_threshold_c": 15.0, "max_wellhead_c": 85.0,
              "rca_corrective_count": 3, "rca_lookback_days": 365},
    "T350": {"producing_threshold_c": 20.0, "max_wellhead_c": 95.0,
              "rca_corrective_count": 3, "rca_lookback_days": 365},
}


def read_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_fleet(con: duckdb.DuckDBPyConnection, dataset_root: Path) -> None:
    fleet = dataset_root / "fleet"
    areas_doc = read_json(fleet / "areas.json")

    for team in areas_doc.get("teams", []):
        con.execute(
            "INSERT INTO teams VALUES (?, ?, ?)",
            [team["team_id"], team["name"], team.get("lead_user_id")],
        )

    for area in areas_doc.get("areas", []):
        center = area.get("center", {})
        con.execute(
            "INSERT INTO areas VALUES (?, ?, ?, ?, ?)",
            [area["area_id"], area["name"], area.get("team_id"),
             center.get("lat"), center.get("lon")],
        )

    users_doc = read_json(fleet / "users.json")
    for user in users_doc.get("users", []):
        con.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?)",
            [user["user_id"], user["name"], user.get("role"), user.get("team_id")],
        )

    for well_type, rules in WELL_TYPE_RULES.items():
        con.execute(
            "INSERT INTO well_type_rules VALUES (?, ?, ?, ?, ?)",
            [well_type, rules["producing_threshold_c"], rules["max_wellhead_c"],
             rules["rca_corrective_count"], rules["rca_lookback_days"]],
        )


def load_well(con: duckdb.DuckDBPyConnection, well_dir: Path) -> None:
    meta = read_json(well_dir / "metadata.json")
    asset_id = meta["asset_id"]
    loc = meta.get("location", {})
    last_insp = meta.get("last_inspection") or {}

    con.execute(
        "INSERT INTO wells VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            asset_id, meta.get("name"), meta["well_type"], meta.get("area_id"),
            meta.get("team_id"), meta.get("in_charge_user_id"),
            loc.get("lat"), loc.get("lon"), meta.get("installed_on"),
            meta.get("recorded_status"), last_insp.get("inspection_id"),
        ],
    )

    for eq in meta.get("equipment", []):
        con.execute(
            "INSERT INTO well_equipment VALUES (?, ?, ?, ?)",
            [asset_id, eq["equipment_id"], eq.get("kind"), eq.get("model")],
        )

    for insp in read_jsonl(well_dir / "inspections.jsonl"):
        con.execute(
            "INSERT INTO inspections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                insp["inspection_id"], asset_id, insp["inspected_at"],
                insp.get("inspector_user_id"), insp.get("method"),
                insp.get("ambient_temp_c"), insp.get("wellhead_temp_c"),
                insp.get("spill_detected"), insp.get("spill_area_pct"),
                insp.get("spill_est_volume_bbl"), insp.get("notes"),
            ],
        )

    maintenance_rows = read_jsonl(well_dir / "maintenance.jsonl")
    report_file_by_mnt_id: dict[str, str] = {}
    for m in maintenance_rows:
        cost = m.get("cost_usd") or {}
        con.execute(
            "INSERT INTO maintenance VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                m["maintenance_id"], asset_id, m.get("started_at"), m.get("completed_at"),
                m.get("maintenance_type"), m.get("equipment_kind"), m.get("issue"),
                m.get("action_taken"), m.get("technician_user_id"), m.get("contractor"),
                m.get("downtime_hours"), cost.get("labor"), cost.get("parts"),
                cost.get("total"), m.get("triggered_by_inspection_id"), m.get("report_file"),
            ],
        )
        if m.get("report_file"):
            report_file_by_mnt_id[m["report_file"]] = m["maintenance_id"]

    for event in read_jsonl(well_dir / "regulatory-events.jsonl"):
        con.execute(
            "INSERT INTO regulatory_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                event["event_id"], asset_id, event.get("date"), event.get("agency"),
                event.get("event_type"), event.get("protocol_id"),
                event.get("related_inspection_id"), event.get("hours_to_report"),
                event.get("detail"), event.get("fine_usd"), event.get("status"),
            ],
        )

    for note in read_jsonl(well_dir / "context.jsonl"):
        con.execute(
            "INSERT INTO context_notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                note["note_id"], asset_id, note["created_at"], note.get("author_user_id"),
                note["note_type"], note.get("inspection_id"), note["text"],
                note.get("valid_from"), note.get("valid_to"), "dataset",
            ],
        )

    # Reports: filenames vary (see dataset guide), so we always resolve
    # through maintenance.report_file, never by guessing from maintenance_id.
    reports_dir = well_dir / "maintenance-reports"
    if reports_dir.exists():
        for report_path in sorted(reports_dir.glob("*.md")):
            rel_path = f"maintenance-reports/{report_path.name}"
            maintenance_id = report_file_by_mnt_id.get(rel_path)
            started_at = None
            if maintenance_id:
                row = con.execute(
                    "SELECT started_at FROM maintenance WHERE maintenance_id = ?",
                    [maintenance_id],
                ).fetchone()
                started_at = row[0] if row else None
            body = report_path.read_text(encoding="utf-8")
            con.execute(
                "INSERT INTO maintenance_reports VALUES (?, ?, ?, ?, ?, ?)",
                [str(report_path), asset_id, maintenance_id, meta.get("area_id"),
                 started_at, body],
            )


def ingest(dataset_root: Path, db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    con = duckdb.connect(str(db_path))
    con.install_extension("fts")
    con.load_extension("fts")
    con.install_extension("json")
    con.load_extension("json")

    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    print(f"Loading fleet data from {dataset_root / 'fleet'} ...")
    load_fleet(con, dataset_root)

    assets_dir = dataset_root / "assets"
    well_dirs = sorted(d for d in assets_dir.iterdir() if d.is_dir() and d.name.startswith("WELL-"))
    print(f"Loading {len(well_dirs)} well folders ...")
    for i, well_dir in enumerate(well_dirs, 1):
        try:
            load_well(con, well_dir)
        except Exception as e:
            print(f"  WARNING: failed to load {well_dir.name}: {e}", file=sys.stderr)
        if i % 50 == 0 or i == len(well_dirs):
            print(f"  {i}/{len(well_dirs)} wells loaded")

    con.execute(VIEWS_PATH.read_text(encoding="utf-8"))

    # FTS index over report bodies (coarse; Phase 3.2 adds chunk-level FTS
    # over report_chunks once chunking is implemented).
    try:
        con.execute(
            "PRAGMA create_fts_index('maintenance_reports', 'report_path', 'body', overwrite=1)"
        )
    except Exception as e:
        print(f"  WARNING: FTS index build failed: {e}", file=sys.stderr)

    counts = {}
    for table in ["wells", "inspections", "maintenance", "maintenance_reports",
                   "regulatory_events", "context_notes", "areas", "users"]:
        counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    con.close()

    print("\nIngest complete:")
    for table, n in counts.items():
        print(f"  {table}: {n}")
    print(f"\nDatabase written to {db_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_root", type=Path, help="Path to sample-data/ or test-data/")
    parser.add_argument(
        "--db-path", type=Path,
        default=Path(__file__).parent.parent / "backend" / "data.duckdb",
        help="Output DuckDB file (default: backend/data.duckdb)",
    )
    args = parser.parse_args()

    if not args.dataset_root.exists():
        print(f"ERROR: dataset root does not exist: {args.dataset_root}", file=sys.stderr)
        sys.exit(1)
    if not (args.dataset_root / "assets").exists():
        print(f"ERROR: no assets/ folder under {args.dataset_root}", file=sys.stderr)
        sys.exit(1)

    ingest(args.dataset_root, args.db_path)


if __name__ == "__main__":
    main()
