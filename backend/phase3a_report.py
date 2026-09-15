"""Read-only report endpoint helpers for Phase 3A."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from pit_repository import PITRepository
from phase3a_state_machine import report_from_repository

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"


def build_report(db_path: Path | str = DB) -> dict[str, Any]:
    return report_from_repository(PITRepository(db_path))


__all__ = ["build_report"]


if __name__ == "__main__":
    import json
    print(json.dumps(build_report(), ensure_ascii=False, indent=2))
