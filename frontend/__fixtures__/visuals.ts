import type { UIVisual } from "@/lib/types";

export interface VisualFixture {
  name: string;
  visual: UIVisual;
}

/** Fixtures mirror what the backend tools return (and the canonical shapes in lib/types.ts). */
export const visualFixtures: VisualFixture[] = [
  {
    name: "status_card — not producing (well_status_now shape)",
    visual: {
      type: "status_card",
      data: {
        asset_id: "WELL-0164",
        state: "not_producing",
        reasons: [
          "Wellhead 38.2 °C is only 4.1 °C above ambient (needs ≥ 20 °C for T350) — INS-0164-0201",
          "Open repair MNT-0164-0007, pump jack gearbox, started 2026-10-09",
        ],
        conflicts: [
          { type: "claim_vs_data", left: "Recorded status: Active", right: "Latest inspection: not producing" },
        ],
      },
    },
  },
  {
    name: "status_card — producing (canonical, with metrics)",
    visual: {
      type: "status_card",
      data: {
        title: "WELL-0007",
        state: "producing",
        reasons: ["Wellhead 71.0 °C is 31.5 °C above ambient — INS-0007-0203"],
        metrics: [
          { label: "Wellhead", value: "71.0 °C" },
          { label: "Ambient", value: "39.5 °C" },
        ],
      },
    },
  },
  {
    name: "table — canonical columns + object rows",
    visual: {
      type: "table",
      data: {
        columns: [
          { key: "asset_id", label: "Well" },
          { key: "well_type", label: "Type" },
          { key: "delta_c", label: "Δ temp (°C)" },
          { key: "reason", label: "Reason" },
        ],
        rows: [
          { asset_id: "WELL-0164", well_type: "T350", delta_c: 4.1, reason: "Repair MNT-0164-0007, still open" },
          { asset_id: "WELL-0121", well_type: "T200", delta_c: 9.8, reason: "No recorded reason." },
          { asset_id: "WELL-0007", well_type: "T200", delta_c: 12.25, reason: "Hot-work permit hold" },
          { asset_id: "WELL-0033", well_type: "T350", delta_c: 2, reason: null },
        ],
      },
    },
  },
  {
    name: "table — raw tool shape (wells_not_producing: no columns)",
    visual: {
      type: "table",
      data: {
        as_of: "2026-10-16",
        wells: [
          { asset_id: "WELL-0164", name: "Eagle 164", well_type: "T350", delta_c: 4.1, recorded_status: "active (out of date)" },
          { asset_id: "WELL-0121", name: "Falcon 121", well_type: "T200", delta_c: 9.8, recorded_status: "shut_in" },
        ],
      },
    },
  },
  {
    name: "line — raw tool shape (temp_timeseries points)",
    visual: {
      type: "line",
      data: {
        asset_id: "WELL-0164",
        points: [
          ["2026-07-24", 78.1, 36.0],
          ["2026-07-31", 80.4, 37.2],
          ["2026-08-07", 79.0, 38.0],
          ["2026-08-14", 52.3, 38.4],
          ["2026-08-21", 49.9, 37.5],
          ["2026-08-28", 77.2, 36.8],
          ["2026-09-04", 79.5, 35.1],
        ].map(([inspected_at, wellhead_temp_c, ambient_temp_c], i) => ({
          inspection_id: `INS-0164-${200 + i}`,
          inspected_at: `${inspected_at}T00:00:00`,
          wellhead_temp_c,
          ambient_temp_c,
          is_producing: (wellhead_temp_c as number) - (ambient_temp_c as number) >= 20,
        })),
      },
    },
  },
  {
    name: "bar — raw tool shape (top_downtime wells)",
    visual: {
      type: "bar",
      data: {
        start: "2026-01-01",
        end: "2026-09-30",
        wells: [
          { asset_id: "WELL-0121", name: "Falcon 121", well_type: "T200", downtime_days: 49 },
          { asset_id: "WELL-0164", name: "Eagle 164", well_type: "T350", downtime_days: 35 },
          { asset_id: "WELL-0007", name: "Harbor 7", well_type: "T200", downtime_days: 21 },
        ],
      },
    },
  },
  {
    name: "bar — repair_costs shape (costs map)",
    visual: {
      type: "bar",
      data: { year: 2026, costs: { "WELL-0007": 12450.5, "WELL-0121": 8300, "WELL-0164": 21975.25 } },
    },
  },
  {
    name: "map — spill_map pins",
    visual: {
      type: "map",
      data: {
        start: "2026-07-18",
        end: "2026-10-16",
        pins: [
          { asset_id: "WELL-0007", name: "Harbor 7", date: "2026-08-03", bbl: 2.4, lat: 31.9686, lon: -102.0779 },
          { asset_id: "WELL-0121", name: "Falcon 121", date: "2026-09-12", bbl: 5.1, lat: 32.0453, lon: -101.9602 },
          { asset_id: "WELL-0164", name: "Eagle 164", date: "2026-10-01", bbl: 0.8, lat: 31.8801, lon: -102.2314 },
        ],
      },
    },
  },
  {
    name: "heatmap — spill_heatmap matrix",
    visual: {
      type: "heatmap",
      data: {
        year: 2026,
        areas: ["Eagle Flats", "Harbor", "North Ridge"],
        months: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
        matrix: {
          "Eagle Flats": { 1: 1, 3: 2, 6: 4, 9: 1 },
          Harbor: { 2: 3, 3: 1, 8: 5, 10: 2 },
          "North Ridge": { 5: 1, 7: 2 },
        },
        totals: { "Eagle Flats": 8, Harbor: 11, "North Ridge": 3 },
      },
    },
  },
  { name: "empty — status_card", visual: { type: "status_card", data: {} } },
  { name: "empty — table", visual: { type: "table", data: { columns: ["a"], rows: [] } } },
  { name: "empty — line", visual: { type: "line", data: { points: [] } } },
  { name: "empty — bar", visual: { type: "bar", data: {} } },
  { name: "empty — map", visual: { type: "map", data: { pins: [] } } },
  { name: "empty — heatmap", visual: { type: "heatmap", data: { areas: [], matrix: {} } } },
  {
    name: "malformed — unknown type",
    visual: { type: "pie" as UIVisual["type"], data: { x: 1 } },
  },
  {
    name: "malformed — garbage data",
    visual: { type: "bar", data: { wells: "not-an-array", costs: 7 } },
  },
];
