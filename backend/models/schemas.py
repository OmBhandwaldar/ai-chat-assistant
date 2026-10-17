"""Pydantic models: Evidence, ToolResult, WellState, UIVisual, AgentAnswer."""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_type: Literal[
        "inspection", "maintenance", "report", "context",
        "regulatory", "sop", "manual", "override", "claim",
    ]


class ToolResult(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    data: T
    evidence: list[Evidence] = Field(default_factory=list)


class Conflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    left: str
    right: str


class WellState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["producing", "not_producing", "unknown"]
    reasons: list[str] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)


class UIVisual(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["status_card", "table", "line", "bar", "map", "heatmap", "none"]
    data: dict = Field(default_factory=dict)


class AgentAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    visual: UIVisual
    citations: list[Evidence] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)


# --- Plain mirrors of the DuckDB tables (see backend/db/schema.sql) ---
# Used for typed reads out of the database and as tool-return payloads.


class Team(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: str
    name: str
    lead_user_id: str | None = None


class Area(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area_id: str
    name: str
    team_id: str | None = None
    lat: float | None = None
    lon: float | None = None


class User(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    name: str
    role: str
    team_id: str | None = None


class WellTypeRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    well_type: str
    producing_threshold_c: float
    max_wellhead_c: float
    rca_corrective_count: int = 3
    rca_lookback_days: int = 365


class Well(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    name: str
    well_type: str
    area_id: str | None = None
    team_id: str | None = None
    in_charge_user_id: str | None = None
    lat: float | None = None
    lon: float | None = None
    installed_on: str | None = None
    recorded_status: str | None = None
    last_inspection_id: str | None = None


class Inspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inspection_id: str
    asset_id: str
    inspected_at: str
    inspector_user_id: str | None = None
    method: str | None = None
    ambient_temp_c: float | None = None
    wellhead_temp_c: float | None = None
    spill_detected: bool | None = None
    spill_area_pct: float | None = None
    spill_est_volume_bbl: float | None = None
    notes: str | None = None


class MaintenanceJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maintenance_id: str
    asset_id: str
    started_at: str | None = None
    completed_at: str | None = None
    maintenance_type: str | None = None
    equipment_kind: str | None = None
    issue: str | None = None
    action_taken: str | None = None
    technician_user_id: str | None = None
    contractor: str | None = None
    downtime_hours: float | None = None
    cost_labor: float | None = None
    cost_parts: float | None = None
    cost_total: float | None = None
    triggered_by_inspection_id: str | None = None
    report_file: str | None = None


class RegulatoryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    asset_id: str
    date: str | None = None
    agency: str | None = None
    event_type: str | None = None
    protocol_id: str | None = None
    related_inspection_id: str | None = None
    hours_to_report: float | None = None
    detail: str | None = None
    fine_usd: float | None = None
    status: str | None = None


class ContextNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note_id: str
    asset_id: str
    created_at: str
    author_user_id: str | None = None
    note_type: str
    inspection_id: str | None = None
    text: str
    valid_from: str | None = None
    valid_to: str | None = None
    source: str = "dataset"
