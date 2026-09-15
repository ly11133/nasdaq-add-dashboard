-- Correct the identifier used by an early Phase 1A archive. This is metadata
-- only; raw fetches and observation versions remain append-only and untouched.
INSERT INTO data_series(
    series_id, display_name, asset_id, metric, unit, methodology,
    qualification_class, score_eligible_default, created_at
) VALUES(
    'NDX_FORWARDOWN',
    'Nasdaq-100提供商自算Forward PE（旧归档别名）',
    'NDX',
    'forward_pe',
    'multiple',
    'Legacy Phase 1A archive alias; never merge with terminal Forward PE.',
    'CANDIDATE_ONLY',
    0,
    '2026-09-14T00:00:00Z'
)
ON CONFLICT(series_id) DO UPDATE SET
    display_name=excluded.display_name,
    metric=excluded.metric,
    unit=excluded.unit,
    methodology=excluded.methodology,
    qualification_class=excluded.qualification_class,
    score_eligible_default=excluded.score_eligible_default;
