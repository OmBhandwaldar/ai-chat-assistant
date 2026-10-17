/**
 * Tolerant normalisers for `visual.data`. The backend does not enforce a per-type schema,
 * so every visual component funnels its raw `data` through one of these. They never throw:
 * unusable input yields an empty result, which the component renders as a muted "No data".
 */
import type { Conflict, MapPin, WellState } from "@/lib/types";

type Obj = Record<string, unknown>;

export const isObj = (v: unknown): v is Obj =>
  typeof v === "object" && v !== null && !Array.isArray(v);

const asObj = (v: unknown): Obj => (isObj(v) ? v : {});

export function toNumber(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/** "wellhead_temp_c" -> "Wellhead temp c"; keeps already-pretty labels. */
export function humanize(key: string): string {
  const s = key.replace(/[_-]+/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : key;
}

/** Render any cell value as short text. */
export function formatCell(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/\.?0+$/, "");
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "string") return /^\d{4}-\d{2}-\d{2}T00:00:00/.test(v) ? v.slice(0, 10) : v;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

const ROW_KEYS = ["rows", "wells", "points", "items", "data", "records", "results"];

/** First array of objects found under a conventional key (or any key). */
function findRowObjects(data: Obj): Obj[] | null {
  const candidates = [...ROW_KEYS, ...Object.keys(data)];
  for (const k of candidates) {
    const v = data[k];
    if (Array.isArray(v) && v.length > 0 && v.every(isObj)) return v as Obj[];
  }
  return null;
}

// ---------------------------------------------------------------------------
// table
// ---------------------------------------------------------------------------

export interface TableColumn {
  key: string;
  label: string;
}
export interface NormalizedTable {
  columns: TableColumn[];
  rows: Obj[];
}

export function normalizeTable(data: unknown): NormalizedTable {
  const d = asObj(data);
  const rawCols = Array.isArray(d.columns) ? d.columns : null;
  let cols: TableColumn[] | null = rawCols
    ? rawCols
        .map((c): TableColumn | null => {
          if (typeof c === "string") return { key: c, label: humanize(c) };
          if (isObj(c) && typeof c.key === "string") {
            return { key: c.key, label: typeof c.label === "string" ? c.label : humanize(c.key) };
          }
          return null;
        })
        .filter((c): c is TableColumn => c !== null)
    : null;

  let rows: Obj[] = [];
  const rawRows = Array.isArray(d.rows) ? d.rows : null;
  if (rawRows && rawRows.length > 0 && rawRows.every(Array.isArray)) {
    // Array-of-arrays: align to columns by position.
    const keys = cols?.map((c) => c.key) ?? (rawRows[0] as unknown[]).map((_, i) => `col_${i + 1}`);
    if (!cols) cols = keys.map((k) => ({ key: k, label: humanize(k) }));
    rows = (rawRows as unknown[][]).map((r) => Object.fromEntries(keys.map((k, i) => [k, r[i]])));
  } else {
    rows = (rawRows && rawRows.every(isObj) ? (rawRows as Obj[]) : findRowObjects(d)) ?? [];
  }

  if (!cols || cols.length === 0) {
    const keys = new Set<string>();
    rows.slice(0, 20).forEach((r) => Object.keys(r).forEach((k) => keys.add(k)));
    cols = [...keys].map((k) => ({ key: k, label: humanize(k) }));
  }
  return { columns: cols, rows };
}

// ---------------------------------------------------------------------------
// line / bar
// ---------------------------------------------------------------------------

export interface NormalizedSeries {
  index: string;
  /** Rows keyed by `index` and by each category's *display label*. */
  rows: Obj[];
  categories: string[];
  /** Raw category key -> label, so callers can detect e.g. "cost" for currency formatting. */
  rawCategories: string[];
}

const INDEX_PREFERENCE = ["inspected_at", "date", "ts", "time", "week", "month", "name", "asset_id", "label"];

/** `{ "WELL-1": 120.5, ... }` -> rows. Used for repair_costs-style `costs` maps. */
function mapToRows(m: Obj, valueKey: string): Obj[] {
  return Object.entries(m)
    .filter(([, v]) => toNumber(v) !== null)
    .map(([k, v]) => ({ asset_id: k, [valueKey]: toNumber(v) }));
}

export function normalizeSeries(data: unknown): NormalizedSeries {
  const d = asObj(data);
  let rows = findRowObjects(d);
  if (!rows && isObj(d.costs)) rows = mapToRows(d.costs, "cost_total");
  if (!rows) return { index: "", rows: [], categories: [], rawCategories: [] };

  const first = rows[0];
  const keys = Object.keys(first);

  let index = typeof d.index === "string" && d.index in first ? d.index : "";
  if (!index) index = INDEX_PREFERENCE.find((k) => k in first && toNumber(first[k]) === null) ?? "";
  if (!index) index = keys.find((k) => typeof first[k] === "string") ?? keys[0];

  let cats: string[] = Array.isArray(d.categories)
    ? d.categories.filter((c): c is string => typeof c === "string" && c in first)
    : [];
  if (cats.length === 0) {
    cats = keys.filter(
      (k) => k !== index && typeof first[k] !== "boolean" && toNumber(first[k]) !== null
    );
    // Prefer the meaningful measure over id-like numerics when both exist.
    const measures = cats.filter((k) => !/(^|_)(id|lat|lon|threshold|lookback_days)$/.test(k));
    if (measures.length > 0) cats = measures;
  }

  const out = rows.map((r) => {
    const o: Obj = { [index]: formatCell(r[index]) };
    cats.forEach((c) => {
      o[humanize(c)] = toNumber(r[c]) ?? 0;
    });
    return o;
  });
  return {
    index,
    rows: out,
    categories: cats.map(humanize),
    rawCategories: cats,
  };
}

// ---------------------------------------------------------------------------
// status_card
// ---------------------------------------------------------------------------

export interface NormalizedStatus {
  title: string;
  state: WellState;
  reasons: string[];
  conflicts: Conflict[];
  metrics: { label: string; value: string }[];
}

export function normalizeStatus(data: unknown): NormalizedStatus | null {
  const d = asObj(data);
  const rawState = typeof d.state === "string" ? d.state.toLowerCase().replace(/[\s-]+/g, "_") : "";
  const hasContent = rawState || (Array.isArray(d.reasons) && d.reasons.length) || d.asset_id || d.title;
  if (!hasContent) return null;

  const state: WellState =
    rawState === "producing" || rawState === "not_producing" ? rawState : "unknown";
  const strings = (v: unknown) => (Array.isArray(v) ? v.map(String).filter(Boolean) : []);
  const conflicts = Array.isArray(d.conflicts)
    ? d.conflicts.filter(isObj).map((c) => ({
        type: String(c.type ?? "conflict"),
        left: String(c.left ?? ""),
        right: String(c.right ?? ""),
      }))
    : [];
  const metrics = Array.isArray(d.metrics)
    ? d.metrics
        .filter(isObj)
        .map((m) => ({ label: String(m.label ?? ""), value: formatCell(m.value) }))
        .filter((m) => m.label)
    : [];
  return {
    title: String(d.title ?? d.asset_id ?? "Well"),
    state,
    reasons: strings(d.reasons),
    conflicts,
    metrics,
  };
}

// ---------------------------------------------------------------------------
// map
// ---------------------------------------------------------------------------

export function normalizePins(data: unknown): MapPin[] {
  const d = asObj(data);
  const raw = Array.isArray(d.pins) ? d.pins : Array.isArray(d.rows) ? d.rows : Array.isArray(d.wells) ? d.wells : [];
  const pins: MapPin[] = [];
  for (const p of raw) {
    if (!isObj(p)) continue;
    const lat = toNumber(p.lat ?? p.latitude);
    const lon = toNumber(p.lon ?? p.lng ?? p.longitude);
    if (lat === null || lon === null || Math.abs(lat) > 90 || Math.abs(lon) > 180) continue;
    pins.push({
      lat,
      lon,
      name: typeof p.name === "string" ? p.name : undefined,
      asset_id: typeof p.asset_id === "string" ? p.asset_id : undefined,
      date: typeof p.date === "string" ? p.date : undefined,
      bbl: toNumber(p.bbl) ?? undefined,
    });
  }
  return pins;
}

// ---------------------------------------------------------------------------
// heatmap
// ---------------------------------------------------------------------------

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export interface HeatmapSeries {
  id: string;
  data: { x: string; y: number }[];
}

export function normalizeHeatmap(data: unknown): HeatmapSeries[] {
  const d = asObj(data);
  const monthLabel = (m: unknown) => {
    const n = toNumber(m);
    return n !== null && Number.isInteger(n) && n >= 1 && n <= 12 ? MONTHS[n - 1] : String(m);
  };

  // Already nivo-shaped: rows/series = [{id, data:[{x,y}]}]
  const nivo = [d.rows, d.series, d.data].find(
    (v): v is Obj[] => Array.isArray(v) && v.length > 0 && v.every((r) => isObj(r) && Array.isArray(r.data))
  );
  if (nivo) {
    return nivo.map((r) => ({
      id: String(r.id ?? ""),
      data: (r.data as unknown[]).filter(isObj).map((c) => ({
        x: monthLabel(c.x),
        y: toNumber(c.y) ?? 0,
      })),
    }));
  }

  // spill_heatmap shape: matrix[area][month]
  const matrix = asObj(d.matrix);
  const areas = Array.isArray(d.areas) ? d.areas.map(String) : Object.keys(matrix);
  const months = Array.isArray(d.months) && d.months.length ? d.months : Array.from({ length: 12 }, (_, i) => i + 1);
  return areas
    .filter((a) => a in matrix || Array.isArray(d.areas))
    .map((a) => ({
      id: a,
      data: months.map((m) => ({ x: monthLabel(m), y: toNumber(asObj(matrix[a])[String(m)]) ?? 0 })),
    }));
}
