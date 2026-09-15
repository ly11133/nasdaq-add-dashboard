"""Strict PIT derived-feature store.

Every feature is calculated from ``PITRepository.get_history_available``.  The
legacy CSV and research-proxy snapshots are deliberately not imported here.
Incomplete features are still recorded with ``score_eligible=0`` so an audit
can distinguish a missing window from a missing calculation.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from data_contract import as_of_datetime, normalize_date, sha256_json
from pit_repository import PITRepository
from strict_pit import _monthly_values, _number, _rsi


FEATURE_VERSION = "FEATURE_STORE_V1"


def feature_input_hash(
    feature_name: str,
    series_id: str,
    feature_as_of: Any,
    feature_version: str,
    input_observation_ids: Iterable[str],
) -> str:
    ids = sorted({str(value) for value in input_observation_ids})
    return sha256_json({
        "feature_name": str(feature_name),
        "series_id": str(series_id),
        "feature_as_of": as_of_datetime(feature_as_of),
        "feature_version": str(feature_version),
        "input_observation_ids": ids,
    })


def _monthly_input_rows(rows: list[dict[str, Any]], cutoff_date: str, limit: int = 120) -> list[dict[str, Any]]:
    cutoff_month = date.fromisoformat(cutoff_date[:10]).replace(day=1)
    month_end: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        try:
            observed = date.fromisoformat(normalize_date(row.get("observation_date")))
        except (TypeError, ValueError):
            continue
        if observed >= cutoff_month or _number(row.get("value")) is None:
            continue
        key = (observed.year, observed.month)
        previous = month_end.get(key)
        if previous is None or str(row["observation_date"]) > str(previous["observation_date"]):
            month_end[key] = row
    return [month_end[key] for key in sorted(month_end)[-limit:]]


def _feature_result(
    *,
    feature_name: str,
    series_id: str,
    cutoff: str,
    value: float | None,
    input_rows: Iterable[Mapping[str, Any]],
    status: str,
    reason: str,
    version: str = FEATURE_VERSION,
) -> dict[str, Any]:
    ids = sorted({str(row["observation_version_id"]) for row in input_rows if row.get("observation_version_id")})
    input_hash = feature_input_hash(feature_name, series_id, cutoff, version, ids)
    return {
        "feature_name": feature_name,
        "series_id": series_id,
        "feature_as_of": cutoff,
        "as_of_datetime": cutoff,
        "feature_version": version,
        "value": value,
        "input_hash": input_hash,
        "input_observation_ids": ids,
        "score_eligible": value is not None and status == "scored",
        "status": status,
        "reason": reason,
    }


def _price_features(series_id: str, rows: list[dict[str, Any]], cutoff: str) -> list[dict[str, Any]]:
    numeric_rows = [row for row in rows if _number(row.get("value")) is not None]
    values = [_number(row.get("value")) for row in numeric_rows]
    values = [value for value in values if value is not None]
    latest = numeric_rows[-1:] if numeric_rows else []
    if not values:
        empty_reason = "严格 PIT 没有可用收盘观察；历史代理不会进入特征。"
        return [
            _feature_result(feature_name=name, series_id=series_id, cutoff=cutoff, value=None, input_rows=[], status="missing", reason=empty_reason)
            for name in ("ATH", "DRAW_DOWN", "RSI14", "MA200")
        ]
    high = max(values)
    ath = _feature_result(
        feature_name="ATH", series_id=series_id, cutoff=cutoff, value=high,
        input_rows=numeric_rows, status="scored", reason="严格 PIT 可用收盘序列的截止日历史最高收盘。",
    )
    if len(values) < 200:
        reason = f"仅有 {len(values)} 条严格 PIT 收盘观察；DRAW_DOWN/RSI14/MA200 需要更长窗口，拒绝用 proxy 补齐。"
        return [
            ath,
            _feature_result(feature_name="DRAW_DOWN", series_id=series_id, cutoff=cutoff, value=None, input_rows=numeric_rows, status="insufficient_history", reason=reason),
            _feature_result(feature_name="RSI14", series_id=series_id, cutoff=cutoff, value=None, input_rows=numeric_rows, status="insufficient_history", reason=reason + " RSI14 至少需要100个变化。"),
            _feature_result(feature_name="MA200", series_id=series_id, cutoff=cutoff, value=None, input_rows=numeric_rows, status="insufficient_history", reason=reason),
        ]
    current = values[-1]
    drawdown = -(1 - current / high) if high else None
    rsi = _rsi(values)
    ma200 = sum(values[-200:]) / 200
    full_reason = "严格 PIT 收盘序列；每个输入 observation version 的 available_at <= feature_as_of。"
    return [
        ath,
        _feature_result(feature_name="DRAW_DOWN", series_id=series_id, cutoff=cutoff, value=drawdown, input_rows=numeric_rows, status="scored", reason=full_reason),
        _feature_result(feature_name="RSI14", series_id=series_id, cutoff=cutoff, value=rsi, input_rows=numeric_rows, status="scored" if rsi is not None else "insufficient_history", reason=full_reason + " 使用 Wilder RSI14。"),
        _feature_result(feature_name="MA200", series_id=series_id, cutoff=cutoff, value=ma200, input_rows=numeric_rows, status="scored", reason=full_reason),
    ]


def _percentile_feature(feature_name: str, series_id: str, rows: list[dict[str, Any]], cutoff: str) -> dict[str, Any]:
    monthly_rows = _monthly_input_rows(rows, cutoff, limit=120)
    latest = rows[-1:] if rows else []
    input_rows = list(monthly_rows)
    for row in latest:
        if row.get("observation_version_id") not in {x.get("observation_version_id") for x in input_rows}:
            input_rows.append(row)
    values = [_number(row.get("value")) for row in monthly_rows]
    values = [value for value in values if value is not None]
    latest_value = _number(latest[0].get("value")) if latest else None
    if latest_value is None:
        return _feature_result(feature_name=feature_name, series_id=series_id, cutoff=cutoff, value=None, input_rows=input_rows, status="missing", reason="严格 PIT 没有截止日前的可用观察。")
    if len(values) < 60:
        return _feature_result(feature_name=feature_name, series_id=series_id, cutoff=cutoff, value=None, input_rows=input_rows, status="insufficient_history", reason=f"只有 {len(values)} 个截止日前完整月末；至少需要60个，不能用未来或 proxy 月份补齐。")
    percentile = sum(value <= latest_value for value in values) / len(values)
    return _feature_result(feature_name=feature_name, series_id=series_id, cutoff=cutoff, value=percentile, input_rows=input_rows, status="scored", reason=f"严格 PIT 过去{len(values)}个完整月末经验分位；当前值不读取 cutoff 之后记录。")


def generate_feature_snapshots(db_path: Path | str, as_of: Any, *, market: str = "NDX") -> dict[str, Any]:
    """Calculate and append all Phase 1C feature snapshots for one cutoff."""

    market = str(market).upper()
    cutoff = as_of_datetime(as_of)
    if market == "NDX":
        close_series, vxn_series, real_series, nfci_series = "NDX_CLOSE", "NDX_VXN", "US10Y_REAL", "US_NFCI"
    else:
        close_series, vxn_series, real_series, nfci_series = f"{market}_CLOSE", f"{market}_VIXCLS", f"{market}_DFII10", f"{market}_NFCI"
    repo = PITRepository(db_path)
    histories = {
        close_series: repo.get_history_available(close_series, cutoff),
        vxn_series: repo.get_history_available(vxn_series, cutoff),
        real_series: repo.get_history_available(real_series, cutoff),
        nfci_series: repo.get_history_available(nfci_series, cutoff),
    }
    snapshots = _price_features(close_series, histories[close_series], cutoff)
    snapshots.extend([
        _percentile_feature("VXN_PERCENTILE", vxn_series, histories[vxn_series], cutoff),
        _percentile_feature("REAL_YIELD_PERCENTILE", real_series, histories[real_series], cutoff),
        _percentile_feature("NFCI_PERCENTILE", nfci_series, histories[nfci_series], cutoff),
    ])
    inserted = 0
    duplicates = 0
    for snapshot in snapshots:
        _, created = repo.record_feature_snapshot(snapshot)
        if created:
            inserted += 1
        else:
            duplicates += 1
    return {
        "feature_version": FEATURE_VERSION,
        "market": market,
        "feature_as_of": cutoff,
        "snapshots": snapshots,
        "inserted": inserted,
        "duplicates": duplicates,
        "score_eligible": sum(bool(item["score_eligible"]) for item in snapshots),
        "strict_input_policy": "PITRepository.get_history_available only; no CSV/proxy fallback",
    }


__all__ = ["FEATURE_VERSION", "feature_input_hash", "generate_feature_snapshots"]
