-- Phase 1B metadata needed to distinguish PE/EPS periods and future breadth
-- membership proofs.  Existing rows receive an empty metadata object; their
-- eligibility and timestamps remain unchanged.
ALTER TABLE observation_versions
    ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_observation_versions_metadata
    ON observation_versions(eligibility_origin, source_version);
