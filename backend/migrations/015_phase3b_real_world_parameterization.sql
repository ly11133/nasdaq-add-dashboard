-- Phase 3B: real-world capital profiles and cash-flow validation.
-- This layer stores simulation inputs and aggregate outcomes only. It never
-- writes positions, orders, Core DCA balances, or emergency-fund transfers.

CREATE TABLE IF NOT EXISTS real_world_parameterization_runs (
    real_world_run_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market = 'NDX'),
    model_version TEXT NOT NULL CHECK (model_version = 'NDX_REAL_WORLD_PARAMETERIZATION_V1'),
    phase3a_batch_id TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL CHECK (length(config_hash) = 64),
    data_snapshot_json TEXT NOT NULL,
    data_cutoff TEXT NOT NULL,
    simulation_only INTEGER NOT NULL CHECK (simulation_only IN (0, 1)),
    auto_trade INTEGER NOT NULL CHECK (auto_trade IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_real_world_parameterization_runs_time
    ON real_world_parameterization_runs(market, start_date, end_date, created_at);

CREATE TRIGGER IF NOT EXISTS real_world_parameterization_runs_completed_immutable_update
BEFORE UPDATE ON real_world_parameterization_runs
WHEN OLD.status IN ('COMPLETED', 'FAILED')
BEGIN
    SELECT RAISE(ABORT, 'completed real_world_parameterization_runs are immutable; create a new run');
END;

CREATE TRIGGER IF NOT EXISTS real_world_parameterization_runs_append_only_delete
BEFORE DELETE ON real_world_parameterization_runs
BEGIN
    SELECT RAISE(ABORT, 'real_world_parameterization_runs is append-only; deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS real_world_profile_scenarios (
    scenario_id TEXT PRIMARY KEY,
    real_world_run_id TEXT NOT NULL REFERENCES real_world_parameterization_runs(real_world_run_id),
    profile_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    cap_multiplier REAL NOT NULL CHECK (cap_multiplier IN (1.0, 1.5, 2.0)),
    refill_mode TEXT NOT NULL CHECK (refill_mode IN ('FIXED', 'INCOME_LINKED')),
    refill_ratio REAL,
    growth_scenario TEXT NOT NULL,
    surplus_policy TEXT NOT NULL CHECK (surplus_policy IN ('S0', 'S1', 'S2')),
    ladder_id TEXT NOT NULL CHECK (ladder_id IN ('C', 'D')),
    profile_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    result_hash TEXT NOT NULL CHECK (length(result_hash) = 64),
    created_at TEXT NOT NULL,
    UNIQUE(real_world_run_id, profile_id, target_id, cap_multiplier, refill_mode, refill_ratio, growth_scenario, surplus_policy, ladder_id)
);

CREATE INDEX IF NOT EXISTS idx_real_world_profile_scenarios_run
    ON real_world_profile_scenarios(real_world_run_id, profile_id, target_id, ladder_id);

CREATE TRIGGER IF NOT EXISTS real_world_profile_scenarios_append_only_update
BEFORE UPDATE ON real_world_profile_scenarios
BEGIN
    SELECT RAISE(ABORT, 'real_world_profile_scenarios is append-only');
END;

CREATE TRIGGER IF NOT EXISTS real_world_profile_scenarios_append_only_delete
BEFORE DELETE ON real_world_profile_scenarios
BEGIN
    SELECT RAISE(ABORT, 'real_world_profile_scenarios deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS real_world_path_results (
    path_result_id TEXT PRIMARY KEY,
    real_world_run_id TEXT NOT NULL REFERENCES real_world_parameterization_runs(real_world_run_id),
    path_id TEXT NOT NULL,
    path_type TEXT NOT NULL CHECK (path_type IN ('HISTORICAL', 'STRESS')),
    profile_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    cap_multiplier REAL NOT NULL,
    refill_mode TEXT NOT NULL,
    refill_ratio REAL,
    growth_scenario TEXT NOT NULL,
    surplus_policy TEXT NOT NULL,
    ladder_id TEXT NOT NULL CHECK (ladder_id IN ('C', 'D')),
    result_json TEXT NOT NULL,
    result_hash TEXT NOT NULL CHECK (length(result_hash) = 64),
    created_at TEXT NOT NULL,
    UNIQUE(real_world_run_id, path_id, profile_id, target_id, cap_multiplier, refill_mode, refill_ratio, growth_scenario, surplus_policy, ladder_id)
);

CREATE INDEX IF NOT EXISTS idx_real_world_path_results_run
    ON real_world_path_results(real_world_run_id, path_id, profile_id, ladder_id);

CREATE TRIGGER IF NOT EXISTS real_world_path_results_append_only_update
BEFORE UPDATE ON real_world_path_results
BEGIN
    SELECT RAISE(ABORT, 'real_world_path_results is append-only');
END;

CREATE TRIGGER IF NOT EXISTS real_world_path_results_append_only_delete
BEFORE DELETE ON real_world_path_results
BEGIN
    SELECT RAISE(ABORT, 'real_world_path_results deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS real_world_parameterization_reports (
    report_id TEXT PRIMARY KEY,
    real_world_run_id TEXT NOT NULL REFERENCES real_world_parameterization_runs(real_world_run_id),
    report_hash TEXT NOT NULL CHECK (length(report_hash) = 64),
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(real_world_run_id)
);

CREATE TRIGGER IF NOT EXISTS real_world_parameterization_reports_append_only_update
BEFORE UPDATE ON real_world_parameterization_reports
BEGIN
    SELECT RAISE(ABORT, 'real_world_parameterization_reports is append-only');
END;

CREATE TRIGGER IF NOT EXISTS real_world_parameterization_reports_append_only_delete
BEFORE DELETE ON real_world_parameterization_reports
BEGIN
    SELECT RAISE(ABORT, 'real_world_parameterization_reports deletion is forbidden');
END;
