"""Phase 2B research-only validation for an independent proxy signal.

``NDX_PROXY_RESEARCH_V1`` is deliberately separate from ``NDX_SCORE_V2.0``.
It uses only price, volatility and macro proxy series that have reasonably
long histories.  The signal is ranked, rather than assigned an investment
threshold, and its future returns are written by a later evaluation pass.

The module has three boundaries that are easy to audit:

* ``generate_proxy_signals`` reads only ``HISTORICAL_PROXY`` observations up
  to the current replay day and never opens an outcome table.
* ``write_proxy_outcomes`` is the separate evaluation layer.
* ``build_proxy_report`` consumes both tables only after the frozen run has
  completed.  It cannot change the model configuration or the V2 decision log.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from calendar import monthrange
from datetime import date, datetime, timezone
import math
import random
from typing import Any, Iterable, Mapping

from data_contract import as_of_datetime, normalize_date, sha256_json
from pit_repository import PITRepository


PROXY_MODEL_VERSION = "NDX_PROXY_RESEARCH_V1"
PROXY_CONFIG_CREATED_AT = "2026-09-14T00:00:00Z"
PROXY_FEATURE_ORDER = (
    "drawdown",
    "rsi14",
    "distance_ma200",
    "vxn",
    "real_yield",
    "nfci",
)
PROXY_SERIES = {
    "drawdown": "NDX_CLOSE",
    "rsi14": "NDX_CLOSE",
    "distance_ma200": "NDX_CLOSE",
    "vxn": "NDX_VXN",
    "real_yield": "US10Y_REAL",
    "nfci": "US_NFCI",
}
PROXY_HORIZONS = {
    "6m": "forward_6m",
    "1y": "forward_1y",
    "3y": "forward_3y",
    "5y": "forward_5y",
}
PROXY_BINS = (
    "0-10%",
    "10-25%",
    "25-50%",
    "50-75%",
    "75-90%",
    "90-100%",
)
PROXY_SAMPLINGS = ("daily", "quarterly", "annual")

# This configuration is intentionally written before any forward results are
# read.  The V2 mapping gives Drawdown 20 points, the V2 technical/breadth
# slot gives the MA200 distance proxy 5 points, and the other four allowed
# indicators retain their existing 5-point slots.  Proxy-B is equal weight.
PROXY_MODEL_CONFIG: dict[str, Any] = {
    "model_version": PROXY_MODEL_VERSION,
    "created_at": PROXY_CONFIG_CREATED_AT,
    "research_only": True,
    "strict_pit": False,
    "investment_action_eligible": False,
    "features": {
        "drawdown": {
            "series_id": "NDX_CLOSE",
            "direction": "higher_opportunity_when_deeper_drawdown",
            "transform": "max(0, -drawdown)",
            "price_min_observations": 1,
        },
        "rsi14": {
            "series_id": "NDX_CLOSE",
            "direction": "higher_opportunity_when_lower_rsi",
            "transform": "-Wilder_RSI14",
            "price_min_observations": 15,
        },
        "distance_ma200": {
            "series_id": "NDX_CLOSE",
            "direction": "higher_opportunity_when_further_below_ma200",
            "transform": "-(close / MA200 - 1)",
            "price_min_observations": 200,
        },
        "vxn": {
            "series_id": "NDX_VXN",
            "direction": "higher_opportunity_when_higher_volatility",
            "transform": "VXN level",
            "max_stale_days": 7,
        },
        "real_yield": {
            "series_id": "US10Y_REAL",
            "direction": "higher_opportunity_when_lower_real_yield",
            "transform": "-10Y real yield",
            "max_stale_days": 7,
        },
        "nfci": {
            "series_id": "US_NFCI",
            "direction": "higher_opportunity_when_lower_NFCI",
            "transform": "-NFCI",
            "max_stale_days": 14,
        },
    },
    "rank": {
        "reference": "strictly_prior_observations",
        "minimum_prior_observations": 60,
        "bins": list(PROXY_BINS),
    },
    "proxy_a": {
        "weight_source": "NDX_SCORE_V2.0 corresponding allowed slots",
        "weights": {
            "drawdown": 20,
            "rsi14": 5,
            "distance_ma200": 5,
            "vxn": 5,
            "real_yield": 5,
            "nfci": 5,
        },
    },
    "proxy_b": {
        "weight_source": "equal_weight",
        "weights": {name: 1 for name in PROXY_FEATURE_ORDER},
    },
    "sampling": {
        "quarterly_rule": "first available signal day in each calendar quarter",
        "annual_rule": "first available signal day in each calendar year",
    },
    "regimes": {
        "high_rate_real_yield": 1.0,
        "high_volatility_vxn": 25.0,
        "bull_market_distance_ma200": 0.0,
        "periods": {
            "2000-2006": ["2000-01-01", "2006-12-31"],
            "2007-2013": ["2007-01-01", "2013-12-31"],
            "2014-2019": ["2014-01-01", "2019-12-31"],
            "2020-2026": ["2020-01-01", "2026-12-31"],
        },
    },
    "placebo": {"permutations": 1000, "seed": 20260914},
    "bootstrap": {"method": "circular_block", "block_length": 4, "resamples": 2000, "seed": 20260914},
    "classification": {
        "primary_sampling": "annual",
        "required_horizons": ["1y", "3y", "5y"],
        "minimum_rows": 12,
        "strong_ci_lower": 0.0,
        "strong_placebo_percentile": 0.99,
        "positive_ci_lower": 0.0,
        "positive_placebo_percentile": 0.95,
        "incremental_positive_spread": 0.02,
    },
}
PROXY_CONFIG_HASH = sha256_json(PROXY_MODEL_CONFIG)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + int(months)
    year, month0 = divmod(index, 12)
    month = month0 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _stats(values: Iterable[Any]) -> dict[str, Any]:
    data = sorted(value for value in (_finite(item) for item in values) if value is not None)
    if not data:
        return {"mean": None, "median": None, "p25": None, "p75": None, "sample_count": 0}

    def q(position: float) -> float:
        if len(data) == 1:
            return data[0]
        point = (len(data) - 1) * position
        low = int(math.floor(point)); high = int(math.ceil(point))
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
    x = _rank([pair[0] for pair in pairs]); y = _rank([pair[1] for pair in pairs])
    xbar = sum(x) / len(x); ybar = sum(y) / len(y)
    numerator = sum((a - xbar) * (b - ybar) for a, b in zip(x, y))
    denominator = math.sqrt(sum((a - xbar) ** 2 for a in x) * sum((b - ybar) ** 2 for b in y))
    return numerator / denominator if denominator else 0.0


def _audit_ids(rows: Iterable[Mapping[str, Any]], limit: int = 512) -> list[str]:
    rows = list(rows)
    ids = [str(row["observation_version_id"]) for row in rows if row.get("observation_version_id")]
    if len(ids) <= limit:
        return sorted(set(ids))
    selected = ids[:2] + ids[-limit:]
    return sorted(set(selected))


def _full_ids_hash(rows: Iterable[Mapping[str, Any]]) -> str:
    return sha256_json(sorted({str(row["observation_version_id"]) for row in rows if row.get("observation_version_id")}))


class _Index:
    """Deterministic date index used only with repository-provided rows."""

    def __init__(self, rows: Iterable[Mapping[str, Any]]):
        clean = []
        for raw in rows:
            value = _finite(raw.get("value"))
            if value is None:
                continue
            row = dict(raw)
            row["observation_date"] = normalize_date(row.get("observation_date"))
            row["value"] = value
            clean.append(row)
        clean.sort(key=lambda row: (row["observation_date"], str(row.get("observation_version_id", ""))))
        by_date: dict[str, dict[str, Any]] = {}
        for row in clean:
            by_date[row["observation_date"]] = row
        self.rows = [by_date[key] for key in sorted(by_date)]
        self.dates = [row["observation_date"] for row in self.rows]
        self.values = [float(row["value"]) for row in self.rows]

    def through(self, day: str) -> list[dict[str, Any]]:
        return self.rows[:bisect_right(self.dates, normalize_date(day))]

    def latest(self, day: str) -> dict[str, Any] | None:
        index = bisect_right(self.dates, normalize_date(day)) - 1
        return self.rows[index] if index >= 0 else None

    def first_on_or_after(self, day: date) -> dict[str, Any] | None:
        index = bisect_left(self.dates, day.isoformat())
        return self.rows[index] if index < len(self.rows) else None


def _rsi14(values: list[float]) -> float | None:
    if len(values) < 15:
        return None
    changes = [b - a for a, b in zip(values, values[1:])]
    gain = sum(max(change, 0.0) for change in changes[:14]) / 14.0
    loss = sum(max(-change, 0.0) for change in changes[:14]) / 14.0
    for change in changes[14:]:
        gain = (gain * 13.0 + max(change, 0.0)) / 14.0
        loss = (loss * 13.0 + max(-change, 0.0)) / 14.0
    if gain == loss == 0:
        return 50.0
    if loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def _opportunity_value(name: str, raw_value: float) -> float:
    if name == "drawdown":
        return max(0.0, -raw_value)
    if name in {"rsi14", "distance_ma200", "real_yield", "nfci"}:
        return -raw_value
    return raw_value


def _bucket(percentile: Any) -> str:
    value = _finite(percentile)
    if value is None:
        return "missing"
    if value < 0.10:
        return "0-10%"
    if value < 0.25:
        return "10-25%"
    if value < 0.50:
        return "25-50%"
    if value < 0.75:
        return "50-75%"
    if value < 0.90:
        return "75-90%"
    return "90-100%"


def _rank_against_past(sorted_values: list[float], value: float) -> tuple[float | None, float | None]:
    minimum = int(PROXY_MODEL_CONFIG["rank"]["minimum_prior_observations"])
    if len(sorted_values) < minimum:
        return None, None
    rank = float(bisect_left(sorted_values, value) + 1)
    percentile = bisect_right(sorted_values, value) / len(sorted_values)
    return rank, percentile


def _feature_status(
    *,
    name: str,
    raw_value: float | None,
    opportunity_value: float | None,
    percentile: float | None,
    rank: float | None,
    status: str,
    source_row: Mapping[str, Any] | None,
    input_rows: Iterable[Mapping[str, Any]],
    reason: str,
) -> dict[str, Any]:
    rows = list(input_rows)
    return {
        "status": status,
        "reason": reason,
        "raw_value": raw_value,
        "opportunity_value": opportunity_value,
        "rank": rank,
        "percentile": percentile,
        "source_observation_date": source_row.get("observation_date") if source_row else None,
        "source_observation_version_id": source_row.get("observation_version_id") if source_row else None,
        "input_observation_ids": _audit_ids(rows),
        "full_input_hash": _full_ids_hash(rows),
    }


def _price_feature_rows(close: _Index) -> dict[str, dict[str, Any]]:
    """Compute price features once, in chronological order."""

    result: dict[str, dict[str, Any]] = {}
    running_high = -float("inf")
    gains: list[float] = []
    losses: list[float] = []
    gain: float | None = None
    loss: float | None = None
    for index, row in enumerate(close.rows):
        value = float(row["value"])
        running_high = max(running_high, value)
        if index:
            change = value - close.values[index - 1]
            gains.append(max(change, 0.0)); losses.append(max(-change, 0.0))
        rsi = None
        if len(gains) >= 14:
            if gain is None or loss is None:
                gain = sum(gains[:14]) / 14.0; loss = sum(losses[:14]) / 14.0
            elif len(gains) > 14:
                gain = (gain * 13.0 + gains[-1]) / 14.0
                loss = (loss * 13.0 + losses[-1]) / 14.0
            if gain == loss == 0:
                rsi = 50.0
            elif loss == 0:
                rsi = 100.0
            else:
                rsi = 100.0 - 100.0 / (1.0 + gain / loss)
        ma = sum(close.values[index - 199:index + 1]) / 200.0 if index >= 199 else None
        drawdown = value / running_high - 1.0 if running_high else None
        distance = value / ma - 1.0 if ma else None
        result[row["observation_date"]] = {
            "drawdown": drawdown,
            "rsi14": rsi,
            "distance_ma200": distance,
            "close_index": index,
        }
    return result


def _load_proxy_indexes(repo: PITRepository, end_date: str) -> dict[str, _Index]:
    return {
        series_id: _Index(repo.get_history_proxy(series_id, as_of_datetime(f"{normalize_date(end_date)}T23:59:59.999999Z")))
        for series_id in sorted(set(PROXY_SERIES.values()))
    }


def _data_snapshot(indexes: Mapping[str, _Index]) -> dict[str, Any]:
    series = {}
    for series_id, index in sorted(indexes.items()):
        series[series_id] = {
            "count": len(index.rows),
            "min_date": index.dates[0] if index.dates else None,
            "max_date": index.dates[-1] if index.dates else None,
            "input_hash": sha256_json([str(row.get("observation_version_id")) for row in index.rows if row.get("observation_version_id")]),
            "reader": "PITRepository.get_history_proxy",
            "eligibility_origin": "HISTORICAL_PROXY",
        }
    return {"repository": "PITRepository", "mode": "RESEARCH_PROXY", "series": series}


def generate_proxy_signals(
    repo: PITRepository,
    *,
    proxy_run_id: str,
    start_date: str,
    end_date: str,
    indexes: Mapping[str, _Index] | None = None,
) -> list[dict[str, Any]]:
    """Generate chronological proxy signals without reading future outcomes."""

    indexes = dict(indexes or _load_proxy_indexes(repo, end_date))
    close = indexes["NDX_CLOSE"]
    price_features = _price_feature_rows(close)
    days = [day for day in close.dates if normalize_date(start_date) <= day <= normalize_date(end_date)]
    feature_histories: dict[str, list[float]] = {name: [] for name in PROXY_FEATURE_ORDER}
    composite_histories = {"proxy_a": [], "proxy_b": []}
    previous_macro_observation: dict[str, str | None] = {name: None for name in ("vxn", "real_yield", "nfci")}
    output: list[dict[str, Any]] = []
    weights_a = PROXY_MODEL_CONFIG["proxy_a"]["weights"]
    weights_b = PROXY_MODEL_CONFIG["proxy_b"]["weights"]

    for day in days:
        asof = as_of_datetime(f"{day}T23:59:59.999999Z")
        feature_values: dict[str, float | None] = {}
        feature_percentiles: dict[str, float | None] = {}
        feature_status: dict[str, dict[str, Any]] = {}
        all_input_ids: list[str] = []

        # Price features use the close observation at this replay date and
        # only the preceding price rows.  No evaluation table is reachable.
        price = price_features.get(day, {})
        close_index = int(price.get("close_index", -1))
        close_rows = close.rows[: close_index + 1] if close_index >= 0 else []
        for name in ("drawdown", "rsi14", "distance_ma200"):
            raw_value = _finite(price.get(name))
            source = close_rows[-1] if close_rows else None
            if raw_value is None:
                status = "insufficient_history"
                reason = "截至 replay 日价格历史不足该特征的固定预热长度。"
                opportunity = None
                rank = percentile = None
            else:
                opportunity = _opportunity_value(name, raw_value)
                rank, percentile = _rank_against_past(feature_histories[name], opportunity)
                status = "available" if percentile is not None else "insufficient_rank_history"
                reason = "方向和变换在评估前冻结；分位只比较严格早于当前 replay 日的信号。"
            input_rows = close_rows if name == "drawdown" else close_rows[-200:] if name == "distance_ma200" and close_rows else close_rows[-101:] if close_rows else []
            if raw_value is not None:
                feature_values[name] = raw_value
            else:
                feature_values[name] = None
            feature_percentiles[name] = percentile
            record = _feature_status(
                name=name, raw_value=raw_value, opportunity_value=opportunity,
                percentile=percentile, rank=rank, status=status, source_row=source,
                input_rows=input_rows, reason=reason,
            )
            feature_status[name] = record
            all_input_ids.extend(record["input_observation_ids"])

        # Macro features are aligned to the latest observation on or before
        # the replay day, with a frozen freshness window.  Repeated days may
        # carry the same observation, but it is added to the rank reference
        # only once by observation id.
        for name in ("vxn", "real_yield", "nfci"):
            series_id = PROXY_SERIES[name]
            index = indexes[series_id]
            source = index.latest(day)
            history_rows = index.through(day)
            raw_value = _finite(source.get("value")) if source else None
            age_days = None
            if source:
                age_days = (date.fromisoformat(day) - date.fromisoformat(source["observation_date"])).days
            max_age = int(PROXY_MODEL_CONFIG["features"][name]["max_stale_days"])
            if source is None:
                status = "missing"; reason = "截至 replay 日没有历史代理观察。"; opportunity = None; rank = percentile = None
            elif age_days is not None and age_days > max_age:
                status = "stale"; reason = f"最近观察距 replay 日 {age_days} 天，超过固定 freshness window {max_age} 天。"; opportunity = None; rank = percentile = None
            else:
                opportunity = _opportunity_value(name, raw_value)
                rank, percentile = _rank_against_past(feature_histories[name], opportunity)
                status = "available" if percentile is not None else "insufficient_rank_history"
                reason = "宏观/波动方向在评估前冻结；分位只比较严格早于当前 replay 日的观测。"
            feature_values[name] = raw_value
            feature_percentiles[name] = percentile
            input_rows = history_rows
            record = _feature_status(
                name=name, raw_value=raw_value, opportunity_value=opportunity,
                percentile=percentile, rank=rank, status=status, source_row=source,
                input_rows=input_rows, reason=reason,
            )
            record["age_days"] = age_days
            feature_status[name] = record
            all_input_ids.extend(record["input_observation_ids"])

        # Composite-A and B use only already-ranked features.  ``available``
        # is a research coverage measure, never a formal V2 coverage gate.
        available_a = sum(float(weights_a[name]) for name in PROXY_FEATURE_ORDER if feature_percentiles[name] is not None)
        raw_a = sum(float(weights_a[name]) * float(feature_percentiles[name]) for name in PROXY_FEATURE_ORDER if feature_percentiles[name] is not None)
        fraction_a = raw_a / available_a if available_a else None
        available_b = sum(float(weights_b[name]) for name in PROXY_FEATURE_ORDER if feature_percentiles[name] is not None)
        raw_b = sum(float(weights_b[name]) * float(feature_percentiles[name]) for name in PROXY_FEATURE_ORDER if feature_percentiles[name] is not None)
        fraction_b = raw_b / available_b if available_b else None
        rank_a, percentile_a = _rank_against_past(composite_histories["proxy_a"], fraction_a) if fraction_a is not None else (None, None)
        rank_b, percentile_b = _rank_against_past(composite_histories["proxy_b"], fraction_b) if fraction_b is not None else (None, None)
        signal = {
            "proxy_run_id": proxy_run_id,
            "as_of_datetime": asof,
            "market": "NDX",
            "proxy_model_version": PROXY_MODEL_VERSION,
            "proxy_raw_score": raw_a if fraction_a is not None else None,
            "proxy_available_weight": available_a,
            "proxy_score_fraction": fraction_a,
            "proxy_rank": rank_a,
            "proxy_percentile": percentile_a,
            "proxy_b_raw_score": raw_b if fraction_b is not None else None,
            "proxy_b_available_weight": available_b,
            "proxy_b_score_fraction": fraction_b,
            "proxy_b_rank": rank_b,
            "proxy_b_percentile": percentile_b,
            "feature_values": feature_values,
            "feature_percentiles": feature_percentiles,
            "feature_status": feature_status,
            "input_observation_ids": sorted(set(all_input_ids)),
            "input_hash": sha256_json(sorted(set(all_input_ids))),
        }
        output.append(signal)
        for name in PROXY_FEATURE_ORDER:
            opportunity = feature_status[name].get("opportunity_value")
            if opportunity is not None:
                position = bisect_right(feature_histories[name], float(opportunity))
                feature_histories[name].insert(position, float(opportunity))
        if fraction_a is not None:
            position = bisect_right(composite_histories["proxy_a"], float(fraction_a)); composite_histories["proxy_a"].insert(position, float(fraction_a))
        if fraction_b is not None:
            position = bisect_right(composite_histories["proxy_b"], float(fraction_b)); composite_histories["proxy_b"].insert(position, float(fraction_b))
    return output


def write_proxy_outcomes(repo: PITRepository, *, proxy_run_id: str, signals: list[dict[str, Any]], close_index: _Index) -> int:
    """Write forward labels after signal generation, in a separate pass."""

    count = 0
    for signal in signals:
        day = date.fromisoformat(normalize_date(signal["as_of_datetime"]))
        current = close_index.first_on_or_after(day)
        if not current or current["observation_date"] != day.isoformat() or _finite(current.get("value")) in (None, 0):
            continue
        current_value = float(current["value"])
        future_ids: list[str] = []
        values: dict[str, float | None] = {}
        for months, field in ((6, "forward_6m"), (12, "forward_1y"), (36, "forward_3y"), (60, "forward_5y")):
            row = close_index.first_on_or_after(_add_months(day, months))
            if row is None:
                values[field] = None
            else:
                value = _finite(row.get("value")); values[field] = value / current_value - 1.0 if value is not None else None
                if row.get("observation_version_id"):
                    future_ids.append(str(row["observation_version_id"]))
        end = _add_months(day, 12)
        future_rows = [row for row in close_index.rows[bisect_right(close_index.dates, day.isoformat()):] if row["observation_date"] <= end.isoformat()]
        future_values = [float(row["value"]) for row in future_rows if _finite(row.get("value")) is not None]
        values["max_drawdown_next_1y"] = min((value / current_value - 1.0 for value in future_values), default=None)
        values["max_gain_next_1y"] = max((value / current_value - 1.0 for value in future_values), default=None)
        missing = [field for field in PROXY_HORIZONS.values() if values.get(field) is None]
        outcome = {
            "proxy_run_id": proxy_run_id,
            "proxy_signal_id": sha256_json({"proxy_run_id": proxy_run_id, "as_of_datetime": signal["as_of_datetime"]})[:32],
            "market": "NDX",
            "decision_date": day.isoformat(),
            "source_series_id": "NDX_CLOSE",
            "source_observation_version_ids": future_ids,
            **values,
            "status": "COMPLETE" if not missing else "PARTIAL",
            "reason": "Evaluation layer only; proxy feature generation completed before future rows were read." + (f" 缺少: {', '.join(missing)}" if missing else ""),
        }
        repo.append_proxy_forward_outcome(outcome)
        count += 1
    return count


def _sample_signals(signals: list[dict[str, Any]], sampling: str) -> list[dict[str, Any]]:
    if sampling == "daily":
        return list(signals)
    selected: dict[Any, dict[str, Any]] = {}
    for signal in sorted(signals, key=lambda item: item["as_of_datetime"]):
        day = date.fromisoformat(normalize_date(signal["as_of_datetime"]))
        key = (day.year, (day.month - 1) // 3 + 1) if sampling == "quarterly" else day.year
        selected.setdefault(key, signal)
    return list(sorted(selected.values(), key=lambda item: item["as_of_datetime"]))


def _signal_rows(signals: list[dict[str, Any]], outcomes: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for signal in signals:
        signal_id = str(signal.get("proxy_signal_id") or sha256_json({"proxy_run_id": signal["proxy_run_id"], "as_of_datetime": signal["as_of_datetime"]})[:32])
        outcome = outcomes.get(signal_id)
        if outcome:
            rows.append({"signal": signal, "outcome": outcome, "date": normalize_date(signal["as_of_datetime"]), "signal_id": signal_id})
    return rows


def _validation(rows: list[dict[str, Any]], score_getter, *, score_name: str) -> dict[str, Any]:
    result: dict[str, Any] = {"score_name": score_name, "rank_bins": {}, "monotonicity": {}}
    for bucket in PROXY_BINS:
        result["rank_bins"][bucket] = {}
        for label, field in PROXY_HORIZONS.items():
            values = [item["outcome"].get(field) for item in rows if _bucket(score_getter(item)) == bucket]
            result["rank_bins"][bucket][label] = _stats(values)
    for label, field in PROXY_HORIZONS.items():
        means = [result["rank_bins"][bucket][label]["mean"] for bucket in PROXY_BINS]
        known = [value for value in means if value is not None]
        monotone = len(known) >= 3 and all(a <= b for a, b in zip(known, known[1:]))
        score_values = [score_getter(item) for item in rows]
        return_values = [item["outcome"].get(field) for item in rows]
        result["monotonicity"][label] = {
            "ordered_bin_means": means,
            "non_decreasing": monotone,
            "spearman_rank_correlation": spearman(score_values, return_values),
        }
    return result


def _annualized_difference(top_mean: float | None, bottom_mean: float | None, years: int) -> float | None:
    if top_mean is None or bottom_mean is None:
        return None
    if top_mean <= -1 or bottom_mean <= -1:
        return None
    return (1.0 + top_mean) ** (1.0 / years) - (1.0 + bottom_mean) ** (1.0 / years)


def _quartile_metrics(rows: list[dict[str, Any]], score_getter, horizon: str) -> dict[str, Any]:
    field = PROXY_HORIZONS[horizon]
    top = [item["outcome"].get(field) for item in rows if (_finite(score_getter(item)) is not None and float(score_getter(item)) >= 0.75)]
    bottom = [item["outcome"].get(field) for item in rows if (_finite(score_getter(item)) is not None and float(score_getter(item)) < 0.25)]
    top_stats = _stats(top); bottom_stats = _stats(bottom)
    spread = top_stats["mean"] - bottom_stats["mean"] if top_stats["mean"] is not None and bottom_stats["mean"] is not None else None
    years = {"1y": 1, "3y": 3, "5y": 5}.get(horizon, 1)
    drawdown_top = _stats([item["outcome"].get("max_drawdown_next_1y") for item in rows if (_finite(score_getter(item)) is not None and float(score_getter(item)) >= 0.75)])
    drawdown_bottom = _stats([item["outcome"].get("max_drawdown_next_1y") for item in rows if (_finite(score_getter(item)) is not None and float(score_getter(item)) < 0.25)])
    downside_reduction = drawdown_top["mean"] - drawdown_bottom["mean"] if drawdown_top["mean"] is not None and drawdown_bottom["mean"] is not None else None
    return {
        "top_quartile": top_stats,
        "bottom_quartile": bottom_stats,
        "top_minus_bottom_spread": spread,
        "annualized_difference": _annualized_difference(top_stats["mean"], bottom_stats["mean"], years),
        "top_hit_rate": sum(value > 0 for value in top if _finite(value) is not None) / len([value for value in top if _finite(value) is not None]) if any(_finite(value) is not None for value in top) else None,
        "bottom_hit_rate": sum(value > 0 for value in bottom if _finite(value) is not None) / len([value for value in bottom if _finite(value) is not None]) if any(_finite(value) is not None for value in bottom) else None,
        "future_drawdown_top": drawdown_top,
        "future_drawdown_bottom": drawdown_bottom,
        "downside_reduction_top_minus_bottom": downside_reduction,
    }


def _bootstrap_spread(rows: list[dict[str, Any]], score_getter, horizon: str) -> dict[str, Any]:
    field = PROXY_HORIZONS[horizon]
    eligible = [item for item in rows if _finite(score_getter(item)) is not None and _finite(item["outcome"].get(field)) is not None]
    top = [item for item in eligible if float(score_getter(item)) >= 0.75]
    bottom = [item for item in eligible if float(score_getter(item)) < 0.25]
    n = len(eligible); block = int(PROXY_MODEL_CONFIG["bootstrap"]["block_length"]); reps = int(PROXY_MODEL_CONFIG["bootstrap"]["resamples"])
    if n < 8 or len(top) < 3 or len(bottom) < 3:
        return {"mean": None, "lower_95": None, "upper_95": None, "sample_count": n, "resamples": 0, "block_length": block, "status": "INCONCLUSIVE_SMALL_SAMPLE"}
    rng = random.Random(int(PROXY_MODEL_CONFIG["bootstrap"]["seed"]) + sum(ord(c) for c in horizon))
    spreads = []
    for _ in range(reps):
        sampled: list[dict[str, Any]] = []
        while len(sampled) < n:
            start = rng.randrange(n)
            sampled.extend(eligible[(start + offset) % n] for offset in range(block))
        sampled = sampled[:n]
        values = _quartile_metrics(sampled, score_getter, horizon)["top_minus_bottom_spread"]
        if values is not None:
            spreads.append(float(values))
    if not spreads:
        return {"mean": None, "lower_95": None, "upper_95": None, "sample_count": n, "resamples": 0, "block_length": block, "status": "INCONCLUSIVE"}
    ordered = sorted(spreads)
    return {
        "mean": sum(spreads) / len(spreads),
        "lower_95": ordered[int((len(ordered) - 1) * 0.025)],
        "upper_95": ordered[int((len(ordered) - 1) * 0.975)],
        "sample_count": n,
        "resamples": len(spreads),
        "block_length": block,
        "status": "OK",
    }


def _placebo(rows: list[dict[str, Any]], score_getter, horizons: tuple[str, ...] = ("1y", "3y", "5y")) -> dict[str, Any]:
    eligible = [item for item in rows if _finite(score_getter(item)) is not None]
    if len(eligible) < 8:
        return {
            "permutations": 0,
            "configured_permutations": int(PROXY_MODEL_CONFIG["placebo"]["permutations"]),
            "seed": int(PROXY_MODEL_CONFIG["placebo"]["seed"]),
            "sample_count": len(eligible),
            "status": "INCONCLUSIVE_SMALL_SAMPLE",
        }
    actual = {horizon: _quartile_metrics(eligible, score_getter, horizon)["top_minus_bottom_spread"] for horizon in horizons}
    values = [float(score_getter(item)) for item in eligible]
    rng = random.Random(int(PROXY_MODEL_CONFIG["placebo"]["seed"]))
    distributions = {horizon: [] for horizon in horizons}; correlations = {horizon: [] for horizon in horizons}
    for _ in range(int(PROXY_MODEL_CONFIG["placebo"]["permutations"])):
        shuffled = values[:]; rng.shuffle(shuffled)
        for horizon in horizons:
            field = PROXY_HORIZONS[horizon]
            returns = [item["outcome"].get(field) for item in eligible]
            fake_rows = [{"outcome": {field: outcome}, "fake_score": score} for outcome, score in zip(returns, shuffled)]
            spread = _quartile_metrics(fake_rows, lambda item: item["fake_score"], horizon)["top_minus_bottom_spread"]
            if spread is not None:
                distributions[horizon].append(float(spread))
            correlations[horizon].append(spearman(shuffled, returns))
    result = {"permutations": int(PROXY_MODEL_CONFIG["placebo"]["permutations"]), "seed": int(PROXY_MODEL_CONFIG["placebo"]["seed"]), "sample_count": len(eligible), "status": "OK", "horizons": {}}
    for horizon in horizons:
        spreads = distributions[horizon]; cors = [value for value in correlations[horizon] if value is not None]
        result["horizons"][horizon] = {
            "actual_top_minus_bottom_spread": actual[horizon],
            "placebo_spread_stats": _stats(spreads),
            "placebo_rank_correlation_stats": _stats(cors),
            "actual_spread_percentile": (sum(value <= actual[horizon] for value in spreads) + 1) / (len(spreads) + 1) if actual[horizon] is not None and spreads else None,
            "actual_rank_correlation": spearman(values, [item["outcome"].get(PROXY_HORIZONS[horizon]) for item in eligible]),
            "actual_rank_correlation_percentile": (sum(value <= spearman(values, [item["outcome"].get(PROXY_HORIZONS[horizon]) for item in eligible]) for value in cors) + 1) / (len(cors) + 1) if cors else None,
        }
    return result


def _score_getter(model: str):
    return lambda item: item["signal"].get("proxy_percentile" if model == "proxy_a" else "proxy_b_percentile")


def _feature_score_getter(name: str):
    return lambda item: (item["signal"].get("feature_percentiles") or {}).get(name)


def _regime_for_signal(signal: Mapping[str, Any]) -> dict[str, str]:
    values = signal.get("feature_values") or {}
    distance = _finite(values.get("distance_ma200")); real = _finite(values.get("real_yield")); vxn = _finite(values.get("vxn"))
    return {
        "rate": "High Rate" if real is not None and real >= float(PROXY_MODEL_CONFIG["regimes"]["high_rate_real_yield"]) else "Low Rate" if real is not None else "Unknown Rate",
        "volatility": "High Volatility" if vxn is not None and vxn >= float(PROXY_MODEL_CONFIG["regimes"]["high_volatility_vxn"]) else "Low Volatility" if vxn is not None else "Unknown Volatility",
        "market": "Bull Market" if distance is not None and distance >= float(PROXY_MODEL_CONFIG["regimes"]["bull_market_distance_ma200"]) else "Bear Market" if distance is not None else "Unknown Market",
    }


def _group_summary(rows: list[dict[str, Any]], score_getter) -> dict[str, Any]:
    return {
        "sample_count": len(rows),
        "horizons": {
            horizon: {
                "all": _stats([item["outcome"].get(field) for item in rows]),
                **_quartile_metrics(rows, score_getter, horizon),
            }
            for horizon, field in PROXY_HORIZONS.items()
        },
    }


def _coverage_attribution(repo: PITRepository) -> dict[str, Any]:
    runs = repo.get_replay_runs(market="NDX", mode="RESEARCH_PROXY", limit=1000)
    candidates = []
    for run in runs:
        decisions = repo.get_replay_decisions(run["replay_run_id"], limit=100000)
        candidates.append((len(decisions), run, decisions))
    if not candidates:
        return {
            "status": "NO_V2_REPLAY",
            "decision_count": 0,
            "gate_failure_days": 0,
            "named_missing_union_days": 0,
            "counts": {},
            "weighted_missing_points_days": {},
            "groups": {},
            "purchase_priority_by_structural_coverage": [],
            "attribution_sum_check": {"gate_days": 0, "named_union_days": 0, "other_gate_days": 0, "passes": True},
        }
    _, run, decisions = max(candidates, key=lambda item: (item[0], item[1].get("end_date", ""), item[1].get("created_at", "")))
    v2_weights = {key: int(value.get("weight", 0)) for key, value in ((run.get("summary") or {}).get("component_config") or {}).items() if isinstance(value, Mapping)}
    if not v2_weights:
        v2_weights = {"forward_pe": 20, "ttm_pe": 10, "revision": 15, "growth": 10, "drawdown": 20, "vxn": 5, "real": 5, "nfci": 5, "breadth": 5, "rsi": 5}
    groups = {
        "A_FORWARD_PE": ["forward_pe"],
        "B_EPS_REVISION": ["revision", "growth"],
        "C_HISTORICAL_CONSTITUENTS_BREADTH": ["breadth"],
    }
    counts: dict[str, int] = {key: 0 for key in ("forward_pe", "ttm_pe", "revision", "growth", "breadth", "other")}
    weight_days: dict[str, float] = {key: 0.0 for key in counts}
    gate_days = 0; union_days = 0
    for decision in decisions:
        missing = {str(item.get("key")) for item in (decision.get("missing_items") or [])}
        if float(decision.get("coverage") or 0) < 100 or decision.get("gate_status") != "PASS":
            gate_days += 1
        named = set()
        for key in missing:
            bucket = key if key in counts and key != "other" else "other"
            counts[bucket] += 1; weight_days[bucket] += float(v2_weights.get(key, 0)); named.add(bucket)
        if named:
            union_days += 1
    scenarios = []
    for label, keys in groups.items():
        residual = 0
        affected = 0
        for decision in decisions:
            missing = {str(item.get("key")) for item in (decision.get("missing_items") or [])}
            if missing.intersection(keys): affected += 1
            if missing.difference(keys): residual += 1
        scenarios.append({
            "candidate": label,
            "keys_assumed_filled": keys,
            "missing_days_affected": affected,
            "residual_gate_days_if_only_this_group_filled": residual,
            "weighted_missing_points_days": sum(float(v2_weights.get(key, 0)) * counts.get(key, 0) for key in keys),
        })
    scenarios.sort(key=lambda item: (-item["weighted_missing_points_days"], item["candidate"]))
    return {
        "status": "OK",
        "source_replay_run_id": run["replay_run_id"],
        "decision_count": len(decisions),
        "gate_failure_days": gate_days,
        "named_missing_union_days": union_days,
        "counts": counts,
        "weighted_missing_points_days": weight_days,
        "groups": groups,
        "purchase_priority_by_structural_coverage": scenarios,
        "attribution_sum_check": {"gate_days": gate_days, "named_union_days": union_days, "other_gate_days": max(0, gate_days - union_days), "passes": union_days <= gate_days},
        "note": "仅按当前 V2 replay 的缺失结构和权重贡献排序；没有使用 forward return 选优。",
    }


def _classify_model(validation: Mapping[str, Any], bootstrap: Mapping[str, Any], placebo: Mapping[str, Any], *, model: str) -> str:
    config = PROXY_MODEL_CONFIG["classification"]
    horizon_values = []
    for horizon in config["required_horizons"]:
        metrics = (validation.get(horizon) or {})
        spread = metrics.get("top_minus_bottom_spread")
        ci = (bootstrap.get(horizon) or {}).get("lower_95")
        placebo_p = (placebo.get("horizons", {}).get(horizon) or {}).get("actual_spread_percentile")
        horizon_values.append((spread, ci, placebo_p))
    if len(horizon_values) < 3 or any(item[0] is None for item in horizon_values):
        return "INCONCLUSIVE"
    if any((item[1] is None or item[2] is None) for item in horizon_values):
        return "INCONCLUSIVE"
    if all(item[0] > 0 and item[1] > float(config["strong_ci_lower"]) and item[2] >= float(config["strong_placebo_percentile"]) for item in horizon_values):
        return "STRONG"
    if sum(item[0] > 0 and item[1] > float(config["positive_ci_lower"]) and item[2] >= float(config["positive_placebo_percentile"]) for item in horizon_values) >= 2:
        return "POSITIVE"
    if sum(item[0] > 0 for item in horizon_values) >= 1:
        return "WEAK"
    if sum(item[0] <= 0 for item in horizon_values) >= 2:
        return "NONE"
    return "INCONCLUSIVE"


def _combine_information_values(values: list[str]) -> str:
    if not values or any(value == "INCONCLUSIVE" for value in values):
        return "INCONCLUSIVE"
    if all(value == "STRONG" for value in values):
        return "STRONG"
    if all(value in {"STRONG", "POSITIVE"} for value in values) and any(value == "POSITIVE" for value in values):
        return "POSITIVE"
    if any(value in {"STRONG", "POSITIVE", "WEAK"} for value in values):
        return "WEAK"
    if all(value == "NONE" for value in values):
        return "NONE"
    return "INCONCLUSIVE"


def build_proxy_report(repo: PITRepository, proxy_run_id: str) -> dict[str, Any]:
    signals = repo.get_proxy_signals(proxy_run_id, limit=500000)
    outcomes = repo.get_proxy_forward_outcomes(proxy_run_id, limit=500000)
    outcome_map = {str(item["proxy_signal_id"]): item for item in outcomes}
    joined = _signal_rows(signals, outcome_map)
    coverage_distribution: dict[str, int] = {}
    for signal in signals:
        available = float(signal.get("proxy_available_weight") or 0)
        maximum = sum(float(value) for value in PROXY_MODEL_CONFIG["proxy_a"]["weights"].values())
        bucket = "0-40%" if available / maximum < 0.40 else "40-60%" if available / maximum < 0.60 else "60-80%" if available / maximum < 0.80 else "80-99%" if available < maximum else "100%"
        coverage_distribution[bucket] = coverage_distribution.get(bucket, 0) + 1

    single: dict[str, Any] = {}
    for name in PROXY_FEATURE_ORDER:
        single[name] = {}
        for sampling in PROXY_SAMPLINGS:
            sample = _sample_signals(signals, sampling)
            rows = _signal_rows(sample, outcome_map)
            single[name][sampling] = _validation(rows, _feature_score_getter(name), score_name=name)

    composites: dict[str, Any] = {}
    comparison: dict[str, Any] = {}
    bootstrap: dict[str, Any] = {}
    placebo: dict[str, Any] = {}
    model_classes: list[str] = []
    for model in ("proxy_a", "proxy_b"):
        composites[model] = {}; comparison[model] = {}; bootstrap[model] = {}; placebo[model] = {}
        for sampling in PROXY_SAMPLINGS:
            sample = _sample_signals(signals, sampling); rows = _signal_rows(sample, outcome_map); getter = _score_getter(model)
            composites[model][sampling] = _validation(rows, getter, score_name=model)
            comparison[model][sampling] = {}
            for horizon in PROXY_HORIZONS:
                comparison[model][sampling][horizon] = _quartile_metrics(rows, getter, horizon)
            # Long-horizon uncertainty and permutation tests use the annual
            # de-overlapped sample as the primary inferential view.  Daily
            # and quarterly descriptive tables remain available, while
            # avoiding thousands of redundant resamples of overlapping rows.
            if sampling == "annual":
                bootstrap[model][sampling] = {horizon: _bootstrap_spread(rows, getter, horizon) for horizon in ("1y", "3y", "5y")}
                placebo[model][sampling] = _placebo(rows, getter)
            else:
                bootstrap[model][sampling] = {horizon: {"status": "PRIMARY_ANNUAL_ONLY", "sample_count": len(rows), "resamples": 0, "block_length": int(PROXY_MODEL_CONFIG["bootstrap"]["block_length"]), "mean": None, "lower_95": None, "upper_95": None} for horizon in ("1y", "3y", "5y")}
                placebo[model][sampling] = {"status": "PRIMARY_ANNUAL_ONLY", "permutations": 0, "sample_count": len(rows)}
        annual_composite = {horizon: comparison[model]["annual"][horizon] for horizon in PROXY_HORIZONS}
        model_classes.append(_classify_model(annual_composite, bootstrap[model]["annual"], placebo[model]["annual"], model=model))

    baseline: dict[str, Any] = {}
    for sampling in PROXY_SAMPLINGS:
        rows = _signal_rows(_sample_signals(signals, sampling), outcome_map)
        getter = _feature_score_getter("drawdown")
        baseline[sampling] = {horizon: _quartile_metrics(rows, getter, horizon) for horizon in PROXY_HORIZONS}
        for model in ("proxy_a", "proxy_b"):
            comparison[model][sampling]["spearman_vs_drawdown"] = spearman(
                [getter(item) for item in rows], [_score_getter(model)(item) for item in rows]
            )
            comparison[model][sampling]["incremental_spread_vs_drawdown"] = {
                horizon: ((comparison[model][sampling][horizon].get("top_minus_bottom_spread") - baseline[sampling][horizon].get("top_minus_bottom_spread")) if comparison[model][sampling][horizon].get("top_minus_bottom_spread") is not None and baseline[sampling][horizon].get("top_minus_bottom_spread") is not None else None)
                for horizon in PROXY_HORIZONS
            }

    annual_inc = []
    annual_quartiles_complete = True
    for model in ("proxy_a", "proxy_b"):
        for horizon in PROXY_HORIZONS:
            metric = comparison[model]["annual"][horizon]
            baseline_metric = baseline["annual"][horizon]
            if metric["top_quartile"]["sample_count"] < 3 or metric["bottom_quartile"]["sample_count"] < 3 or baseline_metric["top_quartile"]["sample_count"] < 3 or baseline_metric["bottom_quartile"]["sample_count"] < 3:
                annual_quartiles_complete = False
        annual_inc.extend(value for value in comparison[model]["annual"]["incremental_spread_vs_drawdown"].values() if value is not None)
    threshold = float(PROXY_MODEL_CONFIG["classification"]["incremental_positive_spread"])
    if not annual_quartiles_complete or len(annual_inc) < 2:
        incremental_value = "INCONCLUSIVE"
    elif sum(value >= threshold for value in annual_inc) >= 4:
        incremental_value = "POSITIVE"
    elif sum(value > 0 for value in annual_inc) >= 2:
        incremental_value = "WEAK"
    elif all(value <= 0 for value in annual_inc):
        incremental_value = "NONE"
    else:
        incremental_value = "INCONCLUSIVE"

    period_audit: dict[str, Any] = {}
    regime_audit: dict[str, Any] = {}
    annual_signals = _sample_signals(signals, "annual")
    for label, (start, end) in PROXY_MODEL_CONFIG["regimes"]["periods"].items():
        sample = [item for item in annual_signals if start <= normalize_date(item["as_of_datetime"]) <= end]
        period_audit[label] = {model: _group_summary(_signal_rows(sample, outcome_map), _score_getter(model)) for model in ("proxy_a", "proxy_b")}
    for regime_name in ("rate", "volatility", "market"):
        regime_audit[regime_name] = {}
        for label in sorted({value for signal in signals for value in _regime_for_signal(signal).values() if value.endswith({"rate": " Rate", "volatility": " Volatility", "market": " Market"}.get(regime_name, ""))}):
            sample = [item for item in annual_signals if _regime_for_signal(item)[regime_name] == label]
            regime_audit[regime_name][label] = {model: _group_summary(_signal_rows(sample, outcome_map), _score_getter(model)) for model in ("proxy_a", "proxy_b")}

    crisis_periods = {
        "2000-2002": ("2000-01-01", "2002-12-31"),
        "2007-2009": ("2007-01-01", "2009-12-31"),
        "2018Q4": ("2018-10-01", "2018-12-31"),
        "2020": ("2020-01-01", "2020-12-31"),
        "2022": ("2022-01-01", "2022-12-31"),
    }
    crisis_trajectories = {}
    for label, (start, end) in crisis_periods.items():
        sample = [item for item in _sample_signals(signals, "quarterly") if start <= normalize_date(item["as_of_datetime"]) <= end]
        crisis_trajectories[label] = [
            {
                "date": normalize_date(item["as_of_datetime"]),
                "proxy_a_score_fraction": item.get("proxy_score_fraction"),
                "proxy_a_percentile": item.get("proxy_percentile"),
                "proxy_b_score_fraction": item.get("proxy_b_score_fraction"),
                "proxy_b_percentile": item.get("proxy_b_percentile"),
                "feature_percentiles": item.get("feature_percentiles"),
            }
            for item in sample
        ]

    attribution = _coverage_attribution(repo)
    v2_status = "UNTESTED_DUE_TO_DATA" if attribution.get("decision_count", 0) and attribution.get("gate_failure_days") == attribution.get("decision_count") else "TESTABLE" if attribution.get("decision_count", 0) else "UNTESTED_DUE_TO_DATA"
    return {
        "phase": "2B",
        "proxy_model_version": PROXY_MODEL_VERSION,
        "proxy_model_config": PROXY_MODEL_CONFIG,
        "config_hash": PROXY_CONFIG_HASH,
        "research_only": True,
        "strict_pit": False,
        "investment_action_eligible": False,
        "proxy_run_id": proxy_run_id,
        "signal_count": len(signals),
        "forward_outcome_count": len(outcomes),
        "coverage_distribution": coverage_distribution,
        "single_indicator_validation": single,
        "composite_validation": composites,
        "composite_vs_drawdown_only": comparison,
        "drawdown_only_baseline": baseline,
        "block_bootstrap": bootstrap,
        "random_placebo": placebo,
        "period_stability": period_audit,
        "regime_stability": regime_audit,
        "crisis_trajectories": crisis_trajectories,
        "coverage_bottleneck_attribution": attribution,
        "proxy_signal_information_value": _combine_information_values(model_classes),
        "proxy_model_information_values": {"proxy_a": model_classes[0] if model_classes else "INCONCLUSIVE", "proxy_b": model_classes[1] if len(model_classes) > 1 else "INCONCLUSIVE"},
        "composite_incremental_value_over_drawdown": incremental_value,
        "NDX_SCORE_V2_STATUS": v2_status,
        "lookahead_controls": {
            "feature_reader": "PITRepository.get_history_proxy with observation_date <= replay day",
            "rank_reference": "strictly prior signal observations",
            "future_outcome_layer": "proxy_forward_outcomes written after all signal rows",
            "forward_outcomes_used_in_features": False,
        },
        "limitations": [
            "RESEARCH_PROXY 是历史代理研究，不是 STRICT_PIT，也不产生投资动作。",
            "日频长期收益存在重叠；季度和年度抽样按固定首个可用日期去重。",
            "Bootstrap 使用固定长度循环 block，只提供不确定性范围，不输出虚假精确 p 值。",
            "代理系列缺失会降低 available weight；没有用未来数据补齐。",
        ],
    }


def run_proxy_research(
    db_path: str,
    *,
    market: str = "NDX",
    start_date: str = "2000-01-01",
    end_date: str | None = None,
    proxy_run_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    if str(market).upper() != "NDX":
        raise ValueError("NDX_PROXY_RESEARCH_V1 当前只支持 NDX")
    repo = PITRepository(db_path)
    end = normalize_date(end_date or date.today())
    indexes = _load_proxy_indexes(repo, end)
    close = indexes["NDX_CLOSE"]
    if not close.dates:
        raise ValueError("当前代理轨道没有 NDX_CLOSE")
    start = normalize_date(start_date)
    days = [day for day in close.dates if start <= day <= end]
    if not days:
        raise ValueError("指定范围没有 NDX 交易日")
    run_id = proxy_run_id or f"phase2b-proxy-ndx-v1-{start}-{end}-{PROXY_CONFIG_HASH[:12]}"
    data_snapshot = _data_snapshot(indexes)
    run_material = {"market": "NDX", "start_date": start, "end_date": end, "proxy_model_version": PROXY_MODEL_VERSION, "config_hash": PROXY_CONFIG_HASH, "data_snapshot": data_snapshot}
    run_hash = sha256_json(run_material)
    run, created = repo.create_proxy_research_run({
        "proxy_run_id": run_id,
        "market": "NDX",
        "proxy_model_version": PROXY_MODEL_VERSION,
        "start_date": start,
        "end_date": end,
        "proxy_model_config": PROXY_MODEL_CONFIG,
        "config_hash": PROXY_CONFIG_HASH,
        "data_snapshot": data_snapshot,
        "data_cutoff": as_of_datetime(f"{end}T23:59:59.999999Z"),
        "created_at": _utc_now(),
        "started_at": _utc_now(),
    })
    if not created and run.get("status") in {"COMPLETED", "FAILED"} and not force:
        stored = repo.get_proxy_research_report(run_id)
        return {"run": run, "report": stored.get("report") if stored else run.get("summary", {}), "reused": True}
    try:
        signals = generate_proxy_signals(repo, proxy_run_id=run_id, start_date=start, end_date=end, indexes=indexes)
        for signal in signals:
            repo.append_proxy_signal(signal)
        # A separate evaluation-only pass may see rows after the run end.  It
        # is intentionally called only after every signal has been persisted.
        outcome_indexes = _load_proxy_indexes(repo, close.dates[-1])
        outcome_count = write_proxy_outcomes(repo, proxy_run_id=run_id, signals=signals, close_index=outcome_indexes["NDX_CLOSE"])
        report = build_proxy_report(repo, run_id)
        report["forward_outcomes_written"] = outcome_count
        repo.record_proxy_research_report(run_id, report)
        completed = repo.complete_proxy_research_run(run_id, status="COMPLETED", summary=report)
        return {"run": completed, "report": report, "reused": False}
    except Exception as exc:
        repo.complete_proxy_research_run(run_id, status="FAILED", error={"error": str(exc)})
        raise


def blind_proxy_date_view(repo: PITRepository, proxy_run_id: str, day: str) -> dict[str, Any] | None:
    run = repo.get_proxy_research_run(proxy_run_id)
    if not run:
        return None
    normalized = normalize_date(day)
    if not str(run["start_date"]) <= normalized <= str(run["end_date"]):
        return None
    signals = [item for item in repo.get_proxy_signals(proxy_run_id, limit=500000) if normalize_date(item["as_of_datetime"]) == normalized]
    if not signals:
        return None
    signal = signals[0]
    return {
        "proxy_run_id": proxy_run_id,
        "as_of": normalized,
        "as_of_datetime": as_of_datetime(f"{normalized}T23:59:59.999999Z"),
        "market": run["market"],
        "proxy_model_version": run["proxy_model_version"],
        "research_only": True,
        "strict_pit": False,
        "investment_action_eligible": False,
        "proxy_raw_score": signal.get("proxy_raw_score"),
        "proxy_available_weight": signal.get("proxy_available_weight"),
        "proxy_score_fraction": signal.get("proxy_score_fraction"),
        "proxy_rank": signal.get("proxy_rank"),
        "proxy_percentile": signal.get("proxy_percentile"),
        "proxy_b_raw_score": signal.get("proxy_b_raw_score"),
        "proxy_b_available_weight": signal.get("proxy_b_available_weight"),
        "proxy_b_score_fraction": signal.get("proxy_b_score_fraction"),
        "proxy_b_rank": signal.get("proxy_b_rank"),
        "proxy_b_percentile": signal.get("proxy_b_percentile"),
        "feature_values": signal.get("feature_values"),
        "feature_percentiles": signal.get("feature_percentiles"),
        "feature_status": signal.get("feature_status"),
        "input_observation_ids": signal.get("input_observation_ids"),
        "future_hidden": True,
    }


__all__ = [
    "PROXY_MODEL_VERSION", "PROXY_MODEL_CONFIG", "PROXY_CONFIG_HASH", "PROXY_FEATURE_ORDER",
    "generate_proxy_signals", "write_proxy_outcomes", "build_proxy_report", "run_proxy_research",
    "blind_proxy_date_view", "spearman", "_sample_signals",
]


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Run Phase 2B research-only proxy validation")
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parent / "data" / "dashboard.sqlite3")
    parser.add_argument("--market", default="NDX")
    parser.add_argument("--start-date", default="2000-01-01")
    parser.add_argument("--end-date")
    parser.add_argument("--run-id")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_proxy_research(str(args.db), market=args.market, start_date=args.start_date, end_date=args.end_date, proxy_run_id=args.run_id, force=args.force), ensure_ascii=False, indent=2))
