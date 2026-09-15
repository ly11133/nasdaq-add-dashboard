"""Explicit data freshness rules used by strict evaluation and automation."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Mapping

from data_contract import as_of_datetime, normalize_date, parse_datetime


DEFAULT_FRESHNESS_RULES: dict[str, dict[str, Any]] = {
    "NDX_CLOSE": {"max_age_days": 7, "frequency": "daily", "expected_release_pattern": "US market close; next business-day retrieval is acceptable", "stale_behavior": "STALE_REJECT", "rule_version": "FRESHNESS_RULES_V1"},
    "NDX_VXN": {"max_age_days": 7, "frequency": "daily", "expected_release_pattern": "US market close; next business-day retrieval is acceptable", "stale_behavior": "STALE_REJECT", "rule_version": "FRESHNESS_RULES_V1"},
    "VIX": {"max_age_days": 7, "frequency": "daily", "expected_release_pattern": "US market close; next business-day retrieval is acceptable", "stale_behavior": "STALE_REJECT", "rule_version": "FRESHNESS_RULES_V1"},
    "US10Y_REAL": {"max_age_days": 7, "frequency": "daily", "expected_release_pattern": "H.15 daily release; conservative next-day use", "stale_behavior": "STALE_REJECT", "rule_version": "FRESHNESS_RULES_V1"},
    "US_NFCI": {"max_age_days": 14, "frequency": "weekly", "expected_release_pattern": "Chicago Fed weekly ending Friday; publication day may lag observation day", "stale_behavior": "WITHIN_WINDOW", "rule_version": "FRESHNESS_RULES_V1"},
    "NDX_FORWARD_PE": {"max_age_days": 14, "frequency": "provider_defined", "expected_release_pattern": "provider refresh; no assumed daily release", "stale_behavior": "STALE_REJECT", "rule_version": "FRESHNESS_RULES_V1"},
    "NDX_TTM_PE": {"max_age_days": 14, "frequency": "provider_defined", "expected_release_pattern": "provider refresh; no assumed daily release", "stale_behavior": "STALE_REJECT", "rule_version": "FRESHNESS_RULES_V1"},
    "NDX_FORWARD_EPS": {"max_age_days": 14, "frequency": "provider_defined", "expected_release_pattern": "provider refresh; no assumed daily release", "stale_behavior": "STALE_REJECT", "rule_version": "FRESHNESS_RULES_V1"},
    "NDX_EPS_REVISION": {"max_age_days": 45, "frequency": "provider_defined", "expected_release_pattern": "same fiscal-period snapshot; no assumed daily release", "stale_behavior": "STALE_REJECT", "rule_version": "FRESHNESS_RULES_V1"},
    "NDX_BREADTH_MA200": {"max_age_days": 7, "frequency": "daily", "expected_release_pattern": "same-day complete member set required", "stale_behavior": "STALE_REJECT", "rule_version": "FRESHNESS_RULES_V1"},
}


def assess_series_freshness(
    series_id: str,
    latest: Mapping[str, Any] | None,
    as_of: Any,
    *,
    rule: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a visible freshness decision; never silently substitute a day."""

    series_id = str(series_id)
    selected = dict(rule or DEFAULT_FRESHNESS_RULES.get(series_id) or {
        "max_age_days": 0,
        "frequency": "unknown",
        "expected_release_pattern": "no rule",
        "stale_behavior": "STALE_REJECT",
        "rule_version": "UNRESOLVED",
    })
    cutoff = as_of_datetime(as_of)
    result = {
        "series_id": series_id,
        "status": "missing",
        "observation_date": None,
        "age_days": None,
        "max_age_days": int(selected.get("max_age_days", 0)),
        "frequency": str(selected.get("frequency") or "unknown"),
        "expected_release_pattern": str(selected.get("expected_release_pattern") or ""),
        "stale_behavior": str(selected.get("stale_behavior") or "STALE_REJECT"),
        "rule_version": str(selected.get("rule_version") or "UNRESOLVED"),
        "fallback_used": False,
        "reason": "没有可用观察；不使用前一天旧值。",
    }
    if not latest:
        return result
    try:
        observation_date = normalize_date(latest.get("observation_date"), field="observation_date")
        observed = date.fromisoformat(observation_date)
        cutoff_date = parse_datetime(cutoff, field="as_of").date()
    except (TypeError, ValueError):
        result.update(status="stale", reason="观察日期无法解析，拒绝使用。")
        return result
    age_days = (cutoff_date - observed).days
    result.update(observation_date=observation_date, age_days=age_days)
    if age_days < 0:
        result.update(status="stale", reason="观察日期晚于评估截止，拒绝前视记录。")
    elif age_days <= int(selected.get("max_age_days", 0)):
        result.update(status="fresh", fallback_used=age_days > 0, reason=(
            "按明确 freshness window 使用最近观察。" if age_days > 0 else "观察日期与评估截止一致。"
        ))
    else:
        result.update(status="stale", reason="超过 freshness window；需要新采集或显式 DATA_STALE。")
    return result


def evaluate_freshness(latest_by_series: Mapping[str, Mapping[str, Any] | None], as_of: Any, rules: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    rules = rules or DEFAULT_FRESHNESS_RULES
    return {
        str(series_id): assess_series_freshness(str(series_id), latest, as_of, rule=rules.get(str(series_id)))
        for series_id, latest in latest_by_series.items()
    }


def load_freshness_rules_from_db(db_path: Path | str) -> dict[str, dict[str, Any]]:
    import sqlite3

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT series_id, max_age_days, frequency, expected_release_pattern, stale_behavior, rule_version FROM freshness_rules ORDER BY series_id, rule_version"
        ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result[str(row["series_id"])] = dict(row)
    return result or dict(DEFAULT_FRESHNESS_RULES)


__all__ = ["DEFAULT_FRESHNESS_RULES", "assess_series_freshness", "evaluate_freshness", "load_freshness_rules_from_db"]
