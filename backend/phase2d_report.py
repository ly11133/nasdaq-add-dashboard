"""Machine-readable Phase 2D drawdown-overlay status."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pit_repository import PITRepository
from phase2d_research import build_overlay_report


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"


def _blind_run(run: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in run.items() if key not in {"summary", "error"}}


def build_report(db_path: Path | str = DB) -> dict[str, Any]:
    repo = PITRepository(db_path)
    runs = repo.get_drawdown_overlay_runs(market="NDX", limit=1000)
    completed = [run for run in runs if run.get("status") == "COMPLETED"]
    latest = completed[0] if completed else None
    report_row = repo.get_drawdown_overlay_report(latest["overlay_run_id"]) if latest else None
    report = dict((report_row or {}).get("report") or {})
    status = "PASS_WITH_LIMITATIONS" if latest and report_row else "NOT_RUN"
    report.update({
        "phase": "2D",
        "PHASE_2D_STATUS": status,
        "DRAWDOWN_PRIMARY_SIGNAL": report.get("DRAWDOWN_PRIMARY_SIGNAL", "INCONCLUSIVE"),
        "OVERALL_OVERLAY_INCREMENTAL_VALUE": report.get("OVERALL_OVERLAY_INCREMENTAL_VALUE", "INCONCLUSIVE"),
        "RSI_OVERLAY_VALUE": report.get("RSI_OVERLAY_VALUE", "INCONCLUSIVE"),
        "MA200_OVERLAY_VALUE": report.get("MA200_OVERLAY_VALUE", "INCONCLUSIVE"),
        "VXN_OVERLAY_VALUE": report.get("VXN_OVERLAY_VALUE", "INCONCLUSIVE"),
        "REAL_YIELD_OVERLAY_VALUE": report.get("REAL_YIELD_OVERLAY_VALUE", "INCONCLUSIVE"),
        "NFCI_OVERLAY_VALUE": report.get("NFCI_OVERLAY_VALUE", "INCONCLUSIVE"),
        "NEXT_ARCHITECTURE": report.get("NEXT_ARCHITECTURE", "MORE_DATA_REQUIRED"),
        "latest_run": _blind_run(latest) if latest else None,
        "runs": [_blind_run(run) for run in runs],
        "strict_drawdown_primary": True,
        "overlay_can_veto": False,
        "strict_proxy_separation": True,
        "next_step": "继续收集可验证的历史发布时点；在 Overlay 结论明确或全部为 NONE/WEAK 前，不进入资金状态机。",
    })
    return report


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Print Phase 2D drawdown overlay status")
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()
    print(json.dumps(build_report(args.db), ensure_ascii=False, indent=2))


__all__ = ["build_report"]
