export type UserRole =
  | "production_supervisor"
  | "field_operator"
  | "field_inspector"
  | "maintenance_lead"
  | "maintenance_technician"
  | "hse_officer";

export interface AppUser {
  user_id: string;
  name: string;
  role: UserRole;
  team_id: string;
}

export interface ConversationSummary {
  conversation_id: string;
  user_id: string;
  title: string;
  updated_at: string;
}

export type VisualType =
  | "status_card"
  | "table"
  | "line"
  | "bar"
  | "map"
  | "heatmap"
  | "none";

export interface Evidence {
  source_id: string;
  source_type:
    | "inspection"
    | "maintenance"
    | "report"
    | "context"
    | "regulatory"
    | "sop"
    | "manual"
    | "override"
    | "claim";
}

export interface Conflict {
  type: string;
  left: string;
  right: string;
}

export interface UIVisual {
  type: VisualType;
  data: Record<string, unknown>;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  visual?: UIVisual;
  citations?: Evidence[];
  conflicts?: Conflict[];
  thoughtSeconds?: number;
  streaming?: boolean;
  ts: string;
}

// ---------------------------------------------------------------------------
// Canonical `visual.data` shapes (one per visual type).
//
// The backend types `UIVisual.data` as a free-form dict, so the LLM may copy a tool's
// raw result straight into it. Components therefore normalise through lib/visual-data.ts,
// which accepts these canonical shapes AND the raw tool shapes
// (wells[], points[], pins[], costs{}, matrix{}). Prefer the canonical form.
// ---------------------------------------------------------------------------

export type WellState = "producing" | "not_producing" | "unknown";

/** status_card — from well_status_now: {asset_id, state, reasons[], conflicts[]}. */
export interface StatusCardData {
  title?: string;
  asset_id?: string;
  state: WellState;
  reasons?: string[];
  conflicts?: Conflict[];
  /** Optional extra key figures shown under the state, e.g. [{label:"Delta", value:"4.2 °C"}]. */
  metrics?: { label: string; value: string | number }[];
}

/** table — columns are keys or {key,label}; rows are objects (or arrays aligned to columns). */
export interface TableData {
  columns?: (string | { key: string; label?: string })[];
  rows: Record<string, unknown>[] | unknown[][];
}

/** line / bar — one object per x position; `categories` are the numeric series keys. */
export interface SeriesData {
  index: string;
  categories: string[];
  rows: Record<string, unknown>[];
}

export interface MapPin {
  lat: number;
  lon: number;
  name?: string;
  asset_id?: string;
  date?: string;
  bbl?: number;
}

/** map — from spill_map: {pins: MapPin[]}. */
export interface MapData {
  pins: MapPin[];
}

/** heatmap — from spill_heatmap: matrix[area][month] = count. */
export interface HeatmapData {
  areas: string[];
  months: (string | number)[];
  matrix: Record<string, Record<string, number>>;
}
