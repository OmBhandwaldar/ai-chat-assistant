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
