"""Machine-readable Phase 2E tactical drawdown status.

The default status and run-list responses intentionally omit stored evaluation
summaries.  A caller must explicitly request a report/outcomes payload, which
keeps the date-view API contemporaneous by default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pit_repository import PITRepository
from phase2e_research import TACTICAL_MODEL_VERSION


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"


def _blind_run(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(run, dict):
        return run
    return {key: value for key, value in run.items() if key not in {"summary", "error"}}


def build_report(db_path: Path | str = DB) -> dict[str, Any]:
    repo = PITRepository(db_path)
    runs = repo.get_tactical_drawdown_runs(market="NDX", limit=1000)
    completed = [run for run in runs if run.get("status") == "COMPLETED"]
    latest = completed[0] if completed else None
    stored = repo.get_tactical_drawdown_report(latest["tactical_run_id"]) if latest else None
    report = dict((stored or {}).get("report") or {})
    status = "PASS_WITH_LIMITATIONS" if latest and stored else "NOT_RUN"
    report.update({
        "phase": "2E",
        "PHASE_2E_STATUS": status,
        "tactical_model_version": report.get("tactical_model_version", TACTICAL_MODEL_VERSION),
        "TACTICAL_DRAWDOWN_SIGNAL": report.get("TACTICAL_DRAWDOWN_SIGNAL", "INCONCLUSIVE"),
        "DRAWDOWN_REFERENCE": report.get("DRAWDOWN_REFERENCE", "INCONCLUSIVE"),
        "OVERALL_OVERLAY_INCREMENTAL_VALUE": report.get("OVERALL_OVERLAY_INCREMENTAL_VALUE", "INCONCLUSIVE"),
        "READY_FOR_CAPITAL_STATE_MACHINE": report.get("READY_FOR_CAPITAL_STATE_MACHINE", "NO"),
        "latest_run": _blind_run(latest),
        "runs": [_blind_run(run) for run in runs],
        "research_only": True,
        "strict_pit": False,
        "investment_action_eligible": False,
        "overlay_can_veto": False,
        "macro_episode_parent_preserved": True,
        "next_step": (
            "继续按自然日历验证；在 Tactical signal、密度、LOEO 与 Overlay 结论满足冻结门槛前，"
            "不进入资金状态机。"
        ),
    })
    return report


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Print Phase 2E tactical drawdown status")
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()
    print(json.dumps(build_report(args.db), ensure_ascii=False, indent=2))


__all__ = ["build_report"]
