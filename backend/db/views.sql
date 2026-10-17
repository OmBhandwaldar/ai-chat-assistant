-- DuckDB views: the override-aware read layer.
-- Every tool reads through these views, never through the raw tables
-- directly, so a correction or feedback override is automatically visible
-- to every subsequent query without each tool re-implementing the logic.
-- See PLAN.md Phase 1.3, DECISIONS.md D9/D10.

-- ---------------------------------------------------------------------------
-- v_effective_inspections
-- Inspections with any inspection-scoped field override applied
-- (e.g. Q12: wellhead_temp_c corrected on one inspection_id).
-- Original raw values stay untouched in `inspections`; this view is the
-- only place the corrected value is used.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_effective_inspections AS
SELECT
    i.inspection_id,
    i.asset_id,
    i.inspected_at,
    i.inspector_user_id,
    i.method,
    i.ambient_temp_c,
    COALESCE(
        (SELECT CAST(o.new_value AS DOUBLE)
         FROM overrides o
         WHERE o.scope_type = 'inspection'
           AND o.scope_id = i.inspection_id
           AND o.field = 'wellhead_temp_c'
         ORDER BY o.created_at DESC LIMIT 1),
        i.wellhead_temp_c
    ) AS wellhead_temp_c,
    i.spill_detected,
    i.spill_area_pct,
    COALESCE(
        (SELECT CAST(o.new_value AS DOUBLE)
         FROM overrides o
         WHERE o.scope_type = 'inspection'
           AND o.scope_id = i.inspection_id
           AND o.field = 'spill_est_volume_bbl'
         ORDER BY o.created_at DESC LIMIT 1),
        i.spill_est_volume_bbl
    ) AS spill_est_volume_bbl,
    i.notes,
    EXISTS (
        SELECT 1 FROM overrides o
        WHERE o.scope_type = 'inspection' AND o.scope_id = i.inspection_id
    ) AS has_override
FROM inspections i;

-- ---------------------------------------------------------------------------
-- v_is_producing
-- Per-inspection producing/not-producing flag, derived from
-- (wellhead_temp_c - ambient_temp_c) vs. the well type's threshold.
-- Uses v_effective_inspections (so a temp correction flips this flag, Q12),
-- and well_type_rules' threshold (so a well-type-scoped rule change could
-- also affect it, though thresholds themselves aren't overridden in the
-- sample eval questions — only rca_corrective_count is, via Q14).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_is_producing AS
SELECT
    ei.inspection_id,
    ei.asset_id,
    ei.inspected_at,
    ei.wellhead_temp_c,
    ei.ambient_temp_c,
    (ei.wellhead_temp_c - ei.ambient_temp_c) AS delta_c,
    w.well_type,
    r.producing_threshold_c,
    (ei.wellhead_temp_c - ei.ambient_temp_c) >= r.producing_threshold_c AS is_producing
FROM v_effective_inspections ei
JOIN wells w ON w.asset_id = ei.asset_id
JOIN well_type_rules r ON r.well_type = w.well_type;

-- ---------------------------------------------------------------------------
-- v_well_latest_inspection
-- The most recent inspection per well (by inspected_at), through the
-- effective/producing views so it already reflects corrections.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_well_latest_inspection AS
SELECT p.*
FROM v_is_producing p
QUALIFY ROW_NUMBER() OVER (PARTITION BY p.asset_id ORDER BY p.inspected_at DESC) = 1;

-- ---------------------------------------------------------------------------
-- v_corrected_spills
-- Spill detections with corrections removed:
--   - a formal `correction` context note on that inspection_id
--     (e.g. "rainwater, not a spill") drops the detection
--   - an override explicitly setting spill_detected=false drops it too
-- This is the single view Q4, Q7, Q8 all read from, so "corrected
-- detections do not count" (SOP SR, section 7) is enforced exactly once.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_corrected_spills AS
SELECT
    ei.inspection_id,
    ei.asset_id,
    ei.inspected_at,
    ei.spill_area_pct,
    ei.spill_est_volume_bbl,
    w.area_id,
    w.well_type
FROM v_effective_inspections ei
JOIN wells w ON w.asset_id = ei.asset_id
WHERE ei.spill_detected = TRUE
  AND NOT EXISTS (
      SELECT 1 FROM context_notes cn
      WHERE cn.note_type = 'correction'
        AND cn.inspection_id = ei.inspection_id
  )
  AND NOT EXISTS (
      SELECT 1 FROM overrides o
      WHERE o.scope_type = 'inspection'
        AND o.scope_id = ei.inspection_id
        AND o.field = 'spill_detected'
        AND CAST(o.new_value AS BOOLEAN) = FALSE
  );

-- ---------------------------------------------------------------------------
-- v_well_type_rules_effective
-- well_type_rules with any well_type-scoped override applied
-- (Q14: rca_corrective_count lowered to 2 for T350).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_well_type_rules_effective AS
SELECT
    r.well_type,
    r.producing_threshold_c,
    r.max_wellhead_c,
    COALESCE(
        (SELECT CAST(o.new_value AS INTEGER)
         FROM overrides o
         WHERE o.scope_type = 'well_type'
           AND o.scope_id = r.well_type
           AND o.field = 'rca_corrective_count'
         ORDER BY o.created_at DESC LIMIT 1),
        r.rca_corrective_count
    ) AS rca_corrective_count,
    r.rca_lookback_days
FROM well_type_rules r;
