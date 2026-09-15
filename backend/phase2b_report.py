"""Machine-readable Phase 2B research status."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pit_repository import PITRepository


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"


def _blind_run(run: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in run.items() if key not in {"summary", "error"}}


def build_report(db_path: Path | str = DB) -> dict[str, Any]:
    repo = PITRepository(db_path)
    runs = repo.get_proxy_research_runs(market="NDX", limit=1000)
    completed = [run for run in runs if run.get("status") == "COMPLETED"]
    latest = completed[0] if completed else None
    stored = repo.get_proxy_research_report(latest["proxy_run_id"]) if latest else None
    report = dict((stored or {}).get("report") or {})
    status = "PASS_WITH_LIMITATIONS" if latest and stored else "NOT_RUN"
    report.update({
        "phase": "2B",
        "PHASE_2B_STATUS": status,
        "proxy_signal_information_value": report.get("proxy_signal_information_value", "INCONCLUSIVE"),
        "composite_incremental_value_over_drawdown": report.get("composite_incremental_value_over_drawdown", "INCONCLUSIVE"),
        "NDX_SCORE_V2_STATUS": report.get("NDX_SCORE_V2_STATUS", "UNTESTED_DUE_TO_DATA"),
        "latest_run": _blind_run(latest) if latest else None,
        "runs": [_blind_run(run) for run in runs],
        "strict_proxy_separation": True,
        "next_step": "继续积累严格 PIT 历史；不得用代理结果改写 NDX_SCORE_V2.0 或 Strict Decision Log。",
    })
    return report


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Print Phase 2B research status")
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()
    print(json.dumps(build_report(args.db), ensure_ascii=False, indent=2))


__all__ = ["build_report"]
