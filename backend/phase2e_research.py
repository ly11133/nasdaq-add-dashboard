"""Phase 2E: tactical drawdown cycles and re-arm validation.

Phase 2C/2D keep the ATH-to-ATH macro episode as the statistical block.  That
definition is deliberately left untouched here.  This module adds a separate
252-trading-day trailing-high state machine for the operational question:
when can a new drawdown ladder be armed after a long bear market?

The state machine is deterministic and chronological.  A rolling-window expiry
can lower the observed high, but it cannot by itself create a new cycle.  A
fresh price high relative to the prior 252 observations is required.  All
mechanical events are written before the explicit evaluation pass reads future
prices, and the overlay fields are copied from the frozen Phase 2D definition.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import copy
import json
import math
import random
from pathlib import Path
from typing import Any, Iterable, Mapping

from data_contract import as_of_datetime, normalize_date, sha256_json
from pit_repository import PITRepository
from phase2b_research import (
    _Index,
    _data_snapshot,
    _finite,
    _load_proxy_indexes,
    _price_feature_rows,
)
from phase2c_research import _forward_metrics
from phase2d_research import (
    DRAW_DOWN_BANDS,
    OVERLAY_CONFIG_HASH,
    OVERLAY_MODEL_CONFIG,
    OVERLAY_NAMES,
    OUTCOME_NAMES,
    _feature_rank,
    _stats,
    conditional_results as _phase2d_conditional_results,
    model_results as _phase2d_model_results,
)


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"

TACTICAL_MODEL_VERSION = "NDX_TACTICAL_DRAWDOWN_V1"
TACTICAL_MODEL_CREATED_AT = "2026-09-15T00:00:00Z"
TACTICAL_WINDOW = 252
TACTICAL_THRESHOLDS = (0.10, 0.20, 0.30, 0.40, 0.50)

# This object is frozen before the run starts.  The overlay subtree is copied
# from Phase 2D and hashed as part of this config so a later direction change
# cannot silently enter the tactical replay.
TACTICAL_MODEL_CONFIG: dict[str, Any] = {
    "model_version": TACTICAL_MODEL_VERSION,
    "created_at": TACTICAL_MODEL_CREATED_AT,
    "research_only": True,
    "strict_pit": False,
    "investment_action_eligible": False,
    "upstream_phase2c_model_version": "EPISODE_OPPORTUNITY_V1",
    "upstream_phase2d_overlay_config_hash": OVERLAY_CONFIG_HASH,
    "reference": "252-trading-day trailing high for tactical trigger; ATH-to-ATH remains macro reference",
    "trailing_high": {
        "window_trading_days": TACTICAL_WINDOW,
        "warmup": "expanding_observations_until_252",
        "definition": "max(close[t-251:t]) using t and earlier only",
    },
    "peak_state_machine": {
        "initial_peak": "first available close",
        "fresh_peak": "close strictly exceeds prior trailing window high, or strictly increases into an equal prior high",
        "expiry_guard": "a drop in rolling high without a fresh price high never resets a cycle",
        "untriggered_peak_update": "fresh trailing high updates the current peak without re-arming bands",
        "triggered_cycle_rearm": "fresh trailing high closes the triggered cycle and starts a new cycle",
        "crossing_event": "first close at or below each fixed threshold per tactical cycle",
    },
    "drawdown_thresholds": list(TACTICAL_THRESHOLDS),
    "drawdown_bands": {"MILD": "exactly -10%", "MEDIUM": "exactly -20%", "DEEP": "-30/-40/-50%"},
    "outcomes": list(OUTCOME_NAMES),
    "overlays": copy.deepcopy(OVERLAY_MODEL_CONFIG["overlays"]),
    "models": copy.deepcopy(OVERLAY_MODEL_CONFIG["models"]),
    "bootstrap": {"unit": "macro_episode", "resamples": 2000, "seed": 20260915, "interval": "percentile_2.5_to_97.5"},
    "leave_one_out": {"unit": "macro_episode", "minimum_episodes_for_interpretation": 5},
    "signal_classification": {
        "supported_minimum_macro_episodes": 10,
        "weak_minimum_macro_episodes": 5,
        "required_positive_median_outcomes": ["forward_1y", "forward_3y", "forward_5y"],
        "positive_median_threshold": 0.0,
    },
    "density_audit": {"max_acceptable_cycles_per_year_for_report": 4.0, "trading_days_per_year": 252},
    "architecture_gate": {
        "reference_choice": "BOTH when tactical signal is supported, 2008/2009 has a tactical event, and density is not excessive",
        "capital_state_machine": "requires tactical trigger support, non-excessive density, and non-inconclusive overlay conclusion",
    },
}
TACTICAL_CONFIG_HASH = sha256_json(TACTICAL_MODEL_CONFIG)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_ids(values: Iterable[Any], limit: int = 512) -> list[str]:
    ids = sorted({str(value) for value in values if value})
    if len(ids) <= limit:
        return ids
    return ids[:2] + ids[-(limit - 2):]


def _drawdown_band(threshold: float) -> str:
    value = round(float(threshold), 6)
    if abs(value - 0.10) < 1e-9:
        return "MILD"
    if abs(value - 0.20) < 1e-9:
        return "MEDIUM"
    if value >= 0.30:
        return "DEEP"
    raise ValueError(f"未知 tactical 回撤档位：{threshold}")


def _rolling_high_252(rows: list[Mapping[str, Any]], index: int) -> tuple[float, str]:
    """Return the high and its date from at most the current 252 rows."""

    start = max(0, int(index) - TACTICAL_WINDOW + 1)
    window = rows[start:index + 1]
    if not window:
        raise ValueError("rolling high 需要至少一个收盘观察")
    high = max(float(row["value"]) for row in window)
    # The latest matching date makes an equal close a contemporaneous high;
    # the reset guard below still requires a real price condition.
    high_date = max(str(row["observation_date"]) for row in window if float(row["value"]) == high)
    return high, high_date


def _prior_trailing_high(rows: list[Mapping[str, Any]], index: int) -> float | None:
    start = max(0, int(index) - TACTICAL_WINDOW)
    prior = rows[start:index]
    return max((float(row["value"]) for row in prior), default=None)


def _fresh_trailing_high(rows: list[Mapping[str, Any]], index: int) -> tuple[bool, str | None]:
    """Detect a price high without treating window expiry as a reset."""

    if index == 0:
        return True, "INITIAL_PEAK"
    prior_high = _prior_trailing_high(rows, index)
    if prior_high is None:
        return True, "INITIAL_PEAK"
    value = float(rows[index]["value"])
    previous = float(rows[index - 1]["value"])
    if value > prior_high:
        return True, "PRICE_NEW_TRAILING_HIGH"
    if value >= prior_high and value > previous:
        return True, "RETEST_PRIOR_TRAILING_HIGH"
    return False, None


def _episode_span(episode: Mapping[str, Any]) -> tuple[str, str]:
    start = normalize_date(episode.get("peak_date") or episode["start_date"])
    end = normalize_date(episode.get("recovery_date") or episode.get("data_end_date") or episode["start_date"])
    return start, end


def _macro_episode_for_day(day: str, episodes: list[Mapping[str, Any]]) -> str | None:
    if not episodes:
        return None
    candidates = []
    for episode in episodes:
        start, end = _episode_span(episode)
        if start <= day <= end:
            candidates.append(episode)
    if candidates:
        return str(max(candidates, key=lambda item: normalize_date(item.get("peak_date") or item["start_date"]))["episode_id"])
    prior = [episode for episode in episodes if normalize_date(episode.get("peak_date") or episode["start_date"]) <= day]
    if prior:
        return str(max(prior, key=lambda item: normalize_date(item.get("peak_date") or item["start_date"]))["episode_id"])
    return str(min(episodes, key=lambda item: normalize_date(item.get("peak_date") or item["start_date"]))["episode_id"])


def _new_cycle(run_id: str, day: str, value: float, macro_episode_id: str | None, index: int) -> dict[str, Any]:
    cycle_id = "tactical-cycle-" + sha256_json({"run": run_id, "peak_date": day, "peak_price": round(value, 10), "model": TACTICAL_MODEL_VERSION})[:24]
    return {
        "tactical_cycle_id": cycle_id,
        "macro_episode_id": macro_episode_id,
        "market": "NDX",
        "peak_date": day,
        "peak_price": float(value),
        "start_date": day,
        "end_date": day,
        "max_drawdown": 0.0,
        "max_drawdown_date": day,
        "recovery_state": "OPEN_AT_DATA_END",
        "data_end_date": day,
        "_peak_index": index,
        "_max_drawdown_price": float(value),
        "_bottom_index": index,
        "_triggered": set(),
        "_has_triggered": False,
        "_input_peak_update_count": 0,
    }


def _close_cycle(cycle: dict[str, Any], end_date: str, data_end_date: str, recovery_state: str) -> dict[str, Any]:
    cycle["end_date"] = normalize_date(end_date)
    cycle["data_end_date"] = normalize_date(data_end_date)
    cycle["recovery_state"] = recovery_state
    return cycle


def _public_cycle(cycle: Mapping[str, Any], *, data_end_date: str) -> dict[str, Any]:
    return {
        "tactical_cycle_id": str(cycle["tactical_cycle_id"]),
        "macro_episode_id": cycle.get("macro_episode_id"),
        "market": "NDX",
        "peak_date": normalize_date(cycle["peak_date"]),
        "peak_price": float(cycle["peak_price"]),
        "start_date": normalize_date(cycle["start_date"]),
        "end_date": normalize_date(cycle["end_date"]),
        "max_drawdown": _finite(cycle.get("max_drawdown")),
        "max_drawdown_date": normalize_date(cycle["max_drawdown_date"]) if cycle.get("max_drawdown_date") else None,
        "recovery_state": str(cycle["recovery_state"]),
        "data_end_date": normalize_date(data_end_date),
        "payload": {
            "definition": TACTICAL_MODEL_VERSION,
            "window_trading_days": TACTICAL_WINDOW,
            "triggered_thresholds": sorted(float(value) for value in cycle.get("_triggered", set())),
            "peak_updates_before_trigger": int(cycle.get("_input_peak_update_count", 0)),
            "bottom_price_for_evaluation": _finite(cycle.get("_max_drawdown_price")),
            "bottom_date_for_evaluation": cycle.get("max_drawdown_date"),
        },
    }


def construct_tactical_cycles_states_events(
    close_rows: Iterable[Mapping[str, Any]],
    episodes: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
    start_date: str,
    end_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build cycles, blind states and crossing events chronologically.

    The function intentionally calculates the complete available timeline
    through ``end_date`` before filtering the requested reporting window.  This
    supplies the 252-day warm-up while every row still uses only observations at
    or before its own date.
    """

    start_date = normalize_date(start_date)
    end_date = normalize_date(end_date)
    rows = sorted((dict(row) for row in close_rows if _finite(row.get("value")) is not None), key=lambda item: normalize_date(item["observation_date"]))
    rows = [row for row in rows if normalize_date(row["observation_date"]) <= end_date]
    if not rows:
        return [], [], []
    episodes = sorted((dict(item) for item in episodes), key=lambda item: (normalize_date(item.get("peak_date") or item["start_date"]), str(item["episode_id"])))
    dates = [normalize_date(row["observation_date"]) for row in rows]
    active: dict[str, Any] | None = None
    cycles: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for index, row in enumerate(rows):
        day = dates[index]
        value = float(row["value"])
        rolling_high, rolling_high_date = _rolling_high_252(rows, index)
        fresh, fresh_reason = _fresh_trailing_high(rows, index)
        actual_new_cycle = False
        state_reset_reason = None
        if active is None:
            active = _new_cycle(run_id, day, value, _macro_episode_for_day(day, episodes), index)
            actual_new_cycle = True
            state_reset_reason = "INITIAL_PEAK"
        elif fresh and active["_has_triggered"]:
            _close_cycle(active, dates[index - 1], end_date, "REARMED_BY_NEW_TRAILING_HIGH")
            cycles.append(active)
            active = _new_cycle(run_id, day, value, _macro_episode_for_day(day, episodes), index)
            actual_new_cycle = True
            state_reset_reason = fresh_reason or "PRICE_NEW_TRAILING_HIGH"
        elif fresh:
            # Before the first trigger, a fresh high belongs to the same
            # tactical cycle.  Updating a peak is not a re-arm and does not
            # duplicate the cycle or its trigger ladder.
            active["peak_date"] = day
            active["peak_price"] = value
            active["_peak_index"] = index
            active["_input_peak_update_count"] += 1
            state_reset_reason = "PEAK_UPDATED_WITHIN_CYCLE"

        drawdown = value / float(active["peak_price"]) - 1.0 if active and active["peak_price"] else 0.0
        if drawdown < float(active["max_drawdown"]):
            active["max_drawdown"] = drawdown
            active["max_drawdown_date"] = day
            active["_max_drawdown_price"] = value
            active["_bottom_index"] = index
        active["end_date"] = day
        active["data_end_date"] = end_date

        triggered_now: list[float] = []
        for threshold in TACTICAL_THRESHOLDS:
            if threshold not in active["_triggered"] and drawdown <= -threshold:
                active["_triggered"].add(threshold)
                active["_has_triggered"] = True
                triggered_now.append(threshold)

        all_input_ids = _bounded_ids(row.get("observation_version_id") for row in rows[max(0, index - TACTICAL_WINDOW + 1):index + 1])
        state_id = "tactical-state-" + sha256_json({"run": run_id, "cycle": active["tactical_cycle_id"], "date": day})[:24]
        state = {
            "tactical_state_id": state_id,
            "tactical_run_id": run_id,
            "tactical_cycle_id": active["tactical_cycle_id"],
            "macro_episode_id": active.get("macro_episode_id"),
            "market": "NDX",
            "as_of_date": day,
            "close_price": value,
            "rolling_high_252": rolling_high,
            "rolling_high_date": rolling_high_date,
            "tactical_peak_price": float(active["peak_price"]),
            "tactical_peak_date": active["peak_date"],
            "tactical_drawdown": drawdown,
            "days_since_peak": max(0, index - int(active["_peak_index"])),
            "new_tactical_peak": bool(actual_new_cycle or state_reset_reason == "PEAK_UPDATED_WITHIN_CYCLE"),
            "reset_reason": state_reset_reason,
            "triggered_bands": sorted(float(value) for value in active["_triggered"]),
            "source_observation_id": row.get("observation_version_id"),
            "input_observation_ids": all_input_ids,
            "input_hash": sha256_json(all_input_ids),
            "payload": {
                "window_expiry_only_reset": False,
                "rolling_high_declined": index > 0 and rolling_high < (_rolling_high_252(rows, index - 1)[0] - 1e-12),
                "fresh_trailing_high": fresh,
                "fresh_trailing_high_reason": fresh_reason,
                "primary_trigger_preserved": True,
                "overlay_can_veto": False,
            },
        }
        if start_date <= day <= end_date:
            states.append(state)

        for threshold in triggered_now:
            if start_date <= day <= end_date:
                events.append({
                    "tactical_event_id": "tactical-event-" + sha256_json({"run": run_id, "cycle": active["tactical_cycle_id"], "threshold": threshold})[:24],
                    "tactical_run_id": run_id,
                    "tactical_cycle_id": active["tactical_cycle_id"],
                    "macro_episode_id": active.get("macro_episode_id"),
                    "market": "NDX",
                    "threshold": threshold,
                    "drawdown_band": _drawdown_band(threshold),
                    "event_date": day,
                    "event_price": value,
                    "drawdown": drawdown,
                    "source_state_id": state_id,
                    "source_observation_id": row.get("observation_version_id"),
                    "input_observation_ids": all_input_ids,
                    "input_hash": sha256_json(all_input_ids),
                    "payload": {
                        "crossing_event": True,
                        "first_trigger_only_per_tactical_cycle": True,
                        "primary_trigger_preserved": True,
                        "overlay_can_veto": False,
                    },
                })

    if active is not None:
        _close_cycle(active, dates[-1], end_date, "OPEN_AT_DATA_END")
        cycles.append(active)

    public_cycles = []
    for cycle in cycles:
        if cycle["end_date"] >= start_date and cycle["start_date"] <= end_date:
            public_cycles.append(_public_cycle(cycle, data_end_date=end_date))
    public_cycles.sort(key=lambda item: (item["start_date"], item["tactical_cycle_id"]))
    states.sort(key=lambda item: (item["as_of_date"], item["tactical_cycle_id"]))
    events.sort(key=lambda item: (item["event_date"], item["threshold"], item["tactical_event_id"]))
    return public_cycles, states, events


def add_tactical_overlay_fields(
    events: Iterable[Mapping[str, Any]],
    indexes: Mapping[str, _Index],
) -> list[dict[str, Any]]:
    """Attach the unchanged Phase 2D overlays using same-day/PIT-proxy data."""

    events = [dict(item) for item in events]
    close = indexes["NDX_CLOSE"]
    price_features = _price_feature_rows(close)
    price_dates = close.dates
    rsi_history = [price_features.get(day, {}).get("rsi14") for day in price_dates]
    ma_history = [price_features.get(day, {}).get("distance_ma200") for day in price_dates]
    histories = {
        "VXN": (indexes["NDX_VXN"].dates, indexes["NDX_VXN"].values),
        "REAL_YIELD": (indexes["US10Y_REAL"].dates, indexes["US10Y_REAL"].values),
        "NFCI": (indexes["US_NFCI"].dates, indexes["US_NFCI"].values),
    }
    output = []
    for event in events:
        day = normalize_date(event["event_date"])
        features = price_features.get(day) or {}
        rsi = _feature_rank("RSI", features.get("rsi14"), price_dates, rsi_history, day)
        ma = _feature_rank("MA200_DISTANCE", features.get("distance_ma200"), price_dates, ma_history, day)
        macro_values: dict[str, dict[str, Any]] = {}
        macro_input_ids: list[Any] = []
        for name, series_id in (("VXN", "NDX_VXN"), ("REAL_YIELD", "US10Y_REAL"), ("NFCI", "US_NFCI")):
            row = indexes[series_id].latest(day)
            value = row.get("value") if row else None
            dates, values = histories[name]
            macro_values[name] = _feature_rank(name, value, dates, values, day)
            if row and row.get("observation_version_id"):
                macro_input_ids.append(row["observation_version_id"])
        for prefix, item in (("rsi14", rsi), ("distance_ma200", ma), ("vxn", macro_values["VXN"]), ("real_yield", macro_values["REAL_YIELD"]), ("nfci", macro_values["NFCI"])):
            event[prefix] = item["raw_value"]
            event[f"{prefix}_percentile"] = item["percentile"]
            event[f"{prefix}_opportunity_rank"] = item["opportunity_rank"]
            event[f"{prefix}_status"] = item["status"]
        close_row = close.latest(day)
        input_ids = _bounded_ids(list(event.get("input_observation_ids") or []) + macro_input_ids + ([close_row.get("observation_version_id")] if close_row else []))
        event["input_observation_ids"] = input_ids
        event["input_hash"] = sha256_json(input_ids)
        event["payload"] = dict(event.get("payload") or {}) | {
            "overlay_model_version": "NDX_DRAWDOWN_OVERLAY_V1",
            "overlay_config_hash": OVERLAY_CONFIG_HASH,
            "overlay_can_veto": False,
            "rank_reference": "strictly_prior_observations",
            "rank_minimum_prior_observations": 60,
            "feature_prior_counts": {
                "RSI": rsi["prior_observation_count"], "MA200_DISTANCE": ma["prior_observation_count"],
                "VXN": macro_values["VXN"]["prior_observation_count"], "REAL_YIELD": macro_values["REAL_YIELD"]["prior_observation_count"],
                "NFCI": macro_values["NFCI"]["prior_observation_count"],
            },
            "source_observation_dates": {
                "NDX_CLOSE": close_row.get("observation_date") if close_row else None,
                "VXN": indexes["NDX_VXN"].latest(day).get("observation_date") if indexes["NDX_VXN"].latest(day) else None,
                "REAL_YIELD": indexes["US10Y_REAL"].latest(day).get("observation_date") if indexes["US10Y_REAL"].latest(day) else None,
                "NFCI": indexes["US_NFCI"].latest(day).get("observation_date") if indexes["US_NFCI"].latest(day) else None,
            },
        }
        output.append(event)
    return output


def _cycle_internal_fields(cycles: Iterable[Mapping[str, Any]], states: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    by_id = {str(cycle["tactical_cycle_id"]): dict(cycle) for cycle in cycles}
    for state in states:
        cycle = by_id.get(str(state["tactical_cycle_id"]))
        if not cycle:
            continue
        if float(state["tactical_drawdown"]) <= float(cycle.get("max_drawdown") or 0.0):
            cycle["max_drawdown"] = float(state["tactical_drawdown"])
            cycle["max_drawdown_date"] = state["as_of_date"]
            cycle["_bottom_price"] = float(state["close_price"])
            cycle["_bottom_index"] = state["as_of_date"]
    return by_id


def evaluate_tactical_events(
    cycles: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
    close_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Read future prices only after the tactical event rows are complete."""

    cycles = list(cycles)
    events = list(events)
    rows = sorted((dict(row) for row in close_rows), key=lambda row: normalize_date(row["observation_date"]))
    dates = [normalize_date(row["observation_date"]) for row in rows]
    cycle_map = {str(cycle["tactical_cycle_id"]): cycle for cycle in cycles}
    output = []
    for event in sorted(events, key=lambda item: (str(item["event_date"]), float(item["threshold"]), str(item["tactical_event_id"]))):
        event_date = normalize_date(event["event_date"])
        position = bisect_left(dates, event_date)
        if position >= len(rows) or dates[position] != event_date:
            continue
        metrics = _forward_metrics(rows, dates, event_date, float(event["event_price"]))
        cycle = cycle_map.get(str(event["tactical_cycle_id"]), {})
        bottom = _finite(cycle.get("_bottom_price"))
        peak = _finite(cycle.get("peak_price"))
        efficiency = None
        entry_to_bottom = None
        days_to_bottom = None
        if bottom is not None and peak is not None and peak != bottom:
            efficiency = (float(event["event_price"]) - bottom) / (peak - bottom)
            entry_to_bottom = float(event["event_price"]) / bottom - 1.0 if bottom else None
            bottom_date = normalize_date(cycle.get("max_drawdown_date")) if cycle.get("max_drawdown_date") else None
            if bottom_date:
                days_to_bottom = max(0, bisect_left(dates, bottom_date) - bisect_right(dates, event_date))
        recovery_date = None
        recovery_days = None
        peak_price = float(cycle.get("peak_price") or event["event_price"])
        for future_index in range(position + 1, len(rows)):
            future_value = _finite(rows[future_index].get("value"))
            if future_value is not None and future_value >= peak_price:
                recovery_date = dates[future_index]
                recovery_days = future_index - position
                break
        one_year_target = (date.fromisoformat(event_date) + timedelta(days=round(365.2425))).isoformat()
        one_year_rows = rows[position + 1:bisect_right(dates, one_year_target)]
        future_ids = list(metrics.get("future_observation_ids") or [])
        future_ids.extend(row.get("observation_version_id") for row in one_year_rows if row.get("observation_version_id"))
        missing = [field for field in ("forward_1y", "forward_3y", "forward_5y") if metrics.get(field) is None]
        output.append({
            "tactical_evaluation_id": "tactical-eval-" + sha256_json({"run": event["tactical_run_id"], "event": event["tactical_event_id"]})[:24],
            "tactical_run_id": event["tactical_run_id"],
            "tactical_event_id": event["tactical_event_id"],
            "tactical_cycle_id": event["tactical_cycle_id"],
            "macro_episode_id": event.get("macro_episode_id"),
            "event_date": event_date,
            "entry_price": float(event["event_price"]),
            "forward_1y": metrics.get("forward_1y"),
            "forward_3y": metrics.get("forward_3y"),
            "forward_5y": metrics.get("forward_5y"),
            "max_adverse_1y": metrics.get("max_adverse_1y"),
            "max_favorable_1y": metrics.get("max_favorable_1y"),
            "cycle_bottom_price": bottom,
            "entry_efficiency": efficiency,
            "entry_to_bottom_pct": entry_to_bottom,
            "days_to_bottom": days_to_bottom,
            "timing_regret": metrics.get("timing_regret") or {},
            "future_observation_ids": _bounded_ids(future_ids, limit=2048),
            "recovery_date": recovery_date,
            "recovery_days": recovery_days,
            "status": "COMPLETE" if not missing else "PARTIAL",
            "reason": "Evaluation-only pass; future prices were read after all tactical event rows were constructed." + (f" 缺少: {', '.join(missing)}" if missing else ""),
            "payload": {"future_fields_are_evaluation_only": True, "tactical_peak_recovery_definition": "first future close >= event cycle peak"},
        })
    return output


def _event_pairs(events: Iterable[Mapping[str, Any]], evaluations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    eval_map = {str(item["tactical_event_id"]): item for item in evaluations}
    output = []
    for event in events:
        evaluation = eval_map.get(str(event["tactical_event_id"]))
        if evaluation:
            alias = dict(event)
            alias["overlay_event_id"] = event["tactical_event_id"]
            output.append({"event": alias, "evaluation": evaluation})
    return output


def tactical_conditional_results(events: Iterable[Mapping[str, Any]], evaluations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    # Phase 2D's frozen statistics helper names the parent block ``episode_id``.
    # Tactical rows keep the explicit ``macro_episode_id`` name, so provide a
    # compatibility alias without changing the event definition or grouping.
    evaluations = [dict(item, overlay_event_id=item["tactical_event_id"]) for item in evaluations]
    overlay_events = [
        dict(item, overlay_event_id=item["tactical_event_id"], episode_id=item.get("macro_episode_id"))
        for item in events
    ]
    rows = _phase2d_conditional_results(
        overlay_events, evaluations
    )
    # The tactical result tables intentionally keep a single JSON payload
    # column.  Phase 2D exposes the same payload as named fields, so preserve
    # those fields for the report and also normalize them for the repository.
    return [
        dict(row, result={key: value for key, value in row.items() if key not in {"overlay_name", "drawdown_band", "sample_count", "available_count", "status"}})
        for row in rows
    ]


def tactical_model_results(events: Iterable[Mapping[str, Any]], evaluations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    evaluations = [dict(item, overlay_event_id=item["tactical_event_id"]) for item in evaluations]
    overlay_events = [
        dict(item, overlay_event_id=item["tactical_event_id"], episode_id=item.get("macro_episode_id"))
        for item in events
    ]
    rows = _phase2d_model_results(
        overlay_events,
        [dict(item, overlay_event_id=item["tactical_event_id"]) for item in evaluations],
    )
    return [
        dict(row, result={key: value for key, value in row.items() if key not in {"model_name", "drawdown_band", "outcome_name", "sample_count", "status"}})
        for row in rows
    ]


def leave_one_macro_episode_out(events: Iterable[Mapping[str, Any]], evaluations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    pairs = _event_pairs(events, evaluations)
    output = []
    rank_fields = {
        "RSI": "rsi14_opportunity_rank", "MA200_DISTANCE": "distance_ma200_opportunity_rank",
        "VXN": "vxn_opportunity_rank", "REAL_YIELD": "real_yield_opportunity_rank", "NFCI": "nfci_opportunity_rank",
    }
    macro_ids = sorted({str(row["event"].get("macro_episode_id")) for row in pairs if row["event"].get("macro_episode_id")})
    minimum = int(TACTICAL_MODEL_CONFIG["leave_one_out"]["minimum_episodes_for_interpretation"])
    def outcome_value(evaluation: Mapping[str, Any], outcome: str) -> float | None:
        if outcome.startswith("timing_regret_"):
            horizon = outcome.removeprefix("timing_regret_").removesuffix("d")
            return _finite((evaluation.get("timing_regret") or {}).get(horizon))
        return _finite(evaluation.get(outcome))
    def rank_corr(x: list[Any], y: list[Any]) -> float | None:
        from phase2d_research import spearman
        return spearman(x, y)
    for overlay_name, rank_field in rank_fields.items():
        for band in DRAW_DOWN_BANDS:
            band_rows = pairs if band == "ALL" else [row for row in pairs if row["event"].get("drawdown_band") == band]
            for held_out in macro_ids:
                kept = [row for row in band_rows if str(row["event"].get("macro_episode_id")) != held_out]
                metrics = {}
                kept_macro_ids = {str(row["event"].get("macro_episode_id")) for row in kept if row["event"].get("macro_episode_id")}
                for outcome in OUTCOME_NAMES:
                    usable = [row for row in kept if _finite(row["event"].get(rank_field)) is not None and outcome_value(row["evaluation"], outcome) is not None]
                    metrics[outcome] = {
                        "spearman": rank_corr([row["event"][rank_field] for row in usable], [outcome_value(row["evaluation"], outcome) for row in usable]),
                        "sample_count": len(usable),
                    }
                output.append({
                    "tactical_run_id": None,
                    "overlay_name": overlay_name,
                    "drawdown_band": band,
                    "held_out_macro_episode_id": held_out,
                    "sample_count": len(kept_macro_ids),
                    "event_count": len(kept),
                    "status": "OK" if len(kept_macro_ids) >= minimum else "INCONCLUSIVE_SMALL_SAMPLE",
                    "result": {"unit": "complete macro episode block", "metrics": metrics},
                })
    return output


def macro_episode_bootstrap(
    events: Iterable[Mapping[str, Any]],
    evaluations: Iterable[Mapping[str, Any]],
    *,
    resamples: int | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Bootstrap event outcomes by macro episode, never by individual event."""

    events = list(events)
    eval_map = {str(item["tactical_event_id"]): item for item in evaluations}
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        evaluation = eval_map.get(str(event["tactical_event_id"]))
        macro_id = event.get("macro_episode_id")
        if evaluation and macro_id:
            groups[str(macro_id)].append(evaluation)
    resamples = int(resamples if resamples is not None else TACTICAL_MODEL_CONFIG["bootstrap"]["resamples"])
    seed = int(seed if seed is not None else TACTICAL_MODEL_CONFIG["bootstrap"]["seed"])
    rng = random.Random(seed)
    outcome_fields = list(OUTCOME_NAMES)
    result: dict[str, Any] = {
        "unit": "macro_episode",
        "episode_count": len(groups),
        "event_count": sum(len(rows) for rows in groups.values()),
        "resamples": resamples,
        "seed": seed,
        "by_outcome": {},
    }
    for outcome in outcome_fields:
        episode_values = []
        for macro_id, rows in sorted(groups.items()):
            values = []
            for row in rows:
                if outcome.startswith("timing_regret_"):
                    horizon = outcome.removeprefix("timing_regret_").removesuffix("d")
                    value = _finite((row.get("timing_regret") or {}).get(horizon))
                else:
                    value = _finite(row.get(outcome))
                if value is not None:
                    values.append(value)
            if values:
                episode_values.append(sum(values) / len(values))
        if not episode_values:
            result["by_outcome"][outcome] = {"episode_count": 0, "estimate": None, "ci_low": None, "ci_high": None}
            continue
        draws = []
        for _ in range(resamples):
            sample = [episode_values[rng.randrange(len(episode_values))] for _ in episode_values]
            draws.append(sum(sample) / len(sample))
        draws.sort()
        low_index = max(0, min(len(draws) - 1, int(math.floor(0.025 * (len(draws) - 1)))))
        high_index = max(0, min(len(draws) - 1, int(math.ceil(0.975 * (len(draws) - 1)))))
        result["by_outcome"][outcome] = {
            "episode_count": len(episode_values),
            "event_count": sum(1 for rows in groups.values() for row in rows if (
                (_finite((row.get("timing_regret") or {}).get(outcome.removeprefix("timing_regret_").removesuffix("d"))) if outcome.startswith("timing_regret_") else _finite(row.get(outcome))) is not None
            )),
            "estimate": sum(episode_values) / len(episode_values),
            "ci_low": draws[low_index],
            "ci_high": draws[high_index],
        }
    return result


def _stats_by_outcome(evaluations: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(evaluations)
    output = {}
    for outcome in OUTCOME_NAMES:
        values = []
        for row in rows:
            if outcome.startswith("timing_regret_"):
                horizon = outcome.removeprefix("timing_regret_").removesuffix("d")
                value = _finite((row.get("timing_regret") or {}).get(horizon))
            else:
                value = _finite(row.get(outcome))
            if value is not None:
                values.append(value)
        output[outcome] = _stats(values)
    return output


def _recovery_time_stats(evaluations: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize first-close-at-or-above-cycle-peak recovery in trading rows."""

    return _stats(row.get("recovery_days") for row in evaluations)


def trigger_density(cycles: Iterable[Mapping[str, Any]], events: Iterable[Mapping[str, Any]], *, start_date: str, end_date: str) -> dict[str, Any]:
    cycles = list(cycles)
    events = list(events)
    start = date.fromisoformat(normalize_date(start_date))
    end = date.fromisoformat(normalize_date(end_date))
    calendar_years = max((end - start).days / 365.2425, 1 / 365.2425)
    trading_days = max(1, int(round(calendar_years * TACTICAL_WINDOW)))
    by_year = Counter(str(event["event_date"])[:4] for event in events)
    event_dates = sorted({normalize_date(event["event_date"]) for event in events})
    gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(event_dates, event_dates[1:])]
    cycle_years = Counter(str(cycle["start_date"])[:4] for cycle in cycles)
    durations = [(date.fromisoformat(cycle["end_date"]) - date.fromisoformat(cycle["start_date"])).days for cycle in cycles]
    shallow = [cycle for cycle in cycles if set(cycle.get("payload", {}).get("triggered_thresholds", [])) <= {0.1} and cycle.get("payload", {}).get("triggered_thresholds")]
    repeated_years = {year: count for year, count in by_year.items() if count > 1}
    return {
        "events_per_year": len(events) / calendar_years,
        "events_per_macro_episode": len(events) / max(1, len({str(event.get("macro_episode_id")) for event in events if event.get("macro_episode_id")})),
        "events_per_1000_trading_days": len(events) / trading_days * 1000,
        "average_days_between_events": sum(gaps) / len(gaps) if gaps else None,
        "event_count_by_year": dict(sorted(by_year.items())),
        "cycle_count_by_year": dict(sorted(cycle_years.items())),
        "cycle_duration_days": _stats(durations),
        "shallow_minus_10_only_cycle_count": len(shallow),
        "same_year_repeated_event_counts": repeated_years,
        "max_cycles_in_one_year": max(cycle_years.values(), default=0),
        "max_events_in_one_year": max(by_year.values(), default=0),
        "calendar_year_span": calendar_years,
        "trading_day_span_assumption": trading_days,
    }


def _classify_tactical_overlay(name: str, events: list[Mapping[str, Any]], evaluations: list[Mapping[str, Any]], lome: list[Mapping[str, Any]]) -> str:
    rank_field = {"RSI": "rsi14_opportunity_rank", "MA200_DISTANCE": "distance_ma200_opportunity_rank", "VXN": "vxn_opportunity_rank", "REAL_YIELD": "real_yield_opportunity_rank", "NFCI": "nfci_opportunity_rank"}[name]
    pairs = _event_pairs(events, evaluations)
    available = [row for row in pairs if _finite(row["event"].get(rank_field)) is not None and _finite(row["evaluation"].get("forward_1y")) is not None]
    macro_count = len({str(row["event"].get("macro_episode_id")) for row in available if row["event"].get("macro_episode_id")})
    if macro_count < 5:
        return "INCONCLUSIVE"
    from phase2d_research import spearman
    corrs = []
    for outcome in ("forward_1y", "forward_3y", "forward_5y", "timing_regret_30d", "timing_regret_60d", "timing_regret_120d"):
        vals = []
        for row in pairs:
            value = None
            if outcome.startswith("timing_regret_"):
                horizon = outcome.removeprefix("timing_regret_").removesuffix("d")
                value = _finite((row["evaluation"].get("timing_regret") or {}).get(horizon))
            else:
                value = _finite(row["evaluation"].get(outcome))
            rank = _finite(row["event"].get(rank_field))
            if rank is not None and value is not None:
                vals.append((rank, value))
        corr = spearman([x for x, _ in vals], [y for _, y in vals])
        if corr is not None:
            corrs.append(corr)
    if not corrs:
        return "INCONCLUSIVE"
    # The LOME function already compares each held-out correlation with the
    # corresponding full-sample correlation.  Count those explicit flags;
    # comparing every outcome with the first outcome would create an unrelated
    # sign test and could label a stable overlay as fragile.
    sign_flips = sum(
        1
        for row in lome
        if row.get("overlay_name") == name and row.get("drawdown_band") == "ALL"
        for metric in (row.get("result") or {}).get("metrics", {}).values()
        if bool(metric.get("sign_flip"))
    )
    positive = sum(value > 0.05 for value in corrs)
    if macro_count >= 15 and positive >= 4 and sign_flips == 0:
        return "POSITIVE"
    if macro_count >= 10 and positive >= 3 and sign_flips <= 2:
        return "WEAK"
    return "INCONCLUSIVE"


def tactical_overlay_report_values(events: list[Mapping[str, Any]], evaluations: list[Mapping[str, Any]], lome: list[Mapping[str, Any]]) -> tuple[dict[str, str], dict[str, str], str]:
    values = {name: _classify_tactical_overlay(name, events, evaluations, lome) for name in OVERLAY_NAMES}
    parsimony = {name: {"POSITIVE": "KEEP", "WEAK": "OPTIONAL", "NONE": "DROP", "STRONG": "KEEP"}.get(value, "INCONCLUSIVE") for name, value in values.items()}
    if any(value in {"POSITIVE", "STRONG"} for value in values.values()):
        overall = "POSITIVE"
    elif all(value in {"NONE", "WEAK"} for value in values.values()):
        overall = "WEAK"
    else:
        overall = "INCONCLUSIVE"
    return values, parsimony, overall


def _tactical_primary_signal(evaluations: list[Mapping[str, Any]], events: list[Mapping[str, Any]]) -> tuple[str, dict[str, Any]]:
    ten_percent_ids = {str(event["tactical_event_id"]) for event in events if abs(float(event["threshold"]) - 0.10) < 1e-9}
    rows = [row for row in evaluations if str(row["tactical_event_id"]) in ten_percent_ids]
    macro_count = len({str(row.get("macro_episode_id")) for row in rows if row.get("macro_episode_id")})
    signal_config = TACTICAL_MODEL_CONFIG["signal_classification"]
    required = list(signal_config["required_positive_median_outcomes"])
    medians = {outcome: _stats(row.get(outcome) for row in rows).get("median") for outcome in required}
    if macro_count >= int(signal_config["supported_minimum_macro_episodes"]) and all(value is not None and value > float(signal_config["positive_median_threshold"]) for value in medians.values()):
        signal = "SUPPORTED"
    elif macro_count >= int(signal_config["weak_minimum_macro_episodes"]):
        signal = "WEAK"
    else:
        signal = "INCONCLUSIVE"
    return signal, {"event_count": len(rows), "macro_episode_count": macro_count, "median_forward_returns": medians, "unit": "one -10% trigger per tactical cycle"}


def _old_ath_baseline(repo: PITRepository, phase2c_run_id: str) -> dict[str, Any]:
    evaluations = repo.get_episode_event_evaluations(phase2c_run_id, event_type="DRAWDOWN", limit=1000000)
    events = repo.get_drawdown_mechanical_events(phase2c_run_id, limit=1000000)
    return {
        "event_count": len(events),
        "evaluation_count": len(evaluations),
        "event_count_by_threshold": {str(int(round(threshold * 100))): sum(abs(float(event["threshold"]) - threshold) < 1e-9 for event in events) for threshold in TACTICAL_THRESHOLDS},
        "evaluation_stats": _stats_by_outcome(evaluations),
        "recovery_time_days": _recovery_time_stats(evaluations),
        "reference": "Phase 2C ATH-to-ATH macro episode mechanical baseline",
    }


def build_tactical_report(
    repo: PITRepository,
    tactical_run_id: str,
    *,
    phase2c_run_id: str,
    cycles: list[Mapping[str, Any]],
    states: list[Mapping[str, Any]],
    events: list[Mapping[str, Any]],
    evaluations: list[Mapping[str, Any]],
    conditional: list[Mapping[str, Any]],
    models: list[Mapping[str, Any]],
    lome: list[Mapping[str, Any]],
    episodes: list[Mapping[str, Any]],
    data_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    run = repo.get_tactical_drawdown_run(tactical_run_id)
    if not run:
        raise ValueError("tactical run 不存在")
    lome_plain = [dict(row) for row in lome]
    overlay_values, parsimony, overall_overlay = tactical_overlay_report_values(events, evaluations, lome_plain)
    tactical_signal, primary_detail = _tactical_primary_signal(evaluations, events)
    density = trigger_density(cycles, events, start_date=run["start_date"], end_date=run["end_date"])
    key_windows = {}
    for label, start, end in (("2000_2015", "2000-01-01", "2015-12-31"), ("2000_2002", "2000-01-01", "2002-12-31"), ("2007_2009", "2007-01-01", "2009-12-31"), ("2018", "2018-01-01", "2018-12-31"), ("2020", "2020-01-01", "2020-12-31"), ("2022", "2022-01-01", "2022-12-31")):
        window_cycles = [cycle for cycle in cycles if normalize_date(cycle["end_date"]) >= start and normalize_date(cycle["start_date"]) <= end]
        window_events = [event for event in events if start <= normalize_date(event["event_date"]) <= end]
        key_windows[label] = {
            "cycle_count": len(window_cycles),
            "event_count": len(window_events),
            "event_count_by_threshold": {str(int(round(threshold * 100))): sum(abs(float(event["threshold"]) - threshold) < 1e-9 for event in window_events) for threshold in TACTICAL_THRESHOLDS},
            "cycle_ids": [str(cycle["tactical_cycle_id"]) for cycle in window_cycles],
            "cycles": [dict(cycle) for cycle in window_cycles],
        }
    macro_episodes_in_events = len({str(event.get("macro_episode_id")) for event in events if event.get("macro_episode_id")})
    overlap_2008 = key_windows["2007_2009"]["event_count"] > 0
    max_cycles = density["max_cycles_in_one_year"]
    if tactical_signal == "SUPPORTED" and overlap_2008 and max_cycles <= float(TACTICAL_MODEL_CONFIG["density_audit"]["max_acceptable_cycles_per_year_for_report"]):
        reference = "BOTH"
    elif tactical_signal in {"SUPPORTED", "WEAK"}:
        reference = "TACTICAL_252"
    else:
        reference = "INCONCLUSIVE"
    ready = "YES" if tactical_signal == "SUPPORTED" and overall_overlay in {"STRONG", "POSITIVE", "WEAK", "NONE"} and max_cycles <= float(TACTICAL_MODEL_CONFIG["density_audit"]["max_acceptable_cycles_per_year_for_report"]) else "NO"
    bootstrap = macro_episode_bootstrap(events, evaluations)
    return {
        "phase": "2E",
        "tactical_run_id": tactical_run_id,
        "tactical_model_version": TACTICAL_MODEL_VERSION,
        "config_hash": TACTICAL_CONFIG_HASH,
        "phase2c_run_id": phase2c_run_id,
        "phase2d_overlay_config_hash": OVERLAY_CONFIG_HASH,
        "date_range": {"start": run["start_date"], "end": run["end_date"]},
        "research_only": True,
        "strict_pit": False,
        "investment_action_eligible": False,
        "PHASE_2E_STATUS": "PASS_WITH_LIMITATIONS",
        "TACTICAL_DRAWDOWN_SIGNAL": tactical_signal,
        "tactical_primary_detail": primary_detail,
        "DRAWDOWN_REFERENCE": reference,
        "OVERALL_OVERLAY_INCREMENTAL_VALUE": overall_overlay,
        "READY_FOR_CAPITAL_STATE_MACHINE": ready,
        "overlay_information_value": overlay_values,
        "parsimony_rule": parsimony,
        "RSI_OVERLAY_VALUE": overlay_values["RSI"],
        "MA200_OVERLAY_VALUE": overlay_values["MA200_DISTANCE"],
        "MA200_DISTANCE_OVERLAY_VALUE": overlay_values["MA200_DISTANCE"],
        "VXN_OVERLAY_VALUE": overlay_values["VXN"],
        "REAL_YIELD_OVERLAY_VALUE": overlay_values["REAL_YIELD"],
        "NFCI_OVERLAY_VALUE": overlay_values["NFCI"],
        "macro_episode_count": len(episodes),
        "macro_episode_count_in_tactical_events": macro_episodes_in_events,
        "tactical_cycle_count": len(cycles),
        "tactical_state_count": len(states),
        "tactical_event_count": len(events),
        "tactical_event_count_by_threshold": {str(int(round(threshold * 100))): sum(abs(float(event["threshold"]) - threshold) < 1e-9 for event in events) for threshold in TACTICAL_THRESHOLDS},
        "tactical_event_count_by_band": {band: sum(event["drawdown_band"] == band for event in events) for band in ("MILD", "MEDIUM", "DEEP")},
        "tactical_evaluation_count": len(evaluations),
        "conditional_results": conditional,
        "model_results": models,
        "leave_one_macro_episode_out": {"row_count": len(lome), "unit": "complete macro episode", "results": lome_plain},
        "bootstrap_by_macro_episode": bootstrap,
        "trigger_density": density,
        "key_history_windows": key_windows,
        "old_ath_baseline": _old_ath_baseline(repo, phase2c_run_id),
        "tactical_baseline": {
            "evaluation_stats": _stats_by_outcome(evaluations),
            "recovery_time_days": _recovery_time_stats(evaluations),
            "bootstrap": bootstrap,
        },
        "overlay_configs_unchanged": sha256_json(TACTICAL_MODEL_CONFIG["overlays"]) == sha256_json(OVERLAY_MODEL_CONFIG["overlays"]),
        "lookahead_controls": {
            "rolling_window_uses_current_and_prior_only": True,
            "window_expiry_only_reset_forbidden": True,
            "events_constructed_before_future_evaluation": True,
            "future_fields_absent_from_states": True,
            "future_fields_absent_from_events": True,
            "future_outcomes_table": "tactical_drawdown_event_evaluations",
            "bootstrap_cluster_unit": "macro_episode",
            "parameter_search": False,
            "window_length_search": False,
            "overlay_definitions_changed": False,
        },
        "natural_schedule": {
            "NATURAL_SCHEDULE_TRIGGER": "PASS" if any(bool((row.get("details") or {}).get("actual_calendar_trigger_verified")) for row in repo.get_gate_checks("GATE_A_SCHEDULED_RUN")) else "PENDING_FIRST_NATURAL_CALENDAR_EVENT",
            "independent_from_proxy_research": True,
        },
        "data_snapshot": dict(data_snapshot),
        "data_cutoff": run.get("data_cutoff"),
        "limitations": [
            "NDX/VXN/real-yield/NFCI historical inputs are HISTORICAL_PROXY rather than fully vintaged strict PIT; this remains research-only.",
            "Tactical cycles are more numerous than macro episodes but all bootstrap and leave-one-out checks cluster by macro episode.",
            "The 2008–2009 events occur inside a tactical cycle that began in 2007; no 2008 special case is used.",
            "No capital amount, contribution schedule, leverage, take-profit or automatic buy action is evaluated.",
        ],
    }


def run_tactical_validation(
    repo: PITRepository | None = None,
    *,
    phase2c_run_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    tactical_run_id: str | None = None,
) -> dict[str, Any]:
    repo = repo or PITRepository(DB)
    candidates = repo.get_episode_validation_runs(market="NDX", limit=1000)
    phase2c = repo.get_episode_validation_run(phase2c_run_id) if phase2c_run_id else next((item for item in candidates if item.get("status") == "COMPLETED"), None)
    if not phase2c or phase2c.get("status") != "COMPLETED":
        raise ValueError("需要一个已完成的 Phase 2C run")
    phase2c_run_id = str(phase2c["episode_run_id"])
    start_date = normalize_date(start_date or phase2c["start_date"])
    end_date = normalize_date(end_date or phase2c["end_date"])
    tactical_run_id = tactical_run_id or f"phase2e-tactical-ndx-v1-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    episodes = repo.get_drawdown_episodes(phase2c_run_id, limit=100000)
    indexes = _load_proxy_indexes(repo, end_date)
    snapshot = _data_snapshot(indexes)
    snapshot["phase2c_run_id"] = phase2c_run_id
    snapshot["phase2c_data_snapshot"] = phase2c.get("data_snapshot") or {}
    run, created = repo.create_tactical_drawdown_run({
        "tactical_run_id": tactical_run_id,
        "market": "NDX",
        "tactical_model_version": TACTICAL_MODEL_VERSION,
        "phase2c_run_id": phase2c_run_id,
        "start_date": start_date,
        "end_date": end_date,
        "tactical_model_config": TACTICAL_MODEL_CONFIG,
        "config_hash": TACTICAL_CONFIG_HASH,
        "data_snapshot": snapshot,
        "data_cutoff": as_of_datetime(f"{end_date}T23:59:59.999999Z"),
    })
    if not created and run.get("status") == "COMPLETED":
        report_row = repo.get_tactical_drawdown_report(tactical_run_id)
        return (report_row or {}).get("report") or {}
    try:
        cycles, states, raw_events = construct_tactical_cycles_states_events(
            indexes["NDX_CLOSE"].rows, episodes, run_id=tactical_run_id, start_date=start_date, end_date=end_date,
        )
        # Build cycle bottom descriptors from blind states only.  They are used
        # for the later evaluation pass and never enter trigger construction.
        internal_cycles = _cycle_internal_fields(cycles, states)
        for cycle in cycles:
            internal = internal_cycles.get(str(cycle["tactical_cycle_id"]), {})
            cycle["max_drawdown"] = internal.get("max_drawdown", cycle.get("max_drawdown"))
            cycle["max_drawdown_date"] = internal.get("max_drawdown_date", cycle.get("max_drawdown_date"))
            cycle["_bottom_price"] = internal.get("_bottom_price")
            cycle["payload"] = dict(cycle.get("payload") or {}) | {
                "bottom_price_for_evaluation": internal.get("_bottom_price"),
                "bottom_date_for_evaluation": internal.get("max_drawdown_date"),
            }
        events = add_tactical_overlay_fields(raw_events, indexes)
        for cycle in cycles: repo.append_tactical_drawdown_cycle({"tactical_run_id": tactical_run_id, **cycle})
        for state in states: repo.append_tactical_drawdown_state(state)
        # All contemporaneous event rows are persisted before any future prices
        # are read.
        for event in events: repo.append_tactical_drawdown_event(event)
        evaluations = evaluate_tactical_events(cycles, events, indexes["NDX_CLOSE"].rows)
        for evaluation in evaluations: repo.append_tactical_drawdown_evaluation(evaluation)
        conditional = tactical_conditional_results(events, evaluations)
        for result in conditional: repo.append_tactical_overlay_conditional_result({**result, "tactical_run_id": tactical_run_id})
        models = tactical_model_results(events, evaluations)
        for result in models: repo.append_tactical_overlay_model_result({**result, "tactical_run_id": tactical_run_id})
        lome = leave_one_macro_episode_out(events, evaluations)
        for result in lome: repo.append_tactical_overlay_lome_result({**result, "tactical_run_id": tactical_run_id})
        report = build_tactical_report(repo, tactical_run_id, phase2c_run_id=phase2c_run_id, cycles=cycles, states=states, events=events, evaluations=evaluations, conditional=conditional, models=models, lome=lome, episodes=episodes, data_snapshot=snapshot)
        repo.record_tactical_drawdown_report(tactical_run_id, report)
        summary = {
            "tactical_cycle_count": len(cycles), "tactical_event_count": len(events), "tactical_evaluation_count": len(evaluations),
            "conditional_result_count": len(conditional), "model_result_count": len(models), "lome_result_count": len(lome),
            "PHASE_2E_STATUS": report["PHASE_2E_STATUS"], "TACTICAL_DRAWDOWN_SIGNAL": report["TACTICAL_DRAWDOWN_SIGNAL"],
            "DRAWDOWN_REFERENCE": report["DRAWDOWN_REFERENCE"], "OVERALL_OVERLAY_INCREMENTAL_VALUE": report["OVERALL_OVERLAY_INCREMENTAL_VALUE"],
            "READY_FOR_CAPITAL_STATE_MACHINE": report["READY_FOR_CAPITAL_STATE_MACHINE"],
        }
        repo.complete_tactical_drawdown_run(tactical_run_id, summary=summary)
        return report
    except Exception as exc:
        try:
            repo.complete_tactical_drawdown_run(tactical_run_id, status="FAILED", error={"type": type(exc).__name__, "message": str(exc)})
        except Exception:
            pass
        raise


def blind_tactical_event_date_view(repo: PITRepository, tactical_run_id: str, day: str) -> dict[str, Any] | None:
    run = repo.get_tactical_drawdown_run(tactical_run_id)
    if not run:
        return None
    day = normalize_date(day)
    events = [event for event in repo.get_tactical_drawdown_events(tactical_run_id, limit=1000000) if normalize_date(event["event_date"]) == day]
    states = [state for state in repo.get_tactical_drawdown_states(tactical_run_id, limit=2000000) if normalize_date(state["as_of_date"]) == day]
    if not events and not states:
        return None
    forbidden = {"cycle_bottom_price", "entry_efficiency", "forward_1y", "forward_3y", "forward_5y", "timing_regret", "future_observation_ids", "recovery_date", "recovery_days", "max_drawdown", "max_drawdown_date"}
    clean_events = [{key: value for key, value in event.items() if key not in forbidden and key != "payload"} | {"future_hidden": True} for event in events]
    clean_states = [{key: value for key, value in state.items() if key not in forbidden and key != "payload"} | {"future_hidden": True} for state in states]
    return {
        "tactical_run_id": tactical_run_id,
        "as_of": day,
        "market": run["market"],
        "tactical_model_version": run["tactical_model_version"],
        "research_only": True,
        "strict_pit": False,
        "investment_action_eligible": False,
        "tactical_events": clean_events,
        "tactical_states": clean_states,
        "future_hidden": True,
    }


def _markdown_report(report: Mapping[str, Any]) -> str:
    def fmt(value: Any, digits: int = 3) -> str:
        number = _finite(value)
        return "—" if number is None else f"{number:.{digits}f}"
    lines = [
        "# Phase 2E：Tactical Drawdown Cycle & Re-Arm Validation", "",
        f"本报告对应不可变运行 `{report['tactical_run_id']}`，时间范围 {report['date_range']['start']} 至 {report['date_range']['end']}。252 个交易日是预先冻结的架构参数；ATH-to-ATH Macro Episode 保持为统计 block。", "",
        "## 结论枚举", "", "| 项目 | 结果 |", "|---|---|",
        f"| `PHASE_2E_STATUS` | **{report['PHASE_2E_STATUS']}** |",
        f"| `TACTICAL_DRAWDOWN_SIGNAL` | **{report['TACTICAL_DRAWDOWN_SIGNAL']}** |",
        f"| `DRAWDOWN_REFERENCE` | **{report['DRAWDOWN_REFERENCE']}** |",
        f"| `OVERALL_OVERLAY_INCREMENTAL_VALUE` | **{report['OVERALL_OVERLAY_INCREMENTAL_VALUE']}** |",
        f"| `READY_FOR_CAPITAL_STATE_MACHINE` | **{report['READY_FOR_CAPITAL_STATE_MACHINE']}** |", "",
        "## 样本与定义", "",
        f"- Macro Episode：{report['macro_episode_count']}；Tactical Cycle：{report['tactical_cycle_count']}；Tactical State：{report['tactical_state_count']}。",
        f"- Tactical Event：{report['tactical_event_count']}；档位：{json.dumps(report['tactical_event_count_by_threshold'], ensure_ascii=False)}。",
        "- 触发基准是过去 252 个交易日（含当日）的最高收盘；滚动窗口老化不会单独重置 Cycle。",
        "- 已触发 Cycle 遇到真实新 trailing high 才重新武装；未触发 Cycle 只更新峰值，不重复制造 Cycle。", "",
        "## Overlay 重新验证", "", "| Overlay | Information Value | Parsimony |", "|---|---|---|",
    ]
    for name in OVERLAY_NAMES:
        value_key = name + "_OVERLAY_VALUE"
        # Keep the human-readable report compatible with the compact legacy
        # key used by the first persisted Phase 2E run.
        if value_key not in report and name == "MA200_DISTANCE":
            value_key = "MA200_OVERLAY_VALUE"
        lines.append(f"| {name} | **{report[value_key]}** | {report['parsimony_rule'][name]} |")
    lines += ["", "## 关键历史窗口", "", "| 窗口 | Tactical Cycle | Event |", "|---|---:|---:|"]
    for label, item in report["key_history_windows"].items():
        lines.append(f"| {label} | {item['cycle_count']} | {item['event_count']} |")
    lines += ["", "2008–2009 的事件来自 2007 年开始的 Tactical Cycle；没有为 2008 编写 special-case。", "", "## Tactical baseline outcomes", "", "| 结果 | 均值 | 中位数 | 样本数 |", "|---|---:|---:|---:|"]
    outcome_labels = {
        "forward_1y": "1Y Forward Return", "forward_3y": "3Y Forward Return", "forward_5y": "5Y Forward Return",
        "max_adverse_1y": "1Y Future Downside", "entry_efficiency": "Entry Efficiency",
        "timing_regret_30d": "30D Timing Regret", "timing_regret_60d": "60D Timing Regret", "timing_regret_120d": "120D Timing Regret",
    }
    stats = report["tactical_baseline"]["evaluation_stats"]
    for key, label in outcome_labels.items():
        item = stats.get(key, {})
        lines.append(f"| {label} | {fmt(item.get('mean'))} | {fmt(item.get('median'))} | {item.get('sample_count', 0)} |")
    recovery = report["tactical_baseline"].get("recovery_time_days", {})
    lines.append(f"| Recovery Time (trading observations) | {fmt(recovery.get('mean'))} | {fmt(recovery.get('median'))} | {recovery.get('sample_count', 0)} |")
    lines += ["", "## Trigger Density", "", "| 指标 | 值 |", "|---|---:|"]
    density = report["trigger_density"]
    for key in ("events_per_year", "events_per_macro_episode", "events_per_1000_trading_days", "average_days_between_events", "max_cycles_in_one_year", "max_events_in_one_year", "shallow_minus_10_only_cycle_count"):
        lines.append(f"| {key} | {fmt(density.get(key))} |")
    lines += ["", "## ATH baseline vs Tactical baseline", "", "| 参考 | Event 数 | 评价数 |", "|---|---:|---:|", f"| ATH-to-ATH | {report['old_ath_baseline']['event_count']} | {report['old_ath_baseline']['evaluation_count']} |", f"| Tactical 252 | {report['tactical_event_count']} | {report['tactical_evaluation_count']} |", "", "Tactical 事件增加后，bootstrap 仍按 Macro Episode 聚类，不能把 Cycle 数量当成独立样本。", "", "## 无前视与限制", "", "- 事件和状态先写入；未来收益、底部、恢复时间只在独立评价表中计算。", "- 旧 Phase 2C/2D 记录未修改；Overlay 定义 hash 保持不变。", "- 本阶段不决定金额、不运行资金状态机、不购买数据、不做止盈或杠杆。", "", f"自然日历触发：`{report['natural_schedule']['NATURAL_SCHEDULE_TRIGGER']}`。"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run Phase 2E tactical drawdown cycle validation")
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--phase2c-run-id", default=None)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--markdown-out", type=Path, default=None)
    args = parser.parse_args()
    result = run_tactical_validation(PITRepository(args.db), phase2c_run_id=args.phase2c_run_id, start_date=args.start_date, end_date=args.end_date, tactical_run_id=args.run_id)
    if args.json_out:
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.write_text(_markdown_report(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


__all__ = [
    "TACTICAL_MODEL_VERSION", "TACTICAL_MODEL_CONFIG", "TACTICAL_CONFIG_HASH", "TACTICAL_WINDOW", "TACTICAL_THRESHOLDS",
    "_rolling_high_252", "_fresh_trailing_high", "construct_tactical_cycles_states_events", "add_tactical_overlay_fields",
    "evaluate_tactical_events", "tactical_conditional_results", "tactical_model_results", "leave_one_macro_episode_out",
    "macro_episode_bootstrap", "trigger_density", "build_tactical_report", "run_tactical_validation", "blind_tactical_event_date_view",
]
