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
