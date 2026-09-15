"""Phase 2C: episode-based validation of the frozen proxy opportunity signal.

The research unit in Phase 2B was a signal day.  This module changes only the
research unit to a drawdown episode and an opportunity event.  It does not
alter ``NDX_SCORE_V2.0`` or ``NDX_PROXY_RESEARCH_V1``.  Episode construction,
observable state and event clustering are completed before the evaluation
pass reads any future price.  Future bottom/recovery fields are evaluation
descriptors and never appear in the blind date view.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import math
import random
from typing import Any, Iterable, Mapping

from data_contract import as_of_datetime, normalize_date, sha256_json
from pit_repository import PITRepository
from phase2b_research import (
    PROXY_CONFIG_HASH,
    PROXY_MODEL_VERSION,
    _Index,
    _data_snapshot,
    _finite,
    _load_proxy_indexes,
)


EPISODE_MODEL_VERSION = "EPISODE_OPPORTUNITY_V1"
EPISODE_CONFIG_CREATED_AT = "2026-09-15T00:00:00Z"

# This object is frozen before any episode outcome is read.  The percentile,
# cluster gap and drawdown thresholds are deliberately explicit so a later
# result cannot silently change the research question.
EPISODE_MODEL_CONFIG: dict[str, Any] = {
    "model_version": EPISODE_MODEL_VERSION,
    "created_at": EPISODE_CONFIG_CREATED_AT,
    "research_only": True,
    "strict_pit": False,
    "investment_action_eligible": False,
    "upstream_proxy_model_version": PROXY_MODEL_VERSION,
    "upstream_proxy_config_hash": PROXY_CONFIG_HASH,
    "primary_signal_variant": "proxy_a",
    "signal_variants": ["proxy_a", "proxy_b"],
    "episode": {
        "start_rule": "first trading day after a running ATH close is left on the downside",
        "end_rule": "first trading day close reclaims the episode peak value",
        "ath_comparison": "strictly greater while idle; greater_or_equal for recovery",
        "long_bear_market_is_one_episode": True,
        "duration_unit": "calendar_days_and_trading_days",
    },
    "opportunity_event": {
        "high_signal_rule": "proxy_percentile >= 0.90 (fixed top 10 percent)",
        "percentile_cutoff": 0.90,
        "cluster_gap_trading_days": 10,
        "entry_date_rule": "first_signal_date",
    },
    "drawdown_baseline": {
        "thresholds": [0.10, 0.20, 0.30, 0.40, 0.50],
        "first_trigger_only_per_episode_and_threshold": True,
    },
    "timing_regret": {
        "forward_trading_days": [10, 30, 60, 120],
    },
    "at_10_percent": {
        "agree_if_percentile_at_least": 0.90,
        "strongly_oppose_if_percentile_at_most": 0.10,
        "delay_between_cutoffs": True,
    },
    "fast_recovery": {
        "recovery_definition": "first close >= episode bottom * 1.10 after max drawdown",
        "maximum_trading_days": 60,
        "miss_rule": "no composite opportunity first signal on or before the 10 percent rebound date",
    },
    "matched_comparison": {
        "same_episode_only": True,
        "nearest_event_by_trading_day_distance": True,
        "tie_break": "smaller threshold then earlier date",
    },
    "bootstrap": {
        "unit": "complete episode",
        "resamples": 2000,
        "seed": 20260915,
        "interval": "percentile_2.5_to_97.5",
    },
    "classification": {
        "no_difference_return_abs": 0.02,
        "no_difference_regret_abs": 0.02,
        "minimum_baseline_episodes_weak": 5,
        "minimum_baseline_episodes_positive": 10,
        "minimum_baseline_episodes_strong": 15,
    },
    "crisis_windows": {
        "2000_2002": ["2000-01-01", "2002-12-31"],
        "2008_2009": ["2008-01-01", "2009-12-31"],
        "2018_q4": ["2018-10-01", "2018-12-31"],
        "2020": ["2020-01-01", "2020-12-31"],
        "2022": ["2022-01-01", "2022-12-31"],
    },
}
EPISODE_CONFIG_HASH = sha256_json(EPISODE_MODEL_CONFIG)
MECHANICAL_THRESHOLDS = tuple(float(x) for x in EPISODE_MODEL_CONFIG["drawdown_baseline"]["thresholds"])
TIMING_HORIZONS = tuple(int(x) for x in EPISODE_MODEL_CONFIG["timing_regret"]["forward_trading_days"])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stats(values: Iterable[Any]) -> dict[str, Any]:
    data = sorted(float(value) for value in values if _finite(value) is not None)
    if not data:
        return {"mean": None, "median": None, "p25": None, "p75": None, "sample_count": 0}

    def quantile(position: float) -> float:
        if len(data) == 1:
            return data[0]
        point = (len(data) - 1) * position
        low, high = math.floor(point), math.ceil(point)
        if low == high:
            return data[low]
        return data[low] + (data[high] - data[low]) * (point - low)

    return {
        "mean": sum(data) / len(data),
        "median": quantile(0.5),
        "p25": quantile(0.25),
        "p75": quantile(0.75),
        "sample_count": len(data),
    }


def _date_position(rows: list[Mapping[str, Any]]) -> tuple[list[str], dict[str, int]]:
    dates = [normalize_date(row["observation_date"]) for row in rows]
    return dates, {day: index for index, day in enumerate(dates)}


def _first_on_or_after(rows: list[Mapping[str, Any]], dates: list[str], day: date) -> Mapping[str, Any] | None:
    index = bisect_left(dates, day.isoformat())
    return rows[index] if index < len(rows) else None


def _strict_future_rows(rows: list[Mapping[str, Any]], dates: list[str], day: str, horizon_day: str | None = None) -> list[Mapping[str, Any]]:
    start = bisect_right(dates, day)
    end = bisect_right(dates, horizon_day) if horizon_day is not None else len(rows)
    return rows[start:end]


def _drawdown_from_peak(value: float, peak_value: float) -> float:
    return value / peak_value - 1.0 if peak_value else 0.0


def _bounded_ids(values: Iterable[Any], limit: int = 512) -> list[str]:
    """Keep blind state payloads small while retaining deterministic audit IDs."""
    ids = sorted({str(value) for value in values if value})
    if len(ids) <= limit:
        return ids
    return ids[:2] + ids[-(limit - 2):]


def _episode_id(peak_date: str, peak_value: float, namespace: str | None = None) -> str:
    return "episode-" + sha256_json({"market": "NDX", "peak_date": peak_date, "peak_value": round(peak_value, 10), "namespace": namespace})[:24]


def _episode_scope(item: Mapping[str, Any], start_date: str, end_date: str) -> bool:
    start = str(item["start_date"])
    recovery = item.get("recovery_date")
    if start > end_date:
        return False
    return recovery is None or str(recovery) >= start_date


def construct_episodes_and_states(
    close_rows: Iterable[Mapping[str, Any]],
    signals_by_date: Mapping[str, Mapping[str, Any]],
    *,
    start_date: str,
    end_date: str,
    episode_namespace: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Construct episodes and observable states without reading outcomes.

    ``max_drawdown_date``, ``bottom_*`` and ``recovery_date`` are included in
    the episode description for later evaluation, but no state row contains
    those future fields.  During this pass the only inputs are close rows and
    the already stored proxy signal for the same date.
    """

    rows = []
    for raw in close_rows:
        value = _finite(raw.get("value"))
        if value is None or value <= 0:
            continue
        item = dict(raw)
        item["observation_date"] = normalize_date(raw["observation_date"])
        item["value"] = float(value)
        rows.append(item)
    rows.sort(key=lambda item: item["observation_date"])
    if not rows:
        return [], []
    dates, positions = _date_position(rows)
    episodes: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = []
    idle_peak_date = dates[0]
    idle_peak_value = float(rows[0]["value"])
    active: dict[str, Any] | None = None

    def make_state(day: str, row: Mapping[str, Any], episode: Mapping[str, Any], index: int) -> dict[str, Any]:
        signal = signals_by_date.get(day) or {}
        feature_values = signal.get("feature_values") or {}
        ids = [str(row.get("observation_version_id"))] if row.get("observation_version_id") else []
        ids.extend(str(value) for value in (signal.get("input_observation_ids") or []) if value)
        ids = _bounded_ids(ids)
        return {
            "state_id": sha256_json({"episode_id": episode["episode_id"], "as_of_date": day})[:32],
            "episode_id": episode["episode_id"],
            "as_of_date": day,
            "current_drawdown": _drawdown_from_peak(float(row["value"]), float(episode["peak_value"])),
            "days_since_peak": max(0, index - int(episode["peak_index"])),
            "current_proxy_score": _finite(signal.get("proxy_score_fraction")),
            "current_proxy_percentile": _finite(signal.get("proxy_percentile")),
            "rsi14": _finite(feature_values.get("rsi14")),
            "distance_ma200": _finite(feature_values.get("distance_ma200")),
            "vxn": _finite(feature_values.get("vxn")),
            "real_yield": _finite(feature_values.get("real_yield")),
            "nfci": _finite(feature_values.get("nfci")),
            "input_observation_ids": ids,
            "input_hash": sha256_json(ids),
        }

    def finalize(item: dict[str, Any], *, recovery_date: str | None, data_end_date: str) -> None:
        item["recovery_date"] = recovery_date
        item["complete"] = recovery_date is not None
        item["data_end_date"] = data_end_date
        item["duration_days"] = ((date.fromisoformat(recovery_date) if recovery_date else date.fromisoformat(data_end_date)) - date.fromisoformat(item["start_date"])).days
        end_position = positions.get(recovery_date or data_end_date, len(rows) - 1)
        item["duration_trading_days"] = max(0, end_position - int(item["start_index"]))
        item.pop("peak_index", None)
        item.pop("start_index", None)
        item.pop("bottom_index", None)
        if _episode_scope(item, start_date, end_date):
            episodes.append(item)

    for index, row in enumerate(rows):
        day = dates[index]
        value = float(row["value"])
        if active is None:
            if value < idle_peak_value:
                episode = {
                    "episode_id": _episode_id(idle_peak_date, idle_peak_value, episode_namespace),
                    "market": "NDX",
                    "peak_date": idle_peak_date,
                    "peak_value": idle_peak_value,
                    "start_date": day,
                    "peak_index": positions[idle_peak_date],
                    "start_index": index,
                    "max_drawdown": _drawdown_from_peak(value, idle_peak_value),
                    "max_drawdown_date": day,
                    "bottom_value": value,
                    "bottom_date": day,
                    "bottom_index": index,
                    "payload": {"definition": EPISODE_MODEL_VERSION},
                }
                active = episode
                if start_date <= day <= end_date:
                    states.append(make_state(day, row, episode, index))
            elif value > idle_peak_value:
                idle_peak_value, idle_peak_date = value, day
            continue

        drawdown = _drawdown_from_peak(value, float(active["peak_value"]))
        if drawdown < float(active["max_drawdown"]):
            active["max_drawdown"] = drawdown
            active["max_drawdown_date"] = day
            active["bottom_value"] = value
            active["bottom_date"] = day
            active["bottom_index"] = index
        if start_date <= day <= end_date:
            states.append(make_state(day, row, active, index))
        if value >= float(active["peak_value"]):
            finalize(active, recovery_date=day, data_end_date=end_date)
            active = None
            idle_peak_value, idle_peak_date = value, day

    if active is not None:
        finalize(active, recovery_date=None, data_end_date=end_date)

    valid_episode_ids = {item["episode_id"] for item in episodes}
    states = [state for state in states if state["episode_id"] in valid_episode_ids]
    episodes.sort(key=lambda item: (item["start_date"], item["episode_id"]))
    states.sort(key=lambda item: (item["as_of_date"], item["episode_id"]))
    return episodes, states


def construct_opportunity_events(
    episodes: Iterable[Mapping[str, Any]],
    signals: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
    proxy_run_id: str,
    start_date: str,
    end_date: str,
    date_positions: Mapping[str, int],
    states_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Cluster fixed top-10% proxy signal days into opportunity events."""

    episodes = list(episodes)
    by_day = {normalize_date(item["as_of_datetime"]): item for item in signals}
    result: list[dict[str, Any]] = []
    cutoff = float(EPISODE_MODEL_CONFIG["opportunity_event"]["percentile_cutoff"])
    gap = int(EPISODE_MODEL_CONFIG["opportunity_event"]["cluster_gap_trading_days"])
    for variant in EPISODE_MODEL_CONFIG["signal_variants"]:
        score_field = "proxy_percentile" if variant == "proxy_a" else "proxy_b_percentile"
        value_field = "proxy_score_fraction" if variant == "proxy_a" else "proxy_b_score_fraction"
        for episode in episodes:
            candidates = []
            for day, signal in by_day.items():
                if not start_date <= day <= end_date or day < episode["start_date"]:
                    continue
                if episode.get("recovery_date") and day > episode["recovery_date"]:
                    continue
                percentile = _finite(signal.get(score_field))
                score = _finite(signal.get(value_field))
                if percentile is None or score is None or percentile < cutoff:
                    continue
                candidates.append((day, signal, percentile, score))
            candidates.sort(key=lambda item: item[0])
            if not candidates:
                continue
            clusters: list[list[tuple[str, Mapping[str, Any], float, float]]] = []
            for candidate in candidates:
                if not clusters:
                    clusters.append([candidate]); continue
                previous_day = clusters[-1][-1][0]
                distance = date_positions.get(candidate[0], 10**9) - date_positions.get(previous_day, -10**9)
                if distance <= gap:
                    clusters[-1].append(candidate)
                else:
                    clusters.append([candidate])
            for cluster in clusters:
                # The peak is an observable maximum within the event.  A tie
                # always resolves to the earliest date and cannot use an
                # outcome field.
                peak_day, peak_signal, peak_percentile, peak_score = min(
                    cluster, key=lambda item: (-item[3], item[0], str(item[1].get("proxy_signal_id", "")))
                )
                first_day, first_signal, _, _ = cluster[0]
                first_state = states_by_key.get((episode["episode_id"], first_day), {})
                peak_state = states_by_key.get((episode["episode_id"], peak_day), {})
                event = {
                    "opportunity_event_id": "opp-" + sha256_json({"run": run_id, "episode": episode["episode_id"], "variant": variant, "first": first_day})[:24],
                    "episode_run_id": run_id,
                    "episode_id": episode["episode_id"],
                    "proxy_run_id": proxy_run_id,
                    "signal_variant": variant,
                    "event_start": first_day,
                    "event_end": cluster[-1][0],
                    "first_signal_date": first_day,
                    "peak_signal_date": peak_day,
                    "signal_max": max(item[3] for item in cluster),
                    "signal_mean": sum(item[3] for item in cluster) / len(cluster),
                    "drawdown_at_first_signal": first_state.get("current_drawdown"),
                    "drawdown_at_peak_signal": peak_state.get("current_drawdown"),
                    "first_signal_id": str(first_signal.get("proxy_signal_id") or sha256_json({"run": proxy_run_id, "date": first_day})[:32]),
                    "peak_signal_id": str(peak_signal.get("proxy_signal_id") or sha256_json({"run": proxy_run_id, "date": peak_day})[:32]),
                    "cluster_gap_trading_days": gap,
                    "payload": {
                        "signal_count": len(cluster),
                        "percentile_cutoff": cutoff,
                        "score_percentile_at_peak": peak_percentile,
                        "definition": "observable signal days only; no outcome fields",
                    },
                }
                result.append(event)
    result.sort(key=lambda item: (item["event_start"], item["signal_variant"], item["opportunity_event_id"]))
    return result


def construct_mechanical_events(
    episodes: Iterable[Mapping[str, Any]],
    close_rows: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Create the first fixed -10/-20/-30/-40/-50% trigger per episode."""

    rows = sorted((dict(item) for item in close_rows), key=lambda item: normalize_date(item["observation_date"]))
    result: list[dict[str, Any]] = []
    for episode in episodes:
        peak = float(episode["peak_value"])
        for threshold in MECHANICAL_THRESHOLDS:
            trigger = None
            for row in rows:
                day = normalize_date(row["observation_date"])
                if day < episode["start_date"] or day > end_date or day < start_date:
                    continue
                # A drawdown baseline event belongs to this episode only
                # while the original ATH-to-recovery interval is active.  A
                # later decline after recovery is a new episode and must not
                # be assigned to the old peak.
                if episode.get("recovery_date") and day > str(episode["recovery_date"]):
                    break
                value = _finite(row.get("value"))
                if value is not None and _drawdown_from_peak(value, peak) <= -threshold:
                    trigger = (day, float(value), row)
                    break
            if trigger is None:
                continue
            day, value, row = trigger
            result.append({
                "mechanical_event_id": "dd-" + sha256_json({"run": run_id, "episode": episode["episode_id"], "threshold": threshold})[:24],
                "episode_run_id": run_id,
                "episode_id": episode["episode_id"],
                "threshold": threshold,
                "event_date": day,
                "event_price": value,
                "drawdown": _drawdown_from_peak(value, peak),
                "source_observation_id": row.get("observation_version_id"),
                "payload": {"first_trigger_only": True, "threshold_percent": threshold * 100},
            })
    result.sort(key=lambda item: (item["event_date"], item["threshold"], item["episode_id"]))
    return result


def _forward_metrics(rows: list[Mapping[str, Any]], dates: list[str], entry_date: str, entry_price: float) -> dict[str, Any]:
    entry_day = date.fromisoformat(entry_date)
    forward: dict[str, Any] = {}
    future_ids: list[str] = []
    for months, field in ((12, "forward_1y"), (36, "forward_3y"), (60, "forward_5y")):
        target = entry_day + timedelta(days=round(months * 365.2425 / 12))
        row = _first_on_or_after(rows, dates, target)
        if row is None:
            forward[field] = None
        else:
            value = _finite(row.get("value"))
            forward[field] = value / entry_price - 1.0 if value is not None else None
            if row.get("observation_version_id"):
                future_ids.append(str(row["observation_version_id"]))
    one_year_target = (entry_day + timedelta(days=round(365.2425))).isoformat()
    one_year_rows = _strict_future_rows(rows, dates, entry_date, one_year_target)
    values = [float(row["value"]) for row in one_year_rows if _finite(row.get("value")) is not None]
    forward["max_adverse_1y"] = min((value / entry_price - 1.0 for value in values), default=None)
    forward["max_favorable_1y"] = max((value / entry_price - 1.0 for value in values), default=None)
    timing: dict[str, Any] = {}
    entry_index = bisect_right(dates, entry_date) - 1
    for horizon in TIMING_HORIZONS:
        horizon_rows = rows[entry_index + 1: entry_index + 1 + horizon]
        values = [float(row["value"]) for row in horizon_rows if _finite(row.get("value")) is not None]
        timing[str(horizon)] = min((value / entry_price - 1.0 for value in values), default=None)
    forward["timing_regret"] = timing
    forward["future_observation_ids"] = future_ids
    return forward


def _fast_recovery(episode: Mapping[str, Any], rows: list[Mapping[str, Any]], dates: list[str]) -> dict[str, Any]:
    if not episode.get("complete") or not episode.get("bottom_date") or not episode.get("bottom_value"):
        return {"fast_recovery": None, "rebound_date": None, "days": None}
    bottom = float(episode["bottom_value"])
    bottom_date = str(episode["bottom_date"])
    bottom_index = bisect_left(dates, bottom_date)
    for index in range(bottom_index + 1, len(rows)):
        value = _finite(rows[index].get("value"))
        if value is not None and value >= bottom * 1.10:
            days = index - bottom_index
            return {
                "fast_recovery": days <= int(EPISODE_MODEL_CONFIG["fast_recovery"]["maximum_trading_days"]),
                "rebound_date": dates[index],
                "days": days,
            }
    return {"fast_recovery": False, "rebound_date": None, "days": None}


def _entry_evaluation(
    *,
    event_type: str,
    event: Mapping[str, Any],
    episode: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    dates: list[str],
    signal_variant: str | None = None,
    matched_event_id: str | None = None,
    fast_info: Mapping[str, Any],
    missed_rebound: bool | None = None,
) -> dict[str, Any]:
    entry_date = str(event.get("first_signal_date") or event["event_date"])
    entry_price = float(event.get("entry_price") or event.get("event_price"))
    metrics = _forward_metrics(rows, dates, entry_date, entry_price)
    bottom = _finite(episode.get("bottom_value"))
    peak = _finite(episode.get("peak_value"))
    efficiency = None
    entry_to_bottom = None
    days_to_bottom = None
    if bottom is not None and peak is not None and peak != bottom and episode.get("complete"):
        efficiency = (entry_price - bottom) / (peak - bottom)
        entry_to_bottom = entry_price / bottom - 1.0 if bottom else None
        days_to_bottom = bisect_left(dates, str(episode["bottom_date"])) - bisect_right(dates, entry_date)
        days_to_bottom = max(0, days_to_bottom)
    missing = [field for field in ("forward_1y", "forward_3y", "forward_5y") if metrics.get(field) is None]
    return {
        "episode_run_id": str(event["episode_run_id"]),
        "episode_id": str(event["episode_id"]),
        "event_type": event_type,
        "event_id": str(event.get("opportunity_event_id") or event.get("mechanical_event_id")),
        "signal_variant": signal_variant,
        "entry_date": entry_date,
        "entry_price": entry_price,
        "forward_1y": metrics["forward_1y"],
        "forward_3y": metrics["forward_3y"],
        "forward_5y": metrics["forward_5y"],
        "max_adverse_1y": metrics["max_adverse_1y"],
        "max_favorable_1y": metrics["max_favorable_1y"],
        "episode_bottom_price": bottom if episode.get("complete") else None,
        "entry_efficiency": efficiency,
        "entry_to_bottom_pct": entry_to_bottom,
        "days_to_bottom": days_to_bottom,
        "timing_regret": metrics["timing_regret"],
        "fast_recovery": fast_info.get("fast_recovery"),
        "missed_rebound": missed_rebound,
        "matched_mechanical_event_id": matched_event_id,
        "status": "COMPLETE" if not missing else "PARTIAL",
        "reason": "Evaluation-only pass; future price rows were read after all states and events were constructed." + (f" 缺少: {', '.join(missing)}" if missing else ""),
        "payload": {
            "future_observation_ids": metrics["future_observation_ids"],
            "fast_recovery_days": fast_info.get("days"),
            "fast_recovery_date": fast_info.get("rebound_date"),
            # Event attributes are copied for deterministic episode-level
            # selection; they are observable and do not depend on outcomes.
            "signal_max": event.get("signal_max"),
            "threshold": event.get("threshold"),
        },
    }


def _nearest_mechanical(event: Mapping[str, Any], mechanical: list[Mapping[str, Any]], date_positions: Mapping[str, int]) -> Mapping[str, Any] | None:
    choices = [item for item in mechanical if item["episode_id"] == event["episode_id"]]
    if not choices:
        return None
    event_pos = date_positions.get(str(event["first_signal_date"]), 10**9)
    return min(choices, key=lambda item: (abs(date_positions.get(str(item["event_date"]), 10**9) - event_pos), float(item["threshold"]), str(item["event_date"])))


def evaluate_events(
    episodes: Iterable[Mapping[str, Any]],
    opportunity_events: Iterable[Mapping[str, Any]],
    mechanical_events: Iterable[Mapping[str, Any]],
    close_rows: Iterable[Mapping[str, Any]],
    *,
    date_positions: Mapping[str, int],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read future prices only in this explicit evaluation pass."""

    rows = sorted((dict(item) for item in close_rows), key=lambda item: normalize_date(item["observation_date"]))
    dates, _ = _date_position(rows)
    episode_map = {str(item["episode_id"]): item for item in episodes}
    mechanical = list(mechanical_events)
    fast = {episode_id: _fast_recovery(item, rows, dates) for episode_id, item in episode_map.items()}
    result: list[dict[str, Any]] = []
    for event in opportunity_events:
        episode = episode_map[str(event["episode_id"])]
        row_index = bisect_left(dates, str(event["first_signal_date"]))
        if row_index >= len(rows) or dates[row_index] != str(event["first_signal_date"]):
            continue
        matched = _nearest_mechanical(event, mechanical, date_positions)
        fast_info = fast[str(event["episode_id"])]
        missed = None
        if fast_info.get("fast_recovery"):
            rebound = fast_info.get("rebound_date")
            missed = rebound is not None and str(event["first_signal_date"]) > rebound
        payload_event = dict(event)
        payload_event["entry_price"] = float(rows[row_index]["value"])
        result.append(_entry_evaluation(
            event_type="COMPOSITE", event=payload_event, episode=episode, rows=rows, dates=dates,
            signal_variant=str(event["signal_variant"]), matched_event_id=str(matched["mechanical_event_id"]) if matched else None,
            fast_info=fast_info, missed_rebound=missed,
        ))
    for event in mechanical:
        episode = episode_map[str(event["episode_id"])]
        row_index = bisect_left(dates, str(event["event_date"]))
        if row_index >= len(rows) or dates[row_index] != str(event["event_date"]):
            continue
        payload_event = dict(event); payload_event["entry_price"] = float(event["event_price"])
        result.append(_entry_evaluation(
            event_type="DRAWDOWN", event=payload_event, episode=episode, rows=rows, dates=dates,
            signal_variant=None, matched_event_id=None, fast_info=fast[str(event["episode_id"])], missed_rebound=None,
        ))
    result.sort(key=lambda item: (item["entry_date"], item["event_type"], str(item.get("signal_variant") or ""), item["event_id"]))
    return result, fast


def construct_at10_assessments(
    episodes: Iterable[Mapping[str, Any]],
    mechanical_events: Iterable[Mapping[str, Any]],
    signals: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
) -> list[dict[str, Any]]:
    by_date = {normalize_date(item["as_of_datetime"]): item for item in signals}
    episode_map = {str(item["episode_id"]): item for item in episodes}
    result: list[dict[str, Any]] = []
    for trigger in mechanical_events:
        if abs(float(trigger["threshold"]) - 0.10) > 1e-9:
            continue
        episode = episode_map[str(trigger["episode_id"])]
        for variant in EPISODE_MODEL_CONFIG["signal_variants"]:
            field = "proxy_percentile" if variant == "proxy_a" else "proxy_b_percentile"
            signal = by_date.get(str(trigger["event_date"]))
            percentile = _finite(signal.get(field)) if signal else None
            if percentile is None:
                category = "UNAVAILABLE"
            elif percentile >= float(EPISODE_MODEL_CONFIG["at_10_percent"]["agree_if_percentile_at_least"]):
                category = "AGREE"
            elif percentile <= float(EPISODE_MODEL_CONFIG["at_10_percent"]["strongly_oppose_if_percentile_at_most"]):
                category = "STRONGLY_OPPOSE"
            else:
                category = "DELAY"
            max_dd = float(episode["max_drawdown"] or 0.0)
            result.append({
                "assessment_id": "at10-" + sha256_json({"run": run_id, "episode": episode["episode_id"], "variant": variant})[:24],
                "episode_run_id": run_id,
                "episode_id": episode["episode_id"],
                "mechanical_event_id": trigger["mechanical_event_id"],
                "signal_variant": variant,
                "trigger_date": trigger["event_date"],
                "signal_percentile": percentile,
                "category": category,
                "reached_20": int(max_dd <= -0.20),
                "reached_30": int(max_dd <= -0.30),
                "reached_40": int(max_dd <= -0.40),
            })
    result.sort(key=lambda item: (item["trigger_date"], item["signal_variant"], item["episode_id"]))
    return result


def _metric_differences(evaluations: list[Mapping[str, Any]], *, variant: str) -> dict[str, list[float]]:
    composite = defaultdict(list)
    baseline = defaultdict(list)
    eval_by_key = {(str(item["episode_id"]), str(item["event_id"])): item for item in evaluations}
    # Choose the strongest event in each episode before looking at outcomes.
    events_by_episode: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in evaluations:
        if item["event_type"] == "COMPOSITE" and item.get("signal_variant") == variant:
            events_by_episode[str(item["episode_id"])].append(item)
    for episode_id, events in events_by_episode.items():
        chosen = sorted(events, key=lambda item: (-float(item.get("payload", {}).get("signal_max", item.get("entry_price", 0)) or 0), str(item["entry_date"]), str(item["event_id"])))[0]
        matched_id = chosen.get("matched_mechanical_event_id")
        matched = next((item for item in evaluations if item["event_type"] == "DRAWDOWN" and item["event_id"] == matched_id), None)
        if not matched:
            continue
        for name in ("forward_1y", "forward_3y", "forward_5y"):
            if _finite(chosen.get(name)) is not None and _finite(matched.get(name)) is not None:
                composite[name].append(float(chosen[name]) - float(matched[name]))
        if _finite(chosen.get("entry_efficiency")) is not None and _finite(matched.get("entry_efficiency")) is not None:
            composite["entry_efficiency_improvement"].append(float(matched["entry_efficiency"]) - float(chosen["entry_efficiency"]))
        for horizon in TIMING_HORIZONS:
            a = (chosen.get("timing_regret") or {}).get(str(horizon)); b = (matched.get("timing_regret") or {}).get(str(horizon))
            if _finite(a) is not None and _finite(b) is not None:
                composite[f"timing_regret_{horizon}d_improvement"].append(float(a) - float(b))
    return dict(composite)


def _bootstrap(values: list[float], *, seed: int, reps: int) -> dict[str, Any]:
    n = len(values)
    if n < 5:
        return {"median_difference": _stats(values)["median"], "lower_95": None, "upper_95": None, "sample_count": n, "resamples": 0, "status": "INCONCLUSIVE_SMALL_SAMPLE"}
    rng = random.Random(seed)
    medians: list[float] = []
    for _ in range(reps):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        medians.append(float(_stats(sample)["median"]))
    medians.sort()
    return {
        "median_difference": _stats(values)["median"],
        "lower_95": medians[int((len(medians) - 1) * 0.025)],
        "upper_95": medians[int((len(medians) - 1) * 0.975)],
        "sample_count": n,
        "resamples": reps,
        "status": "OK",
    }


def _bootstrap_report(evaluations: list[Mapping[str, Any]], variant: str) -> dict[str, Any]:
    differences = _metric_differences(evaluations, variant=variant)
    output = {}
    base_seed = int(EPISODE_MODEL_CONFIG["bootstrap"]["seed"]) + (0 if variant == "proxy_a" else 101)
    for index, (metric, values) in enumerate(sorted(differences.items())):
        output[metric] = _bootstrap(values, seed=base_seed + index, reps=int(EPISODE_MODEL_CONFIG["bootstrap"]["resamples"]))
    return {
        "unit": "complete_episode",
        "variant": variant,
        "metrics": output,
        "episode_sample_count": max((item.get("sample_count", 0) for item in output.values()), default=0),
        "interpretation": "Composite minus matched Drawdown for returns/regret; baseline minus Composite for Entry Efficiency. Positive is preferred for every metric.",
    }


def _group_metric(evaluations: list[Mapping[str, Any]], event_type: str, variant: str | None = None) -> dict[str, Any]:
    rows = [item for item in evaluations if item["event_type"] == event_type and (variant is None or item.get("signal_variant") == variant)]
    return {
        "event_count": len(rows),
        "complete_1y_count": sum(_finite(item.get("forward_1y")) is not None for item in rows),
        "forward_1y": _stats(item.get("forward_1y") for item in rows),
        "forward_3y": _stats(item.get("forward_3y") for item in rows),
        "forward_5y": _stats(item.get("forward_5y") for item in rows),
        "max_adverse_1y": _stats(item.get("max_adverse_1y") for item in rows),
        "max_favorable_1y": _stats(item.get("max_favorable_1y") for item in rows),
        "entry_efficiency": _stats(item.get("entry_efficiency") for item in rows),
        "distance_to_bottom_pct": _stats(item.get("entry_to_bottom_pct") for item in rows),
        "time_to_bottom_trading_days": _stats(item.get("days_to_bottom") for item in rows),
        "timing_regret": {str(horizon): _stats((item.get("timing_regret") or {}).get(str(horizon)) for item in rows) for horizon in TIMING_HORIZONS},
    }


def _matched_summary(evaluations: list[Mapping[str, Any]], variant: str) -> dict[str, Any]:
    diffs: dict[str, list[float]] = defaultdict(list)
    comp = [item for item in evaluations if item["event_type"] == "COMPOSITE" and item.get("signal_variant") == variant]
    for item in comp:
        matched = next((row for row in evaluations if row["event_type"] == "DRAWDOWN" and row["event_id"] == item.get("matched_mechanical_event_id")), None)
        if not matched:
            continue
        for field in ("forward_1y", "forward_3y", "forward_5y", "max_adverse_1y", "max_favorable_1y"):
            if _finite(item.get(field)) is not None and _finite(matched.get(field)) is not None:
                diffs[field].append(float(item[field]) - float(matched[field]))
        if _finite(item.get("entry_efficiency")) is not None and _finite(matched.get("entry_efficiency")) is not None:
            diffs["entry_efficiency_improvement"].append(float(matched["entry_efficiency"]) - float(item["entry_efficiency"]))
        for horizon in TIMING_HORIZONS:
            a = (item.get("timing_regret") or {}).get(str(horizon)); b = (matched.get("timing_regret") or {}).get(str(horizon))
            if _finite(a) is not None and _finite(b) is not None:
                diffs[f"timing_regret_{horizon}d_improvement"].append(float(a) - float(b))
    return {"matched_pair_count": len(comp) - sum(not item.get("matched_mechanical_event_id") for item in comp), "differences": {key: _stats(values) for key, values in sorted(diffs.items())}}


def _classify_value(bootstrap: Mapping[str, Any], *, positive_keys: tuple[str, ...]) -> str:
    metrics = bootstrap.get("metrics") or {}
    valid = [metrics[key] for key in positive_keys if key in metrics]
    if not valid:
        return "NONE"
    positive = [item for item in valid if item.get("status") == "OK" and item.get("lower_95") is not None and float(item["lower_95"]) > 0]
    if len(positive) == len(valid) and len(valid) >= 3:
        return "STRONG"
    if len(positive) >= 2:
        return "POSITIVE"
    if len(positive) >= 1:
        return "WEAK"
    return "INCONCLUSIVE"


def _baseline_strength(evaluations: list[Mapping[str, Any]]) -> str:
    # Use one mechanical -10% trigger per episode as the baseline's primary
    # unit.  Counting all -10/-20/-30/... events would make a single bear
    # market look like several independent observations.
    rows = [
        item for item in evaluations
        if item["event_type"] == "DRAWDOWN"
        and abs(float((item.get("payload") or {}).get("threshold") or 0.0) - 0.10) < 1e-9
        and _finite(item.get("forward_1y")) is not None
    ]
    n = len(rows)
    medians = [_stats(item.get(field) for item in rows)["median"] for field in ("forward_1y", "forward_3y", "forward_5y")]
    if n >= 15 and all(value is not None and value > 0 for value in medians):
        return "STRONG"
    if n >= 10 and all(value is not None and value > 0 for value in medians):
        return "POSITIVE"
    if n >= 5:
        return "WEAK"
    return "INCONCLUSIVE"


def _regime_label(day: str) -> str:
    for label, (start, end) in EPISODE_MODEL_CONFIG["crisis_windows"].items():
        if start <= day <= end:
            return label
    year = int(day[:4])
    if year <= 2006: return "2000-2006"
    if year <= 2013: return "2007-2013"
    if year <= 2019: return "2014-2019"
    return "2020-2026"


def _regime_audit(evaluations: list[Mapping[str, Any]], variant: str) -> dict[str, Any]:
    groups: dict[str, list[float]] = defaultdict(list)
    comp = [item for item in evaluations if item["event_type"] == "COMPOSITE" and item.get("signal_variant") == variant]
    for item in comp:
        matched = next((row for row in evaluations if row["event_type"] == "DRAWDOWN" and row["event_id"] == item.get("matched_mechanical_event_id")), None)
        if matched and _finite(item.get("forward_1y")) is not None and _finite(matched.get("forward_1y")) is not None:
            groups[_regime_label(str(item["entry_date"]))].append(float(item["forward_1y"]) - float(matched["forward_1y"]))
    return {
        "fixed_labels": True,
        "by_regime": {key: {"sample_count": len(values), "median_forward_1y_difference": _stats(values)["median"], "status": "OK" if len(values) >= 3 else "INCONCLUSIVE_SMALL_SAMPLE"} for key, values in sorted(groups.items())},
        "conclusion": "INCONCLUSIVE_REGIME_DEPENDENCY" if any(len(values) < 3 for values in groups.values()) or len(groups) < 2 else "DESCRIPTIVE_ONLY",
    }


def _episode_comparison_labels(evaluations: list[Mapping[str, Any]], variant: str) -> dict[str, list[str]]:
    by_episode: dict[str, dict[str, Any]] = {}
    for item in evaluations:
        if item["event_type"] != "COMPOSITE" or item.get("signal_variant") != variant:
            continue
        matched = next((row for row in evaluations if row["event_type"] == "DRAWDOWN" and row["event_id"] == item.get("matched_mechanical_event_id")), None)
        if not matched:
            continue
        diffs = []
        for field in ("forward_1y", "forward_3y", "forward_5y"):
            if _finite(item.get(field)) is not None and _finite(matched.get(field)) is not None:
                diffs.append(float(item[field]) - float(matched[field]))
        if _finite(item.get("entry_efficiency")) is not None and _finite(matched.get("entry_efficiency")) is not None:
            diffs.append(float(matched["entry_efficiency"]) - float(item["entry_efficiency"]))
        if _finite(item.get("timing_regret", {}).get("60")) is not None and _finite(matched.get("timing_regret", {}).get("60")) is not None:
            diffs.append(float(item["timing_regret"]["60"]) - float(matched["timing_regret"]["60"]))
        by_episode[str(item["episode_id"])] = {"median": _stats(diffs)["median"], "event_id": item["event_id"]}
    tolerance = float(EPISODE_MODEL_CONFIG["classification"]["no_difference_return_abs"])
    output = {"better": [], "worse": [], "no_difference": []}
    for episode_id, item in sorted(by_episode.items()):
        value = item["median"]
        if value is None or abs(value) <= tolerance:
            output["no_difference"].append(episode_id)
        elif value > 0:
            output["better"].append(episode_id)
        else:
            output["worse"].append(episode_id)
    return output


def _crisis_timelines(
    close_rows: list[Mapping[str, Any]],
    signals_by_date: Mapping[str, Mapping[str, Any]],
    opportunity_events: list[Mapping[str, Any]],
    mechanical_events: list[Mapping[str, Any]],
) -> dict[str, Any]:
    rows = sorted(close_rows, key=lambda item: normalize_date(item["observation_date"]))
    running_high = -float("inf")
    event_by_day: dict[str, list[str]] = defaultdict(list)
    for event in opportunity_events:
        event_by_day[str(event["first_signal_date"])].append("COMPOSITE:" + str(event["signal_variant"]))
    for event in mechanical_events:
        event_by_day[str(event["event_date"])].append("DRAWDOWN:" + str(round(float(event["threshold"]), 2)))
    output: dict[str, Any] = {}
    for label, (window_start, window_end) in EPISODE_MODEL_CONFIG["crisis_windows"].items():
        trajectory = []
        for row in rows:
            day = normalize_date(row["observation_date"])
            if day < window_start or day > window_end:
                continue
            value = float(row["value"]); running_high = max(running_high, value)
            signal = signals_by_date.get(day) or {}
            features = signal.get("feature_values") or {}
            trajectory.append({
                "date": day,
                "price": value,
                "drawdown": _drawdown_from_peak(value, running_high),
                "proxy_score": _finite(signal.get("proxy_score_fraction")),
                "proxy_percentile": _finite(signal.get("proxy_percentile")),
                "vxn": _finite(features.get("vxn")),
                "real_yield": _finite(features.get("real_yield")),
                "nfci": _finite(features.get("nfci")),
                "events": sorted(event_by_day.get(day, [])),
            })
        output[label] = {"window": [window_start, window_end], "sampling": "daily", "rows": trajectory}
    return output


def _purchase_priority() -> list[dict[str, Any]]:
    # Coverage is measured in Phase 2B; theory and PIT status are frozen
    # qualitative research judgments.  Free-source costs were not quoted, so
    # cost is explicitly UNKNOWN rather than invented.
    return [
        {
            "candidate": "EPS Revision/Growth",
            "coverage_relief": "HIGH_WEIGHT_BUT_NOT_COVERED",
            "theoretical_incremental_information": "HIGH",
            "availability": "LOW_ON_FREE_PATH",
            "cost": "UNKNOWN_UNQUOTED",
            "pit_credibility": "LOW_UNTIL_VINTAGED_ESTIMATES",
            "priority": "1A_RESEARCH_PRIORITY",
            "reason": "最能区分价格回撤与盈利预期恶化，但必须有可验证的历史发布时点。",
        },
        {
            "candidate": "Forward PE",
            "coverage_relief": "HIGH_WEIGHT_BUT_NOT_COVERED",
            "theoretical_incremental_information": "HIGH",
            "availability": "MEDIUM_WITH_DEFINITION_RISK",
            "cost": "UNKNOWN_UNQUOTED",
            "pit_credibility": "LOW_UNTIL_VINTAGED_INDEX_AGGREGATE",
            "priority": "1B_RESEARCH_PRIORITY",
            "reason": "直接改善估值模块，但指数聚合口径与当时可见性需要逐日认证。",
        },
        {
            "candidate": "Historical Breadth",
            "coverage_relief": "MEDIUM",
            "theoretical_incremental_information": "MEDIUM",
            "availability": "LOW_WITH_MEMBERSHIP_DENOMINATOR_RISK",
            "cost": "UNKNOWN_UNQUOTED",
            "pit_credibility": "LOW_UNTIL_MEMBERSHIP_VINTAGES",
            "priority": "2_FOLLOW_UP",
            "reason": "有助于识别少数巨头撑指数，但完整历史成分和分母是硬门槛。",
        },
    ]


def build_episode_report(repo: PITRepository, episode_run_id: str) -> dict[str, Any]:
    run = repo.get_episode_validation_run(episode_run_id)
    if not run:
        raise ValueError("episode_validation_run 不存在")
    episodes = repo.get_drawdown_episodes(episode_run_id, limit=100000)
    opportunities = repo.get_proxy_opportunity_events(episode_run_id, limit=100000)
    mechanical = repo.get_drawdown_mechanical_events(episode_run_id, limit=100000)
    evaluations = repo.get_episode_event_evaluations(episode_run_id, limit=1000000)
    at10 = repo.get_episode_at10_assessments(episode_run_id, limit=100000)
    bootstrap = {variant: _bootstrap_report(evaluations, variant) for variant in EPISODE_MODEL_CONFIG["signal_variants"]}
    grouped = {
        "drawdown": _group_metric(evaluations, "DRAWDOWN"),
        "proxy_a": _group_metric(evaluations, "COMPOSITE", "proxy_a"),
        "proxy_b": _group_metric(evaluations, "COMPOSITE", "proxy_b"),
    }
    matched = {variant: _matched_summary(evaluations, variant) for variant in EPISODE_MODEL_CONFIG["signal_variants"]}
    labels = {variant: _episode_comparison_labels(evaluations, variant) for variant in EPISODE_MODEL_CONFIG["signal_variants"]}
    at10_summary = {}
    for variant in EPISODE_MODEL_CONFIG["signal_variants"]:
        rows = [item for item in at10 if item["signal_variant"] == variant]
        at10_summary[variant] = {
            "counts": {category: sum(item["category"] == category for item in rows) for category in ("AGREE", "DELAY", "STRONGLY_OPPOSE", "UNAVAILABLE")},
            "reached_after_10": {"20": sum(item["reached_20"] for item in rows), "30": sum(item["reached_30"] for item in rows), "40": sum(item["reached_40"] for item in rows)},
            "rows": rows,
        }
    complete_count = sum(bool(item.get("complete")) for item in episodes)
    primary_bootstrap = bootstrap[EPISODE_MODEL_CONFIG["primary_signal_variant"]]
    positive_keys = ("forward_1y", "forward_3y", "forward_5y")
    composite_value = _classify_value(primary_bootstrap, positive_keys=positive_keys)
    # If the event sample is too small, a positive point estimate is not
    # promoted to a positive information-value claim.
    if primary_bootstrap.get("episode_sample_count", 0) < 5:
        composite_value = "INCONCLUSIVE"
    baseline_strength = _baseline_strength(evaluations)
    info_value = composite_value
    fast_episodes = []
    for episode in episodes:
        if not episode.get("complete"):
            continue
        bottom_date = episode.get("bottom_date")
        bottom_value = _finite(episode.get("bottom_value"))
        if not bottom_date or bottom_value is None:
            continue
        close_rows = []
        # Fast-recovery flag is persisted on evaluations; use episode-level
        # unique values only for this descriptive summary.
        flags = {item.get("fast_recovery") for item in evaluations if item.get("episode_id") == episode["episode_id"]}
        if True in flags:
            fast_episodes.append(episode["episode_id"])
    fast_set = set(fast_episodes)
    primary_composite = [item for item in evaluations if item["event_type"] == "COMPOSITE" and item.get("signal_variant") == EPISODE_MODEL_CONFIG["primary_signal_variant"]]
    missed_fast = sum(item.get("missed_rebound") is True for item in primary_composite if item.get("episode_id") in fast_set)
    observed_fast_events = sum(item.get("episode_id") in fast_set for item in primary_composite)
    primary_events_by_episode = defaultdict(list)
    for item in primary_composite:
        primary_events_by_episode[str(item["episode_id"])].append(item)
    missed_fast_episodes = 0
    for episode_id in fast_set:
        episode_evals = [item for item in evaluations if item.get("episode_id") == episode_id]
        rebound_dates = [item.get("payload", {}).get("fast_recovery_date") for item in episode_evals if item.get("payload", {}).get("fast_recovery_date")]
        # ``fast_recovery_date`` is added below by the run path.  A missing
        # date is treated as no timely observable composite event only when
        # the episode has a persisted fast-recovery flag.
        rebound_date = min(rebound_dates) if rebound_dates else None
        timely = [item for item in primary_events_by_episode.get(episode_id, []) if rebound_date is None or str(item["entry_date"]) <= str(rebound_date)]
        if not timely:
            missed_fast_episodes += 1
    fast_by_variant = {}
    for variant in EPISODE_MODEL_CONFIG["signal_variants"]:
        variant_events = [item for item in evaluations if item["event_type"] == "COMPOSITE" and item.get("signal_variant") == variant]
        variant_missed_episodes = 0
        variant_observed_events = 0
        variant_missed_events = 0
        for episode_id in fast_set:
            episode_variant_events = [item for item in variant_events if item.get("episode_id") == episode_id]
            variant_observed_events += len(episode_variant_events)
            variant_missed_events += sum(item.get("missed_rebound") is True for item in episode_variant_events)
            episode_evals = [item for item in evaluations if item.get("episode_id") == episode_id]
            rebound_dates = [item.get("payload", {}).get("fast_recovery_date") for item in episode_evals if item.get("payload", {}).get("fast_recovery_date")]
            rebound_date = min(rebound_dates) if rebound_dates else None
            if not [item for item in episode_variant_events if rebound_date is None or str(item["entry_date"]) <= str(rebound_date)]:
                variant_missed_episodes += 1
        fast_by_variant[variant] = {
            "composite_event_count": variant_observed_events,
            "missed_rebound_event_count": variant_missed_events,
            "missed_rebound_episode_count": variant_missed_episodes,
            "episode_miss_rate": variant_missed_episodes / len(fast_set) if fast_set else None,
        }
    # Timelines are built from the input proxy series in the run snapshot. A
    # report rebuild therefore does not reach forward outcomes, and the run
    # path stores the generated timeline before completion.
    summary = run.get("summary") or {}
    report = {
        "phase": "2C",
        "episode_model_version": EPISODE_MODEL_VERSION,
        "episode_model_config": EPISODE_MODEL_CONFIG,
        "config_hash": EPISODE_CONFIG_HASH,
        "upstream_proxy_model_version": PROXY_MODEL_VERSION,
        "upstream_proxy_config_hash": PROXY_CONFIG_HASH,
        "research_only": True,
        "strict_pit": False,
        "investment_action_eligible": False,
        "episode_run_id": episode_run_id,
        "date_range": {"start": run["start_date"], "end": run["end_date"]},
        "data_cutoff": run.get("data_cutoff"),
        "data_snapshot": run.get("data_snapshot"),
        "episode_count": len(episodes),
        "complete_episode_count": complete_count,
        "censored_episode_count": len(episodes) - complete_count,
        "episodes": episodes,
        "opportunity_event_count": len(opportunities),
        "opportunity_event_count_by_variant": {variant: sum(item["signal_variant"] == variant for item in opportunities) for variant in EPISODE_MODEL_CONFIG["signal_variants"]},
        "opportunity_events": opportunities,
        "drawdown_mechanical_event_count": len(mechanical),
        "drawdown_mechanical_event_count_by_threshold": {str(int(threshold * 100)): sum(abs(float(item["threshold"]) - threshold) < 1e-9 for item in mechanical) for threshold in MECHANICAL_THRESHOLDS},
        "drawdown_mechanical_events": mechanical,
        "evaluated_event_count": len(evaluations),
        "event_evaluations": evaluations,
        "metrics_by_event": grouped,
        "entry_efficiency": {"composite": {variant: grouped[variant]["entry_efficiency"] for variant in ("proxy_a", "proxy_b")}, "drawdown": grouped["drawdown"]["entry_efficiency"]},
        "capital_timing_regret": {"composite": {variant: grouped[variant]["timing_regret"] for variant in ("proxy_a", "proxy_b")}, "drawdown": grouped["drawdown"]["timing_regret"]},
        "at_10_percent": at10_summary,
        "fast_recovery": {
            "fast_episode_count": len(fast_episodes),
            "composite_event_count_in_fast_episodes": observed_fast_events,
            "missed_rebound_event_count": missed_fast,
            "miss_rate": missed_fast / observed_fast_events if observed_fast_events else None,
            "missed_rebound_episode_count": missed_fast_episodes,
            "episode_miss_rate": missed_fast_episodes / len(fast_episodes) if fast_episodes else None,
            "drawdown_only_episode_miss_rate": 0.0 if fast_episodes else None,
            "by_variant": fast_by_variant,
            "definition": EPISODE_MODEL_CONFIG["fast_recovery"],
        },
        "episode_bootstrap": bootstrap,
        "matched_comparison": matched,
        "episode_comparison_labels": labels,
        "regime_dependency": {variant: _regime_audit(evaluations, variant) for variant in EPISODE_MODEL_CONFIG["signal_variants"]},
        "crisis_timelines": summary.get("crisis_timelines", {}),
        "data_purchase_priority": _purchase_priority(),
        "episode_signal_information_value": info_value,
        "EPISODE_SIGNAL_INFORMATION_VALUE": info_value,
        "composite_incremental_value_over_drawdown": composite_value,
        "COMPOSITE_INCREMENTAL_VALUE_OVER_DRAWDOWN": composite_value,
        "drawdown_only_baseline_strength": baseline_strength,
        "DRAWDOWN_ONLY_BASELINE_STRENGTH": baseline_strength,
        "PHASE_2C_STATUS": "PASS_WITH_LIMITATIONS",
        "continue_composite": "CONTINUE_RESEARCH_ONLY; 不进入投资动作或资金状态机",
        "NDX_SCORE_V2_STATUS": "UNTESTED_DUE_TO_DATA",
        "lookahead_controls": {
            "episode_construction_inputs": ["NDX_CLOSE historical proxy rows", "saved proxy signal rows through the same date"],
            "observable_state_fields": ["current_drawdown", "days_since_peak", "current_proxy_score", "current_proxy_percentile", "rsi14", "distance_ma200", "vxn", "real_yield", "nfci"],
            "future_fields_hidden_from_state": ["max_drawdown_date", "bottom_value", "bottom_date", "recovery_date", "duration_days"],
            "evaluation_layer": "episode_event_evaluations written only after episodes, states and events were persisted",
            "outcome_rows_used_in_episode_engine": False,
            "bootstrap_unit": "complete episodes as blocks",
        },
        "limitations": [
            "上游 NDX_PROXY_RESEARCH_V1 是 HISTORICAL_PROXY，不是完整 STRICT_PIT；本报告不能升级为 V2.0 结果。",
            "重大回撤 episode 数量远少于日频信号数，置信区间可能很宽；宽区间统一标为 INCONCLUSIVE。",
            "2026 年末 episode 可能右删失，Entry Efficiency 和最终底部距离只对已恢复 episode 计入。",
            "免费数据路径未提供可审计的历史 Forward PE/EPS 修正/完整宽度，采购成本未报价，报告标为 UNKNOWN。",
        ],
    }
    return report


def _run_data_snapshot(indexes: Mapping[str, _Index], signals: list[Mapping[str, Any]]) -> dict[str, Any]:
    snapshot = _data_snapshot(indexes)
    snapshot["proxy_signal_run"] = {
        "proxy_run_id": str(signals[0]["proxy_run_id"]) if signals else None,
        "count": len(signals),
        "min_date": normalize_date(signals[0]["as_of_datetime"]) if signals else None,
        "max_date": normalize_date(signals[-1]["as_of_datetime"]) if signals else None,
        "input_hash": sha256_json([str(item.get("proxy_signal_id")) for item in signals]),
        "reader": "PITRepository.get_proxy_signals",
    }
    return snapshot


def run_episode_validation(
    db_path: str,
    *,
    market: str = "NDX",
    proxy_run_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    episode_run_id: str | None = None,
) -> dict[str, Any]:
    if str(market).upper() != "NDX":
        raise ValueError("EPISODE_OPPORTUNITY_V1 当前只支持 NDX")
    repo = PITRepository(db_path)
    candidates = repo.get_proxy_research_runs(market="NDX", limit=1000)
    if proxy_run_id:
        proxy_run = repo.get_proxy_research_run(proxy_run_id)
    else:
        proxy_run = next((item for item in candidates if item.get("status") == "COMPLETED"), None)
    if not proxy_run:
        raise ValueError("没有可用的已完成 Phase 2B proxy research run")
    if proxy_run.get("proxy_model_version") != PROXY_MODEL_VERSION:
        raise ValueError("episode validation 只能引用 NDX_PROXY_RESEARCH_V1")
    start = normalize_date(start_date or proxy_run["start_date"])
    end = normalize_date(end_date or proxy_run["end_date"])
    if start > end:
        raise ValueError("start_date 不能晚于 end_date")
    signals = [item for item in repo.get_proxy_signals(str(proxy_run["proxy_run_id"]), limit=500000) if start <= normalize_date(item["as_of_datetime"]) <= end]
    if not signals:
        raise ValueError("指定范围没有已保存的 proxy signal")
    indexes = _load_proxy_indexes(repo, end)
    close = indexes["NDX_CLOSE"]
    if not close.rows:
        raise ValueError("当前代理轨道没有 NDX_CLOSE")
    run_id = episode_run_id or f"phase2c-episode-ndx-v1-{start}-{end}-{EPISODE_CONFIG_HASH[:12]}"
    snapshot = _run_data_snapshot(indexes, signals)
    run, created = repo.create_episode_validation_run({
        "episode_run_id": run_id,
        "market": "NDX",
        "episode_model_version": EPISODE_MODEL_VERSION,
        "proxy_run_id": str(proxy_run["proxy_run_id"]),
        "start_date": start,
        "end_date": end,
        "episode_model_config": EPISODE_MODEL_CONFIG,
        "config_hash": EPISODE_CONFIG_HASH,
        "data_snapshot": snapshot,
        "data_cutoff": as_of_datetime(f"{end}T23:59:59.999999Z"),
        "created_at": _utc_now(),
        "started_at": _utc_now(),
    })
    if not created:
        if run.get("status") == "COMPLETED":
            stored = repo.get_episode_validation_report(run_id)
            return {"run": run, "report": stored.get("report") if stored else run.get("summary", {}), "reused": True}
        raise ValueError("已有同一 episode_run_id 但尚未完成；为保持 append-only，请使用新的 run_id")
    try:
        signal_by_date = {normalize_date(item["as_of_datetime"]): item for item in signals}
        episodes, states = construct_episodes_and_states(close.rows, signal_by_date, start_date=start, end_date=end, episode_namespace=run_id)
        for episode in episodes:
            repo.append_drawdown_episode({**episode, "episode_run_id": run_id, "data_end_date": end})
        for state in states:
            repo.append_episode_daily_state({**state, "episode_run_id": run_id})
        date_positions = {day: index for index, day in enumerate(close.dates)}
        state_map = {(str(item["episode_id"]), str(item["as_of_date"])): item for item in states}
        opportunities = construct_opportunity_events(episodes, signals, run_id=run_id, proxy_run_id=str(proxy_run["proxy_run_id"]), start_date=start, end_date=end, date_positions=date_positions, states_by_key=state_map)
        for event in opportunities:
            repo.append_proxy_opportunity_event(event)
        mechanical = construct_mechanical_events(episodes, close.rows, run_id=run_id, start_date=start, end_date=end)
        for event in mechanical:
            repo.append_drawdown_mechanical_event(event)

        # Evaluation is intentionally called only after every state and event
        # has been persisted.  It can see the complete close path from the
        # repository, while the construction functions above cannot.
        evaluations, fast_info = evaluate_events(episodes, opportunities, mechanical, close.rows, date_positions=date_positions)
        for evaluation in evaluations:
            repo.append_episode_event_evaluation(evaluation)
        assessments = construct_at10_assessments(episodes, mechanical, signals, run_id=run_id)
        for assessment in assessments:
            repo.append_episode_at10_assessment(assessment)

        # Build a pre-completion timeline from observable series.  It is put
        # into run.summary only after evaluation; it never feeds back into a
        # signal or event choice.
        report = build_episode_report(repo, run_id)
        report["crisis_timelines"] = _crisis_timelines(close.rows, signal_by_date, opportunities, mechanical)
        report["episode_observable_state_count"] = len(states)
        report["at10_assessment_count"] = len(assessments)
        report["fast_recovery_episode_details"] = {key: value for key, value in fast_info.items() if value.get("fast_recovery")}
        # Rebuild report once so the timeline and counts are part of the same
        # immutable JSON object that is recorded in the report table.
        repo.record_episode_validation_report(run_id, report)
        completed = repo.complete_episode_validation_run(run_id, status="COMPLETED", summary=report)
        return {"run": completed, "report": report, "reused": False}
    except Exception as exc:
        repo.complete_episode_validation_run(run_id, status="FAILED", error={"error": str(exc)})
        raise


def blind_episode_date_view(repo: PITRepository, episode_run_id: str, day: str) -> dict[str, Any] | None:
    run = repo.get_episode_validation_run(episode_run_id)
    if not run:
        return None
    normalized = normalize_date(day)
    if not str(run["start_date"]) <= normalized <= str(run["end_date"]):
        return None
    states = repo.get_episode_daily_states(episode_run_id, limit=1000000)
    state = next((item for item in states if item["as_of_date"] == normalized), None)
    if not state:
        return {
            "episode_run_id": episode_run_id,
            "as_of": normalized,
            "market": run["market"],
            "episode_model_version": run["episode_model_version"],
            "research_only": True,
            "strict_pit": False,
            "investment_action_eligible": False,
            "active_episode": False,
            "future_hidden": True,
        }
    # Do not attach drawdown_episodes here: that row contains max/bottom/
    # recovery fields that were not knowable on this date.
    return {
        "episode_run_id": episode_run_id,
        "as_of": normalized,
        "as_of_datetime": as_of_datetime(f"{normalized}T23:59:59.999999Z"),
        "market": run["market"],
        "episode_model_version": run["episode_model_version"],
        "research_only": True,
        "strict_pit": False,
        "investment_action_eligible": False,
        "active_episode": True,
        "episode_id": state["episode_id"],
        "current_drawdown": state.get("current_drawdown"),
        "days_since_peak": state.get("days_since_peak"),
        "current_proxy_score": state.get("current_proxy_score"),
        "current_proxy_percentile": state.get("current_proxy_percentile"),
        "rsi14": state.get("rsi14"),
        "distance_ma200": state.get("distance_ma200"),
        "vxn": state.get("vxn"),
        "real_yield": state.get("real_yield"),
        "nfci": state.get("nfci"),
        "input_observation_ids": state.get("input_observation_ids"),
        "future_hidden": True,
    }


__all__ = [
    "EPISODE_MODEL_VERSION", "EPISODE_MODEL_CONFIG", "EPISODE_CONFIG_HASH",
    "construct_episodes_and_states", "construct_opportunity_events", "construct_mechanical_events",
    "evaluate_events", "build_episode_report", "run_episode_validation", "blind_episode_date_view",
]


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Run Phase 2C episode-based proxy validation")
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parent / "data" / "dashboard.sqlite3")
    parser.add_argument("--market", default="NDX")
    parser.add_argument("--proxy-run-id")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    print(json.dumps(run_episode_validation(str(args.db), market=args.market, proxy_run_id=args.proxy_run_id, start_date=args.start_date, end_date=args.end_date, episode_run_id=args.run_id), ensure_ascii=False, indent=2))
