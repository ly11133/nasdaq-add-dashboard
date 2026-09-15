-- Phase 1B: strict PIT eligibility and append-only decision audit.
-- Existing observation versions are retained as historical proxies.  The
-- default below labels them explicitly; no value or timestamp is rewritten.
ALTER TABLE observation_versions
    ADD COLUMN eligibility_origin TEXT NOT NULL DEFAULT 'HISTORICAL_PROXY'
    CHECK (eligibility_origin IN (
        'HISTORICAL_PROXY', 'OBSERVED_LIVE', 'PROVIDER_VINTAGE_VERIFIED',
        'MANUAL_VERIFIED', 'CANDIDATE'
    ));

CREATE INDEX IF NOT EXISTS idx_observation_versions_strict_lookup
    ON observation_versions(observation_id, score_eligible, eligibility_origin, available_at);

CREATE TABLE IF NOT EXISTS decision_log (
    decision_id TEXT PRIMARY KEY,
    as_of_datetime TEXT NOT NULL,
    market TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('LEGACY', 'RESEARCH_PROXY', 'STRICT_PIT')),
    score_model_version TEXT NOT NULL REFERENCES score_models(model_version),
    config_hash TEXT NOT NULL CHECK (length(config_hash) = 64),
    data_cutoff TEXT NOT NULL,
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    coverage REAL NOT NULL CHECK (coverage >= 0 AND coverage <= 100),
    score REAL,
    gate_status TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason_json TEXT NOT NULL,
    missing_items_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    previous_decision_hash TEXT,
    decision_hash TEXT NOT NULL UNIQUE CHECK (length(decision_hash) = 64)
);

CREATE INDEX IF NOT EXISTS idx_decision_log_market_time
    ON decision_log(market, as_of_datetime, created_at);
CREATE INDEX IF NOT EXISTS idx_decision_log_input
    ON decision_log(score_model_version, input_hash);

CREATE TRIGGER IF NOT EXISTS decision_log_append_only_update
BEFORE UPDATE ON decision_log
BEGIN
    SELECT RAISE(ABORT, 'decision_log is append-only; insert a new decision instead');
END;

CREATE TRIGGER IF NOT EXISTS decision_log_append_only_delete
BEFORE DELETE ON decision_log
BEGIN
    SELECT RAISE(ABORT, 'decision_log is append-only; deletion is forbidden');
END;
