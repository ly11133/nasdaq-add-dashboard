"""Machine-readable Phase 2F capital feasibility status."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pit_repository import PITRepository
from phase2f_research import CAPITAL_MODEL_VERSION


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"


def _blind_run(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(run, dict):
        return run
    return {key: value for key, value in run.items() if key not in {"summary", "error"}}


def build_report(db_path: Path | str = DB) -> dict[str, Any]:
    repo = PITRepository(db_path)
    runs = repo.get_capital_feasibility_runs(market="NDX", limit=1000)
    completed = [run for run in runs if run.get("status") == "COMPLETED"]
    latest = completed[0] if completed else None
    stored = repo.get_capital_feasibility_report(latest["capital_run_id"]) if latest else None
    report = dict((stored or {}).get("report") or {})
    status = report.get("PHASE_2F_STATUS", "NOT_RUN") if latest and stored else "NOT_RUN"
    report.update({
        "phase": "2F",
        "PHASE_2F_STATUS": status,
        "capital_model_version": report.get("capital_model_version", CAPITAL_MODEL_VERSION),
        "LADDER_A": report.get("LADDER_A", "INCONCLUSIVE"),
        "LADDER_B": report.get("LADDER_B", "INCONCLUSIVE"),
        "LADDER_C": report.get("LADDER_C", "INCONCLUSIVE"),
        "LADDER_D": report.get("LADDER_D", "INCONCLUSIVE"),
        "RECOMMENDED_LADDER_CANDIDATES": report.get("RECOMMENDED_LADDER_CANDIDATES", []),
        "READY_FOR_CAPITAL_STATE_MACHINE": report.get("READY_FOR_CAPITAL_STATE_MACHINE", "NO"),
        "latest_run": _blind_run(latest),
        "runs": [_blind_run(run) for run in runs],
        "research_only": True,
        "strict_pit": False,
        "investment_action_eligible": False,
        "core_dca_untouched": True,
        "overlay_used": False,
        "future_price_used": False,
        "final_wealth_used_for_selection": False,
        "next_step": "资金梯度只完成可行性压力测试；进入下一阶段前仍需人工审阅单位换算、补充节奏和状态机边界。",
    })
    return report


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Print Phase 2F capital feasibility status")
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()
    print(json.dumps(build_report(args.db), ensure_ascii=False, indent=2))


__all__ = ["build_report"]
