"""Round-trip tests for backend/models/schemas.py."""

from backend.models.schemas import (
    AgentAnswer,
    Area,
    ContextNote,
    Conflict,
    Evidence,
    Inspection,
    MaintenanceJob,
    RegulatoryEvent,
    Team,
    ToolResult,
    UIVisual,
    User,
    Well,
    WellState,
    WellTypeRules,
)

MODELS_AND_PAYLOADS = [
    (Evidence, {"source_id": "INS-0001-0203", "source_type": "inspection"}),
    (Conflict, {"type": "claim_vs_data", "left": "Tyler says running fine", "right": "inspection shows 0.4C delta"}),
    (
        WellState,
        {
            "state": "not_producing",
            "reasons": ["delta 3.2C below T200 threshold"],
            "conflicts": [],
        },
    ),
    (UIVisual, {"type": "status_card", "data": {"asset_id": "WELL-0001"}}),
    (
        AgentAnswer,
        {
            "text": "WELL-0001 is not producing oil right now.",
            "visual": {"type": "status_card", "data": {}},
            "citations": [{"source_id": "INS-0001-0203", "source_type": "inspection"}],
            "conflicts": [],
        },
    ),
    (Team, {"team_id": "TEAM-NE", "name": "Northeast Production", "lead_user_id": "U-201"}),
    (Area, {"area_id": "NE-08", "name": "Northeast Block 8", "team_id": "TEAM-NE", "lat": 32.152, "lon": -102.164}),
    (User, {"user_id": "U-201", "name": "Michael Turner", "role": "production_supervisor", "team_id": "TEAM-NE"}),
    (WellTypeRules, {"well_type": "T200", "producing_threshold_c": 15.0, "max_wellhead_c": 85.0}),
    (
        Well,
        {
            "asset_id": "WELL-0001", "name": "SW-23 Well 001", "well_type": "T200",
            "area_id": "SW-23", "team_id": "TEAM-SW", "in_charge_user_id": "U-101",
            "lat": 31.854, "lon": -102.502, "installed_on": "2013-03-25",
            "recorded_status": "active", "last_inspection_id": "INS-0001-0203",
        },
    ),
    (
        Inspection,
        {
            "inspection_id": "INS-0001-0203", "asset_id": "WELL-0001",
            "inspected_at": "2026-10-15T09:45:00Z", "inspector_user_id": "U-104",
            "method": "drone", "ambient_temp_c": 22.4, "wellhead_temp_c": 25.6,
            "spill_detected": False, "notes": "Pump jack not moving.",
        },
    ),
    (
        MaintenanceJob,
        {
            "maintenance_id": "MNT-0001-032", "asset_id": "WELL-0001",
            "maintenance_type": "corrective", "equipment_kind": "flowline",
            "cost_total": 5643.0, "report_file": "maintenance-reports/MNT-0001-032.md",
        },
    ),
    (
        RegulatoryEvent,
        {
            "event_id": "REG-0001-001", "asset_id": "WELL-0001", "date": "2026-06-13",
            "agency": "State Oil & Gas Commission", "event_type": "spill_report_filed",
            "protocol_id": "SR-01", "hours_to_report": 41.0, "fine_usd": 0.0, "status": "closed",
        },
    ),
    (
        ContextNote,
        {
            "note_id": "CTX-0001-003", "asset_id": "WELL-0001", "created_at": "2026-10-08T06:40:00Z",
            "author_user_id": "U-102", "note_type": "operator_feedback",
            "text": "Well shut in from 7 Oct for planned rod string replacement.",
            "valid_from": "2026-10-07", "valid_to": "2026-10-20",
        },
    ),
]


def test_round_trip_all_models():
    for model_cls, payload in MODELS_AND_PAYLOADS:
        instance = model_cls.model_validate(payload)
        dumped = instance.model_dump()
        reloaded = model_cls.model_validate(dumped)
        assert reloaded == instance, f"{model_cls.__name__} round-trip mismatch"


def test_extra_fields_forbidden():
    import pytest
    from pydantic import ValidationError

    for model_cls, payload in MODELS_AND_PAYLOADS:
        with pytest.raises(ValidationError):
            model_cls.model_validate({**payload, "unexpected_field": "boom"})


def test_tool_result_generic():
    result = ToolResult[Well](
        data=Well(asset_id="WELL-0001", name="SW-23 Well 001", well_type="T200"),
        evidence=[Evidence(source_id="WELL-0001", source_type="inspection")],
    )
    assert result.data.asset_id == "WELL-0001"
    assert len(result.evidence) == 1
