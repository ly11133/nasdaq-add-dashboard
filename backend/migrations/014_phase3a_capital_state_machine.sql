-- Phase 3A: deterministic capital allocation state machine.
-- This layer consumes the frozen Phase 2E tactical trigger history.  It is a
-- simulation-only, append-only ledger: no positions, orders, or Core DCA
-- contributions are written here.

CREATE TABLE IF NOT EXISTS capital_state_machine_runs (
    state_machine_run_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    market TEXT NOT NULL,
    state_machine_model_version TEXT NOT NULL CHECK (state_machine_model_version = 'NDX_CAPITAL_ALLOCATION_STATE_MACHINE_V1'),
    ladder_id TEXT NOT NULL CHECK (ladder_id IN ('C', 'D')),
    ladder_version TEXT NOT NULL,
    trigger_version TEXT NOT NULL CHECK (trigger_version = 'NDX_TACTICAL_DRAWDOWN_V1'),
    phase2e_run_id TEXT REFERENCES tactical_drawdown_validation_runs(tactical_run_id),
    phase2f_run_id TEXT REFERENCES capital_feasibility_validation_runs(capital_run_id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    capital_profile_json TEXT NOT NULL,
    state_machine_config_json TEXT NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_capital_state_machine_runs_time
    ON capital_state_machine_runs(market, ladder_id, start_date, end_date, created_at);

CREATE TRIGGER IF NOT EXISTS capital_state_machine_runs_completed_immutable_update
BEFORE UPDATE ON capital_state_machine_runs
WHEN OLD.status IN ('COMPLETED', 'FAILED')
BEGIN
    SELECT RAISE(ABORT, 'completed capital_state_machine_runs are immutable; create a new run');
END;

CREATE TRIGGER IF NOT EXISTS capital_state_machine_runs_append_only_delete
BEFORE DELETE ON capital_state_machine_runs
BEGIN
    SELECT RAISE(ABORT, 'capital_state_machine_runs is append-only; deletion is forbidden');
END;

-- One row per actual capital event.  Duplicate trigger observations and ATH
-- labels are retained in the replay result payload but do not create spend
-- rows.  Same-day crossings share a transaction_id and execute in threshold
-- order.
CREATE TABLE IF NOT EXISTS capital_event_log (
    capital_event_id TEXT PRIMARY KEY,
    state_machine_run_id TEXT NOT NULL REFERENCES capital_state_machine_runs(state_machine_run_id),
    transaction_id TEXT NOT NULL,
    transaction_sequence INTEGER NOT NULL,
    event_date TEXT NOT NULL,
    tactical_cycle_id TEXT NOT NULL,
    band TEXT NOT NULL CHECK (band IN ('10', '20', '30', '40', '50')),
    trigger_drawdown REAL NOT NULL,
    ath_drawdown REAL,
    ladder_version TEXT NOT NULL,
    planned_amount REAL NOT NULL CHECK (planned_amount >= 0),
    actual_amount REAL NOT NULL CHECK (actual_amount >= 0),
    shortfall REAL NOT NULL CHECK (shortfall >= 0),
    cash_before REAL NOT NULL CHECK (cash_before >= 0),
    cash_after REAL NOT NULL CHECK (cash_after >= 0),
    fund_target REAL NOT NULL CHECK (fund_target >= 0),
    fund_cap REAL NOT NULL CHECK (fund_cap >= 0),
    monthly_refill REAL NOT NULL CHECK (monthly_refill >= 0),
    underfunded INTEGER NOT NULL CHECK (underfunded IN (0, 1)),
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    previous_event_hash TEXT,
    event_hash TEXT NOT NULL CHECK (length(event_hash) = 64),
    event_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(state_machine_run_id, transaction_id, transaction_sequence)
);

CREATE INDEX IF NOT EXISTS idx_capital_event_log_run_date
    ON capital_event_log(state_machine_run_id, event_date, transaction_id, transaction_sequence);

CREATE TRIGGER IF NOT EXISTS capital_event_log_append_only_update
BEFORE UPDATE ON capital_event_log
BEGIN
    SELECT RAISE(ABORT, 'capital_event_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS capital_event_log_append_only_delete
BEFORE DELETE ON capital_event_log
BEGIN
    SELECT RAISE(ABORT, 'capital_event_log deletion is forbidden');
END;

-- A daily observable snapshot.  The row is present even on days with no
-- capital event so the UI can answer the next-trigger question from the
-- current state alone.
CREATE TABLE IF NOT EXISTS capital_daily_snapshots (
    capital_snapshot_id TEXT PRIMARY KEY,
    state_machine_run_id TEXT NOT NULL REFERENCES capital_state_machine_runs(state_machine_run_id),
    as_of_date TEXT NOT NULL,
    tactical_cycle_id TEXT NOT NULL,
    ath_drawdown REAL,
    tactical_drawdown REAL NOT NULL,
    current_band TEXT,
    used_bands_json TEXT NOT NULL DEFAULT '[]',
    armed_bands_json TEXT NOT NULL DEFAULT '[]',
    available_opportunity_cash REAL NOT NULL CHECK (available_opportunity_cash >= 0),
    target_opportunity_cash REAL NOT NULL CHECK (target_opportunity_cash >= 0),
    opportunity_fund_cap REAL NOT NULL CHECK (opportunity_fund_cap >= 0),
    monthly_refill_rate REAL NOT NULL CHECK (monthly_refill_rate >= 0),
    surplus_cash REAL NOT NULL CHECK (surplus_cash >= 0),
    last_capital_event_date TEXT,
    last_capital_event_band TEXT,
    last_capital_event_amount REAL NOT NULL DEFAULT 0 CHECK (last_capital_event_amount >= 0),
    ladder_version TEXT NOT NULL,
    trigger_version TEXT NOT NULL,
    capital_model_version TEXT NOT NULL,
    capital_adequacy_ratio REAL,
    next_trigger_json TEXT NOT NULL DEFAULT '{}',
    core_dca_untouched INTEGER NOT NULL CHECK (core_dca_untouched IN (0, 1)),
    state_hash TEXT NOT NULL CHECK (length(state_hash) = 64),
    previous_state_hash TEXT,
    snapshot_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(state_machine_run_id, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_capital_daily_snapshots_run_date
    ON capital_daily_snapshots(state_machine_run_id, as_of_date);

CREATE TRIGGER IF NOT EXISTS capital_daily_snapshots_append_only_update
BEFORE UPDATE ON capital_daily_snapshots
BEGIN
    SELECT RAISE(ABORT, 'capital_daily_snapshots is append-only');
END;

CREATE TRIGGER IF NOT EXISTS capital_daily_snapshots_append_only_delete
BEFORE DELETE ON capital_daily_snapshots
BEGIN
    SELECT RAISE(ABORT, 'capital_daily_snapshots deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS capital_state_machine_reports (
    capital_report_id TEXT PRIMARY KEY,
    state_machine_run_id TEXT NOT NULL REFERENCES capital_state_machine_runs(state_machine_run_id),
    report_hash TEXT NOT NULL CHECK (length(report_hash) = 64),
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(state_machine_run_id)
);

CREATE TRIGGER IF NOT EXISTS capital_state_machine_reports_append_only_update
BEFORE UPDATE ON capital_state_machine_reports
BEGIN
    SELECT RAISE(ABORT, 'capital_state_machine_reports is append-only');
END;

CREATE TRIGGER IF NOT EXISTS capital_state_machine_reports_append_only_delete
BEFORE DELETE ON capital_state_machine_reports
BEGIN
    SELECT RAISE(ABORT, 'capital_state_machine_reports deletion is forbidden');
END;
