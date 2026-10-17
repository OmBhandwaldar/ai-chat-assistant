"""Tiny isolated ingest rehearsal: perturbed files, no embeddings, no real DB writes.

See PLAN.md Phase 8.1. The dataset lives in pytest's tmp dir and the DuckDB file is
created beside it. backend/data.duckdb is hashed before and after and must not change.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
REAL_DB = ROOT / "backend" / "data.duckdb"

_spec = importlib.util.spec_from_file_location("ingest_mod", ROOT / "scripts" / "ingest.py")
assert _spec and _spec.loader
ingest_mod = importlib.util.module_from_spec(_spec)
sys.modules["ingest_mod"] = ingest_mod
_spec.loader.exec_module(ingest_mod)


def _md5(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_tiny_dataset(root: Path) -> None:
    """A few wells covering the perturbations Phase 8.1 calls out."""
    _write(
        root / "fleet" / "areas.json",
        json.dumps({
            "schema_version": 99,
            "teams": [{
                "team_id": "TEAM-A", "name": "Alpha", "lead_user_id": "U-1",
                "extra_flag": True,
            }],
            "areas": [{
                "area_id": "AR-1", "name": "Area 1", "team_id": "TEAM-A",
                "center": {"lat": 31.0, "lon": -102.0}, "bonus": "ignored",
            }],
        }),
    )
    _write(
        root / "fleet" / "users.json",
        json.dumps({"users": [{
            "user_id": "U-1", "name": "Ada", "role": "operator", "team_id": "TEAM-A",
        }]}),
    )
    _write(
        root / "fleet" / "well-types" / "T200.md",
        "A well is producing when wellhead temperature is at least 15°C above ambient.\n"
        "Alarm when wellhead temperature is at or below 85°C.\n",
    )

    _write(
        root / "assets" / "WELL-OK" / "metadata.json",
        json.dumps({
            "asset_id": "WELL-OK", "name": "Ok Well", "well_type": "T200",
            "area_id": "AR-1", "team_id": "TEAM-A", "in_charge_user_id": "U-1",
            "location": {"lat": 31.1, "lon": -102.1}, "installed_on": "2020-01-01",
            "recorded_status": "active",
            "future_field": {"sensor": "x", "readings": [1, 2]},
            "equipment": [{
                "equipment_id": "EQ1", "kind": "wellhead", "model": "WH", "vendor_sku": "Z9",
            }],
        }),
    )
    _write(
        root / "assets" / "WELL-OK" / "inspections.jsonl",
        json.dumps({
            "inspection_id": "INS-1", "asset_id": "WELL-OK",
            "inspected_at": "2026-10-01T12:00:00Z", "inspector_user_id": "U-1",
            "method": "drone", "ambient_temp_c": 20, "wellhead_temp_c": 40,
            "spill_detected": False, "notes": "ok", "sensor_vendor": "acme",
        }) + "\n",
    )
    _write(
        root / "assets" / "WELL-OK" / "maintenance.jsonl",
        json.dumps({
            "maintenance_id": "MNT-1", "asset_id": "WELL-OK",
            "started_at": "2026-09-01T00:00:00Z", "maintenance_type": "corrective",
            "equipment_kind": "wellhead", "issue": "leak",
            "report_file": "maintenance-reports/MNT-1.md", "unused": 1,
        }) + "\n",
    )
    _write(
        root / "assets" / "WELL-OK" / "maintenance-reports" / "MNT-1.md",
        "# Maintenance Report — MNT-1\n\nFixed the leak.\n",
    )
    (root / "assets" / "WELL-OK" / "maintenance-reports" / "empty.md").write_bytes(b"")

    # Missing optional metadata, empty JSONL (zero inspections), report_file that is absent.
    _write(
        root / "assets" / "WELL-SPARSE" / "metadata.json",
        json.dumps({
            "asset_id": "WELL-SPARSE", "well_type": "T200",
            "area_id": "AR-1", "team_id": "TEAM-A", "mystery": None,
        }),
    )
    (root / "assets" / "WELL-SPARSE" / "inspections.jsonl").write_bytes(b"")
    (root / "assets" / "WELL-SPARSE" / "context.jsonl").write_bytes(b"")
    _write(
        root / "assets" / "WELL-SPARSE" / "maintenance.jsonl",
        json.dumps({
            "maintenance_id": "MNT-MISS",
            "started_at": "2026-08-01T00:00:00Z", "maintenance_type": "scheduled",
            "report_file": "maintenance-reports/does-not-exist.md",
        }) + "\n",
    )

    # Report on disk with no maintenance row.
    _write(
        root / "assets" / "WELL-ORPHAN" / "metadata.json",
        json.dumps({
            "asset_id": "WELL-ORPHAN", "name": "Orphan Well", "well_type": "T200",
            "area_id": "AR-1", "team_id": "TEAM-A",
        }),
    )
    _write(
        root / "assets" / "WELL-ORPHAN" / "inspections.jsonl",
        json.dumps({
            "inspection_id": "INS-ORPH", "asset_id": "WELL-ORPHAN",
            "inspected_at": "2026-10-02T08:00:00Z",
            "ambient_temp_c": 10, "wellhead_temp_c": 12, "spill_detected": False,
        }) + "\n",
    )
    (root / "assets" / "WELL-ORPHAN" / "maintenance.jsonl").write_bytes(b"")
    _write(
        root / "assets" / "WELL-ORPHAN" / "maintenance-reports" / "stray-note.md",
        "# Field note\n\nA report nobody listed in maintenance.jsonl.\n",
    )

    # Empty metadata is a well folder that cannot be loaded.
    empty_meta = root / "assets" / "WELL-EMPTYMETA" / "metadata.json"
    empty_meta.parent.mkdir(parents=True, exist_ok=True)
    empty_meta.write_bytes(b"")
    (root / "assets" / "not-a-well").mkdir(parents=True)
    _write(root / "assets" / "readme.txt", "not a well\n")


def test_tiny_perturbed_ingest_skips_embeddings(tmp_path: Path) -> None:
    dataset = tmp_path / "tiny"
    db_path = tmp_path / "tiny.duckdb"
    _build_tiny_dataset(dataset)
    before = _md5(REAL_DB)

    res = ingest_mod.ingest(dataset, db_path, skip_embeddings=True, verbose=False)

    assert _md5(REAL_DB) == before
    assert res.db_path == db_path
    assert db_path.is_file()
    assert res.chroma_path is None
    assert res.vector_count is None
    assert not (ROOT / "backend" / "data.duckdb.ingest-tmp").exists()

    assert res.counts["wells"] == 3
    assert res.counts["well_equipment"] == 1
    assert res.counts["inspections"] == 2
    assert res.counts["maintenance"] == 2
    assert res.counts["maintenance_reports"] == 1
    assert res.counts["orphan_reports"] == 1
    assert res.counts["regulatory_events"] == 0
    assert res.counts["context_notes"] == 0
    assert res.counts["areas"] == 1
    assert res.counts["teams"] == 1
    assert res.counts["users"] == 1
    assert res.counts["report_chunks"] == 0

    assert res.warnings == {
        "assets_folder_without_metadata": 1,
        "assets_stray_file": 1,
        "json_file_empty": 1,
        "maintenance_report_file_missing": 1,
        "report_file_empty": 1,
        "report_without_maintenance_row": 1,
        "well_skipped_no_metadata": 1,
    }

    con = duckdb.connect(str(db_path), read_only=True)
    sparse = con.execute(
        "SELECT name, lat, lon, installed_on, recorded_status, in_charge_user_id "
        "FROM wells WHERE asset_id = 'WELL-SPARSE'"
    ).fetchone()
    assert sparse == ("WELL-SPARSE", None, None, None, None, None)
    n_insp = con.execute(
        "SELECT COUNT(*) FROM inspections WHERE asset_id = 'WELL-SPARSE'"
    ).fetchone()[0]
    assert n_insp == 0
    warned = dict(con.execute("SELECT category, n FROM ingest_warnings").fetchall())
    assert warned == res.warnings
    con.close()
