"""Machine-readable Phase 1C status report for audits and headless jobs."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import json
from zoneinfo import ZoneInfo

from pit_repository import PITRepository


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"
LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def next_planned_run(now: datetime | None = None) -> str:
    """Return the next weekday 09:30 local time used by the launchd plist."""

    current = (now or datetime.now(LOCAL_ZONE)).astimezone(LOCAL_ZONE)
    candidate = current.replace(hour=9, minute=30, second=0, microsecond=0)
    if current >= candidate or current.weekday() >= 5:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.isoformat()


def build_report(db_path: Path | str = DB) -> dict:
    repo = PITRepository(db_path)
    with repo.connect() as conn:
        schema_version = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
        origin_counts = {
            str(row[0]): int(row[1])
            for row in conn.execute("SELECT eligibility_origin, COUNT(*) FROM observation_versions GROUP BY eligibility_origin")
        }
        observation_count = int(conn.execute("SELECT COUNT(*) FROM observation_versions").fetchone()[0])
        recovery_count = int(conn.execute("SELECT COUNT(*) FROM pit_recovery_audit").fetchone()[0])
        feature_count = int(conn.execute("SELECT COUNT(*) FROM feature_snapshots").fetchone()[0])
        fetch_count = int(conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0])
        run_count = int(conn.execute("SELECT COUNT(*) FROM archive_runs").fetchone()[0])
    decisions = repo.get_decision_log("NDX", limit=1)
    latest_decision = decisions[0] if decisions else None
    features = repo.get_feature_snapshots(series_id="NDX_CLOSE")
    latest_features = {}
    for item in features:
        current = latest_features.get(item["feature_name"])
        if current is None or (item.get("created_at") or "") > (current.get("created_at") or ""):
            latest_features[item["feature_name"]] = item
    return {
        "phase": "1C",
        "status": "PASS_WITH_LIMITATIONS",
        "schema_version": schema_version,
        "historical_recovery_audit_rows": recovery_count,
        "feature_snapshot_rows": feature_count,
        "fetch_attempt_rows": fetch_count,
        "archive_run_rows": run_count,
        "observation_versions": observation_count,
        "observation_origin_counts": origin_counts,
        "recovery_audit": repo.get_recovery_audit(),
        "latest_ndx_features": list(latest_features.values()),
        "latest_strict_decision": latest_decision,
        "decision_chain": repo.verify_decision_chain("NDX"),
        "automation": {
            "headless_entrypoint": str(ROOT.parent / "dashboard_data" / "daily_archive.py"),
            "launchd_plist": str(ROOT / "automation" / "com.nasdaq.add-dashboard.daily.plist"),
            "launchd_registered": (Path.home() / "Library" / "LaunchAgents" / "com.nasdaq.add-dashboard.daily.plist").exists(),
            "next_planned_run_local": next_planned_run(),
            "page_dependency": False,
        },
        "strict_proxy_separation": True,
        "historical_upgrade_policy": "No proxy batch promotion; explicit versioned vintage evidence required.",
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Print the current Phase 1C status")
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()
    print(json.dumps(build_report(args.db), ensure_ascii=False, indent=2))
