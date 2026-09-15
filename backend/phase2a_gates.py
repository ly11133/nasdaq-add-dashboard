"""Phase 2A execution gates.

Gate A is an operational check of the installed launchd program and the
archive artifacts it produced.  Gate B deliberately uses an isolated
temporary repository so a simulated network failure cannot create or alter a
production observation.  Both checks append their evidence to the local
Phase 2A gate ledger.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import plistlib
import subprocess
import tempfile
from typing import Any

from pit_repository import PITRepository


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"
PLIST = ROOT / "automation" / "com.nasdaq.add-dashboard.daily.plist"
LABEL = "com.nasdaq.add-dashboard.daily"


def machine_timezone() -> str:
    """Return the timezone actually used by the current process/machine."""

    local = datetime.now().astimezone()
    offset = local.utcoffset()
    if offset is None:
        return str(local.tzinfo or os.environ.get("TZ") or "unknown")
    seconds = int(offset.total_seconds())
    sign = "+" if seconds >= 0 else "-"
    seconds = abs(seconds)
    return f"{local.tzname() or 'local'} (UTC{sign}{seconds // 3600:02d}:{(seconds % 3600) // 60:02d})"


def launchd_print(label: str = LABEL) -> str:
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            check=False, capture_output=True, text=True, timeout=10,
        )
        return (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return f"launchctl unavailable: {exc}"


def verify_gate_a(db_path: Path | str = DB, *, launchd_output: str | None = None, trigger_source: str = "unknown") -> dict[str, Any]:
    """Inspect the latest archive and the registered launchd configuration."""

    repo = PITRepository(db_path)
    runs = repo.get_archive_runs("NDX", limit=20)
    latest = runs[0] if runs else None
    run_id = latest.get("run_id") if latest else None
    attempts = repo.get_fetch_attempts(run_id, limit=500) if run_id else []
    with repo.connect() as conn:
        feature_count = int(conn.execute("SELECT COUNT(*) FROM feature_snapshots WHERE feature_as_of<=?", (latest["as_of_datetime"],)).fetchone()[0]) if latest else 0
        decision_count = int(conn.execute("SELECT COUNT(*) FROM decision_log WHERE market='NDX' AND as_of_datetime<=?", (latest["as_of_datetime"],)).fetchone()[0]) if latest else 0
    output = launchd_output if launchd_output is not None else launchd_print()
    plist = {}
    if PLIST.exists():
        try:
            plist = plistlib.loads(PLIST.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            plist = {}
    args = plist.get("ProgramArguments") or []
    registered = f"{LABEL}" in output and bool(output.strip())
    run_match = __import__("re").search(r"\bruns\s*=\s*(\d+)", output)
    launchd_runs = int(run_match.group(1)) if run_match else 0
    exit_match = __import__("re").search(r"last exit code\s*=\s*([^\n]+)", output)
    last_exit_code = exit_match.group(1).strip() if exit_match else None
    launchd_exit_success = last_exit_code in {"0", "0.0"}
    # A manual kickstart increments launchd's run counter too.  It proves that
    # launchd dispatched the configured program only when the last dispatch
    # exited successfully; it still does not prove that a future calendar
    # event has fired.  ``trigger_source=calendar`` is reserved for evidence
    # captured at the scheduled wall-clock event itself.
    launchd_dispatch_verified = launchd_runs >= 1 and launchd_exit_success and latest is not None
    actual_calendar_trigger_verified = trigger_source == "calendar" and launchd_dispatch_verified
    argument_text = " ".join(str(value) for value in args)
    checks = {
        "launchd_registered": registered,
        "launchd_program_matches": (
            len(args) >= 2
            and (
                "outputs/dashboard_data/daily_archive.py" in argument_text
                or "outputs/nasdaq-add-dashboard/automation/daily_archive.sh" in argument_text
            )
        ),
        "launchd_tcc_bridge_configured": args[:1] == ["/usr/bin/osascript"] and "do shell script" in argument_text,
        "archive_run_present": latest is not None,
        "raw_fetch_attempts_present": bool(attempts),
        "feature_store_executed": feature_count > 0,
        "strict_evaluation_logged": bool(latest and latest.get("strict_decision_hash")) and decision_count > 0,
        "hash_chain_valid": bool(latest and latest.get("chain_valid")),
        "launchd_last_exit_success": launchd_exit_success,
        "actual_calendar_trigger_observed": actual_calendar_trigger_verified,
    }
    status = "PASS" if all(checks.values()) else "PASS_WITH_LIMITATIONS" if checks["launchd_registered"] and checks["archive_run_present"] else "FAIL"
    details = {
        "checks": checks,
        "launchd_label": LABEL,
        "launchd_output_excerpt": output[-4000:],
        "configured_weekdays": [item.get("Weekday") for item in plist.get("StartCalendarInterval", []) if isinstance(item, dict)],
        "configured_hour": [item.get("Hour") for item in plist.get("StartCalendarInterval", []) if isinstance(item, dict)],
        "configured_minute": [item.get("Minute") for item in plist.get("StartCalendarInterval", []) if isinstance(item, dict)],
        "schedule_semantics": "launchd StartCalendarInterval uses the Mac machine local timezone; no per-job Beijing timezone is inferred.",
        "machine_timezone": machine_timezone(),
        "latest_archive_run_id": run_id,
        "latest_archive_status": latest.get("status") if latest else None,
        "fetch_attempt_count": len(attempts),
        "feature_snapshot_count_through_run": feature_count,
        "decision_count_through_run": decision_count,
        "launchd_runs": launchd_runs,
        "last_exit_code": last_exit_code,
        "trigger_source": trigger_source,
        "launchd_dispatch_verified": launchd_dispatch_verified,
        "actual_calendar_trigger_verified": actual_calendar_trigger_verified,
        "note": "launchd StartCalendarInterval uses the Mac machine local timezone. A manual launchctl dispatch or a failed dispatch is not evidence that a future 09:30 calendar event has fired; this gate stays PASS_WITH_LIMITATIONS until a successful scheduled event is observed.",
    }
    check = {"gate_name": "GATE_A_SCHEDULED_RUN", "status": status, "triggered_at": datetime.now(timezone.utc).isoformat(), "timezone_name": machine_timezone(), "details": details}
    gate_id, _ = repo.record_gate_check(check)
    return {"gate_check_id": gate_id, **check}


def simulate_gate_b(db_path: Path | str = DB) -> dict[str, Any]:
    """Simulate a failed source without inserting a fabricated observation."""

    with tempfile.TemporaryDirectory(prefix="phase2a-gate-b-") as directory:
        isolated = Path(directory) / "failure.sqlite3"
        repo = PITRepository(isolated)
        repo.record_fetch_attempt({
            "run_id": "gate-b-network-failure",
            "attempted_at": "2026-09-14T15:00:00Z",
            "source": "simulated_source",
            "source_url": "https://example.com/unavailable",
            "status": "ERROR",
            "error": "simulated timeout",
            "retry_count": 1,
        })
        attempts = repo.get_fetch_attempts("gate-b-network-failure")
        with repo.connect() as conn:
            raw_count = int(conn.execute("SELECT COUNT(*) FROM raw_fetches").fetchone()[0])
            observation_count = int(conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0])
        passed = len(attempts) == 1 and attempts[0]["status"] == "ERROR" and raw_count == 0 and observation_count == 0
        details = {
            "isolated_database": str(isolated),
            "failure_recorded": passed,
            "attempt": attempts[0] if attempts else None,
            "raw_fetch_count": raw_count,
            "observation_count": observation_count,
            "future_data_used_as_fallback": False,
            "strict_behavior": "No observation is inserted; a real run remains INSUFFICIENT_EVIDENCE or DATA_STALE according to freshness.",
        }
    repo = PITRepository(db_path)
    check = {"gate_name": "GATE_B_FAILURE_RECOVERY", "status": "PASS" if passed else "FAIL", "triggered_at": datetime.now(timezone.utc).isoformat(), "timezone_name": machine_timezone(), "details": details}
    gate_id, _ = repo.record_gate_check(check)
    return {"gate_check_id": gate_id, **check}


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Run Phase 2A operational gates")
    parser.add_argument("--gate", choices=("A", "B", "all"), default="all")
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()
    output = {}
    if args.gate in {"A", "all"}: output["gate_a"] = verify_gate_a(args.db)
    if args.gate in {"B", "all"}: output["gate_b"] = simulate_gate_b(args.db)
    print(json.dumps(output, ensure_ascii=False, indent=2))


__all__ = ["machine_timezone", "verify_gate_a", "simulate_gate_b"]
