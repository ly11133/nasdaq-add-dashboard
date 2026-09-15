-- Phase 2D: drawdown-first conditional overlay validation.
-- Drawdown events are primary and immutable.  Overlay values are a
-- contemporaneous description only; all future outcomes live in a separate
-- evaluation table and are written after event construction.

CREATE TABLE IF NOT EXISTS drawdown_overlay_validation_runs (
    overlay_run_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    overlay_model_version TEXT NOT NULL CHECK (overlay_model_version = 'NDX_DRAWDOWN_OVERLAY_V1'),
    phase2c_run_id TEXT NOT NULL REFERENCES episode_validation_runs(episode_run_id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    overlay_model_config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL CHECK (length(config_hash) = 64),
    data_snapshot_json TEXT NOT NULL,
    data_cutoff TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_drawdown_overlay_runs_time
    ON drawdown_overlay_validation_runs(market, start_date, end_date, created_at);

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_runs_completed_immutable_update
BEFORE UPDATE ON drawdown_overlay_validation_runs
WHEN OLD.status IN ('COMPLETED', 'FAILED')
BEGIN
    SELECT RAISE(ABORT, 'completed drawdown_overlay_validation_runs are immutable; create a new run');
END;

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_runs_append_only_delete
BEFORE DELETE ON drawdown_overlay_validation_runs
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_validation_runs is append-only; deletion is forbidden');
END;

-- One row per mechanical drawdown trigger.  Every trigger remains present;
-- the overlay can only be SUPPORTIVE/NEUTRAL/CAUTION (or UNAVAILABLE), never
-- a buy veto.
CREATE TABLE IF NOT EXISTS drawdown_overlay_events (
    overlay_event_id TEXT PRIMARY KEY,
    overlay_run_id TEXT NOT NULL REFERENCES drawdown_overlay_validation_runs(overlay_run_id),
    phase2c_run_id TEXT NOT NULL REFERENCES episode_validation_runs(episode_run_id),
    mechanical_event_id TEXT NOT NULL REFERENCES drawdown_mechanical_events(mechanical_event_id),
    episode_id TEXT NOT NULL REFERENCES drawdown_episodes(episode_id),
    market TEXT NOT NULL,
    threshold REAL NOT NULL CHECK (threshold IN (0.1, 0.2, 0.3, 0.4, 0.5)),
    drawdown_band TEXT NOT NULL CHECK (drawdown_band IN ('MILD', 'MEDIUM', 'DEEP')),
    event_date TEXT NOT NULL,
    event_price REAL NOT NULL,
    drawdown REAL NOT NULL,
    current_drawdown REAL,
    days_since_peak INTEGER,
    rsi14 REAL,
    rsi14_percentile REAL,
    rsi14_opportunity_rank REAL,
    rsi14_status TEXT NOT NULL CHECK (rsi14_status IN ('SUPPORTIVE', 'NEUTRAL', 'CAUTION', 'UNAVAILABLE')),
    distance_ma200 REAL,
    distance_ma200_percentile REAL,
    distance_ma200_opportunity_rank REAL,
    distance_ma200_status TEXT NOT NULL CHECK (distance_ma200_status IN ('SUPPORTIVE', 'NEUTRAL', 'CAUTION', 'UNAVAILABLE')),
    vxn REAL,
    vxn_percentile REAL,
    vxn_opportunity_rank REAL,
    vxn_status TEXT NOT NULL CHECK (vxn_status IN ('SUPPORTIVE', 'NEUTRAL', 'CAUTION', 'UNAVAILABLE')),
    real_yield REAL,
    real_yield_percentile REAL,
    real_yield_opportunity_rank REAL,
    real_yield_status TEXT NOT NULL CHECK (real_yield_status IN ('SUPPORTIVE', 'NEUTRAL', 'CAUTION', 'UNAVAILABLE')),
    nfci REAL,
    nfci_percentile REAL,
    nfci_opportunity_rank REAL,
    nfci_status TEXT NOT NULL CHECK (nfci_status IN ('SUPPORTIVE', 'NEUTRAL', 'CAUTION', 'UNAVAILABLE')),
    source_state_id TEXT,
    input_observation_ids_json TEXT NOT NULL,
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    overlay_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(overlay_run_id, mechanical_event_id)
);

CREATE INDEX IF NOT EXISTS idx_drawdown_overlay_events_run_band
    ON drawdown_overlay_events(overlay_run_id, drawdown_band, event_date, overlay_event_id);

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_events_append_only_update
BEFORE UPDATE ON drawdown_overlay_events
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_events_append_only_delete
BEFORE DELETE ON drawdown_overlay_events
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_events deletion is forbidden');
END;

-- Evaluation-only table.  No future field is accepted by the event table.
CREATE TABLE IF NOT EXISTS drawdown_overlay_event_evaluations (
    overlay_evaluation_id TEXT PRIMARY KEY,
    overlay_run_id TEXT NOT NULL REFERENCES drawdown_overlay_validation_runs(overlay_run_id),
    overlay_event_id TEXT NOT NULL REFERENCES drawdown_overlay_events(overlay_event_id),
    phase2c_run_id TEXT NOT NULL REFERENCES episode_validation_runs(episode_run_id),
    episode_id TEXT NOT NULL REFERENCES drawdown_episodes(episode_id),
    event_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    forward_1y REAL,
    forward_3y REAL,
    forward_5y REAL,
    max_adverse_1y REAL,
    max_favorable_1y REAL,
    episode_bottom_price REAL,
    entry_efficiency REAL,
    entry_to_bottom_pct REAL,
    days_to_bottom INTEGER,
    timing_regret_json TEXT NOT NULL,
    future_observation_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    evaluation_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(overlay_run_id, overlay_event_id)
);

CREATE INDEX IF NOT EXISTS idx_drawdown_overlay_evaluations_run_date
    ON drawdown_overlay_event_evaluations(overlay_run_id, event_date, overlay_event_id);

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_evaluations_append_only_update
BEFORE UPDATE ON drawdown_overlay_event_evaluations
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_event_evaluations is append-only');
END;

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_evaluations_append_only_delete
BEFORE DELETE ON drawdown_overlay_event_evaluations
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_event_evaluations deletion is forbidden');
END;

-- One row per overlay and pre-registered drawdown band.  The JSON contains
-- conditional correlations, fixed tertile descriptions, and missingness.
CREATE TABLE IF NOT EXISTS drawdown_overlay_conditional_results (
    conditional_result_id TEXT PRIMARY KEY,
    overlay_run_id TEXT NOT NULL REFERENCES drawdown_overlay_validation_runs(overlay_run_id),
    overlay_name TEXT NOT NULL CHECK (overlay_name IN ('RSI', 'MA200_DISTANCE', 'VXN', 'REAL_YIELD', 'NFCI')),
    drawdown_band TEXT NOT NULL CHECK (drawdown_band IN ('ALL', 'MILD', 'MEDIUM', 'DEEP')),
    sample_count INTEGER NOT NULL,
    available_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OK', 'INCONCLUSIVE', 'INCONCLUSIVE_SMALL_SAMPLE', 'NO_DATA')),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(overlay_run_id, overlay_name, drawdown_band)
);

CREATE INDEX IF NOT EXISTS idx_drawdown_overlay_conditional_run
    ON drawdown_overlay_conditional_results(overlay_run_id, overlay_name, drawdown_band);

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_conditional_append_only_update
BEFORE UPDATE ON drawdown_overlay_conditional_results
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_conditional_results is append-only');
END;

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_conditional_append_only_delete
BEFORE DELETE ON drawdown_overlay_conditional_results
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_conditional_results deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS drawdown_overlay_loeo_results (
    loeo_result_id TEXT PRIMARY KEY,
    overlay_run_id TEXT NOT NULL REFERENCES drawdown_overlay_validation_runs(overlay_run_id),
    overlay_name TEXT NOT NULL CHECK (overlay_name IN ('RSI', 'MA200_DISTANCE', 'VXN', 'REAL_YIELD', 'NFCI')),
    drawdown_band TEXT NOT NULL CHECK (drawdown_band IN ('ALL', 'MILD', 'MEDIUM', 'DEEP')),
    held_out_episode_id TEXT NOT NULL REFERENCES drawdown_episodes(episode_id),
    sample_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OK', 'INCONCLUSIVE', 'INCONCLUSIVE_SMALL_SAMPLE', 'NO_DATA')),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(overlay_run_id, overlay_name, drawdown_band, held_out_episode_id)
);

CREATE INDEX IF NOT EXISTS idx_drawdown_overlay_loeo_run
    ON drawdown_overlay_loeo_results(overlay_run_id, overlay_name, drawdown_band, held_out_episode_id);

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_loeo_append_only_update
BEFORE UPDATE ON drawdown_overlay_loeo_results
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_loeo_results is append-only');
END;

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_loeo_append_only_delete
BEFORE DELETE ON drawdown_overlay_loeo_results
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_loeo_results deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS drawdown_overlay_model_results (
    model_result_id TEXT PRIMARY KEY,
    overlay_run_id TEXT NOT NULL REFERENCES drawdown_overlay_validation_runs(overlay_run_id),
    model_name TEXT NOT NULL CHECK (model_name IN ('MODEL_0_DRAWDOWN_ONLY', 'MODEL_1_DRAWDOWN_PLUS_VXN', 'MODEL_2_DRAWDOWN_PLUS_TREND', 'MODEL_3_DRAWDOWN_PLUS_MACRO', 'MODEL_4_DRAWDOWN_PLUS_ALL')),
    drawdown_band TEXT NOT NULL CHECK (drawdown_band IN ('ALL', 'MILD', 'MEDIUM', 'DEEP')),
    outcome_name TEXT NOT NULL CHECK (outcome_name IN ('forward_1y', 'forward_3y', 'forward_5y', 'max_adverse_1y', 'timing_regret_60d', 'entry_efficiency')),
    sample_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OK', 'INCONCLUSIVE', 'INCONCLUSIVE_SMALL_SAMPLE', 'NO_DATA')),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(overlay_run_id, model_name, drawdown_band, outcome_name)
);

CREATE INDEX IF NOT EXISTS idx_drawdown_overlay_model_run
    ON drawdown_overlay_model_results(overlay_run_id, model_name, drawdown_band, outcome_name);

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_model_append_only_update
BEFORE UPDATE ON drawdown_overlay_model_results
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_model_results is append-only');
END;

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_model_append_only_delete
BEFORE DELETE ON drawdown_overlay_model_results
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_model_results deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS drawdown_overlay_validation_reports (
    overlay_report_id TEXT PRIMARY KEY,
    overlay_run_id TEXT NOT NULL REFERENCES drawdown_overlay_validation_runs(overlay_run_id),
    report_hash TEXT NOT NULL CHECK (length(report_hash) = 64),
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(overlay_run_id)
);

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_reports_append_only_update
BEFORE UPDATE ON drawdown_overlay_validation_reports
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_validation_reports is append-only');
END;

CREATE TRIGGER IF NOT EXISTS drawdown_overlay_reports_append_only_delete
BEFORE DELETE ON drawdown_overlay_validation_reports
BEGIN
    SELECT RAISE(ABORT, 'drawdown_overlay_validation_reports deletion is forbidden');
END;
