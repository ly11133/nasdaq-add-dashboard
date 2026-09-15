-- Phase 2A: immutable dual-track chronological replay and isolated outcome
-- evaluation.  Replay tables are deliberately separate from live
-- decision_log; future returns never belong to the decision engine.

CREATE TABLE IF NOT EXISTS replay_runs (
    replay_run_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('STRICT_PIT', 'RESEARCH_PROXY')),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    score_model_version TEXT NOT NULL REFERENCES score_models(model_version),
    feature_model_versions_json TEXT NOT NULL,
    data_snapshot_json TEXT NOT NULL,
    data_cutoff TEXT NOT NULL,
    run_config_hash TEXT NOT NULL CHECK (length(run_config_hash) = 64),
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_replay_runs_market_time
    ON replay_runs(market, mode, start_date, end_date, created_at);

CREATE TRIGGER IF NOT EXISTS replay_runs_completed_immutable_update
BEFORE UPDATE ON replay_runs
WHEN OLD.status IN ('COMPLETED', 'FAILED')
BEGIN
    SELECT RAISE(ABORT, 'completed replay_runs are immutable; create a new run');
END;

CREATE TRIGGER IF NOT EXISTS replay_runs_append_only_delete
BEFORE DELETE ON replay_runs
BEGIN
    SELECT RAISE(ABORT, 'replay_runs is append-only; deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS replay_features (
    feature_id TEXT PRIMARY KEY,
    replay_run_id TEXT NOT NULL REFERENCES replay_runs(replay_run_id),
    as_of_datetime TEXT NOT NULL,
    feature_name TEXT NOT NULL,
    series_id TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    value REAL,
    value_json TEXT,
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    input_observation_ids_json TEXT NOT NULL,
    score_eligible INTEGER NOT NULL CHECK (score_eligible IN (0, 1)),
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(replay_run_id, as_of_datetime, feature_name, series_id, feature_version, input_hash),
    CHECK (value IS NOT NULL OR value_json IS NOT NULL OR score_eligible = 0)
);

CREATE INDEX IF NOT EXISTS idx_replay_features_lookup
    ON replay_features(replay_run_id, as_of_datetime, feature_name);

CREATE TRIGGER IF NOT EXISTS replay_features_append_only_update
BEFORE UPDATE ON replay_features
BEGIN
    SELECT RAISE(ABORT, 'replay_features is append-only; insert a new feature version instead');
END;

CREATE TRIGGER IF NOT EXISTS replay_features_append_only_delete
BEFORE DELETE ON replay_features
BEGIN
    SELECT RAISE(ABORT, 'replay_features is append-only; deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS replay_decisions (
    replay_decision_id TEXT PRIMARY KEY,
    replay_run_id TEXT NOT NULL REFERENCES replay_runs(replay_run_id),
    sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
    as_of_datetime TEXT NOT NULL,
    market TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('STRICT_PIT', 'RESEARCH_PROXY')),
    score_model_version TEXT NOT NULL REFERENCES score_models(model_version),
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    feature_hash TEXT NOT NULL CHECK (length(feature_hash) = 64),
    input_observation_ids_json TEXT NOT NULL,
    feature_ids_json TEXT NOT NULL,
    coverage REAL NOT NULL CHECK (coverage >= 0 AND coverage <= 100),
    score REAL,
    gate_status TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason_json TEXT NOT NULL,
    missing_items_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(replay_run_id, as_of_datetime)
);

CREATE INDEX IF NOT EXISTS idx_replay_decisions_run_time
    ON replay_decisions(replay_run_id, as_of_datetime, sequence_no);
CREATE INDEX IF NOT EXISTS idx_replay_decisions_score
    ON replay_decisions(mode, score, coverage, as_of_datetime);

CREATE TRIGGER IF NOT EXISTS replay_decisions_append_only_update
BEFORE UPDATE ON replay_decisions
BEGIN
    SELECT RAISE(ABORT, 'replay_decisions is append-only; insert a new decision instead');
END;

CREATE TRIGGER IF NOT EXISTS replay_decisions_append_only_delete
BEFORE DELETE ON replay_decisions
BEGIN
    SELECT RAISE(ABORT, 'replay_decisions is append-only; deletion is forbidden');
END;

-- Evaluation-only table.  The replay decision code never reads this table.
CREATE TABLE IF NOT EXISTS forward_outcomes (
    outcome_id TEXT PRIMARY KEY,
    replay_run_id TEXT NOT NULL REFERENCES replay_runs(replay_run_id),
    replay_decision_id TEXT NOT NULL REFERENCES replay_decisions(replay_decision_id),
    market TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    source_series_id TEXT NOT NULL,
    source_observation_version_ids_json TEXT NOT NULL,
    forward_1m REAL,
    forward_3m REAL,
    forward_6m REAL,
    forward_1y REAL,
    forward_3y REAL,
    forward_5y REAL,
    max_drawdown_next_1y REAL,
    max_gain_next_1y REAL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(replay_decision_id, source_series_id)
);

CREATE INDEX IF NOT EXISTS idx_forward_outcomes_bucket
    ON forward_outcomes(market, decision_date, replay_run_id);

CREATE TRIGGER IF NOT EXISTS forward_outcomes_append_only_update
BEFORE UPDATE ON forward_outcomes
BEGIN
    SELECT RAISE(ABORT, 'forward_outcomes is append-only; insert a new evaluation');
END;

CREATE TRIGGER IF NOT EXISTS forward_outcomes_append_only_delete
BEFORE DELETE ON forward_outcomes
BEGIN
    SELECT RAISE(ABORT, 'forward_outcomes is append-only; deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS replay_reports (
    report_id TEXT PRIMARY KEY,
    replay_run_id TEXT NOT NULL REFERENCES replay_runs(replay_run_id),
    report_hash TEXT NOT NULL CHECK (length(report_hash) = 64),
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(replay_run_id)
);

CREATE TRIGGER IF NOT EXISTS replay_reports_append_only_update
BEFORE UPDATE ON replay_reports
BEGIN
    SELECT RAISE(ABORT, 'replay_reports is append-only; insert a new report version');
END;

CREATE TRIGGER IF NOT EXISTS replay_reports_append_only_delete
BEFORE DELETE ON replay_reports
BEGIN
    SELECT RAISE(ABORT, 'replay_reports is append-only; deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS phase2a_gate_checks (
    gate_check_id TEXT PRIMARY KEY,
    gate_name TEXT NOT NULL CHECK (gate_name IN ('GATE_A_SCHEDULED_RUN', 'GATE_B_FAILURE_RECOVERY')),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'PASS_WITH_LIMITATIONS', 'FAIL')),
    triggered_at TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(gate_name, triggered_at)
);

CREATE INDEX IF NOT EXISTS idx_phase2a_gate_checks_name
    ON phase2a_gate_checks(gate_name, triggered_at);

CREATE TRIGGER IF NOT EXISTS phase2a_gate_checks_append_only_update
BEFORE UPDATE ON phase2a_gate_checks
BEGIN
    SELECT RAISE(ABORT, 'phase2a_gate_checks is append-only');
END;

CREATE TRIGGER IF NOT EXISTS phase2a_gate_checks_append_only_delete
BEFORE DELETE ON phase2a_gate_checks
BEGIN
    SELECT RAISE(ABORT, 'phase2a_gate_checks deletion is forbidden');
END;
