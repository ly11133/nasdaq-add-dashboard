-- Phase 2F: capital-ladder feasibility and dry-powder stress tests.
-- This is a research ledger only.  It consumes frozen Phase 2E tactical
-- events and never writes a trade, position or contribution instruction.

CREATE TABLE IF NOT EXISTS capital_feasibility_validation_runs (
    capital_run_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    capital_model_version TEXT NOT NULL CHECK (capital_model_version = 'NDX_CAPITAL_LADDER_FEASIBILITY_V1'),
    phase2e_run_id TEXT NOT NULL REFERENCES tactical_drawdown_validation_runs(tactical_run_id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    capital_model_config_json TEXT NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_capital_feasibility_runs_time
    ON capital_feasibility_validation_runs(market, start_date, end_date, created_at);

CREATE TRIGGER IF NOT EXISTS capital_feasibility_runs_completed_immutable_update
BEFORE UPDATE ON capital_feasibility_validation_runs
WHEN OLD.status IN ('COMPLETED', 'FAILED')
BEGIN
    SELECT RAISE(ABORT, 'completed capital_feasibility_validation_runs are immutable; create a new run');
END;

CREATE TRIGGER IF NOT EXISTS capital_feasibility_runs_append_only_delete
BEFORE DELETE ON capital_feasibility_validation_runs
BEGIN
    SELECT RAISE(ABORT, 'capital_feasibility_validation_runs is append-only; deletion is forbidden');
END;

-- One row per path/configuration replay.  ``result_json`` contains the
-- auditable aggregate metrics; event-level cash accounting is in the ledger
-- below so it can be independently replayed.
CREATE TABLE IF NOT EXISTS capital_feasibility_scenario_results (
    scenario_result_id TEXT PRIMARY KEY,
    capital_run_id TEXT NOT NULL REFERENCES capital_feasibility_validation_runs(capital_run_id),
    path_type TEXT NOT NULL CHECK (path_type IN ('HISTORICAL', 'SYNTHETIC', 'SEQUENCE', 'EXTREME_EXTENSION')),
    path_id TEXT NOT NULL,
    ladder_id TEXT NOT NULL CHECK (ladder_id IN ('A', 'B', 'C', 'D')),
    replenishment_id TEXT NOT NULL CHECK (replenishment_id IN ('R0', 'R1', 'R2')),
    cap_id TEXT NOT NULL CHECK (cap_id IN ('CAP_6M', 'CAP_12M', 'CAP_24M')),
    initial_units REAL NOT NULL,
    m_to_opportunity_units REAL NOT NULL,
    sample_event_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OK', 'INCONCLUSIVE', 'FAILED')),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(capital_run_id, path_type, path_id, ladder_id, replenishment_id, cap_id)
);

CREATE INDEX IF NOT EXISTS idx_capital_scenario_run
    ON capital_feasibility_scenario_results(capital_run_id, path_type, path_id, ladder_id, replenishment_id, cap_id);

CREATE TRIGGER IF NOT EXISTS capital_scenario_append_only_update
BEFORE UPDATE ON capital_feasibility_scenario_results
BEGIN
    SELECT RAISE(ABORT, 'capital_feasibility_scenario_results is append-only');
END;

CREATE TRIGGER IF NOT EXISTS capital_scenario_append_only_delete
BEFORE DELETE ON capital_feasibility_scenario_results
BEGIN
    SELECT RAISE(ABORT, 'capital_feasibility_scenario_results deletion is forbidden');
END;

-- Chronological event ledger.  It records one capital event for each frozen
-- tactical crossing.  ATH labels and overlay values can be present in the
-- payload for audit, but never alter ``deployed_units``.
CREATE TABLE IF NOT EXISTS capital_feasibility_ledger (
    ledger_entry_id TEXT PRIMARY KEY,
    capital_run_id TEXT NOT NULL REFERENCES capital_feasibility_validation_runs(capital_run_id),
    scenario_result_id TEXT NOT NULL REFERENCES capital_feasibility_scenario_results(scenario_result_id),
    path_type TEXT NOT NULL,
    path_id TEXT NOT NULL,
    event_sequence INTEGER NOT NULL,
    event_date TEXT NOT NULL,
    tactical_cycle_id TEXT,
    macro_episode_id TEXT,
    threshold REAL,
    event_kind TEXT NOT NULL CHECK (event_kind IN ('TACTICAL_BAND', 'EXTENSION_OBSERVATION')),
    cash_before_units REAL NOT NULL,
    required_units REAL NOT NULL,
    deployed_units REAL NOT NULL,
    cash_after_units REAL NOT NULL,
    replenishment_units REAL NOT NULL DEFAULT 0,
    surplus_units REAL NOT NULL DEFAULT 0,
    underfunded_event INTEGER NOT NULL CHECK (underfunded_event IN (0, 1)),
    ath_state_ignored INTEGER NOT NULL CHECK (ath_state_ignored IN (0, 1)),
    input_observation_ids_json TEXT NOT NULL DEFAULT '[]',
    ledger_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(scenario_result_id, event_sequence)
);

CREATE INDEX IF NOT EXISTS idx_capital_ledger_run_path
    ON capital_feasibility_ledger(capital_run_id, path_type, path_id, event_date, event_sequence);

CREATE TRIGGER IF NOT EXISTS capital_ledger_append_only_update
BEFORE UPDATE ON capital_feasibility_ledger
BEGIN
    SELECT RAISE(ABORT, 'capital_feasibility_ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS capital_ledger_append_only_delete
BEFORE DELETE ON capital_feasibility_ledger
BEGIN
    SELECT RAISE(ABORT, 'capital_feasibility_ledger deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS capital_feasibility_reports (
    capital_report_id TEXT PRIMARY KEY,
    capital_run_id TEXT NOT NULL REFERENCES capital_feasibility_validation_runs(capital_run_id),
    report_hash TEXT NOT NULL CHECK (length(report_hash) = 64),
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(capital_run_id)
);

CREATE TRIGGER IF NOT EXISTS capital_reports_append_only_update
BEFORE UPDATE ON capital_feasibility_reports
BEGIN
    SELECT RAISE(ABORT, 'capital_feasibility_reports is append-only');
END;

CREATE TRIGGER IF NOT EXISTS capital_reports_append_only_delete
BEFORE DELETE ON capital_feasibility_reports
BEGIN
    SELECT RAISE(ABORT, 'capital_feasibility_reports deletion is forbidden');
END;
