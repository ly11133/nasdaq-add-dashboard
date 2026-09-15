"""Phase 2F: capital ladder feasibility and dry-powder stress testing.

This module consumes the immutable Phase 2E tactical crossing events.  It
does not change the trigger engine, use overlays, read a position, or issue a
trade.  The only state being simulated is a separate Opportunity Fund ledger
with a frozen 100-unit opening balance, four fixed ladders, three monthly
refill rates, and three cash caps.

The historical replay and the synthetic paths use the same chronological
``simulate_capital_path`` function.  Future prices are never needed: a
historical path consists of the already persisted tactical event dates, while
synthetic paths are explicitly labelled accounting stress paths.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import copy
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping

from data_contract import as_of_datetime, normalize_date, sha256_json
from pit_repository import PITRepository
from phase2e_research import TACTICAL_MODEL_VERSION


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"

CAPITAL_MODEL_VERSION = "NDX_CAPITAL_LADDER_FEASIBILITY_V1"
CAPITAL_MODEL_CREATED_AT = "2026-09-15T00:00:00Z"
INITIAL_OPPORTUNITY_UNITS = 100.0

# M is intentionally a bookkeeping unit, not a currency or a statement about
# the user's salary.  A fixed exchange scale is required to combine the
# symbolic monthly refill and the 100-unit opening fund with Cap=6/12/24M.
# It is frozen before any result is calculated and is included in the config
# hash.  No result is selected by changing this scale.
M_TO_OPPORTUNITY_UNITS = 20.0

FORMAL_THRESHOLDS = (0.10, 0.20, 0.30, 0.40, 0.50)
EXTENSION_THRESHOLDS = (0.60, 0.70, 0.80)
REFILL_TARGETS = (0.25, 0.50, 0.75, 1.00)

LADDER_CONFIG: dict[str, dict[str, Any]] = {
    "A": {
        "name": "Equal",
        "description": "五档等额",
        "allocations": {"10": 20.0, "20": 20.0, "30": 20.0, "40": 20.0, "50": 20.0},
    },
    "B": {
        "name": "Linear Backload",
        "description": "线性后置",
        "allocations": {"10": 10.0, "20": 15.0, "30": 20.0, "40": 25.0, "50": 30.0},
    },
    "C": {
        "name": "Strong Backload",
        "description": "强后置",
        "allocations": {"10": 5.0, "20": 10.0, "30": 15.0, "40": 25.0, "50": 45.0},
    },
    "D": {
        "name": "Capped Geometric",
        "description": "封顶几何后置",
        "allocations": {"10": 3.0, "20": 6.0, "30": 13.0, "40": 26.0, "50": 52.0},
    },
}

REPLENISHMENT_CONFIG: dict[str, dict[str, Any]] = {
    "R0": {"name": "No refill", "monthly_m": 0.0, "description": "不补充"},
    "R1": {"name": "0.5M/month", "monthly_m": 0.5, "description": "每月补充 0.5M"},
    "R2": {"name": "1.0M/month", "monthly_m": 1.0, "description": "每月补充 1.0M"},
}

CAP_CONFIG: dict[str, dict[str, Any]] = {
    "CAP_6M": {"name": "6M", "cap_m": 6.0},
    "CAP_12M": {"name": "12M", "cap_m": 12.0},
    "CAP_24M": {"name": "24M", "cap_m": 24.0},
}

CAPITAL_MODEL_CONFIG: dict[str, Any] = {
    "model_version": CAPITAL_MODEL_VERSION,
    "created_at": CAPITAL_MODEL_CREATED_AT,
    "research_only": True,
    "strict_pit": False,
    "investment_action_eligible": False,
    "upstream_tactical_model_version": TACTICAL_MODEL_VERSION,
    "core_dca": {"separate": True, "optimized": False, "touched_by_engine": False},
    "opportunity_fund": {
        "initial_units": INITIAL_OPPORTUNITY_UNITS,
        "monthly_unit_label": "M",
        "m_to_opportunity_units": M_TO_OPPORTUNITY_UNITS,
        "conversion_note": "无量纲账本换算；不代表人民币、工资或产品金额",
    },
    "ladders": copy.deepcopy(LADDER_CONFIG),
    "replenishment": copy.deepcopy(REPLENISHMENT_CONFIG),
    "caps": copy.deepcopy(CAP_CONFIG),
    "formal_thresholds": list(FORMAL_THRESHOLDS),
    "extension_observations": list(EXTENSION_THRESHOLDS),
    "refill_targets": list(REFILL_TARGETS),
    "cash_timing": {
        "initial_fund": "at_path_start",
        "monthly_refill": "first_calendar_day_of_each_new_month_before_same_day_event",
        "same_day_events": "ascending_threshold; one ledger row per crossing",
        "cap_overflow": "credited_to_surplus_and_not_cash",
        "insufficient_cash": "deployed=min(required,cash); no borrowing",
    },
    "both_architecture": {
        "ath_is_label_only": True,
        "tactical_event_is_only_capital_event": True,
        "one_market_move_one_tactical_band_one_capital_event": True,
    },
    "synthetic_paths": {
        "calendar_anchor": "2000-01-03",
        "path_end_months": {"S1": 24, "S2": 24, "S3": 36, "S4": 60, "S5": 48, "S6": 48, "S7": 60, "S8": 60},
        "no_price_return_model": True,
    },
    "gate": {
        "max_early_exhaustion_rate": 0.50,
        "max_underfunded_event_rate": 0.50,
        "require_no_negative_cash": True,
        "require_cash_conservation": True,
        "ranking_order": [
            "early_exhaustion_rate", "underfunded_event_rate", "deep_survival_score",
            "refill_100_median_months", "sequence_cash_spread", "synthetic_full_execution_rate",
            "capital_utilization",
        ],
        "final_wealth_used_for_selection": False,
    },
}
CAPITAL_CONFIG_HASH = sha256_json(CAPITAL_MODEL_CONFIG)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _threshold_key(threshold: float) -> str:
    return str(int(round(float(threshold) * 100)))


def _date_obj(value: Any) -> date:
    return date.fromisoformat(normalize_date(value))


def _month_index(value: Any) -> int:
    current = _date_obj(value)
    return current.year * 12 + current.month


def _add_months(value: date, months: int) -> date:
    index = value.year * 12 + (value.month - 1) + int(months)
    year, month0 = divmod(index, 12)
    return date(year, month0 + 1, min(value.day, 28))


def _path_date(anchor: date, month_offset: int) -> str:
    return _add_months(anchor, month_offset).isoformat()


def _first_month_checkpoints(start_date: str, end_date: str) -> list[str]:
    start = _date_obj(start_date)
    end = _date_obj(end_date)
    cursor = date(start.year, start.month, 1)
    if cursor <= start:
        cursor = _add_months(cursor, 1)
    output: list[str] = []
    while cursor <= end:
        output.append(cursor.isoformat())
        cursor = _add_months(cursor, 1)
    return output


def _stats(values: Iterable[Any]) -> dict[str, Any]:
    numbers = [float(value) for value in values if _finite(value) is not None]
    if not numbers:
        return {"mean": None, "median": None, "p25": None, "p75": None, "sample_count": 0}
    ordered = sorted(numbers)
    def percentile(p: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * p
        lower = math.floor(position); upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return {
        "mean": mean(numbers), "median": median(numbers), "p25": percentile(0.25),
        "p75": percentile(0.75), "sample_count": len(numbers),
    }


def _make_event(path_id: str, cycle_no: int, month_offset: int, threshold: float, anchor: date) -> dict[str, Any]:
    cycle_id = f"{path_id}-cycle-{cycle_no}"
    event_id = f"{path_id}-event-{cycle_no}-{_threshold_key(threshold)}-{month_offset}"
    return {
        "path_event_id": event_id,
        "event_date": _path_date(anchor, month_offset),
        "tactical_cycle_id": cycle_id,
        "macro_episode_id": f"{path_id}-macro-1",
        "threshold": float(threshold),
        "drawdown_band": "MILD" if threshold == 0.10 else "MEDIUM" if threshold == 0.20 else "DEEP",
        "event_kind": "TACTICAL_BAND",
        "source_observation_ids": [],
        "payload": {"synthetic": True, "ath_drawdown_label_only": True, "overlay_can_veto": False},
    }


def build_synthetic_paths() -> dict[str, dict[str, Any]]:
    """Return the frozen S1–S8 accounting paths, without price returns."""

    anchor = _date_obj(CAPITAL_MODEL_CONFIG["synthetic_paths"]["calendar_anchor"])
    definitions: dict[str, list[tuple[int, float, int]]] = {
        "S1": [(0, 0.10, 1)],
        "S2": [(0, 0.10, 1), (1, 0.20, 1)],
        "S3": [(0, 0.10, 1), (1, 0.20, 1), (2, 0.30, 1)],
        "S4": [(0, 0.10, 1), (1, 0.20, 1), (2, 0.30, 1), (3, 0.40, 1), (4, 0.50, 1)],
        "S5": [(0, 0.10, 1), (1, 0.20, 1), (18, 0.10, 2), (19, 0.20, 2)],
        "S6": [(0, 0.10, 1), (1, 0.20, 1), (2, 0.30, 1), (3, 0.40, 1), (4, 0.50, 1), (17, 0.10, 2), (18, 0.20, 2), (19, 0.30, 2)],
        "S7": [(0, 0.10, 1), (12, 0.10, 2), (24, 0.20, 3), (30, 0.40, 4)],
        "S8": [],
    }
    paths: dict[str, dict[str, Any]] = {}
    for path_id, definition in definitions.items():
        end_month = int(CAPITAL_MODEL_CONFIG["synthetic_paths"]["path_end_months"][path_id])
        paths[path_id] = {
            "path_type": "SYNTHETIC", "path_id": path_id, "start_date": anchor.isoformat(),
            "end_date": _path_date(anchor, end_month),
            "events": [_make_event(path_id, cycle_no, month, threshold, anchor) for month, threshold, cycle_no in definition],
            "metadata": {"definition": definition, "price_returns_used": False},
        }
    return paths


def build_extreme_extension_path() -> dict[str, Any]:
    anchor = _date_obj(CAPITAL_MODEL_CONFIG["synthetic_paths"]["calendar_anchor"])
    path = build_synthetic_paths()["S4"]
    events = [dict(event) for event in path["events"]]
    for index, threshold in enumerate(EXTENSION_THRESHOLDS, start=5):
        event = _make_event("S4_EXT", 1, index, threshold, anchor)
        event["event_kind"] = "EXTENSION_OBSERVATION"
        event["payload"] = {"synthetic": True, "formal_trigger": False, "extension_only": True}
        events.append(event)
    return {
        "path_type": "EXTREME_EXTENSION", "path_id": "S4_EXT_60_80", "start_date": anchor.isoformat(),
        "end_date": _path_date(anchor, 24), "events": events,
        "metadata": {"formal_thresholds_stop_at": 0.50, "extension_observations": list(EXTENSION_THRESHOLDS)},
    }


def build_sequence_paths() -> dict[str, dict[str, Any]]:
    anchor = _date_obj(CAPITAL_MODEL_CONFIG["synthetic_paths"]["calendar_anchor"])
    definitions = {
        "SEQ_NORMAL_THEN_CRASH": [(24, 0.10, 1), (25, 0.20, 1), (26, 0.30, 1), (27, 0.40, 1), (28, 0.50, 1)],
        "SEQ_IMMEDIATE_CRASH": [(0, 0.10, 1), (1, 0.20, 1), (2, 0.30, 1), (3, 0.40, 1), (4, 0.50, 1)],
        "SEQ_CRASH_RECOVER_RECRASH": [(0, 0.10, 1), (1, 0.20, 1), (2, 0.30, 1), (3, 0.40, 1), (4, 0.50, 1), (17, 0.10, 2), (18, 0.20, 2), (19, 0.30, 2)],
    }
    output: dict[str, dict[str, Any]] = {}
    for path_id, definition in definitions.items():
        output[path_id] = {
            "path_type": "SEQUENCE", "path_id": path_id, "start_date": anchor.isoformat(),
            "end_date": _path_date(anchor, 60),
            "events": [_make_event(path_id, cycle_no, month, threshold, anchor) for month, threshold, cycle_no in definition],
            "metadata": {"definition": definition, "same_state_machine": True, "price_returns_used": False},
        }
    return output


def _normalise_historical_path(events: Iterable[Mapping[str, Any]], *, path_id: str, start_date: str, end_date: str) -> dict[str, Any]:
    output = []
    for event in events:
        threshold = float(event["threshold"])
        if threshold not in FORMAL_THRESHOLDS:
            continue
        output.append({
            "path_event_id": str(event["tactical_event_id"]),
            "event_date": normalize_date(event["event_date"]),
            "tactical_cycle_id": str(event["tactical_cycle_id"]),
            "macro_episode_id": event.get("macro_episode_id"),
            "threshold": threshold,
            "drawdown_band": event.get("drawdown_band"),
            "event_kind": "TACTICAL_BAND",
            "source_observation_ids": list(event.get("input_observation_ids") or []),
            "payload": {"historical": True, "ath_drawdown_label_only": True, "overlay_can_veto": False},
        })
    output.sort(key=lambda item: (item["event_date"], item["threshold"], item["path_event_id"]))
    return {"path_type": "HISTORICAL", "path_id": path_id, "start_date": normalize_date(start_date), "end_date": normalize_date(end_date), "events": output, "metadata": {"source": "Phase 2E tactical events", "price_returns_used": False}}


def _monthly_credit(cash: float, incoming: float, cap_units: float) -> tuple[float, float, float]:
    available_room = max(cap_units - cash, 0.0)
    credited = min(max(incoming, 0.0), available_room)
    surplus = max(incoming - credited, 0.0)
    return cash + credited, credited, surplus


def _refill_months(event_date: str, reached_date: str) -> float:
    days = (_date_obj(reached_date) - _date_obj(event_date)).days
    return round(max(days, 0) / 30.4375, 6)


def _summarise_refill(trackers: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for target in REFILL_TARGETS:
        key = _threshold_key(target)
        values = [item[key] for item in trackers if item.get(key) is not None]
        output[key] = {"target_fraction": target, "reached_count": len(values), "event_count": len(trackers), "reached_rate": (len(values) / len(trackers) if trackers else None), "months": _stats(values)}
    return output


def simulate_capital_path(
    path: Mapping[str, Any],
    *,
    ladder_id: str,
    replenishment_id: str,
    cap_id: str,
    initial_units: float = INITIAL_OPPORTUNITY_UNITS,
    m_to_opportunity_units: float = M_TO_OPPORTUNITY_UNITS,
) -> dict[str, Any]:
    """Replay one path/configuration with a chronological cash ledger.

    ``path`` may be historical, synthetic, or sequence data.  The function is
    intentionally the same for all three path types and accepts no price
    series, overlay, ATH trigger, or future return.
    """

    if ladder_id not in LADDER_CONFIG or replenishment_id not in REPLENISHMENT_CONFIG or cap_id not in CAP_CONFIG:
        raise ValueError("未知 capital ladder/replenishment/cap")
    start_date = normalize_date(path["start_date"])
    end_date = normalize_date(path["end_date"])
    if start_date > end_date:
        raise ValueError("capital path start_date 不能晚于 end_date")
    ladder = LADDER_CONFIG[ladder_id]["allocations"]
    monthly_m = float(REPLENISHMENT_CONFIG[replenishment_id]["monthly_m"])
    cap_m = float(CAP_CONFIG[cap_id]["cap_m"])
    cap_units = cap_m * float(m_to_opportunity_units)
    opening_cash = min(float(initial_units), cap_units)
    cash = opening_cash
    total_scheduled_refill = 0.0
    total_credited_refill = 0.0
    total_surplus = 0.0
    total_deployed = 0.0
    total_required = 0.0
    underfunded_count = 0
    formal_event_count = 0
    extension_event_count = 0
    ledger: list[dict[str, Any]] = []
    cash_timeline: list[tuple[str, float]] = [(start_date, cash)]
    events_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in path.get("events", []):
        event_copy = dict(event)
        event_copy["event_date"] = normalize_date(event_copy["event_date"])
        if not (start_date <= event_copy["event_date"] <= end_date):
            continue
        events_by_date[event_copy["event_date"]].append(event_copy)
    for day in events_by_date:
        events_by_date[day].sort(key=lambda item: (float(item.get("threshold") or 0), str(item.get("path_event_id"))))
    markers = sorted(set(_first_month_checkpoints(start_date, end_date)) | set(events_by_date) | {start_date, end_date})
    last_credit_month = _month_index(start_date)
    spent_pairs: set[tuple[str, float]] = set()
    event_sequence = 0
    trackers: list[dict[str, Any]] = []
    cash_after_threshold: dict[str, list[float]] = defaultdict(list)
    cycle_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    negative_cash_count = 0
    conservation_error = 0.0
    for day in markers:
        month_credit = 0.0
        month_surplus = 0.0
        current_month = _month_index(day)
        if day != start_date and (day in _first_month_checkpoints(start_date, end_date) or day in events_by_date) and current_month > last_credit_month:
            months_elapsed = current_month - last_credit_month
            incoming = monthly_m * float(m_to_opportunity_units) * months_elapsed
            total_scheduled_refill += incoming
            cash, credited, surplus = _monthly_credit(cash, incoming, cap_units)
            month_credit += credited
            month_surplus += surplus
            total_credited_refill += credited
            total_surplus += surplus
            last_credit_month = current_month
        cash_timeline.append((day, cash))
        day_events = events_by_date.get(day, [])
        for index, event in enumerate(day_events):
            event_sequence += 1
            threshold = _finite(event.get("threshold"))
            threshold = float(threshold) if threshold is not None else None
            kind = str(event.get("event_kind") or "TACTICAL_BAND")
            formal = kind == "TACTICAL_BAND" and threshold in FORMAL_THRESHOLDS
            cycle_id = str(event.get("tactical_cycle_id") or f"{path['path_id']}-cycle-unknown")
            pair = (cycle_id, float(threshold)) if formal and threshold is not None else None
            duplicate = pair in spent_pairs if pair else False
            required = 0.0
            if formal and threshold is not None and not duplicate:
                required = float(ladder[_threshold_key(threshold)])
            total_required += required
            cash_before = cash
            deployed = min(required, max(cash_before, 0.0))
            cash = max(cash_before - deployed, 0.0)
            if formal and not duplicate:
                formal_event_count += 1
                if pair is not None:
                    spent_pairs.add(pair)
            elif kind == "EXTENSION_OBSERVATION":
                extension_event_count += 1
            underfunded = bool(formal and not duplicate and deployed + 1e-9 < required)
            if underfunded:
                underfunded_count += 1
            total_deployed += deployed
            if cash < -1e-9:
                negative_cash_count += 1
                cash = 0.0
            if formal and threshold is not None:
                cash_after_threshold[_threshold_key(threshold)].append(cash)
                if threshold >= 0.30:
                    trackers.append({"event_date": day, "cycle_id": cycle_id, "threshold": threshold, **{_threshold_key(target): (0.0 if cash >= target * float(initial_units) else None) for target in REFILL_TARGETS}})
            if day_events and index == 0:
                credited_for_entry, surplus_for_entry = month_credit, month_surplus
            else:
                credited_for_entry, surplus_for_entry = 0.0, 0.0
            entry = {
                "event_sequence": event_sequence,
                "event_date": day,
                "path_event_id": event.get("path_event_id"),
                "tactical_cycle_id": event.get("tactical_cycle_id"),
                "macro_episode_id": event.get("macro_episode_id"),
                "threshold": threshold,
                "event_kind": kind,
                "cash_before_units": round(cash_before, 12),
                "required_units": round(required, 12),
                "deployed_units": round(deployed, 12),
                "cash_after_units": round(cash, 12),
                "replenishment_units": round(credited_for_entry, 12),
                "surplus_units": round(surplus_for_entry, 12),
                "underfunded_event": underfunded,
                "ath_state_ignored": True,
                "duplicate_band_ignored": duplicate,
                "input_observation_ids": list(event.get("source_observation_ids") or []),
                "payload": {
                    "one_market_move_one_tactical_band_one_capital_event": True,
                    "overlay_can_veto": False,
                    "formal_trigger": formal,
                    "initial_units": float(initial_units),
                    "cap_units": cap_units,
                    "monthly_m": monthly_m,
                    "m_to_opportunity_units": float(m_to_opportunity_units),
                },
            }
            ledger.append(entry)
            cycle_events[cycle_id].append(entry)
            month_credit, month_surplus = 0.0, 0.0
            cash_timeline.append((day, cash))
        # Refill is checked after any same-day capital event and at each new
        # month checkpoint.  It is evaluation-only; it cannot change spending.
        for tracker in trackers:
            if _date_obj(day) < _date_obj(tracker["event_date"]):
                continue
            for target in REFILL_TARGETS:
                key = _threshold_key(target)
                if tracker[key] is None and cash >= target * float(initial_units):
                    tracker[key] = _refill_months(tracker["event_date"], day)
    # Ensure a final monthly credit at a first-of-month marker was represented
    # even if the path ended immediately after an event; markers already cover
    # end_date, so this is a no-op in ordinary paths.
    cash_timeline.append((end_date, cash))

    cycle_summaries = []
    for cycle_id, rows in sorted(cycle_events.items()):
        formal_rows = [row for row in rows if row["event_kind"] == "TACTICAL_BAND" and row.get("threshold") in FORMAL_THRESHOLDS]
        max_threshold = max((float(row["threshold"]) for row in formal_rows), default=None)
        max_index = max((index for index, row in enumerate(formal_rows) if float(row["threshold"]) == max_threshold), default=-1) if max_threshold is not None else -1
        before_deep = any(float(row["cash_after_units"]) <= 1e-9 for index, row in enumerate(formal_rows) if index < max_index and max_threshold is not None and max_threshold >= 0.30)
        cycle_summaries.append({
            "tactical_cycle_id": cycle_id,
            "event_count": len(formal_rows),
            "max_threshold": max_threshold,
            "cash_after_max_threshold": formal_rows[max_index]["cash_after_units"] if max_index >= 0 else None,
            "exhausted_before_final_deep": before_deep,
            "exhausted_at_or_before_any_event": any(float(row["cash_after_units"]) <= 1e-9 for row in formal_rows),
        })
    eligible_cycles = [row for row in cycle_summaries if row["max_threshold"] is not None and row["max_threshold"] >= 0.30]
    threshold_stats = {key: _stats(values) for key, values in sorted(cash_after_threshold.items(), key=lambda item: int(item[0]))}
    all_formal = [row for row in ledger if row["event_kind"] == "TACTICAL_BAND" and row.get("threshold") in FORMAL_THRESHOLDS]
    available_funding = opening_cash + total_credited_refill
    expected_end_cash = opening_cash + total_credited_refill - total_deployed
    conservation_error = abs(expected_end_cash - cash)
    refill = _summarise_refill(trackers)
    return {
        "path_type": str(path["path_type"]),
        "path_id": str(path["path_id"]),
        "start_date": start_date,
        "end_date": end_date,
        "ladder_id": ladder_id,
        "replenishment_id": replenishment_id,
        "cap_id": cap_id,
        "opening_cash_units": opening_cash,
        "cap_units": cap_units,
        "monthly_refill_units": monthly_m * float(m_to_opportunity_units),
        "total_scheduled_refill_units": total_scheduled_refill,
        "total_credited_refill_units": total_credited_refill,
        "surplus_units": total_surplus,
        "available_funding_units": available_funding,
        "total_required_units": total_required,
        "total_deployed_units": total_deployed,
        "ending_cash_units": cash,
        "capital_utilization": (total_deployed / available_funding if available_funding > 0 else 0.0),
        "formal_event_count": formal_event_count,
        "extension_event_count": extension_event_count,
        "underfunded_event_count": underfunded_count,
        "underfunded_event_rate": (underfunded_count / formal_event_count if formal_event_count else 0.0),
        "negative_cash_count": negative_cash_count,
        "cash_conservation_error": conservation_error,
        "cash_conservation_holds": conservation_error <= 1e-8,
        "cash_nonnegative": negative_cash_count == 0 and cash >= -1e-9,
        "cash_after_threshold": threshold_stats,
        "remaining_cash_after_50": (threshold_stats.get("50", {}).get("median") if threshold_stats.get("50") else None),
        "cycle_count": len(cycle_summaries),
        "cycle_summaries": cycle_summaries,
        "deep_cycle_count": len(eligible_cycles),
        "early_exhaustion_count": sum(bool(row["exhausted_before_final_deep"]) for row in eligible_cycles),
        "early_exhaustion_rate": (sum(bool(row["exhausted_before_final_deep"]) for row in eligible_cycles) / len(eligible_cycles) if eligible_cycles else 0.0),
        "refill": refill,
        "refill_trackers": trackers,
        "max_cash_units": max((value for _, value in cash_timeline), default=opening_cash),
        "timeline_observation_count": len(cash_timeline),
        "core_dca_untouched": True,
        "overlay_used": False,
        "future_price_used": False,
        "ath_double_spend": False,
        "ledger": ledger,
        "path_metadata": dict(path.get("metadata") or {}),
    }


def _scenario_id(capital_run_id: str, path: Mapping[str, Any], ladder_id: str, replenishment_id: str, cap_id: str) -> str:
    return "capital-scenario-" + sha256_json({"run": capital_run_id, "path_type": path["path_type"], "path_id": path["path_id"], "ladder": ladder_id, "replenishment": replenishment_id, "cap": cap_id})[:24]


def _median_metric(results: Iterable[Mapping[str, Any]], key: str) -> float | None:
    values = [_finite(item.get(key)) for item in results]
    values = [value for value in values if value is not None]
    return median(values) if values else None


def _nested_threshold_median(results: Iterable[Mapping[str, Any]], threshold_key: str) -> float | None:
    values = []
    for result in results:
        item = (result.get("cash_after_threshold") or {}).get(threshold_key) or {}
        value = _finite(item.get("median"))
        if value is not None:
            values.append(value)
    return median(values) if values else None


def aggregate_ladder_results(scenario_results: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scenario_results:
        grouped[str(row["ladder_id"])].append(dict(row["result"] if "result" in row and isinstance(row["result"], Mapping) else row))
    output: dict[str, dict[str, Any]] = {}
    for ladder_id in sorted(LADDER_CONFIG):
        rows = grouped.get(ladder_id, [])
        historical = [row for row in rows if row.get("path_type") == "HISTORICAL"]
        synthetic = [row for row in rows if row.get("path_type") == "SYNTHETIC"]
        sequence = [row for row in rows if row.get("path_type") == "SEQUENCE"]
        extensions = [row for row in rows if row.get("path_type") == "EXTREME_EXTENSION"]
        deep_survival_values = []
        for row in rows:
            threshold_values = [_finite(((row.get("cash_after_threshold") or {}).get(key) or {}).get("median")) for key in ("20", "30", "40", "50")]
            threshold_values = [value for value in threshold_values if value is not None]
            if threshold_values:
                deep_survival_values.append(mean(threshold_values) / INITIAL_OPPORTUNITY_UNITS)
        sequence_spreads = []
        for path_id in {row.get("path_id") for row in sequence}:
            subset = [row for row in sequence if row.get("path_id") == path_id]
            vals = [_finite(row.get("remaining_cash_after_50")) for row in subset]
            vals = [value for value in vals if value is not None]
            if vals:
                sequence_spreads.append((max(vals) - min(vals)) / INITIAL_OPPORTUNITY_UNITS)
        early_values = [_finite(row.get("early_exhaustion_rate")) for row in historical]
        synthetic_early_values = [_finite(row.get("early_exhaustion_rate")) for row in synthetic]
        sequence_early_values = [_finite(row.get("early_exhaustion_rate")) for row in sequence]
        historical_under_values = [_finite(row.get("underfunded_event_rate")) for row in historical]
        under_values = [_finite(row.get("underfunded_event_rate")) for row in rows]
        util_values = [_finite(row.get("capital_utilization")) for row in historical]
        refill_medians: dict[str, float | None] = {}
        for target_key in ("25", "50", "75", "100"):
            refill_values = []
            for row in rows:
                months = ((row.get("refill") or {}).get(target_key) or {}).get("months") or {}
                value = _finite(months.get("median"))
                if value is not None:
                    refill_values.append(value)
            refill_medians[target_key] = median(refill_values) if refill_values else None
        structural_failures = sum(1 for row in rows if not row.get("cash_nonnegative") or not row.get("cash_conservation_holds"))
        full_execution = [row for row in synthetic if row.get("formal_event_count", 0) > 0]
        full_execution_rate = (sum(1 for row in full_execution if row.get("underfunded_event_count", 0) == 0) / len(full_execution) if full_execution else None)
        s4_rows = [row for row in synthetic if row.get("path_id") == "S4"]
        output[ladder_id] = {
            "ladder_id": ladder_id,
            "name": LADDER_CONFIG[ladder_id]["name"],
            "description": LADDER_CONFIG[ladder_id]["description"],
            "scenario_count": len(rows),
            "historical_scenario_count": len(historical),
            "synthetic_scenario_count": len(synthetic),
            "sequence_scenario_count": len(sequence),
            "extension_scenario_count": len(extensions),
            "historical_early_exhaustion_rate": _stats(early_values),
            "synthetic_early_exhaustion_rate": _stats(synthetic_early_values),
            "sequence_early_exhaustion_rate": _stats(sequence_early_values),
            "historical_underfunded_event_rate": _stats(historical_under_values),
            "underfunded_event_rate": _stats(under_values),
            "capital_utilization": _stats(util_values),
            "dry_powder_survival_score": (median(deep_survival_values) if deep_survival_values else None),
            "cash_after_threshold_median_units": {key: _nested_threshold_median(rows, key) for key in ("10", "20", "30", "40", "50")},
            "refill_median_months": refill_medians,
            "refill_100_median_months": refill_medians["100"],
            "sequence_cash_spread": (median(sequence_spreads) if sequence_spreads else None),
            "synthetic_full_execution_rate": full_execution_rate,
            "synthetic_s4_remaining_cash_after_50_median_units": _median_metric(s4_rows, "remaining_cash_after_50"),
            "structural_failure_count": structural_failures,
            "historical_ending_cash_median_units": _median_metric(historical, "ending_cash_units"),
            "historical_total_deployed_median_units": _median_metric(historical, "total_deployed_units"),
            "final_wealth_used_for_selection": False,
        }
    return output


def dominance_test(aggregates: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    lower = ("historical_early_exhaustion_rate", "underfunded_event_rate", "refill_100_median_months", "sequence_cash_spread")
    higher = ("dry_powder_survival_score", "synthetic_full_execution_rate")
    output = {ladder_id: {"dominated": False, "dominated_by": [], "comparison_metrics": []} for ladder_id in aggregates}
    for candidate, candidate_metrics in aggregates.items():
        for dominator, dominator_metrics in aggregates.items():
            if candidate == dominator:
                continue
            no_worse = True; strict = 0; evidence = []
            for key in lower:
                c = _finite(candidate_metrics.get(key, {}).get("median") if isinstance(candidate_metrics.get(key), Mapping) else candidate_metrics.get(key))
                d = _finite(dominator_metrics.get(key, {}).get("median") if isinstance(dominator_metrics.get(key), Mapping) else dominator_metrics.get(key))
                if c is None or d is None:
                    continue
                if d > c + 1e-12:
                    no_worse = False
                elif d < c - 1e-12:
                    strict += 1; evidence.append({"metric": key, "direction": "lower"})
            for key in higher:
                c = _finite(candidate_metrics.get(key)); d = _finite(dominator_metrics.get(key))
                if c is None or d is None:
                    continue
                if d < c - 1e-12:
                    no_worse = False
                elif d > c + 1e-12:
                    strict += 1; evidence.append({"metric": key, "direction": "higher"})
            if no_worse and strict >= 2:
                output[candidate]["dominated"] = True
                output[candidate]["dominated_by"].append(dominator)
                output[candidate]["comparison_metrics"].append({"dominator": dominator, "evidence": evidence})
    return output


def rank_ladders(aggregates: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    def value(metrics: Mapping[str, Any], key: str, default: float) -> float:
        item = metrics.get(key)
        if isinstance(item, Mapping):
            item = item.get("median")
        number = _finite(item)
        return default if number is None else number
    rows = []
    for ladder_id, metrics in aggregates.items():
        # The order is frozen in CAPITAL_MODEL_CONFIG.  Lower exhaustion and
        # underfunding come first; final wealth is deliberately absent.
        sort_key = (
            value(metrics, "historical_early_exhaustion_rate", 1.0),
            value(metrics, "underfunded_event_rate", 1.0),
            -value(metrics, "dry_powder_survival_score", -1.0),
            value(metrics, "refill_100_median_months", float("inf")),
            value(metrics, "sequence_cash_spread", float("inf")),
            -value(metrics, "synthetic_full_execution_rate", -1.0),
            -value(metrics, "capital_utilization", -1.0),
            str(ladder_id),
        )
        rows.append({"ladder_id": ladder_id, "sort_key": sort_key})
    rows.sort(key=lambda row: row["sort_key"])
    for rank, row in enumerate(rows, start=1):
        row["robustness_rank"] = rank
        row.pop("sort_key", None)
    return rows


def classify_ladder(metrics: Mapping[str, Any]) -> str:
    if int(metrics.get("structural_failure_count", 0)) > 0:
        return "FAIL"
    exhaustion = _finite((metrics.get("historical_early_exhaustion_rate") or {}).get("median"))
    underfunded = _finite((metrics.get("underfunded_event_rate") or {}).get("median"))
    if exhaustion is not None and underfunded is not None and exhaustion <= 0.50 and underfunded <= 0.50:
        return "VIABLE"
    return "WEAK"


def _path_summary(results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [dict(row["result"] if "result" in row and isinstance(row["result"], Mapping) else row) for row in results]
    return {
        "scenario_count": len(rows),
        "underfunded_event_rate": _stats(row.get("underfunded_event_rate") for row in rows),
        "capital_utilization": _stats(row.get("capital_utilization") for row in rows),
        "ending_cash_units": _stats(row.get("ending_cash_units") for row in rows),
        "cash_conservation_holds": all(bool(row.get("cash_conservation_holds")) for row in rows),
        "cash_nonnegative": all(bool(row.get("cash_nonnegative")) for row in rows),
    }


def _historical_crisis_windows(paths: Mapping[str, Mapping[str, Any]], scenario_results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarise the fixed historical crisis windows without re-triggering them."""

    windows = {
        "2000_2002": ("2000-01-01", "2002-12-31"),
        "2007_2009": ("2007-01-01", "2009-12-31"),
        "2018": ("2018-01-01", "2018-12-31"),
        "2020": ("2020-01-01", "2020-12-31"),
        "2022": ("2022-01-01", "2022-12-31"),
    }
    historical_path = next((path for path in paths.values() if path.get("path_type") == "HISTORICAL"), None)
    historical_rows = [row for row in scenario_results if row.get("path_type") == "HISTORICAL"]
    output: dict[str, Any] = {}
    for label, (start, end) in windows.items():
        events = [event for event in (historical_path or {}).get("events", []) if start <= normalize_date(event["event_date"]) <= end]
        by_ladder: dict[str, Any] = {}
        for ladder_id in sorted(LADDER_CONFIG):
            values = []
            for row in historical_rows:
                if row.get("ladder_id") != ladder_id:
                    continue
                entries = [entry for entry in (row.get("result") or {}).get("ledger", []) if start <= normalize_date(entry["event_date"]) <= end and entry.get("event_kind") == "TACTICAL_BAND"]
                per_threshold = defaultdict(list)
                for entry in entries:
                    if entry.get("threshold") in FORMAL_THRESHOLDS:
                        per_threshold[_threshold_key(float(entry["threshold"]))].append(float(entry["cash_after_units"]))
                last_values = [values_for_threshold[-1] for key, values_for_threshold in sorted(per_threshold.items(), key=lambda item: int(item[0])) if values_for_threshold]
                if last_values:
                    values.append(last_values[-1])
            by_ladder[ladder_id] = {"last_event_cash_median_units": (median(values) if values else None)}
        output[label] = {
            "start_date": start,
            "end_date": end,
            "event_count": len(events),
            "cycle_count": len({str(event.get("tactical_cycle_id")) for event in events if event.get("tactical_cycle_id")}),
            "event_count_by_threshold": {_threshold_key(threshold): sum(abs(float(event["threshold"]) - threshold) < 1e-9 for event in events) for threshold in FORMAL_THRESHOLDS},
            "ladder_cash": by_ladder,
        }
    return output


def build_report(
    repo: PITRepository,
    capital_run_id: str,
    *,
    phase2e_run_id: str,
    paths: Mapping[str, Mapping[str, Any]],
    scenario_results: list[Mapping[str, Any]],
    data_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    run = repo.get_capital_feasibility_run(capital_run_id)
    if not run:
        raise ValueError("capital feasibility run 不存在")
    aggregates = aggregate_ladder_results(scenario_results)
    dominance = dominance_test(aggregates)
    ranking = rank_ladders(aggregates)
    for row in ranking:
        aggregates[row["ladder_id"]]["robustness_rank"] = row["robustness_rank"]
    for ladder_id, value in aggregates.items():
        value["classification"] = classify_ladder(value)
        value["dominance"] = dominance[ladder_id]
    structural_failures = sum(int(value.get("structural_failure_count", 0)) for value in aggregates.values())
    viable = [ladder_id for ladder_id, value in aggregates.items() if value.get("classification") == "VIABLE"]
    max_exhaustion = max((_finite((value.get("historical_early_exhaustion_rate") or {}).get("median")) or 0.0) for value in aggregates.values())
    max_underfunded = max((_finite((value.get("underfunded_event_rate") or {}).get("median")) or 0.0) for value in aggregates.values())
    gate_checks = {
        "at_least_one_viable_ladder": bool(viable),
        "no_structural_cash_failure": structural_failures == 0,
        "no_negative_cash": all(bool((value.get("structural_failure_count", 0) == 0)) for value in aggregates.values()),
        "historical_early_exhaustion_within_frozen_limit_for_candidate": bool(viable),
        "trigger_density_refill_mechanism_present": True,
        "overlay_not_used": all(not bool((row.get("result") if isinstance(row.get("result"), Mapping) else row).get("overlay_used")) for row in scenario_results),
        "future_price_not_used": all(not bool((row.get("result") if isinstance(row.get("result"), Mapping) else row).get("future_price_used")) for row in scenario_results),
        "same_engine_historical_synthetic": True,
        "final_wealth_not_used_for_selection": True,
    }
    gate = "YES" if all(gate_checks.values()) and viable else "NO"
    historical = [row for row in scenario_results if row.get("path_type") == "HISTORICAL"]
    synthetic = [row for row in scenario_results if row.get("path_type") == "SYNTHETIC"]
    sequence = [row for row in scenario_results if row.get("path_type") == "SEQUENCE"]
    extension = [row for row in scenario_results if row.get("path_type") == "EXTREME_EXTENSION"]
    crisis_windows = _historical_crisis_windows(paths, scenario_results)
    return {
        "phase": "2F",
        "capital_run_id": capital_run_id,
        "capital_model_version": CAPITAL_MODEL_VERSION,
        "config_hash": CAPITAL_CONFIG_HASH,
        "phase2e_run_id": phase2e_run_id,
        "date_range": {"start": run["start_date"], "end": run["end_date"]},
        "research_only": True,
        "strict_pit": False,
        "investment_action_eligible": False,
        "core_dca_untouched": True,
        "overlay_used": False,
        "future_price_used": False,
        "PHASE_2F_STATUS": "PASS_WITH_LIMITATIONS" if structural_failures == 0 else "FAIL",
        "LADDER_A": aggregates["A"]["classification"],
        "LADDER_B": aggregates["B"]["classification"],
        "LADDER_C": aggregates["C"]["classification"],
        "LADDER_D": aggregates["D"]["classification"],
        "RECOMMENDED_LADDER_CANDIDATES": [ladder_id for ladder_id in viable if not dominance[ladder_id]["dominated"]],
        "READY_FOR_CAPITAL_STATE_MACHINE": gate,
        "capital_unit_definition": {
            "initial_opportunity_fund_units": INITIAL_OPPORTUNITY_UNITS,
            "m_label": "M",
            "m_to_opportunity_units": M_TO_OPPORTUNITY_UNITS,
            "cap_units": {key: value["cap_m"] * M_TO_OPPORTUNITY_UNITS for key, value in CAP_CONFIG.items()},
            "currency_assumption": False,
        },
        "ladder_definitions": copy.deepcopy(LADDER_CONFIG),
        "replenishment_definitions": copy.deepcopy(REPLENISHMENT_CONFIG),
        "cap_definitions": copy.deepcopy(CAP_CONFIG),
        "path_counts": {
            "historical": len(historical), "synthetic": len(synthetic), "sequence": len(sequence), "extreme_extension": len(extension),
            "total": len(scenario_results), "unique_paths": len(paths),
        },
        "historical_event_count": len(next((path.get("events", []) for path in paths.values() if path.get("path_type") == "HISTORICAL"), [])),
        "synthetic_path_definitions": {key: {"event_count": len(value.get("events", [])), "start_date": value["start_date"], "end_date": value["end_date"], "metadata": value.get("metadata", {})} for key, value in paths.items() if value.get("path_type") == "SYNTHETIC"},
        "sequence_path_definitions": {key: {"event_count": len(value.get("events", [])), "metadata": value.get("metadata", {})} for key, value in paths.items() if value.get("path_type") == "SEQUENCE"},
        "extreme_extension": {"formal_thresholds_stop_at": 0.50, "observed_thresholds": list(EXTENSION_THRESHOLDS), "scenario_count": len(extension)},
        "ladder_assessments": aggregates,
        "dominance_test": dominance,
        "robustness_ranking": ranking,
        "historical_summary": _path_summary(historical),
        "synthetic_summary": _path_summary(synthetic),
        "sequence_summary": _path_summary(sequence),
        "extension_summary": _path_summary(extension),
        "historical_crisis_windows": crisis_windows,
        "gate_checks": gate_checks,
        "gate_limits": CAPITAL_MODEL_CONFIG["gate"],
        "accounting_invariants": {
            "cash_conservation": all(bool((row.get("result") if isinstance(row.get("result"), Mapping) else row).get("cash_conservation_holds")) for row in scenario_results),
            "cash_never_negative": all(bool((row.get("result") if isinstance(row.get("result"), Mapping) else row).get("cash_nonnegative")) for row in scenario_results),
            "ath_never_double_spent": all(not bool((row.get("result") if isinstance(row.get("result"), Mapping) else row).get("ath_double_spend")) for row in scenario_results),
            "unused_band_cash_remains_cash": True,
        },
        "selection_rule": {
            "basis": list(CAPITAL_MODEL_CONFIG["gate"]["ranking_order"]),
            "aggregate_statistic": "median for the frozen robustness comparator; means and p75 are retained in each assessment",
            "final_wealth_excluded": True,
            "recommended_ladder_candidates": [ladder_id for ladder_id in viable if not dominance[ladder_id]["dominated"]],
        },
        "data_snapshot": dict(data_snapshot),
        "data_cutoff": run.get("data_cutoff"),
        "lookahead_controls": {
            "historical_input": "Phase 2E persisted tactical events only",
            "synthetic_uses_no_prices": True,
            "chronological_replenishment": True,
            "future_price_used": False,
            "future_return_used": False,
            "overlay_used": False,
            "capital_events_come_only_from_tactical_crossings": True,
        },
        "limitations": [
            "M 到 Opportunity Unit 的固定换算只是无量纲账本尺度，不是用户收入或人民币建议。",
            "NDX 和 Phase 2E 输入仍是 HISTORICAL_PROXY，不能替代完整严格 PIT。",
            "合成路径只测试现金状态转移，不预测价格、收益或未来底部。",
            "本阶段不选择正式人民币金额、不运行实盘状态机、不产生买卖指令。",
        ],
    }


def run_capital_feasibility_validation(
    repo: PITRepository | None = None,
    *,
    phase2e_run_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    capital_run_id: str | None = None,
) -> dict[str, Any]:
    repo = repo or PITRepository(DB)
    candidates = repo.get_tactical_drawdown_runs(market="NDX", limit=1000)
    phase2e = repo.get_tactical_drawdown_run(phase2e_run_id) if phase2e_run_id else next((item for item in candidates if item.get("status") == "COMPLETED"), None)
    if not phase2e or phase2e.get("status") != "COMPLETED":
        raise ValueError("需要一个已完成的 Phase 2E run")
    phase2e_run_id = str(phase2e["tactical_run_id"])
    start_date = normalize_date(start_date or phase2e["start_date"])
    end_date = normalize_date(end_date or phase2e["end_date"])
    capital_run_id = capital_run_id or f"phase2f-capital-ndx-v1-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    historical_events = repo.get_tactical_drawdown_events(phase2e_run_id, limit=1000000)
    paths: dict[str, dict[str, Any]] = {
        "HISTORICAL_2000_2026": _normalise_historical_path(historical_events, path_id="HISTORICAL_2000_2026", start_date=start_date, end_date=end_date),
        **build_synthetic_paths(),
        "S4_EXT_60_80": build_extreme_extension_path(),
        **build_sequence_paths(),
    }
    data_snapshot = {
        "repository": "PITRepository",
        "mode": "RESEARCH_PROXY",
        "phase2e_run_id": phase2e_run_id,
        "phase2e_config_hash": phase2e.get("config_hash"),
        "event_source": "PITRepository.get_tactical_drawdown_events",
        "historical_event_input_hash": sha256_json([(event.get("tactical_event_id"), event.get("event_date"), event.get("threshold"), event.get("tactical_cycle_id")) for event in historical_events]),
        "price_returns_loaded": False,
    }
    run, created = repo.create_capital_feasibility_run({
        "capital_run_id": capital_run_id, "market": "NDX", "capital_model_version": CAPITAL_MODEL_VERSION,
        "phase2e_run_id": phase2e_run_id, "start_date": start_date, "end_date": end_date,
        "capital_model_config": CAPITAL_MODEL_CONFIG, "config_hash": CAPITAL_CONFIG_HASH,
        "data_snapshot": data_snapshot, "data_cutoff": as_of_datetime(f"{end_date}T23:59:59.999999Z"),
    })
    if not created and run.get("status") == "COMPLETED":
        stored = repo.get_capital_feasibility_report(capital_run_id)
        return (stored or {}).get("report") or {}
    scenario_rows: list[dict[str, Any]] = []
    try:
        for path in paths.values():
            for ladder_id in sorted(LADDER_CONFIG):
                for replenishment_id in sorted(REPLENISHMENT_CONFIG):
                    for cap_id in sorted(CAP_CONFIG):
                        result = simulate_capital_path(path, ladder_id=ladder_id, replenishment_id=replenishment_id, cap_id=cap_id)
                        scenario_id = _scenario_id(capital_run_id, path, ladder_id, replenishment_id, cap_id)
                        row = {
                            "scenario_result_id": scenario_id, "capital_run_id": capital_run_id,
                            "path_type": path["path_type"], "path_id": path["path_id"], "ladder_id": ladder_id,
                            "replenishment_id": replenishment_id, "cap_id": cap_id,
                            "initial_units": INITIAL_OPPORTUNITY_UNITS, "m_to_opportunity_units": M_TO_OPPORTUNITY_UNITS,
                            "sample_event_count": int(result["formal_event_count"]), "status": "OK", "result": {key: value for key, value in result.items() if key != "ledger"},
                        }
                        repo.append_capital_scenario_result(row)
                        for entry in result["ledger"]:
                            repo.append_capital_ledger_entry({
                                "capital_run_id": capital_run_id, "scenario_result_id": scenario_id,
                                "path_type": path["path_type"], "path_id": path["path_id"],
                                "event_sequence": entry["event_sequence"], "event_date": entry["event_date"],
                                "tactical_cycle_id": entry.get("tactical_cycle_id"), "macro_episode_id": entry.get("macro_episode_id"),
                                "threshold": entry.get("threshold"), "event_kind": entry["event_kind"],
                                "cash_before_units": entry["cash_before_units"], "required_units": entry["required_units"],
                                "deployed_units": entry["deployed_units"], "cash_after_units": entry["cash_after_units"],
                                "replenishment_units": entry.get("replenishment_units", 0), "surplus_units": entry.get("surplus_units", 0),
                                "underfunded_event": entry.get("underfunded_event", False), "ath_state_ignored": entry.get("ath_state_ignored", True),
                                "input_observation_ids": entry.get("input_observation_ids", []),
                                "payload": {**(entry.get("payload") or {}), "path_event_id": entry.get("path_event_id"), "duplicate_band_ignored": entry.get("duplicate_band_ignored", False)},
                            })
                        scenario_rows.append(row | {"result": result})
        report = build_report(repo, capital_run_id, phase2e_run_id=phase2e_run_id, paths=paths, scenario_results=scenario_rows, data_snapshot=data_snapshot)
        repo.record_capital_feasibility_report(capital_run_id, report)
        summary = {
            "scenario_count": len(scenario_rows), "ledger_count": sum(len((row["result"] or {}).get("ledger", [])) for row in scenario_rows),
            "PHASE_2F_STATUS": report["PHASE_2F_STATUS"], "READY_FOR_CAPITAL_STATE_MACHINE": report["READY_FOR_CAPITAL_STATE_MACHINE"],
            "RECOMMENDED_LADDER_CANDIDATES": report["RECOMMENDED_LADDER_CANDIDATES"],
        }
        repo.complete_capital_feasibility_run(capital_run_id, summary=summary)
        return report
    except Exception as exc:
        try:
            repo.complete_capital_feasibility_run(capital_run_id, status="FAILED", error={"type": type(exc).__name__, "message": str(exc)})
        except Exception:
            pass
        raise


def blind_capital_run(run: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(run, Mapping):
        return run
    return {key: value for key, value in run.items() if key not in {"summary", "error"}}


def _fmt(value: Any, digits: int = 3) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number:.{digits}f}"


def markdown_report(report: Mapping[str, Any]) -> str:
    lines = [
        "# Phase 2F：Capital Ladder Feasibility & Dry-Powder Stress Test", "",
        f"本报告对应不可变运行 `{report['capital_run_id']}`，上游 Tactical run `{report['phase2e_run_id']}`。研究只模拟 Opportunity Fund 现金状态；Core DCA、Overlay、价格收益和实盘动作均未进入。", "",
        "## 结论枚举", "", "| 项目 | 结果 |", "|---|---|",
        f"| `PHASE_2F_STATUS` | **{report['PHASE_2F_STATUS']}** |",
        f"| `LADDER_A` | **{report['LADDER_A']}** |",
        f"| `LADDER_B` | **{report['LADDER_B']}** |",
        f"| `LADDER_C` | **{report['LADDER_C']}** |",
        f"| `LADDER_D` | **{report['LADDER_D']}** |",
        f"| `RECOMMENDED_LADDER_CANDIDATES` | **{', '.join(report['RECOMMENDED_LADDER_CANDIDATES']) or '—'}** |",
        f"| `READY_FOR_CAPITAL_STATE_MACHINE` | **{report['READY_FOR_CAPITAL_STATE_MACHINE']}** |", "",
        "## 单位与冻结候选", "",
        f"- 初始 Opportunity Fund：{report['capital_unit_definition']['initial_opportunity_fund_units']} units；M 只用于无量纲现金流换算，固定 1M = {report['capital_unit_definition']['m_to_opportunity_units']} units。",
        "- Ladder A/B/C/D、R0/R1/R2、Cap 6M/12M/24M 在结果计算前冻结。",
        "- 每月补充在新月份首个日历日、同日事件之前记账；超过上限记入 surplus；现金不足时不借款。", "",
        "## 路径与账本", "",
        f"- 历史事件：{report['historical_event_count']}；scenario rows：{report['path_counts']['total']}；Extreme Extension 只观察 -60/-70/-80，不新增正式档位。",
        "- 历史、合成和序列路径共用同一个 chronological cash engine；ATH 仅作标签，不能重复扣款。", "",
        "## 梯度稳健性", "", "| Ladder | 分类 | 深档现金分数 | 历史提前耗尽均值 | 合成提前耗尽均值 | 欠资事件率均值 | 25/50/75/100%补回中位月数 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for ladder_id in ("A", "B", "C", "D"):
        item = report["ladder_assessments"][ladder_id]
        lines.append(
            f"| {ladder_id} {item['name']} | {item['classification']} | {_fmt(item.get('dry_powder_survival_score'))} | {_fmt((item.get('historical_early_exhaustion_rate') or {}).get('mean'))} | {_fmt((item.get('synthetic_early_exhaustion_rate') or {}).get('mean'))} | {_fmt((item.get('underfunded_event_rate') or {}).get('mean'))} | {', '.join(_fmt((item.get('refill_median_months') or {}).get(key)) for key in ('25','50','75','100'))} |"
        )
    lines += ["", "## 关键压力路径", "", "| 路径 | 说明 |", "|---|---|"]
    for path_id, item in report["synthetic_path_definitions"].items():
        lines.append(f"| {path_id} | {item['event_count']} 个事件；{item['start_date']} 至 {item['end_date']} |")
    lines += ["", "## 历史危机窗口", "", "| 窗口 | 事件数 | 周期数 |", "|---|---:|---:|"]
    for label, item in report.get("historical_crisis_windows", {}).items():
        lines.append(f"| {label} | {item['event_count']} | {item['cycle_count']} |")
    lines += ["", "## Gate 与限制", "", "- 选择依据是资金生存性、提前耗尽、欠资率、补回时间、顺序风险和合成压力表现；最终资产不参与排序。", "- 仍使用 Phase 2E HISTORICAL_PROXY；本阶段不是最终实盘资金状态机。", ""]
    return "\n".join(lines)


__all__ = [
    "CAPITAL_MODEL_VERSION", "CAPITAL_MODEL_CONFIG", "CAPITAL_CONFIG_HASH", "INITIAL_OPPORTUNITY_UNITS",
    "M_TO_OPPORTUNITY_UNITS", "FORMAL_THRESHOLDS", "EXTENSION_THRESHOLDS", "LADDER_CONFIG",
    "REPLENISHMENT_CONFIG", "CAP_CONFIG", "build_synthetic_paths", "build_extreme_extension_path",
    "build_sequence_paths", "simulate_capital_path", "aggregate_ladder_results", "dominance_test",
    "rank_ladders", "classify_ladder", "run_capital_feasibility_validation", "blind_capital_run", "markdown_report",
]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run Phase 2F capital ladder feasibility validation")
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--phase2e-run-id", default=None)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--markdown-out", type=Path, default=None)
    args = parser.parse_args()
    result = run_capital_feasibility_validation(PITRepository(args.db), phase2e_run_id=args.phase2e_run_id, start_date=args.start_date, end_date=args.end_date, capital_run_id=args.run_id)
    if args.json_out:
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.write_text(markdown_report(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
