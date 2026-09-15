-- Phase 2B: independent research-only proxy signal validation.
-- These tables never participate in STRICT_PIT or NDX_SCORE_V2.0 decisions.

CREATE TABLE IF NOT EXISTS proxy_research_runs (
    proxy_run_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    proxy_model_version TEXT NOT NULL CHECK (proxy_model_version = 'NDX_PROXY_RESEARCH_V1'),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    proxy_model_config_json TEXT NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_proxy_research_runs_market_time
    ON proxy_research_runs(market, start_date, end_date, created_at);

CREATE TRIGGER IF NOT EXISTS proxy_research_runs_completed_immutable_update
BEFORE UPDATE ON proxy_research_runs
WHEN OLD.status IN ('COMPLETED', 'FAILED')
BEGIN
    SELECT RAISE(ABORT, 'completed proxy_research_runs are immutable; create a new run');
END;

CREATE TRIGGER IF NOT EXISTS proxy_research_runs_append_only_delete
BEFORE DELETE ON proxy_research_runs
BEGIN
    SELECT RAISE(ABORT, 'proxy_research_runs is append-only; deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS proxy_signal_observations (
    proxy_signal_id TEXT PRIMARY KEY,
    proxy_run_id TEXT NOT NULL REFERENCES proxy_research_runs(proxy_run_id),
    as_of_datetime TEXT NOT NULL,
    market TEXT NOT NULL,
    proxy_model_version TEXT NOT NULL CHECK (proxy_model_version = 'NDX_PROXY_RESEARCH_V1'),
    proxy_raw_score REAL,
    proxy_available_weight REAL NOT NULL CHECK (proxy_available_weight >= 0),
    proxy_score_fraction REAL,
    proxy_rank REAL,
    proxy_percentile REAL,
    proxy_b_raw_score REAL,
    proxy_b_available_weight REAL NOT NULL CHECK (proxy_b_available_weight >= 0),
    proxy_b_score_fraction REAL,
    proxy_b_rank REAL,
    proxy_b_percentile REAL,
    feature_values_json TEXT NOT NULL,
    feature_percentiles_json TEXT NOT NULL,
    feature_status_json TEXT NOT NULL,
    input_observation_ids_json TEXT NOT NULL,
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    created_at TEXT NOT NULL,
    UNIQUE(proxy_run_id, as_of_datetime)
);

CREATE INDEX IF NOT EXISTS idx_proxy_signal_observations_run_time
    ON proxy_signal_observations(proxy_run_id, as_of_datetime);

CREATE TRIGGER IF NOT EXISTS proxy_signal_observations_append_only_update
BEFORE UPDATE ON proxy_signal_observations
BEGIN
    SELECT RAISE(ABORT, 'proxy_signal_observations is append-only');
END;

CREATE TRIGGER IF NOT EXISTS proxy_signal_observations_append_only_delete
BEFORE DELETE ON proxy_signal_observations
BEGIN
    SELECT RAISE(ABORT, 'proxy_signal_observations is append-only');
END;

-- Evaluation-only table.  Proxy signal construction never reads this table.
CREATE TABLE IF NOT EXISTS proxy_forward_outcomes (
    proxy_outcome_id TEXT PRIMARY KEY,
    proxy_run_id TEXT NOT NULL REFERENCES proxy_research_runs(proxy_run_id),
    proxy_signal_id TEXT NOT NULL REFERENCES proxy_signal_observations(proxy_signal_id),
    market TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    source_series_id TEXT NOT NULL,
    source_observation_version_ids_json TEXT NOT NULL,
    forward_6m REAL,
    forward_1y REAL,
    forward_3y REAL,
    forward_5y REAL,
    max_drawdown_next_1y REAL,
    max_gain_next_1y REAL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(proxy_signal_id, source_series_id)
);

CREATE INDEX IF NOT EXISTS idx_proxy_forward_outcomes_run_date
    ON proxy_forward_outcomes(proxy_run_id, decision_date);

CREATE TRIGGER IF NOT EXISTS proxy_forward_outcomes_append_only_update
BEFORE UPDATE ON proxy_forward_outcomes
BEGIN
    SELECT RAISE(ABORT, 'proxy_forward_outcomes is append-only');
END;

CREATE TRIGGER IF NOT EXISTS proxy_forward_outcomes_append_only_delete
BEFORE DELETE ON proxy_forward_outcomes
BEGIN
    SELECT RAISE(ABORT, 'proxy_forward_outcomes is append-only');
END;

CREATE TABLE IF NOT EXISTS proxy_research_reports (
    proxy_report_id TEXT PRIMARY KEY,
    proxy_run_id TEXT NOT NULL REFERENCES proxy_research_runs(proxy_run_id),
    report_hash TEXT NOT NULL CHECK (length(report_hash) = 64),
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(proxy_run_id)
);

CREATE TRIGGER IF NOT EXISTS proxy_research_reports_append_only_update
BEFORE UPDATE ON proxy_research_reports
BEGIN
    SELECT RAISE(ABORT, 'proxy_research_reports is append-only');
END;

CREATE TRIGGER IF NOT EXISTS proxy_research_reports_append_only_delete
BEFORE DELETE ON proxy_research_reports
BEGIN
    SELECT RAISE(ABORT, 'proxy_research_reports is append-only');
END;
