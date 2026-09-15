PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_series (
    series_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    unit TEXT,
    methodology TEXT NOT NULL,
    qualification_class TEXT NOT NULL CHECK (qualification_class IN ('PIT_ELIGIBLE', 'PIT_PROXY', 'CANDIDATE_ONLY', 'UNAVAILABLE')),
    score_eligible_default INTEGER NOT NULL CHECK (score_eligible_default IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_fetches (
    raw_fetch_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_version TEXT,
    vintage TEXT,
    retrieved_at TEXT NOT NULL,
    http_status INTEGER,
    raw_hash TEXT NOT NULL CHECK (length(raw_hash) = 64),
    raw_path TEXT NOT NULL,
    byte_count INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (raw_hash, raw_path)
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES data_series(series_id),
    observation_date TEXT NOT NULL,
    unit TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (series_id, observation_date)
);

CREATE TABLE IF NOT EXISTS observation_versions (
    observation_version_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES observations(observation_id),
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    publication_at TEXT,
    available_at TEXT,
    retrieved_at TEXT NOT NULL,
    source_version TEXT,
    vintage TEXT,
    value_real REAL,
    value_text TEXT,
    value_json TEXT,
    methodology TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    score_eligible INTEGER NOT NULL CHECK (score_eligible IN (0, 1)),
    raw_fetch_id TEXT NOT NULL REFERENCES raw_fetches(raw_fetch_id),
    raw_hash TEXT NOT NULL CHECK (length(raw_hash) = 64),
    inserted_at TEXT NOT NULL,
    UNIQUE (observation_id, version_no),
    UNIQUE (observation_id, raw_fetch_id),
    CHECK (value_real IS NOT NULL OR value_text IS NOT NULL OR value_json IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_observation_versions_pit_lookup
    ON observation_versions(observation_id, score_eligible, available_at, observation_version_id);
CREATE INDEX IF NOT EXISTS idx_observations_series_date
    ON observations(series_id, observation_date);
CREATE INDEX IF NOT EXISTS idx_raw_fetches_retrieved
    ON raw_fetches(retrieved_at, source);

CREATE TABLE IF NOT EXISTS score_models (
    model_version TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    component_config TEXT NOT NULL,
    weights TEXT NOT NULL,
    thresholds TEXT NOT NULL,
    gate_rules TEXT NOT NULL,
    eligibility_rules TEXT NOT NULL,
    config_hash TEXT NOT NULL UNIQUE CHECK (length(config_hash) = 64)
);

CREATE TRIGGER IF NOT EXISTS raw_fetches_append_only_update
BEFORE UPDATE ON raw_fetches
BEGIN
    SELECT RAISE(ABORT, 'raw_fetches is append-only; insert a new fetch instead');
END;

CREATE TRIGGER IF NOT EXISTS raw_fetches_append_only_delete
BEFORE DELETE ON raw_fetches
BEGIN
    SELECT RAISE(ABORT, 'raw_fetches is append-only; deletion is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS observation_versions_append_only_update
BEFORE UPDATE ON observation_versions
BEGIN
    SELECT RAISE(ABORT, 'observation_versions is append-only; insert a new version instead');
END;

CREATE TRIGGER IF NOT EXISTS observation_versions_append_only_delete
BEFORE DELETE ON observation_versions
BEGIN
    SELECT RAISE(ABORT, 'observation_versions is append-only; deletion is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS score_models_append_only_update
BEFORE UPDATE ON score_models
BEGIN
    SELECT RAISE(ABORT, 'score_models is append-only; insert a new model version instead');
END;

CREATE TRIGGER IF NOT EXISTS score_models_append_only_delete
BEFORE DELETE ON score_models
BEGIN
    SELECT RAISE(ABORT, 'score_models is append-only; deletion is forbidden');
END;
