-- Phase 1C: historical recovery audit, derived feature snapshots and
-- independent archive-run observability.  Every table in this migration is
-- append-only; changing a rule creates a new version instead of rewriting an
-- earlier qualification or feature.

ALTER TABLE observation_versions
    ADD COLUMN eligibility_rule_version TEXT;

ALTER TABLE observation_versions
    ADD COLUMN eligibility_evidence_json TEXT;

CREATE INDEX IF NOT EXISTS idx_observation_versions_rule
    ON observation_versions(eligibility_rule_version, eligibility_origin);

CREATE TABLE IF NOT EXISTS pit_recovery_audit (
    audit_id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    historical_source TEXT NOT NULL,
    revision_behavior TEXT NOT NULL,
    publication_semantics TEXT NOT NULL,
    market_close_semantics TEXT NOT NULL,
    vintage_support TEXT NOT NULL,
    candidate_available_at_rule TEXT NOT NULL,
    can_upgrade_historical INTEGER NOT NULL CHECK (can_upgrade_historical IN (0, 1)),
    classification TEXT NOT NULL CHECK (classification IN (
        'STRICT_HISTORICAL_ELIGIBLE',
        'ELIGIBLE_WITH_CONSERVATIVE_DELAY',
        'PROXY_ONLY',
        'UNRESOLVED'
    )),
    confidence TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    audited_at TEXT NOT NULL,
    UNIQUE(series_id, rule_version)
);

CREATE INDEX IF NOT EXISTS idx_pit_recovery_audit_series
    ON pit_recovery_audit(series_id, audited_at);

CREATE TRIGGER IF NOT EXISTS pit_recovery_audit_append_only_update
BEFORE UPDATE ON pit_recovery_audit
BEGIN
    SELECT RAISE(ABORT, 'pit_recovery_audit is append-only; insert a new rule version instead');
END;

CREATE TRIGGER IF NOT EXISTS pit_recovery_audit_append_only_delete
BEFORE DELETE ON pit_recovery_audit
BEGIN
    SELECT RAISE(ABORT, 'pit_recovery_audit is append-only; deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS feature_snapshots (
    feature_id TEXT PRIMARY KEY,
    feature_name TEXT NOT NULL,
    series_id TEXT NOT NULL,
    feature_as_of TEXT NOT NULL,
    as_of_datetime TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    value REAL,
    value_json TEXT,
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    input_observation_ids_json TEXT NOT NULL,
    score_eligible INTEGER NOT NULL CHECK (score_eligible IN (0, 1)),
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(feature_name, series_id, feature_as_of, feature_version, input_hash),
    CHECK (value IS NOT NULL OR value_json IS NOT NULL OR score_eligible = 0)
);

CREATE INDEX IF NOT EXISTS idx_feature_snapshots_lookup
    ON feature_snapshots(series_id, feature_name, feature_as_of, score_eligible);

CREATE TRIGGER IF NOT EXISTS feature_snapshots_append_only_update
BEFORE UPDATE ON feature_snapshots
BEGIN
    SELECT RAISE(ABORT, 'feature_snapshots is append-only; insert a new feature version instead');
END;

CREATE TRIGGER IF NOT EXISTS feature_snapshots_append_only_delete
BEFORE DELETE ON feature_snapshots
BEGIN
    SELECT RAISE(ABORT, 'feature_snapshots is append-only; deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS freshness_rules (
    rule_id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL,
    max_age_days INTEGER NOT NULL CHECK (max_age_days >= 0),
    frequency TEXT NOT NULL,
    expected_release_pattern TEXT NOT NULL,
    stale_behavior TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(series_id, rule_version)
);

CREATE INDEX IF NOT EXISTS idx_freshness_rules_series
    ON freshness_rules(series_id, rule_version);

CREATE TRIGGER IF NOT EXISTS freshness_rules_append_only_update
BEFORE UPDATE ON freshness_rules
BEGIN
    SELECT RAISE(ABORT, 'freshness_rules is append-only; insert a new rule version instead');
END;

CREATE TRIGGER IF NOT EXISTS freshness_rules_append_only_delete
BEFORE DELETE ON freshness_rules
BEGIN
    SELECT RAISE(ABORT, 'freshness_rules is append-only; deletion is forbidden');
END;

INSERT OR IGNORE INTO freshness_rules
    (rule_id, series_id, max_age_days, frequency, expected_release_pattern, stale_behavior, rule_version, created_at)
VALUES
    ('NDX_CLOSE:V1', 'NDX_CLOSE', 7, 'daily', 'US market close; next business-day retrieval is acceptable', 'STALE_REJECT', 'FRESHNESS_RULES_V1', '2026-09-14T00:00:00Z'),
    ('NDX_VXN:V1', 'NDX_VXN', 7, 'daily', 'US market close; next business-day retrieval is acceptable', 'STALE_REJECT', 'FRESHNESS_RULES_V1', '2026-09-14T00:00:00Z'),
    ('VIX:V1', 'VIX', 7, 'daily', 'US market close; next business-day retrieval is acceptable', 'STALE_REJECT', 'FRESHNESS_RULES_V1', '2026-09-14T00:00:00Z'),
    ('US10Y_REAL:V1', 'US10Y_REAL', 7, 'daily', 'H.15 daily release; conservative next-day use', 'STALE_REJECT', 'FRESHNESS_RULES_V1', '2026-09-14T00:00:00Z'),
    ('US_NFCI:V1', 'US_NFCI', 14, 'weekly', 'Chicago Fed weekly ending Friday; publication day may lag observation day', 'WITHIN_WINDOW', 'FRESHNESS_RULES_V1', '2026-09-14T00:00:00Z'),
    ('NDX_FORWARD_PE:V1', 'NDX_FORWARD_PE', 14, 'provider_defined', 'provider refresh; no assumed daily release', 'STALE_REJECT', 'FRESHNESS_RULES_V1', '2026-09-14T00:00:00Z'),
    ('NDX_TTM_PE:V1', 'NDX_TTM_PE', 14, 'provider_defined', 'provider refresh; no assumed daily release', 'STALE_REJECT', 'FRESHNESS_RULES_V1', '2026-09-14T00:00:00Z'),
    ('NDX_FORWARD_EPS:V1', 'NDX_FORWARD_EPS', 14, 'provider_defined', 'provider refresh; no assumed daily release', 'STALE_REJECT', 'FRESHNESS_RULES_V1', '2026-09-14T00:00:00Z'),
    ('NDX_EPS_REVISION:V1', 'NDX_EPS_REVISION', 45, 'provider_defined', 'same fiscal-period snapshot; no assumed daily release', 'STALE_REJECT', 'FRESHNESS_RULES_V1', '2026-09-14T00:00:00Z'),
    ('NDX_BREADTH_MA200:V1', 'NDX_BREADTH_MA200', 7, 'daily', 'same-day complete member set required', 'STALE_REJECT', 'FRESHNESS_RULES_V1', '2026-09-14T00:00:00Z');

CREATE TABLE IF NOT EXISTS fetch_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    retry_count INTEGER NOT NULL CHECK (retry_count >= 0),
    raw_fetch_id TEXT,
    http_status INTEGER,
    raw_hash TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, source, attempted_at, retry_count, status)
);

CREATE INDEX IF NOT EXISTS idx_fetch_attempts_run
    ON fetch_attempts(run_id, attempted_at, source);

CREATE TRIGGER IF NOT EXISTS fetch_attempts_append_only_update
BEFORE UPDATE ON fetch_attempts
BEGIN
    SELECT RAISE(ABORT, 'fetch_attempts is append-only; insert a new attempt instead');
END;

CREATE TRIGGER IF NOT EXISTS fetch_attempts_append_only_delete
BEFORE DELETE ON fetch_attempts
BEGIN
    SELECT RAISE(ABORT, 'fetch_attempts is append-only; deletion is forbidden');
END;

CREATE TABLE IF NOT EXISTS archive_runs (
    run_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    as_of_datetime TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    strict_decision_hash TEXT,
    strict_coverage REAL,
    chain_valid INTEGER CHECK (chain_valid IN (0, 1)),
    error_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_archive_runs_profile_time
    ON archive_runs(profile_id, as_of_datetime, started_at);

CREATE TRIGGER IF NOT EXISTS archive_runs_append_only_update
BEFORE UPDATE ON archive_runs
BEGIN
    SELECT RAISE(ABORT, 'archive_runs is append-only; duplicate runs must use a new run id');
END;

CREATE TRIGGER IF NOT EXISTS archive_runs_append_only_delete
BEFORE DELETE ON archive_runs
BEGIN
    SELECT RAISE(ABORT, 'archive_runs is append-only; deletion is forbidden');
END;
