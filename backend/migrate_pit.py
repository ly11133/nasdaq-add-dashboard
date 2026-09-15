"""Run the Phase 1A–3B SQLite migrations and register the frozen score model."""

from __future__ import annotations

from pathlib import Path
import json

from pit_repository import migrate_database, register_score_model


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"


def main() -> None:
    migration = migrate_database(DB)
    model = register_score_model(DB)
    print(json.dumps({"database": str(DB), "migration": migration, "score_model": {"model_version": model["model_version"], "config_hash": model["config_hash"]}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
