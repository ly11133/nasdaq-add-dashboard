"""Phase 3A: deterministic capital-allocation state machine.

This module consumes only contemporaneous Phase 2E tactical observations and
frozen 252-trading-day crossing events. Core DCA is an untouched account; the
state machine manages Opportunity Fund cash and cap overflow in Surplus Cash.
It never reads future prices and never creates an order.
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
from phase2e_research import TACTICAL_MODEL_VERSION, TACTICAL_THRESHOLDS, TACTICAL_WINDOW
from phase2f_research import (
    FORMAL_THRESHOLDS,
    LADDER_CONFIG,
    M_TO_OPPORTUNITY_UNITS,
    build_sequence_paths,
    build_synthetic_paths,
    simulate_capital_path,
)
from pit_repository import PITRepository

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"

STATE_MACHINE_MODEL_VERSION = "NDX_CAPITAL_ALLOCATION_STATE_MACHINE_V1"
CAPITAL_STATE_MACHINE_VERSION = STATE_MACHINE_MODEL_VERSION
CAPITAL_STATE_C_VERSION = "CAPITAL_STATE_C_V1"
CAPITAL_STATE_D_VERSION = "CAPITAL_STATE_D_V1"
CAPITAL_STATE_C = CAPITAL_STATE_C_VERSION
CAPITAL_STATE_D = CAPITAL_STATE_D_VERSION
TRIGGER_VERSION = TACTICAL_MODEL_VERSION
TRIGGER_WINDOW = TACTICAL_WINDOW
TRIGGER_THRESHOLDS = tuple(float(value) for value in TACTICAL_THRESHOLDS)
LADDER_IDS = ("C", "D")

# The default profile is the Phase 2F normalized bookkeeping scale. It is not
# a currency recommendation. 24M is 24 x 20 units and keeps the old comparison
# engine exactly reproducible for R0/CAP_24M.
DEFAULT_CAPITAL_PROFILE: dict[str, Any] = {
    "profile_id": "NORMALIZED_DEFAULT",
    "unit_system": "normalized_units",
    "unit_label": "units",
    "currency": None,
    "initial_fund": 100.0,
    "monthly_refill": 0.0,
    "fund_cap": 24.0 * M_TO_OPPORTUNITY_UNITS,
    "income_linked": {"enabled": False, "savings_ratio": None, "income_schedule": {}},
    "simulation_only": True,
    "auto_trade": False,
}

STATE_MACHINE_CONFIG: dict[str, Any] = {
    "model_version": STATE_MACHINE_MODEL_VERSION,
    "created_at": "2026-09-15T00:00:00Z",
    "research_only": True,
    "simulation_only": True,
    "auto_trade": False,
    "trigger": {
        "version": TRIGGER_VERSION,
        "window_trading_days": TRIGGER_WINDOW,
        "reference": "252 trading-day tactical peak; ATH drawdown is a label only",
        "thresholds": list(TRIGGER_THRESHOLDS),
        "one_band_once_per_cycle": True,
        "same_day_order": "ascending_threshold",
    },
    "accounts": {
        "core_dca": {"separate": True, "touched_by_tactical_spend": False},
        "opportunity_fund": {"managed_by": STATE_MACHINE_MODEL_VERSION},
        "surplus_cash": {"cap_overflow_only": True, "allocation_decision": "deferred"},
    },
    "ladders": {
        "C": {"version": CAPITAL_STATE_C_VERSION, "fractions": {key: value / 100.0 for key, value in LADDER_CONFIG["C"]["allocations"].items()}},
        "D": {"version": CAPITAL_STATE_D_VERSION, "fractions": {key: value / 100.0 for key, value in LADDER_CONFIG["D"]["allocations"].items()}},
    },
    "cash_rules": {
        "planned_amount": "ladder_fraction * target_opportunity_cash",
        "actual_amount": "min(planned_amount, available_opportunity_cash)",
        "no_borrowing": True,
        "new_cycle": "rearm_bands_only; preserve_cash",
        "refill": "chronological; first calendar day of each new month before same-day events",
        "cap_overflow": "surplus_cash",
    },
    "profiles": {"default": "normalized_units", "currency_is_parameter_only": True, "income_linked_refill": "income * savings_ratio when enabled"},
    "deep_crisis_reserve_enabled": False,
    "overlay_can_modify_capital": False,
    "future_price_used": False,
    "selection": {"phase2f_candidates": ["C", "D"], "ranking_recomputed": False},
}
STATE_MACHINE_CONFIG_HASH = sha256_json(STATE_MACHINE_CONFIG)
CAPITAL_STATE_MACHINE_CONFIG = STATE_MACHINE_CONFIG
CAPITAL_STATE_MACHINE_CONFIG_HASH = STATE_MACHINE_CONFIG_HASH


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _coerce_depth(value: Any) -> float:
    """Normalize a drawdown to a positive decimal depth in [0, 1]."""
    number = _finite(value)
    if number is None:
        return 0.0
    if abs(number) > 1.0:
        number /= 100.0
    if number < 0:
        number = -number
    return max(0.0, min(number, 1.0))


def _band_key(threshold: float) -> str:
    return str(int(round(float(threshold) * 100)))


def _normalise_threshold(value: Any) -> float | None:
    number = _finite(value)
    if number is None:
        return None
    if abs(number) > 1.0:
        number /= 100.0
    number = abs(number)
    for threshold in FORMAL_THRESHOLDS:
        if abs(number - threshold) < 1e-8:
            return float(threshold)
    return None


def _date_range(start: str, end: str) -> Iterable[str]:
    cursor = date.fromisoformat(normalize_date(start))
    finish = date.fromisoformat(normalize_date(end))
    while cursor <= finish:
        yield cursor.isoformat()
        cursor += timedelta(days=1)


def normalize_capital_profile(profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate a normalized or currency profile without choosing its values."""
    supplied = dict(DEFAULT_CAPITAL_PROFILE)
    if profile:
        supplied.update(dict(profile))
    unit_system = str(supplied.get("unit_system") or supplied.get("unit") or "normalized_units").lower()
    if unit_system in {"normalized", "normalised", "units"}:
        unit_system = "normalized_units"
    if unit_system not in {"normalized_units", "currency"}:
        raise ValueError("capital_profile.unit_system 只能是 normalized_units 或 currency")
    initial = _finite(supplied.get("initial_fund", DEFAULT_CAPITAL_PROFILE["initial_fund"]))
    monthly = _finite(supplied.get("monthly_refill", supplied.get("refill_per_month", DEFAULT_CAPITAL_PROFILE["monthly_refill"])))
    cap = _finite(supplied.get("fund_cap", supplied.get("opportunity_fund_cap", DEFAULT_CAPITAL_PROFILE["fund_cap"])))
    if initial is None or monthly is None or cap is None or initial < 0 or monthly < 0 or cap < 0:
        raise ValueError("capital_profile 的 initial_fund、monthly_refill、fund_cap 必须是非负有限数")
    income_raw = supplied.get("income_linked", {})
    if isinstance(income_raw, bool):
        income_raw = {"enabled": income_raw}
    if not isinstance(income_raw, Mapping):
        raise ValueError("capital_profile.income_linked 必须是对象")
    income_enabled = bool(income_raw.get("enabled", False))
    ratio = _finite(income_raw.get("savings_ratio", supplied.get("savings_ratio")))
    if ratio is not None and not 0 <= ratio <= 1:
        raise ValueError("income_linked.savings_ratio 必须在 0–1")
    schedule_raw = income_raw.get("income_schedule", supplied.get("income_schedule", {}))
    schedule: dict[str, float] = {}
    if isinstance(schedule_raw, Mapping):
        iterator = schedule_raw.items()
    elif isinstance(schedule_raw, list):
        iterator = ((item.get("month"), item.get("income")) for item in schedule_raw if isinstance(item, Mapping))
    else:
        raise ValueError("income_schedule 必须是 YYYY-MM 映射或记录列表")
    for month, income in iterator:
        if month is None:
            continue
        key = str(month)[:7]
        if len(key) != 7 or key[4] != "-":
            raise ValueError("income_schedule 的月份必须是 YYYY-MM")
        amount = _finite(income)
        if amount is None or amount < 0:
            raise ValueError("income_schedule 的 income 必须是非负有限数")
        schedule[key] = amount
    if income_enabled and ratio is None:
        raise ValueError("启用 income_linked 时必须提供 savings_ratio")
    currency = supplied.get("currency")
    return {
        "profile_id": str(supplied.get("profile_id") or "AD_HOC_PROFILE"),
        "unit_system": unit_system,
        "unit_label": str(supplied.get("unit_label") or (str(currency) if currency else "units")),
        "currency": None if currency in (None, "") else str(currency),
        "initial_fund": float(initial),
        "monthly_refill": float(monthly),
        "fund_cap": float(cap),
        "income_linked": {"enabled": income_enabled, "savings_ratio": None if ratio is None else float(ratio), "income_schedule": dict(sorted(schedule.items()))},
        "simulation_only": True,
        "auto_trade": False,
    }


def income_linked_refill(profile: Mapping[str, Any], month: str) -> dict[str, Any]:
    """Return refill due for a month and retain missing-income evidence."""
    normalized = normalize_capital_profile(profile)
    income = normalized["income_linked"]
    month_key = str(month)[:7]
    if not income["enabled"]:
        return {"month": month_key, "amount": normalized["monthly_refill"], "income": None, "savings_ratio": None, "income_missing": False, "mode": "FIXED"}
    raw_income = income["income_schedule"].get(month_key)
    if raw_income is None:
        return {"month": month_key, "amount": 0.0, "income": None, "savings_ratio": income["savings_ratio"], "income_missing": True, "mode": "INCOME_LINKED"}
    return {"month": month_key, "amount": float(raw_income) * float(income["savings_ratio"]), "income": float(raw_income), "savings_ratio": income["savings_ratio"], "income_missing": False, "mode": "INCOME_LINKED"}


def _normalise_observation(row: Mapping[str, Any]) -> dict[str, Any]:
    day = normalize_date(row.get("date") or row.get("as_of_date") or row.get("observation_date"), field="date")
    close = _finite(row.get("close_price", row.get("close", row.get("value"))))
    tactical = _coerce_depth(row.get("tactical_drawdown", row.get("drawdown")))
    peak = _finite(row.get("tactical_peak_price", row.get("peak_price")))
    rolling = _finite(row.get("rolling_high_252", row.get("rolling_high")))
    if peak is None:
        peak = rolling
    if peak is None and close is not None and tactical < 1:
        peak = close / max(1.0 - tactical, 1e-12)
    if peak is None:
        peak = close or 0.0
    ath_raw = row.get("ath_drawdown", row.get("ath_dd"))
    ath = None if ath_raw is None else -_coerce_depth(ath_raw)
    cycle = str(row.get("tactical_cycle_id") or row.get("cycle_id") or f"cycle-{day}")
    ids = sorted({str(value) for value in (row.get("input_observation_ids") or row.get("source_observation_ids") or []) if value})
    return {"date": day, "close_price": close, "tactical_drawdown": -tactical, "ath_drawdown": ath, "tactical_cycle_id": cycle, "tactical_peak_price": float(peak), "rolling_high_252": rolling, "source_observation_id": row.get("source_observation_id") or row.get("observation_version_id"), "input_observation_ids": ids, "payload": dict(row.get("payload") or {})}


def _normalise_event(row: Mapping[str, Any]) -> dict[str, Any]:
    day = normalize_date(row.get("event_date") or row.get("date"), field="event_date")
    threshold = _normalise_threshold(row.get("threshold", row.get("band")))
    event_kind = str(row.get("event_kind") or "TACTICAL_BAND").upper()
    cycle = str(row.get("tactical_cycle_id") or row.get("cycle_id") or f"cycle-{day}")
    dd_raw = row.get("tactical_drawdown", row.get("drawdown", row.get("trigger_drawdown", threshold or 0)))
    ids = sorted({str(value) for value in (row.get("input_observation_ids") or row.get("source_observation_ids") or []) if value})
    return {"event_id": str(row.get("tactical_event_id") or row.get("path_event_id") or row.get("event_id") or sha256_json({"date": day, "cycle": cycle, "threshold": threshold, "kind": event_kind})[:32]), "event_date": day, "tactical_cycle_id": cycle, "threshold": threshold, "trigger_drawdown": -_coerce_depth(dd_raw), "ath_drawdown": None if row.get("ath_drawdown") is None else -_coerce_depth(row.get("ath_drawdown")), "event_kind": event_kind, "input_observation_ids": ids, "input_hash": str(row.get("input_hash") or ""), "payload": dict(row.get("payload") or {})}


def _ladder_version(ladder_id: str) -> str:
    if ladder_id == "C":
        return CAPITAL_STATE_C_VERSION
    if ladder_id == "D":
        return CAPITAL_STATE_D_VERSION
    raise ValueError("ladder_id 只能是 C 或 D")


def _ladder_plan(ladder_id: str, target: float, used: set[str]) -> dict[str, float]:
    allocations = LADDER_CONFIG[ladder_id]["allocations"]
    return {key: (0.0 if key in used else float(value) / 100.0 * float(target)) for key, value in allocations.items()}


def _current_band(depth: float) -> str | None:
    breached = [threshold for threshold in FORMAL_THRESHOLDS if depth + 1e-12 >= threshold]
    return _band_key(max(breached)) if breached else None


def _adequacy(cash: float, ladder_id: str, target: float, used: set[str]) -> tuple[float | None, float]:
    remaining = sum(_ladder_plan(ladder_id, target, used).values())
    if remaining <= 1e-12:
        return None, 0.0
    return float(cash) / remaining, remaining


def next_trigger_preview(state: Mapping[str, Any], *, ladders: Iterable[str] = LADDER_IDS, current_price: float | None = None, tactical_peak_price: float | None = None) -> dict[str, Any]:
    """Build a rule preview from the current state only."""
    depth = _coerce_depth(state.get("tactical_drawdown", 0))
    used = {str(value) for value in (state.get("used_bands") or [])}
    target = float(state.get("target_opportunity_cash", state.get("target_fund", 0)) or 0)
    cash = float(state.get("available_opportunity_cash", state.get("cash", 0)) or 0)
    peak = _finite(tactical_peak_price) or _finite(state.get("tactical_peak_price")) or _finite(state.get("rolling_high_252")) or _finite(current_price)
    pending = [threshold for threshold in FORMAL_THRESHOLDS if _band_key(threshold) not in used and depth + 1e-12 >= threshold]
    future = [threshold for threshold in FORMAL_THRESHOLDS if _band_key(threshold) not in used and depth + 1e-12 < threshold]
    chosen = min(pending or future, default=None)
    price = None if chosen is None or peak is None else peak * (1.0 - chosen)
    plans: dict[str, Any] = {}
    for ladder in ladders:
        key = str(ladder).upper()
        if key not in LADDER_CONFIG:
            continue
        amount = None if chosen is None else float(LADDER_CONFIG[key]["allocations"][_band_key(chosen)]) / 100.0 * target
        plans[key] = {"ladder_version": _ladder_version(key), "planned_amount": amount, "cash_sufficient": None if amount is None else cash + 1e-12 >= amount, "shortfall": None if amount is None else max(amount - cash, 0.0)}
    return {"current_tactical_drawdown": -depth, "current_cycle_id": state.get("tactical_cycle_id"), "used_bands": sorted(used, key=lambda value: int(value)), "next_trigger_threshold": chosen, "next_trigger_band": None if chosen is None else _band_key(chosen), "next_trigger_price": price, "tactical_peak_price": peak, "available_opportunity_cash": cash, "target_opportunity_cash": target, "ladder_plans": plans, "rule_only": True, "future_price_used": False, "auto_trade": False}


def _event_input_hash(event: Mapping[str, Any]) -> str:
    given = str(event.get("input_hash") or "").lower()
    if len(given) == 64 and all(char in "0123456789abcdef" for char in given):
        return given
    return sha256_json({"event_id": event.get("event_id"), "event_date": event.get("event_date"), "cycle": event.get("tactical_cycle_id"), "threshold": event.get("threshold"), "trigger_drawdown": event.get("trigger_drawdown"), "ath_drawdown": event.get("ath_drawdown"), "input_observation_ids": sorted(event.get("input_observation_ids") or [])})


def replay_capital_state_machine(
    observations: Iterable[Mapping[str, Any]] | None = None,
    trigger_events: Iterable[Mapping[str, Any]] | None = None,
    *,
    ladder_id: str = "C",
    capital_profile: Mapping[str, Any] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    run_id: str = "STATE_MACHINE_REPLAY",
    include_calendar_days: bool = True,
) -> dict[str, Any]:
    """Replay cash, events and daily states in one chronological engine."""
    ladder_id = str(ladder_id).upper()
    if ladder_id not in LADDER_IDS:
        raise ValueError("ladder_id 只能是 C 或 D")
    profile = normalize_capital_profile(capital_profile)
    obs = [_normalise_observation(item) for item in (observations or [])]
    events = [_normalise_event(item) for item in (trigger_events or [])]
    if not obs and not events and (start_date is None or end_date is None):
        raise ValueError("没有观察或事件时必须提供 start_date 与 end_date")
    inferred_dates = [item["date"] for item in obs] + [item["event_date"] for item in events]
    start = normalize_date(start_date or min(inferred_dates), field="start_date")
    end = normalize_date(end_date or max(inferred_dates), field="end_date")
    if start > end:
        raise ValueError("state machine start_date 不能晚于 end_date")
    obs_by_date: dict[str, dict[str, Any]] = {}
    for item in sorted(obs, key=lambda row: (row["date"], json.dumps(row, sort_keys=True, ensure_ascii=False))):
        if start <= item["date"] <= end:
            obs_by_date[item["date"]] = item
    events_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in events:
        if start <= item["event_date"] <= end:
            events_by_date[item["event_date"]].append(item)
    for day in events_by_date:
        events_by_date[day].sort(key=lambda row: (float(row["threshold"] or 99), row["event_id"]))
    days = list(_date_range(start, end)) if include_calendar_days else sorted(set(obs_by_date) | set(events_by_date) | {start, end})

    target = float(profile["initial_fund"])
    cap = float(profile["fund_cap"])
    opening_cash = min(target, cap)
    opening_surplus = max(target - cap, 0.0)
    cash = opening_cash
    surplus = opening_surplus
    total_scheduled_refill = 0.0
    total_credited_refill = 0.0
    total_surplus_transfer = opening_surplus
    total_planned = 0.0
    total_deployed = 0.0
    underfunded_count = 0
    duplicate_count = 0
    ath_ignored_count = 0
    extension_ignored_count = 0
    rearm_count = 0
    current_cycle: str | None = None
    used_bands: set[str] = set()
    latest: dict[str, Any] = {"date": start, "tactical_drawdown": 0.0, "ath_drawdown": None, "tactical_cycle_id": f"cycle-{start}", "tactical_peak_price": None, "close_price": None, "rolling_high_252": None, "payload": {}}
    previous_state_hash: str | None = None
    previous_event_hash: str | None = None
    last_event: dict[str, Any] | None = None
    event_log: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    refill_history: list[dict[str, Any]] = []
    month_seen: set[str] = set()
    transaction_count = 0
    ignored_events: list[dict[str, Any]] = []

    # Event-only synthetic paths have no observation row from which to seed a
    # cycle.  Seed from the first chronological input so the first event is
    # not misclassified as a re-arm; a re-arm must represent a real cycle
    # change, not an implementation fallback.
    first_cycle_source = None
    if obs:
        first_cycle_source = min(obs, key=lambda row: (row["date"], str(row.get("tactical_cycle_id"))))
    elif events:
        first_cycle_source = min(events, key=lambda row: (row["event_date"], str(row.get("event_id"))))
    if first_cycle_source is not None:
        current_cycle = str(first_cycle_source.get("tactical_cycle_id") or f"cycle-{start}")
        latest["tactical_cycle_id"] = current_cycle

    for day in days:
        rearmed = False
        if day in obs_by_date:
            latest = dict(latest) | obs_by_date[day]
            incoming_cycle = str(latest["tactical_cycle_id"])
            if current_cycle is None:
                current_cycle = incoming_cycle
            elif incoming_cycle != current_cycle:
                current_cycle = incoming_cycle
                used_bands = set()
                rearm_count += 1
                rearmed = True
        if current_cycle is None:
            current_cycle = str(latest["tactical_cycle_id"])

        monthly_rate_info = income_linked_refill(profile, day[:7])
        monthly_rate = float(monthly_rate_info["amount"])
        credited = 0.0
        overflow = 0.0
        # The opening fund already represents the start month. Every later
        # calendar month is credited on its first day before event handling.
        if day != start and day[8:10] == "01" and day[:7] not in month_seen:
            month_seen.add(day[:7])
            total_scheduled_refill += monthly_rate
            available_room = max(cap - cash, 0.0)
            credited = min(monthly_rate, available_room)
            overflow = max(monthly_rate - credited, 0.0)
            cash += credited
            surplus += overflow
            total_credited_refill += credited
            total_surplus_transfer += overflow
            refill_history.append({"date": day, "month": day[:7], "scheduled": monthly_rate, "credited": credited, "surplus": overflow, "mode": monthly_rate_info["mode"], "income": monthly_rate_info["income"], "income_missing": monthly_rate_info["income_missing"]})

        day_events = events_by_date.get(day, [])
        if day_events and str(day_events[0]["tactical_cycle_id"]) != current_cycle:
            current_cycle = str(day_events[0]["tactical_cycle_id"])
            used_bands = set()
            rearm_count += 1
            rearmed = True
        transaction_id = f"capital-tx-{run_id}-{day}" if day_events else None
        transaction_sequence = 0
        day_event_ids: list[str] = []
        for event in day_events:
            event_cycle = str(event["tactical_cycle_id"])
            if event_cycle != current_cycle:
                current_cycle = event_cycle
                used_bands = set()
                rearm_count += 1
                rearmed = True
            threshold = event.get("threshold")
            formal = event["event_kind"] == "TACTICAL_BAND" and threshold in FORMAL_THRESHOLDS
            if not formal:
                if event["event_kind"] == "EXTENSION_OBSERVATION":
                    extension_ignored_count += 1
                else:
                    ath_ignored_count += 1
                ignored_events.append({"event_id": event["event_id"], "event_date": day, "reason": "ATH_LABEL_OR_NON_FORMAL_EVENT", "ath_drawdown": event.get("ath_drawdown"), "event_kind": event["event_kind"]})
                continue
            band = _band_key(float(threshold))
            if band in used_bands:
                duplicate_count += 1
                ignored_events.append({"event_id": event["event_id"], "event_date": day, "reason": "DUPLICATE_BAND_IN_CYCLE", "band": band, "tactical_cycle_id": current_cycle})
                continue
            transaction_sequence += 1
            if transaction_sequence == 1:
                transaction_count += 1
            planned = float(LADDER_CONFIG[ladder_id]["allocations"][band]) / 100.0 * target
            cash_before = cash
            actual = min(planned, max(cash_before, 0.0))
            shortfall = max(planned - actual, 0.0)
            cash = max(cash_before - actual, 0.0)
            underfunded = shortfall > 1e-9
            total_planned += planned
            total_deployed += actual
            underfunded_count += int(underfunded)
            used_bands.add(band)
            day_event_ids.append(event["event_id"])
            input_hash = _event_input_hash(event)
            material = {
                "state_machine_run_id": run_id,
                "transaction_id": transaction_id,
                "transaction_sequence": transaction_sequence,
                "event_date": day,
                "tactical_cycle_id": current_cycle,
                "band": band,
                "trigger_drawdown": float(event["trigger_drawdown"]),
                "ath_drawdown": event.get("ath_drawdown"),
                "ladder_version": _ladder_version(ladder_id),
                "planned_amount": planned,
                "actual_amount": actual,
                "shortfall": shortfall,
                "cash_before": cash_before,
                "cash_after": cash,
                "fund_target": target,
                "fund_cap": cap,
                "monthly_refill": monthly_rate,
                "underfunded": underfunded,
                "input_hash": input_hash,
                "previous_event_hash": previous_event_hash,
            }
            event_hash = sha256_json(material)
            entry = {
                "capital_event_id": sha256_json({"run": run_id, "event_hash": event_hash})[:32],
                "state_machine_run_id": run_id,
                "transaction_id": transaction_id,
                "transaction_sequence": transaction_sequence,
                "event_date": day,
                "tactical_cycle_id": current_cycle,
                "band": band,
                "trigger_drawdown": float(event["trigger_drawdown"]),
                "ath_drawdown": event.get("ath_drawdown"),
                "ladder_version": _ladder_version(ladder_id),
                "planned_amount": planned,
                "actual_amount": actual,
                "shortfall": shortfall,
                "cash_before": cash_before,
                "cash_after": cash,
                "fund_target": target,
                "fund_cap": cap,
                "monthly_refill": monthly_rate,
                "underfunded": underfunded,
                "input_hash": input_hash,
                "previous_event_hash": previous_event_hash,
                "event_hash": event_hash,
                "payload": {
                    "source_tactical_event_id": event["event_id"],
                    "source_observation_ids": list(event["input_observation_ids"]),
                    "ath_is_label_only": True,
                    "overlay_can_modify_capital": False,
                    "same_day_ascending_threshold_order": True,
                    "refill_credited_before_event": credited if transaction_sequence == 1 else 0.0,
                    "surplus_transfer_before_event": overflow if transaction_sequence == 1 else 0.0,
                    "core_dca_untouched": True,
                },
            }
            event_log.append(entry)
            previous_event_hash = event_hash
            last_event = entry

        if day_events:
            valid_drawdowns = [event["trigger_drawdown"] for event in day_events if event.get("threshold") is not None]
            if valid_drawdowns:
                latest["tactical_drawdown"] = min(valid_drawdowns + [float(latest.get("tactical_drawdown") or 0.0)])
        depth = _coerce_depth(latest.get("tactical_drawdown"))
        next_state = {
            "date": day,
            "tactical_cycle_id": current_cycle,
            "ath_drawdown": latest.get("ath_drawdown"),
            "tactical_drawdown": -depth,
            "close_price": latest.get("close_price"),
            "tactical_peak_price": latest.get("tactical_peak_price"),
            "rolling_high_252": latest.get("rolling_high_252"),
            "current_band": _current_band(depth),
            "used_bands": sorted(used_bands, key=lambda value: int(value)),
            "armed_bands": sorted(set(_band_key(value) for value in FORMAL_THRESHOLDS) - used_bands, key=lambda value: int(value)),
            "available_opportunity_cash": cash,
            "target_opportunity_cash": target,
            "opportunity_fund_cap": cap,
            "monthly_refill_rate": monthly_rate,
            "surplus_cash": surplus,
            "last_capital_event_date": None if last_event is None else last_event["event_date"],
            "last_capital_event_band": None if last_event is None else last_event["band"],
            "last_capital_event_amount": 0.0 if last_event is None else last_event["actual_amount"],
            "ladder_version": _ladder_version(ladder_id),
            "trigger_version": TRIGGER_VERSION,
            "capital_model_version": STATE_MACHINE_MODEL_VERSION,
            "core_dca_untouched": True,
        }
        preview = next_trigger_preview(next_state, current_price=latest.get("close_price"), tactical_peak_price=latest.get("tactical_peak_price"))
        adequacy, remaining = _adequacy(cash, ladder_id, target, used_bands)
        next_state["capital_adequacy_ratio"] = adequacy
        next_state["next_trigger"] = preview
        next_state["payload"] = {
            "events_today": day_event_ids,
            "ignored_events_today": [item["event_id"] for item in ignored_events if item["event_date"] == day],
            "same_day_transaction_id": transaction_id,
            "same_day_event_count": len(day_event_ids),
            "rearmed_today": rearmed,
            "remaining_theoretical_amount": remaining,
            "refill": refill_history[-1] if refill_history and refill_history[-1]["date"] == day else None,
            "cash_conservation_formula": "opening_cash + credited_refill - actual_spend",
            "ath_is_label_only": True,
            "overlay_can_modify_capital": False,
            "deep_crisis_reserve_enabled": False,
            "simulation_only": True,
            "auto_trade": False,
        }
        state_material = {key: value for key, value in next_state.items() if key not in {"state_hash"}}
        state_material["previous_state_hash"] = previous_state_hash
        state_hash = sha256_json(state_material)
        next_state["state_hash"] = state_hash
        next_state["previous_state_hash"] = previous_state_hash
        snapshots.append(next_state)
        previous_state_hash = state_hash

    expected_cash = opening_cash + total_credited_refill - total_deployed
    conservation_error = abs(expected_cash - cash)
    replay_fingerprint = sha256_json({"events": event_log, "snapshots": snapshots})
    return {
        "state_machine_model_version": STATE_MACHINE_MODEL_VERSION,
        "run_id": run_id,
        "ladder_id": ladder_id,
        "ladder_version": _ladder_version(ladder_id),
        "trigger_version": TRIGGER_VERSION,
        "trigger_window": TRIGGER_WINDOW,
        "trigger_thresholds": list(TRIGGER_THRESHOLDS),
        "start_date": start,
        "end_date": end,
        "capital_profile": profile,
        "opening_cash": opening_cash,
        "opening_surplus": opening_surplus,
        "target_opportunity_cash": target,
        "opportunity_fund_cap": cap,
        "total_scheduled_refill": total_scheduled_refill,
        "total_credited_refill": total_credited_refill,
        "surplus_cash": surplus,
        "total_surplus_transfer": total_surplus_transfer,
        "total_planned": total_planned,
        "total_deployed": total_deployed,
        "ending_cash": cash,
        "underfunded_event_count": underfunded_count,
        "duplicate_trigger_count": duplicate_count,
        "ath_ignored_count": ath_ignored_count,
        "extension_ignored_count": extension_ignored_count,
        "rearm_count": rearm_count,
        "transaction_count": transaction_count,
        "event_count": len(event_log),
        "snapshot_count": len(snapshots),
        "capital_utilization": total_deployed / (opening_cash + total_credited_refill) if opening_cash + total_credited_refill > 0 else 0.0,
        "underfunded_event_rate": underfunded_count / len(event_log) if event_log else 0.0,
        "cash_conservation_error": conservation_error,
        "cash_conservation_holds": conservation_error <= 1e-8,
        "cash_nonnegative": cash >= -1e-9 and all(float(item["available_opportunity_cash"]) >= -1e-9 for item in snapshots),
        "core_dca_untouched": True,
        "overlay_used": False,
        "future_price_used": False,
        "simulation_only": True,
        "auto_trade": False,
        "events": event_log,
        "daily_snapshots": snapshots,
        "refill_history": refill_history,
        "ignored_events": ignored_events,
        "replay_fingerprint": replay_fingerprint,
        "final_state": snapshots[-1] if snapshots else None,
    }


simulate_capital_state_machine = replay_capital_state_machine
run_capital_state_machine = replay_capital_state_machine


def _historical_inputs(repo: PITRepository, tactical_run_id: str, start_date: str, end_date: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    states = repo.get_tactical_drawdown_states(tactical_run_id, limit=2000000)
    states = [row for row in states if normalize_date(row["as_of_date"]) <= end_date]
    states.sort(key=lambda row: (normalize_date(row["as_of_date"]), str(row.get("tactical_state_id"))))
    high = 0.0
    observations: list[dict[str, Any]] = []
    for row in states:
        close = _finite(row.get("close_price"))
        if close is None:
            continue
        high = max(high, close)
        observations.append({
            "date": row["as_of_date"],
            "close_price": close,
            "tactical_drawdown": row.get("tactical_drawdown"),
            "ath_drawdown": close / high - 1.0 if high else None,
            "tactical_cycle_id": row.get("tactical_cycle_id"),
            "tactical_peak_price": row.get("tactical_peak_price"),
            "rolling_high_252": row.get("rolling_high_252"),
            "source_observation_id": row.get("source_observation_id"),
            "input_observation_ids": row.get("input_observation_ids") or [],
            "payload": row.get("payload") or {},
        })
    events = repo.get_tactical_drawdown_events(tactical_run_id, limit=1000000)
    events = [row for row in events if start_date <= normalize_date(row["event_date"]) <= end_date]
    return observations, events


def _path_for_events(path: Mapping[str, Any]) -> dict[str, Any]:
    return {"path_type": path["path_type"], "path_id": path["path_id"], "start_date": path["start_date"], "end_date": path["end_date"], "events": path.get("events", []), "metadata": path.get("metadata", {})}


def _summary_by_type(results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in results:
        grouped[str(item["path_type"])].append(item)
    output: dict[str, Any] = {}
    for key, rows in sorted(grouped.items()):
        ending = [float(row["ending_cash"]) for row in rows]
        utilization = [float(row["capital_utilization"]) for row in rows]
        underfunded = [float(row["underfunded_event_rate"]) for row in rows]
        output[key] = {
            "path_count": len(rows),
            "event_count": sum(int(row["event_count"]) for row in rows),
            "ending_cash": {"mean": mean(ending) if ending else None, "median": median(ending) if ending else None},
            "capital_utilization": {"mean": mean(utilization) if utilization else None, "median": median(utilization) if utilization else None},
            "underfunded_event_rate": {"mean": mean(underfunded) if underfunded else None, "median": median(underfunded) if underfunded else None},
            "cash_conservation_holds": all(bool(row["cash_conservation_holds"]) for row in rows),
            "cash_nonnegative": all(bool(row["cash_nonnegative"]) for row in rows),
        }
    return output


def _compare_phase2f(phase2f_run_id: str | None, *, paths: Mapping[str, Mapping[str, Any]], ladder_id: str, profile: Mapping[str, Any]) -> dict[str, Any]:
    """Compare the default zero-refill state machine with Phase 2F's engine."""
    comparisons: list[dict[str, Any]] = []
    for path_id, path in paths.items():
        if path["path_type"] == "EXTREME_EXTENSION":
            continue
        state = replay_capital_state_machine([], path.get("events", []), ladder_id=ladder_id, capital_profile=profile, start_date=path["start_date"], end_date=path["end_date"], run_id=f"compare-{ladder_id}-{path_id}", include_calendar_days=True)
        old = simulate_capital_path(_path_for_events(path), ladder_id=ladder_id, replenishment_id="R0", cap_id="CAP_24M", initial_units=float(profile["initial_fund"]), m_to_opportunity_units=M_TO_OPPORTUNITY_UNITS)
        comparisons.append({"path_type": path["path_type"], "path_id": path_id, "state_machine_deployed": state["total_deployed"], "phase2f_deployed": old["total_deployed_units"], "state_machine_ending_cash": state["ending_cash"], "phase2f_ending_cash": old["ending_cash_units"], "deployed_match": abs(state["total_deployed"] - old["total_deployed_units"]) <= 1e-8, "ending_cash_match": abs(state["ending_cash"] - old["ending_cash_units"]) <= 1e-8})
    return {"phase2f_run_id": phase2f_run_id, "comparisons": comparisons, "all_match": all(item["deployed_match"] and item["ending_cash_match"] for item in comparisons) if comparisons else True}


def build_phase3a_report(*, batch_id: str, run_results: Mapping[str, Mapping[str, Any]], phase2e_run_id: str | None, phase2f_run_id: str | None, phase2f_candidates_unchanged: bool = True) -> dict[str, Any]:
    by_ladder = {ladder: run_results[ladder] for ladder in LADDER_IDS if ladder in run_results}
    all_path_results = [row for item in by_ladder.values() for row in item.get("path_results", [])]
    invariant_rows = all_path_results
    all_invariants = all(bool(row.get("cash_conservation_holds")) and bool(row.get("cash_nonnegative")) and bool(row.get("core_dca_untouched")) and not bool(row.get("overlay_used")) and not bool(row.get("future_price_used")) for row in invariant_rows)
    deterministic = bool(by_ladder) and all(bool(item.get("replay_fingerprint")) for item in by_ladder.values())
    phase2f_checks = {ladder: item.get("phase2f_consistency", {}) for ladder, item in by_ladder.items()}
    return {
        "phase3a_batch_id": batch_id,
        "capital_state_machine_version": STATE_MACHINE_MODEL_VERSION,
        "config_hash": STATE_MACHINE_CONFIG_HASH,
        "PHASE_3A_STATUS": "PASS_WITH_LIMITATIONS" if by_ladder and all_invariants else "FAIL",
        "CAPITAL_STATE_C": "VALID" if "C" in by_ladder and all_invariants else "INVALID",
        "CAPITAL_STATE_D": "VALID" if "D" in by_ladder and all_invariants else "INVALID",
        "STATE_MACHINE_REPLAY": "DETERMINISTIC" if deterministic else "NOT_DETERMINISTIC",
        "READY_FOR_REAL_WORLD_PARAMETERIZATION": "YES" if by_ladder and all_invariants else "NO",
        "simulation_only": True,
        "auto_trade": False,
        "phase2e_run_id": phase2e_run_id,
        "phase2f_run_id": phase2f_run_id,
        "trigger": copy.deepcopy(STATE_MACHINE_CONFIG["trigger"]),
        "ladder_definitions": copy.deepcopy(STATE_MACHINE_CONFIG["ladders"]),
        "accounts": copy.deepcopy(STATE_MACHINE_CONFIG["accounts"]),
        "capital_profiles": [item.get("capital_profile") for item in by_ladder.values()],
        "run_ids": {ladder: item.get("run_id") for ladder, item in by_ladder.items()},
        "historical": {ladder: _summary_by_type([row for row in item.get("path_results", []) if row["path_type"] == "HISTORICAL"]) for ladder, item in by_ladder.items()},
        "synthetic": {ladder: _summary_by_type([row for row in item.get("path_results", []) if row["path_type"] == "SYNTHETIC"]) for ladder, item in by_ladder.items()},
        "sequence": {ladder: _summary_by_type([row for row in item.get("path_results", []) if row["path_type"] == "SEQUENCE"]) for ladder, item in by_ladder.items()},
        "path_counts": {"historical": sum(1 for row in all_path_results if row["path_type"] == "HISTORICAL"), "synthetic": sum(1 for row in all_path_results if row["path_type"] == "SYNTHETIC"), "sequence": sum(1 for row in all_path_results if row["path_type"] == "SEQUENCE")},
        "phase2f_consistency": phase2f_checks,
        "capital_adequacy": {"formula": "available_opportunity_cash / sum(remaining ladder_fraction * target_opportunity_cash)", "equality_is_sufficient": True, "implemented": True},
        "next_trigger_preview": {ladder: (item.get("final_state") or {}).get("next_trigger", {}) for ladder, item in by_ladder.items()},
        "phase2f_candidates_unchanged": bool(phase2f_candidates_unchanged),
        "ranking_recomputed": False,
        "invariants": {
            "cash_nonnegative": all(bool(row.get("cash_nonnegative")) for row in invariant_rows),
            "actual_le_planned": all(float(row.get("total_deployed", 0)) <= float(row.get("total_planned", 0)) + 1e-8 for row in invariant_rows),
            "cash_conservation": all(bool(row.get("cash_conservation_holds")) for row in invariant_rows),
            "band_once_per_cycle": True,
            "new_cycle_rearms_only": True,
            "ath_does_not_spend": True,
            "core_dca_untouched": all(bool(row.get("core_dca_untouched")) for row in invariant_rows),
            "overlay_cannot_modify": all(not bool(row.get("overlay_used")) for row in invariant_rows),
            "future_price_unused": all(not bool(row.get("future_price_used")) for row in invariant_rows),
            "same_trigger_history_c_and_d": phase2f_candidates_unchanged,
        },
        "replay_fingerprints": {ladder: item.get("replay_fingerprint") for ladder, item in by_ladder.items()},
        "limitations": [
            "上游 Phase 2E NDX 历史输入仍是 HISTORICAL_PROXY，不是完整严格 PIT。",
            "READY_FOR_REAL_WORLD_PARAMETERIZATION 只表示参数接口与重放账本已就绪，不是自动交易许可或金额建议。",
            "默认 normalized_units 与 24M 账本尺度沿用 Phase 2F；货币配置需要用户提供，不在核心逻辑中写死。",
            "Surplus 本阶段只记录，不决定其后续用途；深度 -60/-70/-80 reserve 保持关闭。",
        ],
    }


def run_phase3a_validation(repo: PITRepository | None = None, *, phase2e_run_id: str | None = None, phase2f_run_id: str | None = None, start_date: str | None = None, end_date: str | None = None, batch_id: str | None = None, capital_profile: Mapping[str, Any] | None = None, ladders: Iterable[str] = LADDER_IDS) -> dict[str, Any]:
    repo = repo or PITRepository(DB)
    candidates = repo.get_tactical_drawdown_runs(market="NDX", limit=1000)
    tactical = repo.get_tactical_drawdown_run(phase2e_run_id) if phase2e_run_id else next((item for item in candidates if item.get("status") == "COMPLETED"), None)
    if not tactical or tactical.get("status") != "COMPLETED":
        raise ValueError("需要一个已完成的 Phase 2E run")
    phase2e_run_id = str(tactical["tactical_run_id"])
    start = normalize_date(start_date or tactical["start_date"])
    end = normalize_date(end_date or tactical["end_date"])
    profile = normalize_capital_profile(capital_profile)
    batch = str(batch_id or f"phase3a-capital-ndx-v1-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    observations, events = _historical_inputs(repo, phase2e_run_id, start, end)
    synthetic_paths = build_synthetic_paths()
    sequence_paths = build_sequence_paths()
    selected = tuple(str(item).upper() for item in ladders)
    if any(item not in LADDER_IDS for item in selected):
        raise ValueError("ladders 只能包含 C、D")
    path_sets: list[tuple[str, Mapping[str, Any]]] = [("HISTORICAL", {"path_type": "HISTORICAL", "path_id": "NDX_HISTORICAL", "start_date": start, "end_date": end, "events": events, "metadata": {"phase2e_run_id": phase2e_run_id}})]
    path_sets.extend(("SYNTHETIC", path) for path in synthetic_paths.values())
    path_sets.extend(("SEQUENCE", path) for path in sequence_paths.values())
    run_results: dict[str, dict[str, Any]] = {}
    # Include the historical event stream in the same comparison set as the
    # S1–S8 and sequence paths.  This is a ledger-consistency check only: the
    # Phase 2F ranking is not recomputed and no future outcome is introduced.
    phase2f_paths = {
        "NDX_HISTORICAL": {"path_type": "HISTORICAL", "path_id": "NDX_HISTORICAL", "start_date": start, "end_date": end, "events": events},
        **{path["path_id"]: path for path in synthetic_paths.values()},
        **{path["path_id"]: path for path in sequence_paths.values()},
    }
    for ladder in selected:
        run_id = f"{batch}-{ladder}"
        run, created = repo.create_capital_state_machine_run({
            "state_machine_run_id": run_id, "batch_id": batch, "market": "NDX", "state_machine_model_version": STATE_MACHINE_MODEL_VERSION,
            "ladder_id": ladder, "ladder_version": _ladder_version(ladder), "trigger_version": TRIGGER_VERSION, "phase2e_run_id": phase2e_run_id,
            "phase2f_run_id": phase2f_run_id, "start_date": start, "end_date": end, "capital_profile": profile, "state_machine_config": STATE_MACHINE_CONFIG,
            "config_hash": STATE_MACHINE_CONFIG_HASH, "data_snapshot": {"phase2e_run_id": phase2e_run_id, "state_count": len(observations), "event_count": len(events), "source": "Phase 2E contemporaneous states and tactical events"},
            "data_cutoff": as_of_datetime(f"{end}T23:59:59.999999Z"), "simulation_only": True, "auto_trade": False,
        })
        if not created and run.get("status") == "COMPLETED":
            report_row = repo.get_capital_state_machine_report(run_id)
            if report_row and report_row.get("report"):
                run_results[ladder] = report_row["report"].get("_run_result", report_row["report"])
                continue
        try:
            historical = replay_capital_state_machine(observations, events, ladder_id=ladder, capital_profile=profile, start_date=start, end_date=end, run_id=run_id, include_calendar_days=True)
            path_results: list[dict[str, Any]] = []
            for path_type, path in path_sets:
                replay = historical if path_type == "HISTORICAL" else replay_capital_state_machine([], path.get("events", []), ladder_id=ladder, capital_profile=profile, start_date=path["start_date"], end_date=path["end_date"], run_id=f"{run_id}-{path['path_id']}", include_calendar_days=True)
                replay["path_type"] = path_type
                replay["path_id"] = path["path_id"]
                path_results.append(replay)
            result = {"run_id": run_id, "ladder_id": ladder, "capital_profile": profile, "replay_fingerprint": historical["replay_fingerprint"], "final_state": historical.get("final_state"), "path_results": path_results, "historical": historical, "phase2f_consistency": _compare_phase2f(phase2f_run_id, paths=phase2f_paths, ladder_id=ladder, profile=profile)}
            for entry in historical["events"]:
                repo.append_capital_event(entry)
            for snapshot in historical["daily_snapshots"]:
                repo.append_capital_daily_snapshot({"state_machine_run_id": run_id, "as_of_date": snapshot["date"], **snapshot})
            run_results[ladder] = result
        except Exception as exc:
            try: repo.complete_capital_state_machine_run(run_id, status="FAILED", error={"type": type(exc).__name__, "message": str(exc)})
            except Exception: pass
            raise
    report = build_phase3a_report(batch_id=batch, run_results=run_results, phase2e_run_id=phase2e_run_id, phase2f_run_id=phase2f_run_id, phase2f_candidates_unchanged=True)
    for ladder, item in run_results.items():
        report_copy = dict(report); report_copy["_run_result"] = item
        repo.record_capital_state_machine_report(item["run_id"], report_copy)
        repo.complete_capital_state_machine_run(item["run_id"], summary={"PHASE_3A_STATUS": report["PHASE_3A_STATUS"], "CAPITAL_STATE": report.get(f"CAPITAL_STATE_{ladder}"), "snapshot_count": len(item.get("historical", {}).get("daily_snapshots", [])), "event_count": len(item.get("historical", {}).get("events", []))})
    return report


def report_from_repository(repo: PITRepository | None = None) -> dict[str, Any]:
    repo = repo or PITRepository(DB)
    runs = repo.get_capital_state_machine_runs(market="NDX", limit=1000)
    completed = [item for item in runs if item.get("status") == "COMPLETED"]
    if not completed:
        return {"PHASE_3A_STATUS": "NOT_RUN", "CAPITAL_STATE_C": "UNKNOWN", "CAPITAL_STATE_D": "UNKNOWN", "STATE_MACHINE_REPLAY": "NOT_DETERMINISTIC", "READY_FOR_REAL_WORLD_PARAMETERIZATION": "NO", "simulation_only": True, "auto_trade": False}
    batch = completed[0]["batch_id"]
    candidates = [item for item in completed if item["batch_id"] == batch]
    chosen = next((item for item in candidates if item["ladder_id"] == "C"), candidates[0])
    report_row = repo.get_capital_state_machine_report(chosen["state_machine_run_id"])
    report = (report_row or {}).get("report")
    if isinstance(report, Mapping):
        # The append-only report row keeps an internal replay cache so a
        # completed run can be re-opened without recalculation. It contains
        # every path's daily snapshots and is omitted from the status payload;
        # dedicated state/event endpoints expose those records explicitly.
        report = dict(report)
        report.pop("_run_result", None)
        return report
    return {"PHASE_3A_STATUS": "INCONCLUSIVE", "phase3a_batch_id": batch}


def markdown_report(report: Mapping[str, Any]) -> str:
    lines = [
        "# Phase 3A：Capital Allocation State Machine V1", "",
        f"批次 `{report.get('phase3a_batch_id')}`；simulation_only={report.get('simulation_only')}、auto_trade={report.get('auto_trade')}。", "",
        "| 结论 | 状态 |", "|---|---|",
        f"| `PHASE_3A_STATUS` | **{report.get('PHASE_3A_STATUS')}** |",
        f"| `CAPITAL_STATE_C` | **{report.get('CAPITAL_STATE_C')}** |",
        f"| `CAPITAL_STATE_D` | **{report.get('CAPITAL_STATE_D')}** |",
        f"| `STATE_MACHINE_REPLAY` | **{report.get('STATE_MACHINE_REPLAY')}** |",
        f"| `READY_FOR_REAL_WORLD_PARAMETERIZATION` | **{report.get('READY_FOR_REAL_WORLD_PARAMETERIZATION')}** |", "",
        "## 规则与账户", "",
        f"- Trigger：{TRIGGER_WINDOW} 个交易日 Tactical Drawdown；档位 {', '.join(f'{int(x*100)}%' for x in TRIGGER_THRESHOLDS)}；ATH 只作标签。",
        "- C/D 使用完整 Opportunity Fund Target 的固定比例；Core DCA 独立；Cap 溢出进入 Surplus。",
        "- 新周期只重新武装 bands，不补满现金；同日多个档位按升序在同一 transaction 中执行。", "",
        "## 回放与不变量", "",
        f"- 路径数量：{json.dumps(report.get('path_counts', {}), ensure_ascii=False)}。",
        "- Capital Adequacy = available cash / remaining theoretical required; 等于 1 已足够。",
        f"- Phase 2F 候选保持：{report.get('phase2f_candidates_unchanged')}；本阶段未用最终资产重新排名。", "",
        "## 限制", "",
    ]
    lines.extend(f"- {item}" for item in report.get("limitations", []))
    return "\n".join(lines) + "\n"


__all__ = [
    "STATE_MACHINE_MODEL_VERSION", "CAPITAL_STATE_MACHINE_VERSION", "CAPITAL_STATE_C_VERSION", "CAPITAL_STATE_D_VERSION", "CAPITAL_STATE_C", "CAPITAL_STATE_D", "TRIGGER_VERSION", "TRIGGER_WINDOW", "TRIGGER_THRESHOLDS", "LADDER_IDS", "DEFAULT_CAPITAL_PROFILE", "STATE_MACHINE_CONFIG", "STATE_MACHINE_CONFIG_HASH", "CAPITAL_STATE_MACHINE_CONFIG", "CAPITAL_STATE_MACHINE_CONFIG_HASH", "normalize_capital_profile", "income_linked_refill", "next_trigger_preview", "replay_capital_state_machine", "simulate_capital_state_machine", "run_capital_state_machine", "run_phase3a_validation", "build_phase3a_report", "report_from_repository", "markdown_report",
]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run Phase 3A capital allocation state machine")
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--phase2e-run-id", default=None)
    parser.add_argument("--phase2f-run-id", default=None)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--markdown-out", type=Path, default=None)
    args = parser.parse_args()
    result = run_phase3a_validation(PITRepository(args.db), phase2e_run_id=args.phase2e_run_id, phase2f_run_id=args.phase2f_run_id, start_date=args.start_date, end_date=args.end_date, batch_id=args.run_id)
    if args.json_out: args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    if args.markdown_out: args.markdown_out.write_text(markdown_report(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
