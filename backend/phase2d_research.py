"""Phase 2D: drawdown-first conditional overlay validation.

Phase 2C established that the mechanical drawdown trigger is the useful
primary object, while the frozen composite proxy is not yet proven to add
stable information.  This module therefore copies the already-frozen
mechanical events into a new research run and records only contemporaneous
RSI/MA200, VXN, real-yield and NFCI information.  The overlay never removes,
blocks or relabels a drawdown event as a trading instruction.

Construction is completed before the explicit evaluation pass reads any
future prices.  Ranks are calculated from strictly earlier observations and
all conclusions remain research-only because the historical inputs are
``HISTORICAL_PROXY`` rather than fully vintaged strict PIT data.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, datetime, timezone
import json
import math
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


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"

OVERLAY_MODEL_VERSION = "NDX_DRAWDOWN_OVERLAY_V1"
OVERLAY_MODEL_CREATED_AT = "2026-09-15T00:00:00Z"
OVERLAY_NAMES = ("RSI", "MA200_DISTANCE", "VXN", "REAL_YIELD", "NFCI")
# Keep the outcome grid fixed before reading any future prices.  The 30/60/120
# trading-day regret horizons are the predeclared early-entry audit for RSI and
# MA200; they are not selected after looking at results.
OUTCOME_NAMES = (
    "forward_1y", "forward_3y", "forward_5y", "max_adverse_1y",
    "timing_regret_30d", "timing_regret_60d", "timing_regret_120d",
    "entry_efficiency",
)
DRAW_DOWN_BANDS = ("ALL", "MILD", "MEDIUM", "DEEP")

# This configuration is intentionally small and descriptive.  No value here
# is selected by searching the historical result.  The fixed tertiles only
# provide a readable SUPPORTIVE/NEUTRAL/CAUTION label; all statistical work
# uses the continuous rank stored beside it.
OVERLAY_MODEL_CONFIG: dict[str, Any] = {
    "model_version": OVERLAY_MODEL_VERSION,
    "created_at": OVERLAY_MODEL_CREATED_AT,
    "research_only": True,
    "strict_pit": False,
    "investment_action_eligible": False,
    "primary_trigger": "Phase 2C mechanical drawdown event; overlay can never veto it",
    "upstream_phase2c_model_version": "EPISODE_OPPORTUNITY_V1",
    "rank": {
        "reference": "strictly_prior_observations",
        "minimum_prior_observations": 60,
        "percentile_definition": "count(value <= current) / count(strictly_prior_values)",
        "status_tertiles": {"caution_if_opportunity_rank_at_most": 0.3333333333333333, "supportive_if_at_least": 0.6666666666666666},
    },
    "overlays": {
        "RSI": {"source": "NDX_CLOSE", "direction": "lower_RSI_is_more_supportive", "raw_field": "rsi14"},
        "MA200_DISTANCE": {"source": "NDX_CLOSE", "direction": "more_negative_distance_is_more_supportive", "raw_field": "distance_ma200"},
        "VXN": {"source": "NDX_VXN", "direction": "higher_VXN_is_more_supportive_for_long_horizon_fear_context", "raw_field": "vxn"},
        "REAL_YIELD": {"source": "US10Y_REAL", "direction": "lower_real_yield_is_more_supportive", "raw_field": "real_yield"},
        "NFCI": {"source": "US_NFCI", "direction": "lower_NFCI_is_more_supportive", "raw_field": "nfci"},
    },
    "drawdown_bands": {
        "MILD": "exactly the first -10% trigger",
        "MEDIUM": "exactly the first -20% trigger",
        "DEEP": "first -30%, -40% and -50% triggers",
    },
    "outcomes": list(OUTCOME_NAMES),
    "models": {
        "MODEL_0_DRAWDOWN_ONLY": ["drawdown_magnitude"],
        "MODEL_1_DRAWDOWN_PLUS_VXN": ["drawdown_magnitude", "vxn_opportunity_rank"],
        "MODEL_2_DRAWDOWN_PLUS_TREND": ["drawdown_magnitude", "rsi_opportunity_rank", "ma200_opportunity_rank"],
        "MODEL_3_DRAWDOWN_PLUS_MACRO": ["drawdown_magnitude", "real_yield_opportunity_rank", "nfci_opportunity_rank"],
        "MODEL_4_DRAWDOWN_PLUS_ALL": [
            "drawdown_magnitude", "rsi_opportunity_rank", "ma200_opportunity_rank",
            "vxn_opportunity_rank", "real_yield_opportunity_rank", "nfci_opportunity_rank",
        ],
    },
    "leave_one_episode_out": {"unit": "episode", "minimum_observations_for_interpretation": 5},
    "regimes": {
        "2000-2006": ["2000-01-01", "2006-12-31"],
        "2007-2013": ["2007-01-01", "2013-12-31"],
        "2014-2019": ["2014-01-01", "2019-12-31"],
        "2020-2026": ["2020-01-01", "2026-12-31"],
    },
    "crisis_windows": {
        "2000_2002": ["2000-01-01", "2002-12-31"],
        "2008_2009": ["2008-01-01", "2009-12-31"],
        "2018_q4": ["2018-10-01", "2018-12-31"],
        "2020": ["2020-01-01", "2020-12-31"],
        "2022": ["2022-01-01", "2022-12-31"],
    },
}
OVERLAY_CONFIG_HASH = sha256_json(OVERLAY_MODEL_CONFIG)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stats(values: Iterable[Any]) -> dict[str, Any]:
    data = sorted(float(value) for value in values if _finite(value) is not None)
    if not data:
        return {"mean": None, "median": None, "p25": None, "p75": None, "sample_count": 0}

    def q(position: float) -> float:
        if len(data) == 1:
            return data[0]
        point = (len(data) - 1) * position
        low, high = math.floor(point), math.ceil(point)
        if low == high:
            return data[low]
        return data[low] + (data[high] - data[low]) * (point - low)

    return {
        "mean": sum(data) / len(data),
        "median": q(0.5),
        "p25": q(0.25),
        "p75": q(0.75),
        "sample_count": len(data),
    }


def _rank(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda pair: (pair[1], pair[0]))
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        average = (cursor + 1 + end) / 2.0
        for position in range(cursor, end):
            result[indexed[position][0]] = average
        cursor = end
    return result


def spearman(x_values: Iterable[Any], y_values: Iterable[Any]) -> float | None:
    pairs = [(float(x), float(y)) for x, y in zip(x_values, y_values) if _finite(x) is not None and _finite(y) is not None]
    if len(pairs) < 3:
        return None
    x, y = _rank([pair[0] for pair in pairs]), _rank([pair[1] for pair in pairs])
    xbar, ybar = sum(x) / len(x), sum(y) / len(y)
    numerator = sum((a - xbar) * (b - ybar) for a, b in zip(x, y))
    denominator = math.sqrt(sum((a - xbar) ** 2 for a in x) * sum((b - ybar) ** 2 for b in y))
    return numerator / denominator if denominator else 0.0


def _slope(x_values: Iterable[Any], y_values: Iterable[Any]) -> float | None:
    pairs = [(float(x), float(y)) for x, y in zip(x_values, y_values) if _finite(x) is not None and _finite(y) is not None]
    if len(pairs) < 3:
        return None
    xbar = sum(x for x, _ in pairs) / len(pairs)
    ybar = sum(y for _, y in pairs) / len(pairs)
    denominator = sum((x - xbar) ** 2 for x, _ in pairs)
    return sum((x - xbar) * (y - ybar) for x, y in pairs) / denominator if denominator else 0.0


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
    raise ValueError(f"未知回撤档位：{threshold}")


def _regime_label(day: str) -> str:
    for label, (start, end) in OVERLAY_MODEL_CONFIG["crisis_windows"].items():
        # Crisis labels are retained for attribution only.  Broad regime
        # results below use the non-overlapping four fixed periods.
        if start <= day <= end:
            return label
    for label, (start, end) in OVERLAY_MODEL_CONFIG["regimes"].items():
        if start <= day <= end:
            return label
    return "OUT_OF_RANGE"


def _broad_regime_label(day: str) -> str:
    for label, (start, end) in OVERLAY_MODEL_CONFIG["regimes"].items():
        if start <= day <= end:
            return label
    return "OUT_OF_RANGE"


def _source_history(index: _Index) -> tuple[list[str], list[float]]:
    return index.dates, index.values


def _strict_prior_percentile(dates: list[str], values: list[float], day: str, value: Any) -> tuple[float | None, int]:
    current = _finite(value)
    cutoff = bisect_left(dates, str(day))
    prior = [float(item) for item in values[:cutoff] if _finite(item) is not None]
    minimum = int(OVERLAY_MODEL_CONFIG["rank"]["minimum_prior_observations"])
    if current is None or len(prior) < minimum:
        return None, len(prior)
    prior.sort()
    percentile = bisect_right(prior, current) / len(prior)
    return float(percentile), len(prior)


def _status(opportunity_rank: float | None) -> str:
    if opportunity_rank is None:
        return "UNAVAILABLE"
    if opportunity_rank >= float(OVERLAY_MODEL_CONFIG["rank"]["status_tertiles"]["supportive_if_at_least"]):
        return "SUPPORTIVE"
    if opportunity_rank <= float(OVERLAY_MODEL_CONFIG["rank"]["status_tertiles"]["caution_if_opportunity_rank_at_most"]):
        return "CAUTION"
    return "NEUTRAL"


def _feature_rank(name: str, value: Any, history_dates: list[str], history_values: list[float], day: str) -> dict[str, Any]:
    raw = _finite(value)
    percentile, prior_count = _strict_prior_percentile(history_dates, history_values, day, raw)
    if name in {"RSI", "MA200_DISTANCE", "REAL_YIELD", "NFCI"}:
        opportunity = None if percentile is None else 1.0 - percentile
    else:
        opportunity = percentile
    return {
        "raw_value": raw,
        "percentile": percentile,
        "opportunity_rank": opportunity,
        "status": _status(opportunity),
        "prior_observation_count": prior_count,
        "rank_reference": "strictly_prior_observations",
    }


def _date_positions(rows: Iterable[Mapping[str, Any]]) -> tuple[list[str], dict[str, int]]:
    dates = sorted({normalize_date(row["observation_date"]) for row in rows})
    return dates, {day: index for index, day in enumerate(dates)}


def _state_lookup(states: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {(str(row["episode_id"]), normalize_date(row["as_of_date"])): row for row in states}


def construct_overlay_events(
    episodes: Iterable[Mapping[str, Any]],
    mechanical_events: Iterable[Mapping[str, Any]],
    states: Iterable[Mapping[str, Any]],
    indexes: Mapping[str, _Index],
    *,
    overlay_run_id: str,
    phase2c_run_id: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Copy every frozen mechanical event with only current overlay data."""

    episodes = list(episodes)
    mechanical_events = list(mechanical_events)
    state_map = _state_lookup(states)
    episode_map = {str(item["episode_id"]): item for item in episodes}
    close = indexes["NDX_CLOSE"]
    close_dates, close_positions = _date_positions(close.rows)
    price_features = _price_feature_rows(close)

    # Derived price features are calculated chronologically by
    # ``_price_feature_rows``.  The rank helper still slices strictly before
    # the event date, so a later close can never enter an earlier rank.
    price_feature_dates = close.dates
    rsi_history = [price_features[day]["rsi14"] if day in price_features else None for day in price_feature_dates]
    ma_history = [price_features[day]["distance_ma200"] if day in price_features else None for day in price_feature_dates]
    macro_histories = {
        "VXN": _source_history(indexes["NDX_VXN"]),
        "REAL_YIELD": _source_history(indexes["US10Y_REAL"]),
        "NFCI": _source_history(indexes["US_NFCI"]),
    }
    output: list[dict[str, Any]] = []
    for mechanical in sorted(mechanical_events, key=lambda item: (str(item["event_date"]), float(item["threshold"]), str(item["mechanical_event_id"]))):
        day = normalize_date(mechanical["event_date"])
        if day < normalize_date(start_date) or day > normalize_date(end_date):
            continue
        episode_id = str(mechanical["episode_id"])
        episode = episode_map.get(episode_id)
        if episode is None:
            continue
        state = state_map.get((episode_id, day), {})
        close_index = close_positions.get(day)
        current_drawdown = _finite(state.get("current_drawdown"))
        if current_drawdown is None:
            current_drawdown = _finite(mechanical.get("drawdown"))
        days_since_peak = state.get("days_since_peak")
        if days_since_peak is None and close_index is not None:
            peak_position = close_positions.get(normalize_date(episode["peak_date"]))
            if peak_position is not None:
                days_since_peak = max(0, close_index - peak_position)

        rsi_value = state.get("rsi14")
        ma_value = state.get("distance_ma200")
        if close_index is not None:
            derived = price_features.get(day) or {}
            rsi_value = rsi_value if _finite(rsi_value) is not None else derived.get("rsi14")
            ma_value = ma_value if _finite(ma_value) is not None else derived.get("distance_ma200")
        vxn_value = state.get("vxn")
        real_yield_value = state.get("real_yield")
        nfci_value = state.get("nfci")
        rsi = _feature_rank("RSI", rsi_value, price_feature_dates, rsi_history, day)
        ma = _feature_rank("MA200_DISTANCE", ma_value, price_feature_dates, ma_history, day)
        vxn_dates, vxn_values = macro_histories["VXN"]
        real_dates, real_values = macro_histories["REAL_YIELD"]
        nfci_dates, nfci_values = macro_histories["NFCI"]
        vxn = _feature_rank("VXN", vxn_value, vxn_dates, vxn_values, day)
        real_yield = _feature_rank("REAL_YIELD", real_yield_value, real_dates, real_values, day)
        nfci = _feature_rank("NFCI", nfci_value, nfci_dates, nfci_values, day)
        input_ids = _bounded_ids(
            list(state.get("input_observation_ids") or []) + ([mechanical.get("source_observation_id")] if mechanical.get("source_observation_id") else [])
        )
        event_id = "overlay-" + sha256_json({"overlay_run_id": overlay_run_id, "mechanical_event_id": str(mechanical["mechanical_event_id"])})[:24]
        output.append({
            "overlay_event_id": event_id,
            "overlay_run_id": overlay_run_id,
            "phase2c_run_id": phase2c_run_id,
            "mechanical_event_id": str(mechanical["mechanical_event_id"]),
            "episode_id": episode_id,
            "market": str(mechanical.get("market") or "NDX").upper(),
            "threshold": float(mechanical["threshold"]),
            "drawdown_band": _drawdown_band(float(mechanical["threshold"])),
            "event_date": day,
            "event_price": float(mechanical["event_price"]),
            "drawdown": float(mechanical["drawdown"]),
            "current_drawdown": current_drawdown,
            "days_since_peak": int(days_since_peak) if days_since_peak is not None else None,
            "rsi14": rsi["raw_value"],
            "rsi14_percentile": rsi["percentile"],
            "rsi14_opportunity_rank": rsi["opportunity_rank"],
            "rsi14_status": rsi["status"],
            "distance_ma200": ma["raw_value"],
            "distance_ma200_percentile": ma["percentile"],
            "distance_ma200_opportunity_rank": ma["opportunity_rank"],
            "distance_ma200_status": ma["status"],
            "vxn": vxn["raw_value"],
            "vxn_percentile": vxn["percentile"],
            "vxn_opportunity_rank": vxn["opportunity_rank"],
            "vxn_status": vxn["status"],
            "real_yield": real_yield["raw_value"],
            "real_yield_percentile": real_yield["percentile"],
            "real_yield_opportunity_rank": real_yield["opportunity_rank"],
            "real_yield_status": real_yield["status"],
            "nfci": nfci["raw_value"],
            "nfci_percentile": nfci["percentile"],
            "nfci_opportunity_rank": nfci["opportunity_rank"],
            "nfci_status": nfci["status"],
            "source_state_id": state.get("state_id"),
            "input_observation_ids": input_ids,
            "input_hash": sha256_json(input_ids),
            "payload": {
                "definition": "mechanical drawdown event with contemporaneous overlays",
                "primary_trigger_preserved": True,
                "overlay_can_veto": False,
                "rank_minimum_prior_observations": int(OVERLAY_MODEL_CONFIG["rank"]["minimum_prior_observations"]),
                "feature_prior_counts": {
                    "RSI": rsi["prior_observation_count"], "MA200_DISTANCE": ma["prior_observation_count"],
                    "VXN": vxn["prior_observation_count"], "REAL_YIELD": real_yield["prior_observation_count"], "NFCI": nfci["prior_observation_count"],
                },
                "source_observation_dates": {
                    "state": state.get("as_of_date"),
                    "mechanical": day,
                },
            },
        })
    return output


def _evaluate_one_event(event: Mapping[str, Any], episode: Mapping[str, Any], rows: list[Mapping[str, Any]], dates: list[str]) -> dict[str, Any]:
    entry_date = normalize_date(event["event_date"])
    entry_price = float(event["event_price"])
    metrics = _forward_metrics(rows, dates, entry_date, entry_price)
    bottom = _finite(episode.get("bottom_value")) if episode.get("complete") else None
    peak = _finite(episode.get("peak_value"))
    efficiency = None
    entry_to_bottom = None
    days_to_bottom = None
    if bottom is not None and peak is not None and peak != bottom:
        efficiency = (entry_price - bottom) / (peak - bottom)
        entry_to_bottom = entry_price / bottom - 1.0 if bottom else None
        days_to_bottom = bisect_left(dates, str(episode["bottom_date"])) - bisect_right(dates, entry_date)
        days_to_bottom = max(0, days_to_bottom)
    missing = [field for field in ("forward_1y", "forward_3y", "forward_5y") if metrics.get(field) is None]
    return {
        "overlay_evaluation_id": "eval-" + sha256_json({"overlay_run_id": event["overlay_run_id"], "overlay_event_id": event["overlay_event_id"]})[:24],
        "overlay_run_id": str(event["overlay_run_id"]),
        "overlay_event_id": str(event["overlay_event_id"]),
        "phase2c_run_id": str(event["phase2c_run_id"]),
        "episode_id": str(event["episode_id"]),
        "event_date": entry_date,
        "entry_price": entry_price,
        "forward_1y": metrics["forward_1y"],
        "forward_3y": metrics["forward_3y"],
        "forward_5y": metrics["forward_5y"],
        "max_adverse_1y": metrics["max_adverse_1y"],
        "max_favorable_1y": metrics["max_favorable_1y"],
        "episode_bottom_price": bottom,
        "entry_efficiency": efficiency,
        "entry_to_bottom_pct": entry_to_bottom,
        "days_to_bottom": days_to_bottom,
        "timing_regret": metrics["timing_regret"],
        "future_observation_ids": metrics["future_observation_ids"],
        "status": "COMPLETE" if not missing else "PARTIAL",
        "reason": "Evaluation-only pass; future price rows were read after all overlay events were constructed." + (f" 缺少: {', '.join(missing)}" if missing else ""),
        "payload": {"future_fields_are_evaluation_only": True},
    }


def evaluate_overlay_events(
    episodes: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
    close_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Read future prices only after all overlay event rows exist."""

    rows = sorted((dict(row) for row in close_rows), key=lambda row: normalize_date(row["observation_date"]))
    dates = [normalize_date(row["observation_date"]) for row in rows]
    episode_map = {str(item["episode_id"]): item for item in episodes}
    result = []
    for event in sorted(events, key=lambda item: (str(item["event_date"]), str(item["overlay_event_id"]))):
        episode = episode_map.get(str(event["episode_id"]))
        if episode is None:
            continue
        position = bisect_left(dates, normalize_date(event["event_date"]))
        if position >= len(rows) or dates[position] != normalize_date(event["event_date"]):
            continue
        result.append(_evaluate_one_event(event, episode, rows, dates))
    return result


def _outcome_value(evaluation: Mapping[str, Any], outcome: str) -> float | None:
    if outcome.startswith("timing_regret_"):
        horizon = outcome.removeprefix("timing_regret_").removesuffix("d")
        return _finite((evaluation.get("timing_regret") or {}).get(horizon))
    return _finite(evaluation.get(outcome))


def _event_pairs(events: Iterable[Mapping[str, Any]], evaluations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    evaluation_map = {str(row["overlay_event_id"]): row for row in evaluations}
    return [{"event": event, "evaluation": evaluation_map[str(event["overlay_event_id"])]} for event in events if str(event["overlay_event_id"]) in evaluation_map]


def _rank_tertiles(rows: list[tuple[float, float]]) -> dict[str, dict[str, Any]]:
    bins = {"LOW": [], "MIDDLE": [], "HIGH": []}
    for rank_value, outcome in rows:
        if rank_value < 1 / 3:
            bins["LOW"].append(outcome)
        elif rank_value < 2 / 3:
            bins["MIDDLE"].append(outcome)
        else:
            bins["HIGH"].append(outcome)
    return {key: _stats(values) for key, values in bins.items()}


def _conditional_result(overlay_name: str, band: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    rank_field = {
        "RSI": "rsi14_opportunity_rank",
        "MA200_DISTANCE": "distance_ma200_opportunity_rank",
        "VXN": "vxn_opportunity_rank",
        "REAL_YIELD": "real_yield_opportunity_rank",
        "NFCI": "nfci_opportunity_rank",
    }[overlay_name]
    values = [row for row in rows if _finite(row["event"].get(rank_field)) is not None]
    metrics: dict[str, Any] = {}
    for outcome in OUTCOME_NAMES:
        pairs = [(_finite(row["event"].get(rank_field)), _outcome_value(row["evaluation"], outcome)) for row in values]
        pairs = [(float(x), float(y)) for x, y in pairs if x is not None and y is not None]
        metrics[outcome] = {
            "sample_count": len(pairs),
            "spearman": spearman((x for x, _ in pairs), (y for _, y in pairs)),
            "slope": _slope((x for x, _ in pairs), (y for _, y in pairs)),
            "outcome_stats": _stats(y for _, y in pairs),
            "rank_tertiles": _rank_tertiles(pairs),
        }
    available_count = len(values)
    sample_count = len(rows)
    if available_count == 0:
        status = "NO_DATA"
    elif len({str(row["event"]["episode_id"]) for row in values}) < int(OVERLAY_MODEL_CONFIG["leave_one_episode_out"]["minimum_observations_for_interpretation"]):
        status = "INCONCLUSIVE_SMALL_SAMPLE"
    else:
        status = "OK"
    return {
        "overlay_name": overlay_name,
        "drawdown_band": band,
        "rank_field": rank_field,
        "sample_count": sample_count,
        "available_count": available_count,
        "episode_count": len({str(row["event"]["episode_id"]) for row in values}),
        "continuous_rank_is_primary": True,
        "status": status,
        "metrics": metrics,
    }


def conditional_results(events: Iterable[Mapping[str, Any]], evaluations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    pairs = _event_pairs(list(events), list(evaluations))
    output = []
    for overlay_name in OVERLAY_NAMES:
        for band in DRAW_DOWN_BANDS:
            rows = pairs if band == "ALL" else [row for row in pairs if row["event"]["drawdown_band"] == band]
            output.append(_conditional_result(overlay_name, band, rows))
    return output


def _solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    size = len(vector)
    augmented = [list(matrix[index]) + [float(vector[index])] for index in range(size)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-10:
            return None
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]
        augmented[col] = [value / scale for value in augmented[col]]
        for row in range(size):
            if row == col:
                continue
            factor = augmented[row][col]
            if factor:
                augmented[row] = [a - factor * b for a, b in zip(augmented[row], augmented[col])]
    return [augmented[row][-1] for row in range(size)]


def _ols(rows: list[dict[str, Any]], features: list[str], outcome: str) -> dict[str, Any]:
    # The frozen model specification uses concise feature names while the
    # append-only event table keeps the explicit source field names.  Resolve
    # that naming boundary here; this does not alter the model, weights or
    # thresholds and prevents a false zero-sample result for Models 2 and 4.
    event_feature_names = {
        "rsi_opportunity_rank": "rsi14_opportunity_rank",
        "ma200_opportunity_rank": "distance_ma200_opportunity_rank",
    }
    complete = []
    for row in rows:
        values = [
            float(row["event"].get("drawdown", 0.0) * -1.0)
            if feature == "drawdown_magnitude"
            else _finite(row["event"].get(event_feature_names.get(feature, feature)))
            for feature in features
        ]
        target = _outcome_value(row["evaluation"], outcome)
        if target is not None and all(value is not None and math.isfinite(float(value)) for value in values):
            complete.append(([1.0] + [float(value) for value in values], float(target)))
    n = len(complete)
    parameter_count = len(features) + 1
    if n == 0:
        return {"sample_count": 0, "status": "NO_DATA", "features": features, "r2": None, "incremental_r2": None, "coefficients": None}
    if n <= parameter_count:
        return {"sample_count": n, "status": "INCONCLUSIVE_SMALL_SAMPLE", "features": features, "r2": None, "incremental_r2": None, "coefficients": None}
    design = [row for row, _ in complete]
    target = [value for _, value in complete]
    gram = [[sum(row[i] * row[j] for row in design) for j in range(parameter_count)] for i in range(parameter_count)]
    rhs = [sum(row[i] * y for row, y in complete) for i in range(parameter_count)]
    coefficients = _solve_linear(gram, rhs)
    if coefficients is None:
        return {"sample_count": n, "status": "INCONCLUSIVE", "features": features, "r2": None, "incremental_r2": None, "coefficients": None}
    mean_target = sum(target) / n
    residual = [y - sum(beta * x for beta, x in zip(coefficients, row)) for row, y in complete]
    total_ss = sum((y - mean_target) ** 2 for y in target)
    residual_ss = sum(value ** 2 for value in residual)
    r2 = 1.0 - residual_ss / total_ss if total_ss else 0.0
    return {
        "sample_count": n,
        "status": "OK",
        "features": features,
        "coefficients": {"intercept": coefficients[0], **{name: coefficients[index + 1] for index, name in enumerate(features)}},
        "r2": r2,
        "adjusted_r2": 1.0 - (1.0 - r2) * (n - 1) / max(1, n - parameter_count),
        "residual_sum_squares": residual_ss,
    }


def _model_rows(events: list[Mapping[str, Any]], evaluations: list[Mapping[str, Any]], band: str) -> list[dict[str, Any]]:
    pairs = _event_pairs(events, evaluations)
    return pairs if band == "ALL" else [row for row in pairs if row["event"]["drawdown_band"] == band]


def model_results(events: Iterable[Mapping[str, Any]], evaluations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    events, evaluations = list(events), list(evaluations)
    output = []
    model_config = OVERLAY_MODEL_CONFIG["models"]
    for band in DRAW_DOWN_BANDS:
        rows = _model_rows(events, evaluations, band)
        for model_name, features in model_config.items():
            for outcome in OUTCOME_NAMES:
                result = _ols(rows, list(features), outcome)
                output.append({"model_name": model_name, "drawdown_band": band, "outcome_name": outcome, **result})
    # Add the incremental R2 relative to Model 0 without fitting any extra
    # model or selecting a best combination.
    by_key = {(row["drawdown_band"], row["outcome_name"], row["model_name"]): row for row in output}
    for row in output:
        base = by_key.get((row["drawdown_band"], row["outcome_name"], "MODEL_0_DRAWDOWN_ONLY"))
        row["baseline_r2"] = base.get("r2") if base else None
        row["incremental_r2"] = (
            row.get("r2") - base.get("r2")
            if row.get("r2") is not None and base and base.get("r2") is not None else None
        )
    return output


def leave_one_episode_out(events: Iterable[Mapping[str, Any]], evaluations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    events, evaluations = list(events), list(evaluations)
    pairs = _event_pairs(events, evaluations)
    eligible_episodes = sorted({str(row["event"]["episode_id"]) for row in pairs})
    output = []
    for overlay_name in OVERLAY_NAMES:
        rank_field = {
            "RSI": "rsi14_opportunity_rank", "MA200_DISTANCE": "distance_ma200_opportunity_rank",
            "VXN": "vxn_opportunity_rank", "REAL_YIELD": "real_yield_opportunity_rank", "NFCI": "nfci_opportunity_rank",
        }[overlay_name]
        for band in DRAW_DOWN_BANDS:
            band_rows = pairs if band == "ALL" else [row for row in pairs if row["event"]["drawdown_band"] == band]
            full_metrics = {}
            for outcome in OUTCOME_NAMES:
                available = [row for row in band_rows if _finite(row["event"].get(rank_field)) is not None and _outcome_value(row["evaluation"], outcome) is not None]
                full_metrics[outcome] = spearman((row["event"][rank_field] for row in available), (_outcome_value(row["evaluation"], outcome) for row in available))
            for held_out in eligible_episodes:
                kept = [row for row in band_rows if str(row["event"]["episode_id"]) != held_out]
                metrics = {}
                for outcome in OUTCOME_NAMES:
                    available = [row for row in kept if _finite(row["event"].get(rank_field)) is not None and _outcome_value(row["evaluation"], outcome) is not None]
                    value = spearman((row["event"][rank_field] for row in available), (_outcome_value(row["evaluation"], outcome) for row in available))
                    full = full_metrics[outcome]
                    metrics[outcome] = {
                        "spearman": value,
                        "full_spearman": full,
                        "sign_flip": bool(value is not None and full is not None and value * full < 0),
                        "sample_count": len(available),
                    }
                n = len({str(row["event"]["episode_id"]) for row in kept if _finite(row["event"].get(rank_field)) is not None})
                output.append({
                    "overlay_name": overlay_name,
                    "drawdown_band": band,
                    "held_out_episode_id": held_out,
                    "sample_count": n,
                    "status": "OK" if n >= int(OVERLAY_MODEL_CONFIG["leave_one_episode_out"]["minimum_observations_for_interpretation"]) else "INCONCLUSIVE_SMALL_SAMPLE",
                    "result": {"unit": "complete episode block", "metrics": metrics},
                })
    return output


def regime_audit(events: Iterable[Mapping[str, Any]], evaluations: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    pairs = _event_pairs(list(events), list(evaluations))
    output: dict[str, Any] = {"fixed_labels": True, "by_overlay": {}, "crisis_influence": {}}
    for overlay_name in OVERLAY_NAMES:
        rank_field = {
            "RSI": "rsi14_opportunity_rank", "MA200_DISTANCE": "distance_ma200_opportunity_rank",
            "VXN": "vxn_opportunity_rank", "REAL_YIELD": "real_yield_opportunity_rank", "NFCI": "nfci_opportunity_rank",
        }[overlay_name]
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in pairs:
            if _finite(row["event"].get(rank_field)) is not None:
                groups[_broad_regime_label(str(row["event"]["event_date"]))].append(row)
        by_regime = {}
        for label, rows in sorted(groups.items()):
            metrics = {
                outcome: {
                    "spearman": spearman((row["event"][rank_field] for row in rows), (_outcome_value(row["evaluation"], outcome) for row in rows)),
                    "sample_count": sum(_outcome_value(row["evaluation"], outcome) is not None for row in rows),
                }
                for outcome in OUTCOME_NAMES
            }
            by_regime[label] = {
                "episode_count": len({str(row["event"]["episode_id"]) for row in rows}),
                "event_count": len(rows),
                "metrics": metrics,
            }
        output["by_overlay"][overlay_name] = by_regime
    for crisis, (start, end) in OVERLAY_MODEL_CONFIG["crisis_windows"].items():
        in_window = [row for row in pairs if start <= str(row["event"]["event_date"]) <= end]
        excluded = [row for row in pairs if not (start <= str(row["event"]["event_date"]) <= end)]
        effects = {}
        for overlay_name in OVERLAY_NAMES:
            rank_field = {
                "RSI": "rsi14_opportunity_rank", "MA200_DISTANCE": "distance_ma200_opportunity_rank",
                "VXN": "vxn_opportunity_rank", "REAL_YIELD": "real_yield_opportunity_rank", "NFCI": "nfci_opportunity_rank",
            }[overlay_name]
            metrics = {}
            for outcome in (
                "forward_1y", "forward_3y", "forward_5y", "max_adverse_1y",
                "timing_regret_30d", "timing_regret_60d", "timing_regret_120d",
            ):
                full_rows = [row for row in pairs if _finite(row["event"].get(rank_field)) is not None and _outcome_value(row["evaluation"], outcome) is not None]
                excluded_rows = [row for row in excluded if _finite(row["event"].get(rank_field)) is not None and _outcome_value(row["evaluation"], outcome) is not None]
                full_corr = spearman((row["event"][rank_field] for row in full_rows), (_outcome_value(row["evaluation"], outcome) for row in full_rows))
                excluded_corr = spearman((row["event"][rank_field] for row in excluded_rows), (_outcome_value(row["evaluation"], outcome) for row in excluded_rows))
                metrics[outcome] = {
                    "full_spearman": full_corr,
                    "excluding_window_spearman": excluded_corr,
                    "delta_excluding_minus_full": excluded_corr - full_corr if excluded_corr is not None and full_corr is not None else None,
                    "excluded_sample_count": len(excluded_rows),
                }
            effects[overlay_name] = metrics
        output["crisis_influence"][crisis] = {
            "event_count": len(in_window),
            "episode_count": len({str(row["event"]["episode_id"]) for row in in_window}),
            "event_ids": [str(row["event"]["overlay_event_id"]) for row in in_window],
            "exclusion_effects": effects,
            "note": "仅按固定日历窗口标记，不能据此调整规则。",
        }
    # A regime is considered stable only when every populated broad period
    # has at least five independent episodes and the primary 1Y/3Y/5Y rank
    # directions do not flip.  This is a descriptive gate, never a tuning
    # criterion.
    stability = {}
    for overlay_name, groups in output["by_overlay"].items():
        populated = [group for group in groups.values() if group["episode_count"] > 0]
        returns = [group["metrics"]["forward_1y"]["spearman"] for group in populated if group["metrics"]["forward_1y"]["spearman"] is not None]
        adequate = bool(populated) and all(group["episode_count"] >= 5 for group in populated)
        same_direction = not returns or all(value >= 0 for value in returns) or all(value <= 0 for value in returns)
        stability[overlay_name] = {
            "populated_regime_count": len(populated),
            "minimum_episode_count": min((group["episode_count"] for group in populated), default=0),
            "forward_1y_directions": returns,
            "stable": adequate and same_direction,
            "status": "STABLE_DESCRIPTIVE" if adequate and same_direction else "INCONCLUSIVE_REGIME_DEPENDENCY",
        }
    output["stability_by_overlay"] = stability
    return output


def _data_purchase_recommendation() -> dict[str, Any]:
    """State the frozen no-purchase decision without inventing prices."""

    return {
        "buy_now": False,
        "decision": "DEFER_UNTIL_OVERLAY_EVIDENCE_OR_PIT_QUOTE",
        "reason": "本阶段只验证已有免费代理；Overlay 总体仍 INCONCLUSIVE，不能据此承诺购买或把缺失数据假设补齐。",
        "candidates": [
            {
                "candidate": "EPS Revision/Growth",
                "coverage_relief": "HIGH_WEIGHT_BUT_NOT_COVERED",
                "theoretical_incremental_information": "HIGH",
                "availability": "LOW_ON_FREE_PATH",
                "cost": "UNKNOWN_UNQUOTED",
                "pit_credibility": "LOW_UNTIL_VINTAGED_ESTIMATES",
                "priority": "1A_IF_AUDITABLE_VINTAGE_QUOTE_EXISTS",
            },
            {
                "candidate": "Forward PE",
                "coverage_relief": "HIGH_WEIGHT_BUT_NOT_COVERED",
                "theoretical_incremental_information": "HIGH",
                "availability": "MEDIUM_WITH_DEFINITION_RISK",
                "cost": "UNKNOWN_UNQUOTED",
                "pit_credibility": "LOW_UNTIL_VINTAGED_INDEX_AGGREGATE",
                "priority": "1B_IF_DEFINITION_AND_VINTAGE_AUDIT_PASS",
            },
            {
                "candidate": "Historical Breadth",
                "coverage_relief": "MEDIUM",
                "theoretical_incremental_information": "MEDIUM",
                "availability": "LOW_WITH_MEMBERSHIP_DENOMINATOR_RISK",
                "cost": "UNKNOWN_UNQUOTED",
                "pit_credibility": "LOW_UNTIL_MEMBERSHIP_VINTAGES",
                "priority": "FOLLOW_UP",
            },
        ],
    }


def _overlay_value_classification(
    overlay_name: str,
    events: list[Mapping[str, Any]],
    evaluations: list[Mapping[str, Any]],
    loo_rows: list[Mapping[str, Any]],
    regimes: Mapping[str, Any],
) -> str:
    pairs = _event_pairs(events, evaluations)
    rank_field = {
        "RSI": "rsi14_opportunity_rank", "MA200_DISTANCE": "distance_ma200_opportunity_rank",
        "VXN": "vxn_opportunity_rank", "REAL_YIELD": "real_yield_opportunity_rank", "NFCI": "nfci_opportunity_rank",
    }[overlay_name]
    available = [row for row in pairs if _finite(row["event"].get(rank_field)) is not None and _outcome_value(row["evaluation"], "forward_1y") is not None]
    episode_count = len({str(row["event"]["episode_id"]) for row in available})
    if episode_count < 5:
        return "INCONCLUSIVE"
    directional = []
    for outcome in (
        "forward_1y", "forward_3y", "forward_5y",
        "timing_regret_30d", "timing_regret_60d", "timing_regret_120d",
    ):
        values = [row for row in pairs if _finite(row["event"].get(rank_field)) is not None and _outcome_value(row["evaluation"], outcome) is not None]
        corr = spearman((row["event"][rank_field] for row in values), (_outcome_value(row["evaluation"], outcome) for row in values))
        if corr is not None:
            directional.append(corr)
    if not directional:
        return "INCONCLUSIVE"
    positive = sum(value > 0.05 for value in directional)
    near_zero = sum(abs(value) <= 0.05 for value in directional)
    loo_for_overlay = [row for row in loo_rows if row["overlay_name"] == overlay_name and row["drawdown_band"] == "ALL" and row["status"] == "OK"]
    flips = sum(1 for row in loo_for_overlay for metric in (row["result"].get("metrics") or {}).values() if metric.get("sign_flip"))
    regime_groups = (regimes.get("by_overlay") or {}).get(overlay_name) or {}
    regime_corrs = [
        (group.get("metrics") or {}).get(outcome, {}).get("spearman")
        for group in regime_groups.values()
        for outcome in (
            "forward_1y", "forward_3y", "forward_5y", "max_adverse_1y",
            "timing_regret_30d", "timing_regret_60d", "timing_regret_120d",
        )
        if (group.get("metrics") or {}).get(outcome, {}).get("spearman") is not None
    ]
    # Opportunity rank is oriented so that a positive relationship is the
    # desirable direction for returns, less-adverse moves and less-negative
    # regret.  Entry Efficiency is intentionally excluded here because its
    # preferred sign is different and it is an evaluation descriptor.
    stable_regimes = len(regime_corrs) == 0 or all(value >= -0.05 for value in regime_corrs)
    if episode_count >= 10 and positive == 0 and near_zero >= 3 and flips == 0:
        return "NONE"
    if positive >= 3 and flips <= max(1, len(loo_for_overlay) // 5) and stable_regimes and episode_count >= 10:
        return "POSITIVE"
    if positive >= 2 and episode_count >= 5 and stable_regimes and flips <= max(1, len(loo_for_overlay) // 3):
        return "WEAK"
    return "INCONCLUSIVE"


def _parsimony(value: str) -> str:
    return {"STRONG": "KEEP", "POSITIVE": "KEEP", "WEAK": "OPTIONAL", "NONE": "DROP", "INCONCLUSIVE": "INCONCLUSIVE"}.get(value, "INCONCLUSIVE")


def _drawdown_primary_signal(events: list[Mapping[str, Any]], evaluations: list[Mapping[str, Any]]) -> tuple[str, dict[str, Any]]:
    eval_map = {str(row["overlay_event_id"]): row for row in evaluations}
    rows = [eval_map[str(event["overlay_event_id"])] for event in events if abs(float(event["threshold"]) - 0.1) < 1e-9 and str(event["overlay_event_id"]) in eval_map]
    rows = [row for row in rows if _finite(row.get("forward_1y")) is not None]
    medians = {field: _stats(row.get(field) for row in rows)["median"] for field in ("forward_1y", "forward_3y", "forward_5y")}
    n = len({str(row["episode_id"]) for row in rows})
    if n >= 10 and all(value is not None and value > 0 for value in medians.values()):
        result = "SUPPORTED"
    elif n >= 5:
        result = "WEAK"
    else:
        result = "INCONCLUSIVE"
    return result, {"episode_count": n, "event_count": len(rows), "median_forward_returns": medians, "evaluation_unit": "one -10% trigger per episode"}


def _natural_schedule_trigger(repo: PITRepository) -> dict[str, Any]:
    gates = repo.get_gate_checks("GATE_A_SCHEDULED_RUN")
    passed = [row for row in gates if bool((row.get("details") or {}).get("actual_calendar_trigger_verified"))]
    return {
        "NATURAL_SCHEDULE_TRIGGER": "PASS" if passed else "PENDING_FIRST_NATURAL_CALENDAR_EVENT",
        "observed_count": len(passed),
        "latest_observed": passed[-1] if passed else None,
        "independent_from_proxy_research": True,
    }


def build_overlay_report(
    repo: PITRepository,
    overlay_run_id: str,
    *,
    episodes: list[Mapping[str, Any]] | None = None,
    events: list[Mapping[str, Any]] | None = None,
    evaluations: list[Mapping[str, Any]] | None = None,
    conditional: list[Mapping[str, Any]] | None = None,
    models: list[Mapping[str, Any]] | None = None,
    loo: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    run = repo.get_drawdown_overlay_run(overlay_run_id)
    if not run:
        raise ValueError("drawdown overlay run 不存在")
    phase2c_run_id = str(run["phase2c_run_id"])
    episodes = list(episodes if episodes is not None else repo.get_drawdown_episodes(phase2c_run_id, limit=100000))
    events = list(events if events is not None else repo.get_drawdown_overlay_events(overlay_run_id, limit=100000))
    evaluations = list(evaluations if evaluations is not None else repo.get_drawdown_overlay_evaluations(overlay_run_id, limit=100000))
    conditional = list(conditional if conditional is not None else repo.get_drawdown_overlay_conditional_results(overlay_run_id, limit=10000))
    models = list(models if models is not None else repo.get_drawdown_overlay_model_results(overlay_run_id, limit=50000))
    loo = list(loo if loo is not None else repo.get_drawdown_overlay_loeo_results(overlay_run_id, limit=500000))
    loo_plain = [{key: value for key, value in row.items() if key != "result_json"} | {"result": row.get("result", {})} for row in loo]
    regime = regime_audit(events, evaluations)
    classifications = {}
    parsimony = {}
    for name in OVERLAY_NAMES:
        value = _overlay_value_classification(name, events, evaluations, loo_plain, regime)
        classifications[name] = value
        parsimony[name] = _parsimony(value)
    primary, primary_detail = _drawdown_primary_signal(events, evaluations)
    overall_values = list(classifications.values())
    if primary != "SUPPORTED":
        next_architecture = "MORE_DATA_REQUIRED"
    elif all(value in {"NONE", "WEAK"} for value in overall_values):
        next_architecture = "DRAWDOWN_ONLY"
    elif any(value in {"STRONG", "POSITIVE"} for value in overall_values):
        next_architecture = "DRAWDOWN_PLUS_OVERLAY"
    else:
        next_architecture = "MORE_DATA_REQUIRED"
    overall = "INCONCLUSIVE"
    if any(value in {"STRONG", "POSITIVE"} for value in overall_values):
        overall = "POSITIVE"
    elif primary == "SUPPORTED" and all(value in {"NONE", "WEAK"} for value in overall_values):
        overall = "WEAK"
    return {
        "phase": "2D",
        "overlay_run_id": overlay_run_id,
        "overlay_model_version": OVERLAY_MODEL_VERSION,
        "config_hash": OVERLAY_CONFIG_HASH,
        "phase2c_run_id": phase2c_run_id,
        "date_range": {"start": run["start_date"], "end": run["end_date"]},
        "research_only": True,
        "strict_pit": False,
        "investment_action_eligible": False,
        "PHASE_2D_STATUS": "PASS_WITH_LIMITATIONS",
        "DRAWDOWN_PRIMARY_SIGNAL": primary,
        "drawdown_primary_detail": primary_detail,
        "overlay_information_value": classifications,
        "parsimony_rule": parsimony,
        "RSI_OVERLAY_VALUE": classifications["RSI"],
        "MA200_OVERLAY_VALUE": classifications["MA200_DISTANCE"],
        "VXN_OVERLAY_VALUE": classifications["VXN"],
        "REAL_YIELD_OVERLAY_VALUE": classifications["REAL_YIELD"],
        "NFCI_OVERLAY_VALUE": classifications["NFCI"],
        "OVERALL_OVERLAY_INCREMENTAL_VALUE": overall,
        "NEXT_ARCHITECTURE": next_architecture,
        "drawdown_event_count": len(events),
        "drawdown_event_count_by_threshold": {str(int(round(float(threshold) * 100))): sum(abs(float(event["threshold"]) - threshold) < 1e-9 for event in events) for threshold in (0.1, 0.2, 0.3, 0.4, 0.5)},
        "drawdown_event_count_by_band": {band: sum(event["drawdown_band"] == band for event in events) for band in ("MILD", "MEDIUM", "DEEP")},
        "episode_count": len(episodes),
        "evaluated_event_count": len(evaluations),
        "conditional_results": conditional,
        "model_results": models,
        "leave_one_episode_out": {"row_count": len(loo), "unit": "complete episode", "results": loo_plain},
        "regime_stability": regime,
        "natural_schedule": _natural_schedule_trigger(repo),
        "data_purchase_recommendation": _data_purchase_recommendation(),
        "data_snapshot": run.get("data_snapshot") or {},
        "data_cutoff": run.get("data_cutoff"),
        "lookahead_controls": {
            "frozen_before_evaluation": True,
            "event_construction_inputs": ["Phase 2C mechanical drawdown events", "same-date observable state", "strictly prior rank history"],
            "future_fields_absent_from_event": True,
            "future_outcomes_table": "drawdown_overlay_event_evaluations",
            "overlay_can_veto_drawdown_event": False,
            "parameter_search": False,
            "new_data_purchased": False,
        },
        "limitations": [
            "Historical proxy series are not fully vintaged strict PIT; this phase is research-only.",
            "Overlay ranks require at least 60 strictly prior observations; missing values remain UNAVAILABLE.",
            "Episode/event counts are small and dependent across thresholds within a bear episode; conclusions are not investment instructions.",
            "No capital amount, weight, stop-loss, take-profit or final state machine is evaluated in Phase 2D.",
        ],
    }


def run_overlay_validation(
    repo: PITRepository | None = None,
    *,
    phase2c_run_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    overlay_run_id: str | None = None,
) -> dict[str, Any]:
    repo = repo or PITRepository(DB)
    candidates = repo.get_episode_validation_runs(market="NDX", limit=1000)
    phase2c = repo.get_episode_validation_run(phase2c_run_id) if phase2c_run_id else next((item for item in candidates if item.get("status") == "COMPLETED"), None)
    if not phase2c or phase2c.get("status") != "COMPLETED":
        raise ValueError("需要一个已完成的 Phase 2C run")
    phase2c_run_id = str(phase2c["episode_run_id"])
    start_date = normalize_date(start_date or phase2c["start_date"])
    end_date = normalize_date(end_date or phase2c["end_date"])
    overlay_run_id = overlay_run_id or f"phase2d-overlay-ndx-v1-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    episodes = repo.get_drawdown_episodes(phase2c_run_id, limit=100000)
    mechanical = repo.get_drawdown_mechanical_events(phase2c_run_id, limit=100000)
    states = repo.get_episode_daily_states(phase2c_run_id, limit=1000000)
    indexes = _load_proxy_indexes(repo, end_date)
    snapshot = _data_snapshot(indexes)
    snapshot["phase2c_run_id"] = phase2c_run_id
    snapshot["phase2c_data_snapshot"] = phase2c.get("data_snapshot") or {}
    run, created = repo.create_drawdown_overlay_run({
        "overlay_run_id": overlay_run_id,
        "market": "NDX",
        "overlay_model_version": OVERLAY_MODEL_VERSION,
        "phase2c_run_id": phase2c_run_id,
        "start_date": start_date,
        "end_date": end_date,
        "overlay_model_config": OVERLAY_MODEL_CONFIG,
        "config_hash": OVERLAY_CONFIG_HASH,
        "data_snapshot": snapshot,
        "data_cutoff": as_of_datetime(f"{end_date}T23:59:59.999999Z"),
    })
    if not created and run.get("status") == "COMPLETED":
        report_row = repo.get_drawdown_overlay_report(overlay_run_id)
        return (report_row or {}).get("report") or build_overlay_report(repo, overlay_run_id)
    try:
        events = construct_overlay_events(episodes, mechanical, states, indexes, overlay_run_id=overlay_run_id, phase2c_run_id=phase2c_run_id, start_date=start_date, end_date=end_date)
        for event in events:
            repo.append_drawdown_overlay_event(event)
        # The explicit evaluation pass starts only after all contemporaneous
        # event rows have been persisted.
        evaluations = evaluate_overlay_events(episodes, events, indexes["NDX_CLOSE"].rows)
        for evaluation in evaluations:
            repo.append_drawdown_overlay_evaluation(evaluation)
        conditional = conditional_results(events, evaluations)
        for result in conditional:
            repo.append_drawdown_overlay_conditional_result({"overlay_run_id": overlay_run_id, **result, "result": result})
        models = model_results(events, evaluations)
        for result in models:
            repo.append_drawdown_overlay_model_result({"overlay_run_id": overlay_run_id, **result, "result": result})
        loo = leave_one_episode_out(events, evaluations)
        for result in loo:
            repo.append_drawdown_overlay_loeo_result({"overlay_run_id": overlay_run_id, **result, "result": result["result"]})
        # Build from the in-memory rows so the classification is identical to
        # what was appended, then record the immutable report.
        report = build_overlay_report(repo, overlay_run_id, episodes=episodes, events=events, evaluations=evaluations, conditional=conditional, models=models, loo=loo)
        repo.record_drawdown_overlay_report(overlay_run_id, report)
        summary = {
            "drawdown_event_count": len(events),
            "evaluated_event_count": len(evaluations),
            "conditional_result_count": len(conditional),
            "model_result_count": len(models),
            "loeo_result_count": len(loo),
            "PHASE_2D_STATUS": report["PHASE_2D_STATUS"],
            "DRAWDOWN_PRIMARY_SIGNAL": report["DRAWDOWN_PRIMARY_SIGNAL"],
            "OVERALL_OVERLAY_INCREMENTAL_VALUE": report["OVERALL_OVERLAY_INCREMENTAL_VALUE"],
            "NEXT_ARCHITECTURE": report["NEXT_ARCHITECTURE"],
        }
        repo.complete_drawdown_overlay_run(overlay_run_id, summary=summary)
        return report
    except Exception as exc:
        try:
            repo.complete_drawdown_overlay_run(overlay_run_id, status="FAILED", error={"type": type(exc).__name__, "message": str(exc)})
        except Exception:
            pass
        raise


def blind_overlay_event_date_view(repo: PITRepository, overlay_run_id: str, day: str) -> dict[str, Any] | None:
    """Return the overlay snapshot for a date without any evaluation fields."""

    run = repo.get_drawdown_overlay_run(overlay_run_id)
    if not run:
        return None
    day = normalize_date(day)
    events = repo.get_drawdown_overlay_events(overlay_run_id, limit=100000)
    selected = [event for event in events if normalize_date(event["event_date"]) == day]
    if not selected:
        return None
    forbidden = {"episode_bottom_price", "entry_efficiency", "forward_1y", "forward_3y", "forward_5y", "timing_regret", "future_observation_ids", "recovery_date", "max_drawdown_date"}
    clean = []
    for event in selected:
        row = {key: value for key, value in event.items() if key not in forbidden and key not in {"payload"}}
        row["future_hidden"] = True
        clean.append(row)
    return {
        "overlay_run_id": overlay_run_id,
        "as_of": day,
        "market": run["market"],
        "overlay_model_version": run["overlay_model_version"],
        "research_only": True,
        "strict_pit": False,
        "investment_action_eligible": False,
        "drawdown_events": clean,
        "future_hidden": True,
    }


def _markdown_report(report: Mapping[str, Any]) -> str:
    def fmt(value: Any, digits: int = 3) -> str:
        number = _finite(value)
        return "—" if number is None else f"{number:.{digits}f}"

    def pct(value: Any) -> str:
        number = _finite(value)
        return "—" if number is None else f"{number * 100:.2f}%"

    def payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
        nested = row.get("result")
        return nested if isinstance(nested, Mapping) and ("metrics" in nested or "r2" in nested or "features" in nested) else row

    lines = [
        "# Phase 2D：Drawdown-First Conditional Overlay Validation",
        "",
        f"本报告对应不可变运行 `{report['overlay_run_id']}`，时间范围 {report['date_range']['start']} 至 {report['date_range']['end']}。Drawdown 是唯一 Primary Trigger；Overlay 只能提供条件信息，不能取消或阻断事件。",
        "",
        "## 结论枚举",
        "",
        "| 项目 | 结果 |",
        "|---|---|",
        f"| `PHASE_2D_STATUS` | **{report['PHASE_2D_STATUS']}** |",
        f"| `DRAWDOWN_PRIMARY_SIGNAL` | **{report['DRAWDOWN_PRIMARY_SIGNAL']}** |",
        f"| `OVERALL_OVERLAY_INCREMENTAL_VALUE` | **{report['OVERALL_OVERLAY_INCREMENTAL_VALUE']}** |",
        f"| `NEXT_ARCHITECTURE` | **{report['NEXT_ARCHITECTURE']}** |",
        "",
        "## 样本与冻结规则",
        "",
        f"- Drawdown events：{report['drawdown_event_count']}；分档：{json.dumps(report['drawdown_event_count_by_band'], ensure_ascii=False)}。",
        f"- Episode：{report['episode_count']}；评价事件：{report['evaluated_event_count']}。",
        "- 回撤档位固定为 -10/-20/-30/-40/-50%；每个事件保留，Overlay 不得 veto。",
        "- 分位使用严格早于事件日的历史观察，至少 60 个；连续 rank 是主要统计量，三等分标签只作展示。",
        "",
        "## 各 Overlay 结论",
        "",
        "| Overlay | Information Value | Parsimony |",
        "|---|---|---|",
    ]
    for name in OVERLAY_NAMES:
        lines.append(f"| {name} | **{report['overlay_information_value'][name]}** | {report['parsimony_rule'][name]} |")
    lines += [
        "",
        "## Drawdown 基线",
        "",
        f"- -10% 触发的 episode 数：{report['drawdown_primary_detail']['episode_count']}；1Y/3Y/5Y 中位数：{json.dumps(report['drawdown_primary_detail']['median_forward_returns'], ensure_ascii=False)}。",
        "- 该统计是描述性基线，不等同于可交易基金的净收益或自动加仓许可。",
        "",
        "## Conditional Overlay 结果",
        "",
        "| Overlay | 档位 | 事件数 | 可用数 | 1Y rank rho | 3Y rank rho | 5Y rank rho | 60d Regret rho |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report.get("conditional_results", []):
        result = payload(row)
        metrics = result.get("metrics") or {}
        lines.append(
            f"| {row.get('overlay_name')} | {row.get('drawdown_band')} | {row.get('sample_count', result.get('sample_count', 0))} | {row.get('available_count', result.get('available_count', 0))} | "
            f"{fmt((metrics.get('forward_1y') or {}).get('spearman'))} | {fmt((metrics.get('forward_3y') or {}).get('spearman'))} | "
            f"{fmt((metrics.get('forward_5y') or {}).get('spearman'))} | {fmt((metrics.get('timing_regret_60d') or {}).get('spearman'))} |"
        )
    lines += [
        "",
        "## 趋势过早进入、未来下行与 Entry Efficiency",
        "",
        "这些结果仍然以 Drawdown 档位为条件；30/60/120 个交易日 regret 用于检查刚触发后继续下跌的风险，Entry Efficiency 只作描述性评价，不参与枚举判定。",
        "",
        "| Overlay | 档位 | 1Y 最大不利走势 rho | 30d Regret rho | 60d Regret rho | 120d Regret rho | Entry Efficiency rho |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in report.get("conditional_results", []):
        result = payload(row)
        metrics = result.get("metrics") or {}
        lines.append(
            f"| {row.get('overlay_name')} | {row.get('drawdown_band')} | {fmt((metrics.get('max_adverse_1y') or {}).get('spearman'))} | "
            f"{fmt((metrics.get('timing_regret_30d') or {}).get('spearman'))} | {fmt((metrics.get('timing_regret_60d') or {}).get('spearman'))} | "
            f"{fmt((metrics.get('timing_regret_120d') or {}).get('spearman'))} | {fmt((metrics.get('entry_efficiency') or {}).get('spearman'))} |"
        )
    lines += [
        "",
        "## Model 0–4",
        "",
        "以下列出 `ALL` 档位；分档结果、系数和样本数保存在 JSON 与数据库。",
        "",
        "| 模型 | Outcome | n | R² | 增量 R² |",
        "|---|---|---:|---:|---:|",
    ]
    for model_name in ("MODEL_0_DRAWDOWN_ONLY", "MODEL_1_DRAWDOWN_PLUS_VXN", "MODEL_2_DRAWDOWN_PLUS_TREND", "MODEL_3_DRAWDOWN_PLUS_MACRO", "MODEL_4_DRAWDOWN_PLUS_ALL"):
        for outcome in ("forward_1y", "forward_3y", "forward_5y"):
            row = next((item for item in report.get("model_results", []) if item.get("model_name") == model_name and item.get("drawdown_band") == "ALL" and item.get("outcome_name") == outcome), None)
            if row is None:
                continue
            result = payload(row)
            lines.append(f"| {model_name} | {outcome} | {result.get('sample_count', row.get('sample_count', 0))} | {fmt(result.get('r2'))} | {fmt(result.get('incremental_r2'))} |" )
    lines += [
        "",
        "模型只用于判断 Drawdown 之后是否存在增量信息，没有搜索 Overlay 权重。",
        "",
        "## Leave-One-Episode-Out 与 regime",
        "",
        f"- LOEO 行数：{report['leave_one_episode_out']['row_count']}；完整 Episode 是 block 单位。",
        "- Broad regime、2000–2002、2008–2009、2018 Q4、2020、2022 的固定窗口审计均只用于稳定性和归因，不用于调参。",
        "",
        "| 危机窗口 | 事件数 | Episode 数 |",
        "|---|---:|---:|",
    ]
    for label, item in (report.get("regime_stability", {}).get("crisis_influence") or {}).items():
        lines.append(f"| {label} | {item.get('event_count', 0)} | {item.get('episode_count', 0)} |")
    lines += [
        "",
        "每个危机窗口的 `exclusion_effects`（排除该窗口前后的 rank 相关变化）见 JSON；如果排除单一危机后方向消失，仍标记为 regime-dependent。",
        "",
        "## 数据购买判断",
        "",
        f"当前决定：`{report['data_purchase_recommendation']['decision']}`；`buy_now={report['data_purchase_recommendation']['buy_now']}`。",
        "",
        "| 候选 | Coverage | 理论信息 | 免费可得性 | 成本 | PIT 可信度 |",
        "|---|---|---|---|---|---|",
    ]
    for item in report.get("data_purchase_recommendation", {}).get("candidates", []):
        lines.append(f"| {item['candidate']} | {item['coverage_relief']} | {item['theoretical_incremental_information']} | {item['availability']} | {item['cost']} | {item['pit_credibility']} |")
    lines += [
        "",
        "## 无前视与限制",
        "",
        "- 所有机械事件和当日 Overlay 快照先写入数据库，随后才读取未来价格评价。",
        "- `/api/drawdown-overlay-date` 只返回当日状态；未来收益和底部字段在显式评价接口中。",
        "- 本阶段不进入资金状态机、不调权重、不改阈值、不购买新数据。",
        "",
        f"自动日历触发状态：`{report['natural_schedule']['NATURAL_SCHEDULE_TRIGGER']}`。",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run Phase 2D drawdown-first conditional overlay validation")
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--phase2c-run-id", default=None)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--markdown-out", type=Path, default=None)
    args = parser.parse_args()
    result = run_overlay_validation(PITRepository(args.db), phase2c_run_id=args.phase2c_run_id, start_date=args.start_date, end_date=args.end_date, overlay_run_id=args.run_id)
    if args.json_out:
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.write_text(_markdown_report(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


__all__ = [
    "OVERLAY_MODEL_VERSION", "OVERLAY_MODEL_CONFIG", "OVERLAY_CONFIG_HASH",
    "construct_overlay_events", "evaluate_overlay_events", "conditional_results",
    "model_results", "leave_one_episode_out", "regime_audit", "build_overlay_report",
    "run_overlay_validation", "blind_overlay_event_date_view", "spearman",
]
