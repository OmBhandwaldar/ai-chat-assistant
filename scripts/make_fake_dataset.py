"""Build a corrupted / shuffled COPY of the sample dataset, reproducibly from a seed.

Rehearsal for PLAN.md Phase 8.1: the final judging window uses a different test dataset
of the same shape, so ingest must not depend on anything particular about the sample.
The original is never modified; the copy goes to --out (default /tmp/fake-test-data).

    uv run python scripts/make_fake_dataset.py                   # build the copy
    uv run python scripts/ingest.py /tmp/fake-test-data --db-path /tmp/fake-test.duckdb
    uv run python scripts/make_fake_dataset.py --check-db /tmp/fake-test.duckdb

Perturbations (each is recorded in <out>/_expected.json together with the row counts and
warning counts the ingest MUST end up with, so --check-db turns the rehearsal into a
pass/fail test instead of an eyeball job):
  * fewer / more wells than 200, new well ids, shuffled folder + row order;
  * extra unknown JSON fields everywhere, optional fields dropped or null;
  * empty files, missing files, an empty and a missing maintenance-reports dir,
    a well with zero inspections, wells with no / empty / corrupt metadata.json;
  * blank lines, a last line without newline, CRLF files, a BOM, a JSON array in place of JSONL;
  * a malformed line, a duplicate line, a row without id, a row with a bad timestamp;
  * maintenance rows pointing at missing report files, report files without a row, report_file
    values with backslashes / ./ prefixes / a wrong name (found again by file name or title);
  * unicode notes and reports; timestamps with / without timezone, offsets, fractions;
  * unknown well type (no manual), well in an area missing from areas.json, unknown team.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = (REPO_ROOT.parent / "flytbase-ahc-swe-asset-intelligence-dataset" / "sample-data")
DEFAULT_OUT = Path("/tmp/fake-test-data")
MARKER = ".fake-dataset"
EXPECTED_NAME = "_expected.json"

ID_RE = re.compile(r"(?<![A-Za-z0-9])(WELL|INS|MNT|REG|CTX)-(\d{4})(?![0-9])")
UNICODE_NOTE = "Fuite détectée près du séparateur — température élevée ✓ 日本語 😀 Ω"
UNICODE_MARK = "日本語"

# heavy perturbations, assigned to distinct wells in this order (cycling if few wells)
ROLES = [
    "crlf", "empty_context", "missing_context", "empty_reg", "zero_inspections", "unknown_type",
    "unknown_area", "sparse_metadata", "dup_and_bad_lines", "string_numbers",
    "json_array_maintenance", "missing_report_file", "orphan_report", "wrong_report_file",
    "backslash_report_file", "empty_reports_dir", "no_reports_dir",
]
REPORT_ROLES = {"missing_report_file", "orphan_report", "wrong_report_file",
                "backslash_report_file", "empty_reports_dir", "no_reports_dir"}


def remap(text: str, mapping: dict[str, str]) -> str:
    return ID_RE.sub(lambda m: f"{m.group(1)}-{mapping.get(m.group(2), m.group(2))}", text)


def parse_jsonl(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class Well:
    """One well folder held in memory, mutated by perturbations, then written."""

    def __init__(self, src_dir: Path, new_num: str):
        old_num = src_dir.name.split("-", 1)[1]
        mapping = {old_num: new_num}
        self.num = new_num
        self.asset_id = f"WELL-{new_num}"

        def read(name: str) -> str:
            p = src_dir / name
            return remap(p.read_text(encoding="utf-8"), mapping) if p.exists() else ""

        self.meta: dict = json.loads(read("metadata.json"))
        self.insp = parse_jsonl(read("inspections.jsonl"))
        self.mnt = parse_jsonl(read("maintenance.jsonl"))
        self.reg = parse_jsonl(read("regulatory-events.jsonl"))
        self.ctx = parse_jsonl(read("context.jsonl"))
        self.reports: dict[str, str] = {}
        rdir = src_dir / "maintenance-reports"
        if rdir.is_dir():
            for p in sorted(rdir.glob("*.md")):
                self.reports[remap(p.name, mapping)] = remap(p.read_text(encoding="utf-8"), mapping)
        # link status per maintenance_id: path | backslash | fallback | missing
        self.link: dict[str, str] = {}
        for m in self.mnt:
            rf = m.get("report_file")
            if rf:
                self.link[m["maintenance_id"]] = "path" if rf.split("/")[-1] in self.reports \
                    else "missing"
        self.orphans: set[str] = set()
        self.meta_dir_flags: set[str] = set()
        self.reports_dir_state = "normal"  # normal | empty | absent
        self.crlf = False
        self.bom_files: set[str] = set()
        self.array_files: set[str] = set()
        self.extra_lines: dict[str, list[str]] = {}
        self.missing_files: set[str] = set()
        self.empty_files: set[str] = set()


def pick_eligible(w: Well, status: str, k: int, rng: random.Random, stem_equals_id: bool = False
                  ) -> list[str]:
    mids = [mid for mid, st in sorted(w.link.items()) if st == status]
    if stem_equals_id:
        mids = [mid for mid in mids if f"{mid}.md" in w.reports]
    rng.shuffle(mids)
    return mids[:k]


def report_name(w: Well, mid: str) -> str:
    row = next(m for m in w.mnt if m["maintenance_id"] == mid)
    return str(row["report_file"]).replace("\\", "/").split("/")[-1]


def apply_role(role: str, w: Well, rng: random.Random, notes: Counter) -> None:
    """Mutate one well for a heavy perturbation; `notes` records expected warnings."""
    if role == "crlf":
        w.crlf = True
        w.bom_files.add("context.jsonl")
        w.meta_dir_flags.add("junk_in_reports")
    elif role == "empty_context":
        w.ctx = []
        w.empty_files.add("context.jsonl")
    elif role == "missing_context":
        w.ctx = []
        w.missing_files.add("context.jsonl")
    elif role == "empty_reg":
        w.reg = []
        w.empty_files.add("regulatory-events.jsonl")
    elif role == "zero_inspections":
        w.insp = []
        w.empty_files.add("inspections.jsonl")
    elif role == "unknown_type":
        w.meta["well_type"] = "T500"
    elif role == "unknown_area":
        w.meta["area_id"] = "NW-99"
    elif role == "sparse_metadata":
        for key in ("name", "location", "installed_on", "last_inspection", "recorded_status",
                    "in_charge_user_id", "area_id", "team_id", "asset_category"):
            w.meta.pop(key, None)
        w.meta_dir_flags.add("sparse")
    elif role == "dup_and_bad_lines":
        w.meta_dir_flags.add("dup_and_bad")
    elif role == "string_numbers":
        w.meta_dir_flags.add("string_numbers")
    elif role == "json_array_maintenance":
        w.array_files.add("maintenance.jsonl")
        notes["jsonl_was_json_array"] += 1
    elif role == "missing_report_file":
        for mid in pick_eligible(w, "path", 2, rng):
            del w.reports[report_name(w, mid)]
            w.link[mid] = "missing"
    elif role == "orphan_report":
        for mid in pick_eligible(w, "path", 2, rng):
            fname = report_name(w, mid)
            w.mnt = [m for m in w.mnt if m["maintenance_id"] != mid]
            del w.link[mid]
            w.orphans.add(fname)
        stray = f"stray-{w.num}-field-note.md"
        w.reports[stray] = (
            "# Maintenance Report — MNT-ORPHAN-1\n\nA report nobody listed in maintenance.jsonl.\n")
        w.orphans.add(stray)
    elif role == "wrong_report_file":
        for mid in pick_eligible(w, "path", 2, rng, stem_equals_id=True):
            next(m for m in w.mnt if m["maintenance_id"] == mid)["report_file"] = \
                f"maintenance-reports/renamed-away-{mid}.md"
            w.link[mid] = "fallback"
    elif role == "backslash_report_file":
        for i, mid in enumerate(pick_eligible(w, "path", 3, rng)):
            row = next(m for m in w.mnt if m["maintenance_id"] == mid)
            name = row["report_file"].split("/")[-1]
            row["report_file"] = (f"maintenance-reports\\{name}" if i % 2 == 0
                                  else f"./maintenance-reports/{name}")
            w.link[mid] = "backslash"
    elif role == "empty_reports_dir":
        w.reports_dir_state = "empty"
    elif role == "no_reports_dir":
        w.reports_dir_state = "absent"


def finalize_reports(w: Well) -> None:
    """Directory-level roles run last: no report file survives, every row's link is missing."""
    if w.reports_dir_state != "normal":
        w.reports.clear()
        w.orphans.clear()
        for mid in w.link:
            w.link[mid] = "missing"


# ---------------------------------------------------------------------------
# Per-row perturbations (apply to every well)
# ---------------------------------------------------------------------------


def ts_variants(dt: datetime, rng: random.Random) -> tuple[str, datetime]:
    """Return (string, expected naive-UTC datetime) for one of several timestamp styles."""
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    choice = rng.choices(
        ["z", "ist", "pdt", "naive", "frac3", "frac9", "space"], [40, 15, 10, 10, 10, 5, 10])[0]
    if choice == "z":
        return base + "Z", dt
    if choice == "ist":
        return (dt + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%dT%H:%M:%S") + "+05:30", dt
    if choice == "pdt":
        return (dt - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%S") + "-07:00", dt
    if choice == "naive":
        return base, dt
    if choice == "frac3":
        return base + ".123Z", dt + timedelta(milliseconds=123)
    if choice == "frac9":
        return base + ".123456789Z", dt + timedelta(microseconds=123456)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + "+00:00", dt


def parse_z(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(UTC).replace(tzinfo=None)


EXTRA_FIELD_FACTORIES = {
    "insp": lambda r: {"drone_serial": f"DR-{r.randint(1000, 9999)}",
                       "weather": {"wind_kph": r.randint(0, 40), "gusts": [1, 2, 3]},
                       "schema_version": 2},
    "mnt": lambda r: {"work_order": f"WO-{r.randint(10000, 99999)}", "priority": None,
                      "parts_used": [{"sku": f"P-{r.randint(1, 99)}", "qty": r.randint(1, 4)}]},
    "reg": lambda r: {"officer_badge": f"B{r.randint(100, 999)}", "attachments": []},
    "ctx": lambda r: {"visibility": "shared", "tags": ["a", "b"], "meta": {"k": None}},
}
DROPPABLE = {
    "insp": ["inspector_user_id", "method", "spill_area_pct", "spill_est_volume_bbl",
             "evidence_images", "notes"],
    "mnt": ["contractor", "triggered_by_inspection_id", "cost_usd", "downtime_hours",
            "action_taken", "technician_user_id", "completed_at"],
    "reg": ["fine_usd", "hours_to_report", "detail", "status", "related_inspection_id"],
    "ctx": ["valid_to", "valid_from", "author_user_id"],
}


def perturb_rows(kind: str, rows: list[dict], rng: random.Random,
                 canaries: dict, *, ts_fields: tuple[str, ...] = ()) -> None:
    for r in rows:
        if rng.random() < 0.3:
            r.update(EXTRA_FIELD_FACTORIES[kind](rng))
        if rng.random() < 0.25:
            for key in rng.sample(DROPPABLE[kind], k=rng.randint(1, 2)):
                if kind == "mnt" and key == "completed_at":
                    continue
                if kind == "ctx" and key == "valid_from" and r.get("note_type") == "correction":
                    continue
                if rng.random() < 0.5:
                    r.pop(key, None)
                else:
                    r[key] = None
        for f in ts_fields:
            if r.get(f) and rng.random() < 0.6:
                original = parse_z(r[f])
                text, expected = ts_variants(original, rng)
                r[f] = text
                if (kind == "insp" and f == "inspected_at" and text != original.strftime(
                        "%Y-%m-%dT%H:%M:%S") + "Z" and len(canaries["timestamps"]) < 60):
                    canaries["timestamps"][r["inspection_id"]] = expected.isoformat()
    if kind == "insp":
        for r in rng.sample(rows, k=min(len(rows), max(1, len(rows) // 30))):
            r["notes"] = UNICODE_NOTE
            if len(canaries["unicode_notes"]) < 20:
                canaries["unicode_notes"][r["inspection_id"]] = UNICODE_NOTE
    if kind == "ctx":
        for r in rows:
            if rng.random() < 0.3 and isinstance(r.get("text"), str):
                r["text"] += " — " + UNICODE_NOTE


# ---------------------------------------------------------------------------
# Writing files
# ---------------------------------------------------------------------------


def write_jsonl(path: Path, rows: list[dict], rng: random.Random, *, crlf: bool, bom: bool,
                as_array: bool, extra_lines: list[str]) -> None:
    ascii_only = rng.random() < 0.4
    if as_array:
        data = json.dumps(rows, ensure_ascii=ascii_only).encode("utf-8")
        path.write_bytes(data)
        return
    lines = [json.dumps(r, ensure_ascii=ascii_only) for r in rows]
    for extra in extra_lines:
        lines.insert(rng.randint(1, max(1, len(lines))), extra)
    out: list[str] = []
    for line in lines:
        out.append(line)
        if rng.random() < 0.03:
            out.append("")
            if rng.random() < 0.3:
                out.append("   ")
    eol = "\r\n" if crlf else "\n"
    text = eol.join(out)
    if rng.random() < 0.65 and out:
        text += eol
    data = text.encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" + data) if bom else data)


def emit_well(w: Well, out_dir: Path, rng: random.Random, canaries: dict) -> None:
    d = out_dir / "assets" / w.asset_id
    d.mkdir(parents=True)
    flags = w.meta_dir_flags

    # extra fields on metadata
    if rng.random() < 0.5:
        w.meta["commissioning"] = {"crew": ["a", "b"], "notes": None}
        w.meta["tags"] = ["pilot", "bakken"]
    meta_text = json.dumps(w.meta, indent=2, ensure_ascii=rng.random() < 0.5)
    if w.crlf:
        meta_text = meta_text.replace("\n", "\r\n")
    (d / "metadata.json").write_bytes(meta_text.encode("utf-8"))

    rows_by_file = {"inspections.jsonl": w.insp, "maintenance.jsonl": w.mnt,
                    "regulatory-events.jsonl": w.reg, "context.jsonl": w.ctx}
    for name, rows in rows_by_file.items():
        if name in w.missing_files:
            continue
        path = d / name
        if name in w.empty_files:
            path.write_bytes(b"")
            continue
        extra = list(w.extra_lines.get(name, []))
        if name == "inspections.jsonl" and rows and "dup_and_bad" in flags:
            extra += ["{this is not json", "[1, 2", '"just a string"', "null"]
            extra.append(json.dumps(rng.choice([r for r in rows if "inspection_id" in r])))
        write_jsonl(path, rows, rng, crlf=w.crlf, bom=name in w.bom_files,
                    as_array=name in w.array_files, extra_lines=extra)

    if w.reports_dir_state != "absent":
        rdir = d / "maintenance-reports"
        rdir.mkdir()
        for fname, body in w.reports.items():
            if w.crlf and fname == min(w.reports):
                body = body.replace("\n", "\r\n")
            (rdir / fname).write_text(body, encoding="utf-8", newline="")
        if "junk_in_reports" in flags:
            (rdir / "Thumbs.db").write_bytes(b"\x00\x01binary junk")
            (rdir / "notes.pdf").write_bytes(b"%PDF-not-a-report")


def prepare_well_rows(w: Well, rng: random.Random, canaries: dict, flags: set[str],
                      notes: Counter) -> None:
    """Row-level perturbations + injected rows for dup/bad-line wells."""
    perturb_rows("insp", w.insp, rng, canaries, ts_fields=("inspected_at",))
    perturb_rows("mnt", w.mnt, rng, canaries, ts_fields=("started_at", "completed_at"))
    perturb_rows("reg", w.reg, rng, canaries)
    perturb_rows("ctx", w.ctx, rng, canaries)
    # a few regulatory dates as full timestamps instead of plain dates
    for r in w.reg:
        if r.get("date") and rng.random() < 0.3:
            r["date"] = r["date"] + "T00:00:00Z"
    # unicode in a few reports
    for fname in rng.sample(sorted(w.reports), k=min(len(w.reports), max(0, len(w.reports) // 5))):
        w.reports[fname] += f"\n\nObservación: {UNICODE_NOTE}\n"

    if "dup_and_bad" in flags and w.insp:
        base = rng.choice(w.insp)
        bad_ts = dict(base, inspection_id=f"INS-{w.num}-BADTS", inspected_at="not-a-date")
        # injected rows go through extra_lines at write time; stash them there
        w.extra_lines.setdefault("inspections.jsonl", []).append(json.dumps(bad_ts))
        notes["timestamp_unparseable"] += 1
        notes["inspection_skipped_no_timestamp"] += 1
        notes["jsonl_line_malformed"] += 2
        notes["jsonl_line_not_object"] += 2
        notes["duplicate_inspections_key"] += 1
        noid = dict(rng.choice(w.insp))
        noid.pop("inspection_id")
        noid["inspected_at"] = "2030-01-01T00:00:00Z"
        w.insp.append(noid)  # valid row, id synthesised by ingest
        notes["inspection_id_synthesized"] += 1
    if "string_numbers" in flags and len(w.insp) >= 6:
        picks = [r for r in w.insp if isinstance(r.get("wellhead_temp_c"), (int, float))][:6]
        for r in picks[:5]:
            r["wellhead_temp_c"] = str(r["wellhead_temp_c"])
        if picks[5:]:
            picks[5]["wellhead_temp_c"] = "N/A"
            notes["value_not_numeric"] += 1


# ---------------------------------------------------------------------------
# Dataset assembly
# ---------------------------------------------------------------------------


def build(src: Path, out: Path, seed: int, n_wells: int) -> dict:
    if not (src / "assets").is_dir():
        raise SystemExit(f"source dataset not found: {src}")
    out = out.resolve()
    src = src.resolve()
    if out == src or src in out.parents or out in src.parents:
        raise SystemExit("--out must be outside the source dataset (the original is never touched)")
    if out.exists():
        if not (out / MARKER).exists() and any(out.iterdir()):
            raise SystemExit(f"refusing to overwrite non-empty {out} (no {MARKER} marker)")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / MARKER).write_text("generated by scripts/make_fake_dataset.py\n")

    rng = random.Random(seed)
    canaries: dict = {"timestamps": {}, "unicode_notes": {}, "well_area": {}}
    notes: Counter = Counter()

    # fleet: copy manuals + SOPs untouched, perturb the two JSON files
    shutil.copytree(src / "fleet", out / "fleet")
    areas_doc = json.loads((src / "fleet" / "areas.json").read_text())
    users_doc = json.loads((src / "fleet" / "users.json").read_text())

    src_wells = sorted(p for p in (src / "assets").iterdir() if p.is_dir())
    order = list(range(len(src_wells)))
    rng.shuffle(order)
    chosen: list[Path] = [src_wells[order[i % len(order)]] for i in range(n_wells)]

    used_nums: set[str] = set()
    wells: list[Well] = []
    for i, sdir in enumerate(chosen):
        orig = sdir.name.split("-", 1)[1]
        if i < len(order) and orig not in used_nums and rng.random() < 0.4:
            num = orig
        else:
            while True:
                num = f"{rng.randint(1000, 9999)}"
                if num not in used_nums:
                    break
        used_nums.add(num)
        wells.append(Well(sdir, num))

    # assign heavy roles (report roles need a well that has at least 3 reports)
    role_wells: dict[str, list[str]] = {}
    free = list(range(len(wells)))
    rng.shuffle(free)
    for r_i, role in enumerate(ROLES):
        cands = [i for i in free if (role not in REPORT_ROLES or len(wells[i].reports) >= 3)]
        if role in {"empty_reports_dir", "no_reports_dir"}:
            cands = [i for i in free if wells[i].link]
        if not cands:
            cands = [i for i in range(len(wells))
                     if role not in REPORT_ROLES or len(wells[i].reports) >= 3]
        if not cands:
            continue
        pick = cands[0]
        if pick in free:
            free.remove(pick)
        role_wells.setdefault(role, []).append(wells[pick].asset_id)
        apply_role(role, wells[pick], rng, notes)

    for w in wells:
        prepare_well_rows(w, rng, canaries, w.meta_dir_flags, notes)
        finalize_reports(w)

    # areas.json: rebuild rosters from the chosen wells, add phantom ids + an empty extra area
    original_area: dict[str, str] = {}
    for a in areas_doc["areas"]:
        for aid in a["asset_ids"]:
            original_area[aid] = a["area_id"]
    src_area_of = {}
    for sdir, w in zip(chosen, wells, strict=True):
        src_area_of[w.asset_id] = original_area.get(sdir.name)
    rosters: dict[str, list[str]] = {a["area_id"]: [] for a in areas_doc["areas"]}
    for w in wells:
        area = w.meta.get("area_id") or src_area_of.get(w.asset_id)
        if "sparse" in w.meta_dir_flags:
            canaries["well_area"][w.asset_id] = src_area_of.get(w.asset_id)
        if area in rosters:
            rosters[area].append(w.asset_id)
    for a in areas_doc["areas"]:
        a["asset_ids"] = rosters[a["area_id"]] + ["WELL-0000"]
        a["x_survey"] = {"year": 2019, "ok": True}
    areas_doc["areas"].append({"area_id": "EXTRA-01", "name": "Empty Extra Block",
                               "team_id": "TEAM-NE", "asset_ids": []})
    areas_doc["schema_version"] = 3
    users_doc["users"].append({"user_id": "U-999", "name": "No Role Person",
                               "team_id": "TEAM-XX", "phone": "+1-555-0100"})
    for u in users_doc["users"][:5]:
        u["x_badge"] = {"color": "blue"}
    notes["team_stub_created"] += 1
    (out / "fleet" / "areas.json").write_text(json.dumps(areas_doc, indent=2), encoding="utf-8")
    (out / "fleet" / "users.json").write_text(json.dumps(users_doc, indent=2), encoding="utf-8")

    (out / "assets").mkdir()
    for i in rng.sample(range(len(wells)), k=len(wells)):  # write order is irrelevant
        emit_well(wells[i], out, rng, canaries)

    # extra folders / files under assets/
    zero_num = next(f"{n}" for n in range(9001, 9999) if f"{n}" not in used_nums)
    zdir = out / "assets" / f"WELL-{zero_num}"
    zdir.mkdir()
    (zdir / "metadata.json").write_text(json.dumps({
        "asset_id": f"WELL-{zero_num}", "name": "Brand New Well", "asset_category": "oil_well",
        "well_type": "T350", "area_id": "SW-23", "team_id": "TEAM-SW", "installed_on": "2026-10-10",
        "equipment": [{"equipment_id": f"WELL-{zero_num}-WH", "kind": "wellhead",
                       "model": "WH-3000"}],
        "future_field": {"anything": [1, 2, 3]}}), encoding="utf-8")
    (zdir / "inspections.jsonl").write_bytes(b"")
    (out / "assets" / "WELL-BADMETA").mkdir()
    (out / "assets" / "WELL-BADMETA" / "metadata.json").write_text("{not json", encoding="utf-8")
    (out / "assets" / "WELL-EMPTYMETA").mkdir()
    (out / "assets" / "WELL-EMPTYMETA" / "metadata.json").write_bytes(b"")
    (out / "assets" / "WELL-NOMETA").mkdir()
    (out / "assets" / "WELL-NOMETA" / "inspections.jsonl").write_text("{}\n")
    (out / "assets" / "README.txt").write_text("not a well\n")
    (out / "assets" / ".DS_Store").write_bytes(b"\x00")

    if any(w.meta.get("well_type") == "T500" for w in wells):
        notes["well_type_rule_assumed"] += 1
    stub_area = any(w.meta.get("area_id") == "NW-99" for w in wells)
    if stub_area:
        notes["area_stub_created"] += 1
    notes["json_file_malformed"] += 1
    notes["json_file_empty"] += 1
    notes["well_skipped_no_metadata"] += 2
    notes["assets_folder_without_metadata"] += 1
    notes["assets_stray_file"] += 1

    # ---- expected counts, tallied from the final in-memory state
    linked = sum(1 for w in wells for st in w.link.values() if st != "missing")
    fallback = sum(1 for w in wells for st in w.link.values() if st == "fallback")
    missing = sum(1 for w in wells for st in w.link.values() if st == "missing")
    orphans = sum(len(w.orphans) for w in wells)
    notes["maintenance_report_file_missing"] += missing
    notes["report_linked_by_name_or_title"] += fallback
    notes["report_without_maintenance_row"] += orphans
    # the 'wrong_report_file' fallback only works while the stem-named file exists; (we only
    # pick rows whose stem == id, so a surviving file is always found)

    expected = {
        "seed": seed,
        "counts": {
            "wells": len(wells) + 1,
            "well_equipment": sum(len(w.meta.get("equipment") or []) for w in wells) + 1,
            "inspections": sum(len(w.insp) for w in wells),
            "maintenance": sum(len(w.mnt) for w in wells),
            "maintenance_reports": linked,
            "orphan_reports": orphans,
            "regulatory_events": sum(len(w.reg) for w in wells),
            "context_notes": sum(len(w.ctx) for w in wells),
            "areas": len(areas_doc["areas"]) + (1 if stub_area else 0),
            "teams": len(areas_doc.get("teams", [])) + 1,
            "users": len(users_doc["users"]),
        },
        "warnings": {k: v for k, v in sorted(notes.items()) if v},
        "roles": role_wells,
        "canaries": canaries,
    }
    (out / EXPECTED_NAME).write_text(json.dumps(expected, indent=2), encoding="utf-8")
    return expected


# ---------------------------------------------------------------------------
# --check-db
# ---------------------------------------------------------------------------


def check_db(db_path: Path, expected: dict) -> list[str]:
    import duckdb

    problems: list[str] = []
    con = duckdb.connect(str(db_path), read_only=True)
    for table, want in expected["counts"].items():
        got = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if got != want:
            problems.append(f"count {table}: got {got}, expected {want}")
    warned = dict(con.execute("SELECT category, n FROM ingest_warnings").fetchall())
    for cat, want in expected["warnings"].items():
        if warned.get(cat, 0) != want:
            problems.append(f"warning {cat}: got {warned.get(cat, 0)}, expected {want}")
    for cat in sorted(set(warned) - set(expected["warnings"])):
        problems.append(f"unexpected warning {cat}: {warned[cat]}")
    for iid, iso in expected["canaries"]["timestamps"].items():
        row = con.execute("SELECT inspected_at FROM inspections WHERE inspection_id = ?",
                          [iid]).fetchone()
        if row is None or row[0] != datetime.fromisoformat(iso):
            problems.append(f"timestamp {iid}: got {row and row[0]}, expected {iso}")
    for iid, note in expected["canaries"]["unicode_notes"].items():
        row = con.execute("SELECT notes FROM inspections WHERE inspection_id = ?", [iid]).fetchone()
        if row is None or row[0] != note:
            problems.append(f"unicode note {iid} damaged: {row}")
    for asset, area in expected["canaries"]["well_area"].items():
        row = con.execute("SELECT area_id FROM wells WHERE asset_id = ?", [asset]).fetchone()
        if row is None or row[0] != area:
            problems.append(f"roster area for {asset}: got {row}, expected {area}")
    crs = con.execute("SELECT COUNT(*) FROM maintenance_reports WHERE contains(body, chr(13))"
                      ).fetchone()[0]
    if crs:
        problems.append(f"{crs} report bodies still contain carriage returns")
    con.close()
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=Path(os.environ.get("DATASET_ROOT") or DEFAULT_SRC),
                    help="clean sample dataset (default: $DATASET_ROOT or the repo's sibling "
                         "flytbase-ahc-swe-asset-intelligence-dataset/sample-data)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--seed", type=int, default=20261017)
    ap.add_argument("--wells", type=int, default=160, help="number of wells in the copy")
    ap.add_argument("--check-db", type=Path, default=None,
                    help="instead of generating: compare an ingested DB with <out>/_expected.json")
    args = ap.parse_args(argv)

    if args.check_db:
        expected = json.loads((args.out / EXPECTED_NAME).read_text())
        problems = check_db(args.check_db, expected)
        for p in problems:
            print("FAIL", p)
        print("rehearsal check:", "FAILED" if problems else "all expectations met")
        return 1 if problems else 0

    src = args.src
    if not src.exists() and not src.is_absolute() and (REPO_ROOT / src).exists():
        src = REPO_ROOT / src
    expected = build(src, args.out, args.seed, args.wells)
    print(f"Wrote {args.out} (seed {args.seed}, {expected['counts']['wells']} wells)")
    print("Expected counts:", json.dumps(expected["counts"]))
    print("Expected warnings:", json.dumps(expected["warnings"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
