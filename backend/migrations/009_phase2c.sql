-- Phase 2C: episode-based opportunity validation.
-- This schema is independent from NDX_SCORE_V2.0, STRICT_PIT and the Phase
-- 2B proxy signal/outcome tables.  Episode structure and observable states
-- are written before any evaluation rows are read.

CREATE TABLE IF NOT EXISTS episode_validation_runs (
    episode_run_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    episode_model_version TEXT NOT NULL CHECK (episode_model_version = 'EPISODE_OPPORTUNITY_V1'),
    proxy_run_id TEXT NOT NULL REFERENCES proxy_research_runs(proxy_run_id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    episode_model_config_json TEXT NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_episode_validation_runs_time
    ON episode_validation_runs(market, start_date, end_date, created_at);

CREATE TRIGGER IF NOT EXISTS episode_validation_runs_completed_immutable_update
BEFORE UPDATE ON episode_validation_runs
WHEN OLD.status IN ('COMPLETED', 'FAILED')
BEGIN
    SELECT RAISE(ABORT, 'completed episode_validation_runs are immutable; create a new run');
END;

CREATE TRIGGER IF NOT EXISTS episode_validation_runs_append_only_delete
BEFORE DELETE ON episode_validation_runs
BEGIN
    SELECT RAISE(ABORT, 'episode_validation_runs is append-only; deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS drawdown_episodes (
    episode_id TEXT PRIMARY KEY,
    episode_run_id TEXT NOT NULL REFERENCES episode_validation_runs(episode_run_id),
    market TEXT NOT NULL,
    peak_date TEXT NOT NULL,
    peak_value REAL NOT NULL,
    start_date TEXT NOT NULL,
    max_drawdown REAL,
    max_drawdown_date TEXT,
    bottom_value REAL,
    bottom_date TEXT,
    recovery_date TEXT,
    duration_days INTEGER,
    duration_trading_days INTEGER,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    data_end_date TEXT NOT NULL,
    episode_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(episode_run_id, episode_id)
);

CREATE INDEX IF NOT EXISTS idx_drawdown_episodes_run_dates
    ON drawdown_episodes(episode_run_id, start_date, recovery_date, episode_id);

CREATE TRIGGER IF NOT EXISTS drawdown_episodes_append_only_update
BEFORE UPDATE ON drawdown_episodes
BEGIN
    SELECT RAISE(ABORT, 'drawdown_episodes is append-only');
END;

CREATE TRIGGER IF NOT EXISTS drawdown_episodes_append_only_delete
BEFORE DELETE ON drawdown_episodes
BEGIN
    SELECT RAISE(ABORT, 'drawdown_episodes deletion is forbidden');
END;

-- Only fields knowable on the state date are allowed in this table.  Future
-- bottom/recovery fields live in drawdown_episodes and evaluation tables.
CREATE TABLE IF NOT EXISTS episode_daily_states (
    state_id TEXT PRIMARY KEY,
    episode_run_id TEXT NOT NULL REFERENCES episode_validation_runs(episode_run_id),
    episode_id TEXT NOT NULL REFERENCES drawdown_episodes(episode_id),
    as_of_date TEXT NOT NULL,
    current_drawdown REAL,
    days_since_peak INTEGER,
    current_proxy_score REAL,
    current_proxy_percentile REAL,
    rsi14 REAL,
    distance_ma200 REAL,
    vxn REAL,
    real_yield REAL,
    nfci REAL,
    input_observation_ids_json TEXT NOT NULL,
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    created_at TEXT NOT NULL,
    UNIQUE(episode_run_id, episode_id, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_episode_daily_states_run_date
    ON episode_daily_states(episode_run_id, as_of_date, episode_id);

CREATE TRIGGER IF NOT EXISTS episode_daily_states_append_only_update
BEFORE UPDATE ON episode_daily_states
BEGIN
    SELECT RAISE(ABORT, 'episode_daily_states is append-only');
END;

CREATE TRIGGER IF NOT EXISTS episode_daily_states_append_only_delete
BEFORE DELETE ON episode_daily_states
BEGIN
    SELECT RAISE(ABORT, 'episode_daily_states deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS proxy_opportunity_events (
    opportunity_event_id TEXT PRIMARY KEY,
    episode_run_id TEXT NOT NULL REFERENCES episode_validation_runs(episode_run_id),
    episode_id TEXT NOT NULL REFERENCES drawdown_episodes(episode_id),
    proxy_run_id TEXT NOT NULL REFERENCES proxy_research_runs(proxy_run_id),
    signal_variant TEXT NOT NULL CHECK (signal_variant IN ('proxy_a', 'proxy_b')),
    event_start TEXT NOT NULL,
    event_end TEXT NOT NULL,
    first_signal_date TEXT NOT NULL,
    peak_signal_date TEXT NOT NULL,
    signal_max REAL NOT NULL,
    signal_mean REAL NOT NULL,
    drawdown_at_first_signal REAL,
    drawdown_at_peak_signal REAL,
    first_signal_id TEXT NOT NULL,
    peak_signal_id TEXT NOT NULL,
    cluster_gap_trading_days INTEGER NOT NULL CHECK (cluster_gap_trading_days = 10),
    event_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(episode_run_id, signal_variant, episode_id, first_signal_date)
);

CREATE INDEX IF NOT EXISTS idx_proxy_opportunity_events_run_date
    ON proxy_opportunity_events(episode_run_id, signal_variant, event_start, episode_id);

CREATE TRIGGER IF NOT EXISTS proxy_opportunity_events_append_only_update
BEFORE UPDATE ON proxy_opportunity_events
BEGIN
    SELECT RAISE(ABORT, 'proxy_opportunity_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS proxy_opportunity_events_append_only_delete
BEFORE DELETE ON proxy_opportunity_events
BEGIN
    SELECT RAISE(ABORT, 'proxy_opportunity_events deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS drawdown_mechanical_events (
    mechanical_event_id TEXT PRIMARY KEY,
    episode_run_id TEXT NOT NULL REFERENCES episode_validation_runs(episode_run_id),
    episode_id TEXT NOT NULL REFERENCES drawdown_episodes(episode_id),
    threshold REAL NOT NULL CHECK (threshold IN (0.1, 0.2, 0.3, 0.4, 0.5)),
    event_date TEXT NOT NULL,
    event_price REAL NOT NULL,
    drawdown REAL NOT NULL,
    source_observation_id TEXT,
    event_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(episode_run_id, episode_id, threshold)
);

CREATE INDEX IF NOT EXISTS idx_drawdown_mechanical_events_run_date
    ON drawdown_mechanical_events(episode_run_id, event_date, episode_id, threshold);

CREATE TRIGGER IF NOT EXISTS drawdown_mechanical_events_append_only_update
BEFORE UPDATE ON drawdown_mechanical_events
BEGIN
    SELECT RAISE(ABORT, 'drawdown_mechanical_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS drawdown_mechanical_events_append_only_delete
BEFORE DELETE ON drawdown_mechanical_events
BEGIN
    SELECT RAISE(ABORT, 'drawdown_mechanical_events deletion is forbidden');
END;

-- Evaluation-only table.  Episode/state/event construction never reads it.
CREATE TABLE IF NOT EXISTS episode_event_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    episode_run_id TEXT NOT NULL REFERENCES episode_validation_runs(episode_run_id),
    episode_id TEXT NOT NULL REFERENCES drawdown_episodes(episode_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('COMPOSITE', 'DRAWDOWN')),
    event_id TEXT NOT NULL,
    signal_variant TEXT,
    entry_date TEXT NOT NULL,
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
    fast_recovery INTEGER,
    missed_rebound INTEGER,
    matched_mechanical_event_id TEXT,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    evaluation_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(episode_run_id, event_type, event_id, entry_date)
);

CREATE INDEX IF NOT EXISTS idx_episode_event_evaluations_run_type
    ON episode_event_evaluations(episode_run_id, event_type, signal_variant, entry_date, episode_id);

CREATE TRIGGER IF NOT EXISTS episode_event_evaluations_append_only_update
BEFORE UPDATE ON episode_event_evaluations
BEGIN
    SELECT RAISE(ABORT, 'episode_event_evaluations is append-only');
END;

CREATE TRIGGER IF NOT EXISTS episode_event_evaluations_append_only_delete
BEFORE DELETE ON episode_event_evaluations
BEGIN
    SELECT RAISE(ABORT, 'episode_event_evaluations deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS episode_at10_assessments (
    assessment_id TEXT PRIMARY KEY,
    episode_run_id TEXT NOT NULL REFERENCES episode_validation_runs(episode_run_id),
    episode_id TEXT NOT NULL REFERENCES drawdown_episodes(episode_id),
    mechanical_event_id TEXT NOT NULL REFERENCES drawdown_mechanical_events(mechanical_event_id),
    signal_variant TEXT NOT NULL CHECK (signal_variant IN ('proxy_a', 'proxy_b')),
    trigger_date TEXT NOT NULL,
    signal_percentile REAL,
    category TEXT NOT NULL CHECK (category IN ('AGREE', 'DELAY', 'STRONGLY_OPPOSE', 'UNAVAILABLE')),
    reached_20 INTEGER NOT NULL CHECK (reached_20 IN (0, 1)),
    reached_30 INTEGER NOT NULL CHECK (reached_30 IN (0, 1)),
    reached_40 INTEGER NOT NULL CHECK (reached_40 IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(episode_run_id, episode_id, signal_variant)
);

CREATE TRIGGER IF NOT EXISTS episode_at10_assessments_append_only_update
BEFORE UPDATE ON episode_at10_assessments
BEGIN
    SELECT RAISE(ABORT, 'episode_at10_assessments is append-only');
END;

CREATE TRIGGER IF NOT EXISTS episode_at10_assessments_append_only_delete
BEFORE DELETE ON episode_at10_assessments
BEGIN
    SELECT RAISE(ABORT, 'episode_at10_assessments deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS episode_validation_reports (
    episode_report_id TEXT PRIMARY KEY,
    episode_run_id TEXT NOT NULL REFERENCES episode_validation_runs(episode_run_id),
    report_hash TEXT NOT NULL CHECK (length(report_hash) = 64),
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(episode_run_id)
);

CREATE TRIGGER IF NOT EXISTS episode_validation_reports_append_only_update
BEFORE UPDATE ON episode_validation_reports
BEGIN
    SELECT RAISE(ABORT, 'episode_validation_reports is append-only');
END;

CREATE TRIGGER IF NOT EXISTS episode_validation_reports_append_only_delete
BEFORE DELETE ON episode_validation_reports
BEGIN
    SELECT RAISE(ABORT, 'episode_validation_reports deletion is forbidden');
END;
