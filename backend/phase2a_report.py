"""Machine-readable Phase 2A status and audit report."""

from __future__ import annotations

from pathlib import Path
import json

from phase2a_gates import verify_gate_a, simulate_gate_b
from pit_repository import PITRepository


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"


def build_report(db_path: Path | str = DB) -> dict:
    repo = PITRepository(db_path)
    runs = repo.get_replay_runs(limit=100)
    completed = [run for run in runs if run.get("status") == "COMPLETED"]
    gates = repo.get_gate_checks()
    latest_by_mode: dict[str, dict] = {}
    for run in completed:
        latest_by_mode.setdefault(str(run["mode"]), run)
    latest_proxy = latest_by_mode.get("RESEARCH_PROXY")
    latest_proxy_report = repo.get_replay_report(latest_proxy["replay_run_id"]) if latest_proxy else None
    information_value = ((latest_proxy_report or {}).get("report") or {}).get("current_score_information_value")
    # A score model whose every replay day is blocked by the 100% coverage
    # gate has not generated a comparable score sample.  The immutable Phase
    # 2A report may contain the old provisional label ``NONE``; expose the
    # corrected research conclusion without rewriting that audit row.
    proxy_payload = (latest_proxy_report or {}).get("report") or {}
    proxy_decisions = proxy_payload.get("decision_distribution") or {}
    if information_value in (None, "NONE") and proxy_decisions:
        if sum(int(value or 0) for key, value in proxy_decisions.items() if key != "INSUFFICIENT_EVIDENCE") == 0:
            information_value = "INCONCLUSIVE"
    latest_gate = {}
    for gate in gates:
        # get_gate_checks is chronological ascending; the last row for each
        # name is therefore the latest immutable check.
        latest_gate[str(gate["gate_name"])] = gate
    phase_status = "PASS_WITH_LIMITATIONS" if completed else "NOT_RUN"
    return {
        "phase": "2A",
        "status": phase_status,
        "PHASE_2A_STATUS": phase_status,
        "CURRENT_SCORE_INFORMATION_VALUE": information_value or "INCONCLUSIVE",
        "model_version": "NDX_SCORE_V2.0",
        "strict_proxy_separation": True,
        "replay_runs": runs,
        "latest_completed_by_mode": latest_by_mode,
        "gate_checks": gates,
        "latest_gate_by_name": latest_gate,
        "operational_scope": {
            "decision_engine_reads": ["PITRepository", "replay_features", "score_model.json"],
            "decision_engine_forbidden": ["forward_outcomes", "legacy evidence", "current full CSV", "future labels"],
            "future_outcome_layer": "isolated evaluation table; written after decisions",
        },
        "next_step": "Add or archive better historical vintages before interpreting strict coverage; do not retune V2.0 from this report.",
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Print Phase 2A status")
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--run-gates", action="store_true", help="record the current gate checks")
    args = parser.parse_args()
    if args.run_gates:
        verify_gate_a(args.db)
        simulate_gate_b(args.db)
    print(json.dumps(build_report(args.db), ensure_ascii=False, indent=2))


__all__ = ["build_report"]
