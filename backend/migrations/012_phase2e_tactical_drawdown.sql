-- Phase 2E: tactical drawdown cycles with a fixed trailing 252-day high.
-- Macro ATH-to-ATH episodes remain the upstream statistical blocks.  These
-- tables are an independent append-only research ledger; future outcomes are
-- kept out of the daily states and mechanical event rows.

CREATE TABLE IF NOT EXISTS tactical_drawdown_validation_runs (
    tactical_run_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    tactical_model_version TEXT NOT NULL CHECK (tactical_model_version = 'NDX_TACTICAL_DRAWDOWN_V1'),
    phase2c_run_id TEXT NOT NULL REFERENCES episode_validation_runs(episode_run_id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    tactical_model_config_json TEXT NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_tactical_drawdown_runs_time
    ON tactical_drawdown_validation_runs(market, start_date, end_date, created_at);

CREATE TRIGGER IF NOT EXISTS tactical_drawdown_runs_completed_immutable_update
BEFORE UPDATE ON tactical_drawdown_validation_runs
WHEN OLD.status IN ('COMPLETED', 'FAILED')
BEGIN
    SELECT RAISE(ABORT, 'completed tactical_drawdown_validation_runs are immutable; create a new run');
END;

CREATE TRIGGER IF NOT EXISTS tactical_drawdown_runs_append_only_delete
BEFORE DELETE ON tactical_drawdown_validation_runs
BEGIN
    SELECT RAISE(ABORT, 'tactical_drawdown_validation_runs is append-only; deletion is forbidden');
END;

-- A daily, contemporaneous state.  ``max_drawdown`` and recovery fields are
-- deliberately absent; they belong to the cycle/evaluation layer only.
CREATE TABLE IF NOT EXISTS tactical_drawdown_states (
    tactical_state_id TEXT PRIMARY KEY,
    tactical_run_id TEXT NOT NULL REFERENCES tactical_drawdown_validation_runs(tactical_run_id),
    tactical_cycle_id TEXT NOT NULL,
    macro_episode_id TEXT,
    market TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    close_price REAL NOT NULL,
    rolling_high_252 REAL NOT NULL,
    rolling_high_date TEXT NOT NULL,
    tactical_peak_price REAL NOT NULL,
    tactical_peak_date TEXT NOT NULL,
    tactical_drawdown REAL NOT NULL,
    days_since_peak INTEGER NOT NULL,
    new_tactical_peak INTEGER NOT NULL CHECK (new_tactical_peak IN (0, 1)),
    reset_reason TEXT,
    triggered_bands_json TEXT NOT NULL DEFAULT '[]',
    source_observation_id TEXT,
    input_observation_ids_json TEXT NOT NULL,
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    state_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(tactical_run_id, tactical_cycle_id, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_tactical_states_run_date
    ON tactical_drawdown_states(tactical_run_id, as_of_date, tactical_cycle_id);

CREATE TRIGGER IF NOT EXISTS tactical_drawdown_states_append_only_update
BEFORE UPDATE ON tactical_drawdown_states
BEGIN
    SELECT RAISE(ABORT, 'tactical_drawdown_states is append-only');
END;

CREATE TRIGGER IF NOT EXISTS tactical_drawdown_states_append_only_delete
BEFORE DELETE ON tactical_drawdown_states
BEGIN
    SELECT RAISE(ABORT, 'tactical_drawdown_states deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS tactical_drawdown_cycles (
    tactical_cycle_id TEXT PRIMARY KEY,
    tactical_run_id TEXT NOT NULL REFERENCES tactical_drawdown_validation_runs(tactical_run_id),
    macro_episode_id TEXT REFERENCES drawdown_episodes(episode_id),
    market TEXT NOT NULL,
    peak_date TEXT NOT NULL,
    peak_price REAL NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    max_drawdown REAL,
    max_drawdown_date TEXT,
    recovery_state TEXT NOT NULL,
    data_end_date TEXT NOT NULL,
    cycle_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(tactical_run_id, tactical_cycle_id)
);

CREATE INDEX IF NOT EXISTS idx_tactical_cycles_run_date
    ON tactical_drawdown_cycles(tactical_run_id, start_date, end_date, tactical_cycle_id);

CREATE TRIGGER IF NOT EXISTS tactical_cycles_append_only_update
BEFORE UPDATE ON tactical_drawdown_cycles
BEGIN
    SELECT RAISE(ABORT, 'tactical_drawdown_cycles is append-only');
END;

CREATE TRIGGER IF NOT EXISTS tactical_cycles_append_only_delete
BEFORE DELETE ON tactical_drawdown_cycles
BEGIN
    SELECT RAISE(ABORT, 'tactical_drawdown_cycles deletion is forbidden');
END;

-- Every crossing event is preserved.  Overlay fields are the same frozen
-- Phase 2D fields and are descriptive only; no field here can veto a trigger.
CREATE TABLE IF NOT EXISTS tactical_drawdown_events (
    tactical_event_id TEXT PRIMARY KEY,
    tactical_run_id TEXT NOT NULL REFERENCES tactical_drawdown_validation_runs(tactical_run_id),
    tactical_cycle_id TEXT NOT NULL,
    macro_episode_id TEXT,
    market TEXT NOT NULL,
    threshold REAL NOT NULL CHECK (threshold IN (0.1, 0.2, 0.3, 0.4, 0.5)),
    drawdown_band TEXT NOT NULL CHECK (drawdown_band IN ('MILD', 'MEDIUM', 'DEEP')),
    event_date TEXT NOT NULL,
    event_price REAL NOT NULL,
    drawdown REAL NOT NULL,
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
    source_observation_id TEXT,
    input_observation_ids_json TEXT NOT NULL,
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    event_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(tactical_run_id, tactical_cycle_id, threshold)
);

CREATE INDEX IF NOT EXISTS idx_tactical_events_run_date
    ON tactical_drawdown_events(tactical_run_id, event_date, threshold, tactical_event_id);

CREATE TRIGGER IF NOT EXISTS tactical_events_append_only_update
BEFORE UPDATE ON tactical_drawdown_events
BEGIN
    SELECT RAISE(ABORT, 'tactical_drawdown_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS tactical_events_append_only_delete
BEFORE DELETE ON tactical_drawdown_events
BEGIN
    SELECT RAISE(ABORT, 'tactical_drawdown_events deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS tactical_drawdown_event_evaluations (
    tactical_evaluation_id TEXT PRIMARY KEY,
    tactical_run_id TEXT NOT NULL REFERENCES tactical_drawdown_validation_runs(tactical_run_id),
    tactical_event_id TEXT NOT NULL REFERENCES tactical_drawdown_events(tactical_event_id),
    tactical_cycle_id TEXT NOT NULL,
    macro_episode_id TEXT,
    event_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    forward_1y REAL,
    forward_3y REAL,
    forward_5y REAL,
    max_adverse_1y REAL,
    max_favorable_1y REAL,
    cycle_bottom_price REAL,
    entry_efficiency REAL,
    entry_to_bottom_pct REAL,
    days_to_bottom INTEGER,
    timing_regret_json TEXT NOT NULL,
    future_observation_ids_json TEXT NOT NULL,
    recovery_date TEXT,
    recovery_days INTEGER,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    evaluation_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(tactical_run_id, tactical_event_id)
);

CREATE INDEX IF NOT EXISTS idx_tactical_evaluations_run_date
    ON tactical_drawdown_event_evaluations(tactical_run_id, event_date, tactical_event_id);

CREATE TRIGGER IF NOT EXISTS tactical_evaluations_append_only_update
BEFORE UPDATE ON tactical_drawdown_event_evaluations
BEGIN
    SELECT RAISE(ABORT, 'tactical_drawdown_event_evaluations is append-only');
END;

CREATE TRIGGER IF NOT EXISTS tactical_evaluations_append_only_delete
BEFORE DELETE ON tactical_drawdown_event_evaluations
BEGIN
    SELECT RAISE(ABORT, 'tactical_drawdown_event_evaluations deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS tactical_overlay_conditional_results (
    conditional_result_id TEXT PRIMARY KEY,
    tactical_run_id TEXT NOT NULL REFERENCES tactical_drawdown_validation_runs(tactical_run_id),
    overlay_name TEXT NOT NULL CHECK (overlay_name IN ('RSI', 'MA200_DISTANCE', 'VXN', 'REAL_YIELD', 'NFCI')),
    drawdown_band TEXT NOT NULL CHECK (drawdown_band IN ('ALL', 'MILD', 'MEDIUM', 'DEEP')),
    sample_count INTEGER NOT NULL,
    available_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OK', 'INCONCLUSIVE', 'INCONCLUSIVE_SMALL_SAMPLE', 'NO_DATA')),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tactical_run_id, overlay_name, drawdown_band)
);

CREATE INDEX IF NOT EXISTS idx_tactical_conditional_run
    ON tactical_overlay_conditional_results(tactical_run_id, overlay_name, drawdown_band);

CREATE TRIGGER IF NOT EXISTS tactical_conditional_append_only_update
BEFORE UPDATE ON tactical_overlay_conditional_results
BEGIN
    SELECT RAISE(ABORT, 'tactical_overlay_conditional_results is append-only');
END;

CREATE TRIGGER IF NOT EXISTS tactical_conditional_append_only_delete
BEFORE DELETE ON tactical_overlay_conditional_results
BEGIN
    SELECT RAISE(ABORT, 'tactical_overlay_conditional_results deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS tactical_overlay_lome_results (
    lome_result_id TEXT PRIMARY KEY,
    tactical_run_id TEXT NOT NULL REFERENCES tactical_drawdown_validation_runs(tactical_run_id),
    overlay_name TEXT NOT NULL CHECK (overlay_name IN ('RSI', 'MA200_DISTANCE', 'VXN', 'REAL_YIELD', 'NFCI')),
    drawdown_band TEXT NOT NULL CHECK (drawdown_band IN ('ALL', 'MILD', 'MEDIUM', 'DEEP')),
    held_out_macro_episode_id TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OK', 'INCONCLUSIVE', 'INCONCLUSIVE_SMALL_SAMPLE', 'NO_DATA')),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tactical_run_id, overlay_name, drawdown_band, held_out_macro_episode_id)
);

CREATE INDEX IF NOT EXISTS idx_tactical_lome_run
    ON tactical_overlay_lome_results(tactical_run_id, overlay_name, drawdown_band, held_out_macro_episode_id);

CREATE TRIGGER IF NOT EXISTS tactical_lome_append_only_update
BEFORE UPDATE ON tactical_overlay_lome_results
BEGIN
    SELECT RAISE(ABORT, 'tactical_overlay_lome_results is append-only');
END;

CREATE TRIGGER IF NOT EXISTS tactical_lome_append_only_delete
BEFORE DELETE ON tactical_overlay_lome_results
BEGIN
    SELECT RAISE(ABORT, 'tactical_overlay_lome_results deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS tactical_overlay_model_results (
    model_result_id TEXT PRIMARY KEY,
    tactical_run_id TEXT NOT NULL REFERENCES tactical_drawdown_validation_runs(tactical_run_id),
    model_name TEXT NOT NULL CHECK (model_name IN ('MODEL_0_DRAWDOWN_ONLY', 'MODEL_1_DRAWDOWN_PLUS_VXN', 'MODEL_2_DRAWDOWN_PLUS_TREND', 'MODEL_3_DRAWDOWN_PLUS_MACRO', 'MODEL_4_DRAWDOWN_PLUS_ALL')),
    drawdown_band TEXT NOT NULL CHECK (drawdown_band IN ('ALL', 'MILD', 'MEDIUM', 'DEEP')),
    outcome_name TEXT NOT NULL CHECK (outcome_name IN ('forward_1y', 'forward_3y', 'forward_5y', 'max_adverse_1y', 'timing_regret_30d', 'timing_regret_60d', 'timing_regret_120d', 'entry_efficiency')),
    sample_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OK', 'INCONCLUSIVE', 'INCONCLUSIVE_SMALL_SAMPLE', 'NO_DATA')),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tactical_run_id, model_name, drawdown_band, outcome_name)
);

CREATE INDEX IF NOT EXISTS idx_tactical_model_run
    ON tactical_overlay_model_results(tactical_run_id, model_name, drawdown_band, outcome_name);

CREATE TRIGGER IF NOT EXISTS tactical_model_append_only_update
BEFORE UPDATE ON tactical_overlay_model_results
BEGIN
    SELECT RAISE(ABORT, 'tactical_overlay_model_results is append-only');
END;

CREATE TRIGGER IF NOT EXISTS tactical_model_append_only_delete
BEFORE DELETE ON tactical_overlay_model_results
BEGIN
    SELECT RAISE(ABORT, 'tactical_overlay_model_results deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS tactical_drawdown_validation_reports (
    tactical_report_id TEXT PRIMARY KEY,
    tactical_run_id TEXT NOT NULL REFERENCES tactical_drawdown_validation_runs(tactical_run_id),
    report_hash TEXT NOT NULL CHECK (length(report_hash) = 64),
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tactical_run_id)
);

CREATE TRIGGER IF NOT EXISTS tactical_reports_append_only_update
BEFORE UPDATE ON tactical_drawdown_validation_reports
BEGIN
    SELECT RAISE(ABORT, 'tactical_drawdown_validation_reports is append-only');
END;

CREATE TRIGGER IF NOT EXISTS tactical_reports_append_only_delete
BEFORE DELETE ON tactical_drawdown_validation_reports
BEGIN
    SELECT RAISE(ABORT, 'tactical_drawdown_validation_reports deletion is forbidden');
END;
