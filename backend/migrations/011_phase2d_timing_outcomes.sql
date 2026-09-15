-- Phase 2D outcome-grid extension.
-- The original Phase 2D table was frozen with the first 60-trading-day
-- regret field.  This migration preserves every existing row while adding the
-- predeclared 30- and 120-trading-day regret outcomes needed for the
-- over-early-entry audit.  No row is updated in place: the old table is
-- copied into a new append-only table and then removed as a schema operation.

DROP TRIGGER IF EXISTS drawdown_overlay_model_append_only_update;
DROP TRIGGER IF EXISTS drawdown_overlay_model_append_only_delete;
DROP INDEX IF EXISTS idx_drawdown_overlay_model_run;

ALTER TABLE drawdown_overlay_model_results RENAME TO drawdown_overlay_model_results_v010;

CREATE TABLE drawdown_overlay_model_results (
    model_result_id TEXT PRIMARY KEY,
    overlay_run_id TEXT NOT NULL REFERENCES drawdown_overlay_validation_runs(overlay_run_id),
    model_name TEXT NOT NULL CHECK (model_name IN ('MODEL_0_DRAWDOWN_ONLY', 'MODEL_1_DRAWDOWN_PLUS_VXN', 'MODEL_2_DRAWDOWN_PLUS_TREND', 'MODEL_3_DRAWDOWN_PLUS_MACRO', 'MODEL_4_DRAWDOWN_PLUS_ALL')),
    drawdown_band TEXT NOT NULL CHECK (drawdown_band IN ('ALL', 'MILD', 'MEDIUM', 'DEEP')),
    outcome_name TEXT NOT NULL CHECK (outcome_name IN ('forward_1y', 'forward_3y', 'forward_5y', 'max_adverse_1y', 'timing_regret_30d', 'timing_regret_60d', 'timing_regret_120d', 'entry_efficiency')),
    sample_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OK', 'INCONCLUSIVE', 'INCONCLUSIVE_SMALL_SAMPLE', 'NO_DATA')),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(overlay_run_id, model_name, drawdown_band, outcome_name)
);

INSERT INTO drawdown_overlay_model_results (
    model_result_id, overlay_run_id, model_name, drawdown_band, outcome_name,
    sample_count, status, result_json, created_at
)
SELECT
    model_result_id, overlay_run_id, model_name, drawdown_band, outcome_name,
    sample_count, status, result_json, created_at
FROM drawdown_overlay_model_results_v010;

DROP TABLE drawdown_overlay_model_results_v010;

CREATE INDEX idx_drawdown_overlay_model_run
    ON drawdown_overlay_model_results(overlay_run_id, model_name, drawdown_band, outcome_name);

CREATE TRIGGER drawdown_overlay_model_append_only_update
BEFORE UPDATE ON drawdown_overlay_model_results
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_model_results is append-only');
END;

CREATE TRIGGER drawdown_overlay_model_append_only_delete
BEFORE DELETE ON drawdown_overlay_model_results
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_model_results deletion is forbidden');
END;
