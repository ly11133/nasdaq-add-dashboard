"""Phase 2A chronological dual-track replay engine.

The engine has two deliberately different readers:

* ``STRICT_PIT`` calls ``PITRepository.get_history_available`` and can only
  see versions whose proven ``available_at`` is before the replay cutoff.
* ``RESEARCH_PROXY`` calls ``PITRepository.get_history_proxy`` and is clearly
  labelled research-only.  It may use bulk historical proxies, but only when
  their observation date is on or before the current replay day.

Both paths walk trading days in ascending order.  Future returns are computed
by a separate evaluation function after all decisions have been written; no
decision function imports or queries ``forward_outcomes``.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import math
import random
from typing import Any, Iterable, Mapping

from data_contract import as_of_datetime, normalize_date, sha256_json
from freshness import assess_series_freshness, load_freshness_rules_from_db
from pit_repository import PITRepository, register_score_model
from score_model import CURRENT_SCORE_MODEL_VERSION, load_model
from strict_pit import NDX_SERIES, ROW_META, _number, _rsi, _valuation_proof_complete, _breadth_proof_complete
from verification import RULES


REPLAY_FEATURE_VERSION = "REPLAY_FEATURES_V1"
REPLAY_PRICE_FEATURE_VERSION = "REPLAY_PRICE_FEATURES_V1"
REPLAY_PERCENTILE_FEATURE_VERSION = "REPLAY_PERCENTILE_FEATURES_V1"
REPLAY_MODES = ("STRICT_PIT", "RESEARCH_PROXY")
DEFAULT_PROXY_START = "2000-01-01"
HORIZONS = (1, 3, 6, 12, 36, 60)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _asof_day(day: str) -> str:
    return as_of_datetime(f"{normalize_date(day)}T23:59:59.999999Z")


def _finite(value: Any) -> float | None:
    result = _number(value)
    return result if result is not None and math.isfinite(result) else None


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + int(months)
    year, month0 = divmod(index, 12)
    month = month0 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _empirical_percentile(rows: list[dict[str, Any]], cutoff_date: str) -> tuple[float | None, int, list[dict[str, Any]]]:
    """Calculate the frozen prior-month-end percentile without future rows."""

    cutoff_month = date.fromisoformat(cutoff_date).replace(day=1)
    month_end: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        try:
            observed = date.fromisoformat(normalize_date(row.get("observation_date")))
        except (TypeError, ValueError):
            continue
        if observed >= cutoff_month:
            continue
        value = _finite(row.get("value"))
        if value is None:
            continue
        key = (observed.year, observed.month)
        previous = month_end.get(key)
        if previous is None or str(row.get("observation_date")) > str(previous.get("observation_date")):
            month_end[key] = row
    selected = [month_end[key] for key in sorted(month_end)[-120:]]
    latest = rows[-1] if rows else None
    latest_value = _finite(latest.get("value")) if latest else None
    if latest_value is None or len(selected) < 60:
        return None, len(selected), selected + ([latest] if latest and latest not in selected else [])
    percentile = sum(_finite(row.get("value")) <= latest_value for row in selected if _finite(row.get("value")) is not None) / len(selected)
    inputs = selected + ([latest] if latest not in selected else [])
    return percentile, len(selected), inputs


def _audit_ids(rows: Iterable[Mapping[str, Any]], *, limit: int = 512) -> list[str]:
    """Keep feature audit payloads bounded while retaining deterministic IDs.

    A long-run ATH/drawdown calculation uses every preceding price.  The
    feature's ``value_json`` stores a hash of the complete chronological input
    list, while this audit list retains the latest window and the historical
    high setters so the API stays practical for a multi-decade replay.
    """

    rows = list(rows)
    ids = [str(row["observation_version_id"]) for row in rows if row.get("observation_version_id")]
    if len(ids) <= limit:
        return sorted(set(ids))
    selected: list[str] = []
    high = -float("inf")
    for row in rows:
        value = _finite(row.get("value"))
        if value is not None and value >= high:
            high = value
            if row.get("observation_version_id"):
                selected.append(str(row["observation_version_id"]))
    selected.extend(ids[-limit:])
    selected.extend(ids[:2])
    return sorted(set(selected))


def _full_input_hash(rows: Iterable[Mapping[str, Any]]) -> str:
    return sha256_json(sorted({str(row["observation_version_id"]) for row in rows if row.get("observation_version_id")}))


class _SeriesIndex:
    """In-memory, date-sorted view loaded exclusively through PITRepository."""

    def __init__(self, rows: Iterable[Mapping[str, Any]]):
        clean: list[dict[str, Any]] = []
        for row in rows:
            value = _finite(row.get("value"))
            if value is None:
                continue
            item = dict(row)
            item["observation_date"] = normalize_date(item.get("observation_date"))
            item["value"] = value
            clean.append(item)
        clean.sort(key=lambda row: (row["observation_date"], str(row.get("observation_version_id", ""))))
        # Repository queries already select one version per observation date,
        # but keeping the latest deterministic row here protects replay from
        # malformed fixtures without reading another source.
        by_date: dict[str, dict[str, Any]] = {}
        for row in clean:
            by_date[row["observation_date"]] = row
        self.rows = [by_date[key] for key in sorted(by_date)]
        self.dates = [row["observation_date"] for row in self.rows]

    def until(self, day: str, *, asof_datetime: str | None = None, strict: bool = False) -> list[dict[str, Any]]:
        end = bisect_right(self.dates, normalize_date(day))
        rows = self.rows[:end]
        if not strict or not asof_datetime:
            return rows
        cutoff = as_of_datetime(asof_datetime)
        return [row for row in rows if row.get("available_at") and str(row["available_at"]) <= cutoff]

    def after(self, day: str, target: date | None = None) -> list[dict[str, Any]]:
        start = bisect_right(self.dates, normalize_date(day))
        if target is None:
            return self.rows[start:]
        target_text = target.isoformat()
        start = max(start, bisect_left(self.dates, target_text))
        return self.rows[start:]

    def first_on_or_after(self, target: date) -> dict[str, Any] | None:
        index = bisect_left(self.dates, target.isoformat())
        return self.rows[index] if index < len(self.rows) else None


def _series_map(market: str) -> dict[str, str]:
    market = str(market).upper()
    if market == "NDX":
        return dict(NDX_SERIES)
    return {
        "drawdown": f"{market}_CLOSE",
        "vxn": f"{market}_VIXCLS",
        "vix": f"{market}_VIXCLS",
        "real": f"{market}_DFII10",
        "nfci": f"{market}_NFCI",
        "forward_pe": f"{market}_FORWARD_PE",
        "ttm_pe": f"{market}_TTM_PE",
        "revision": f"{market}_EPS_REVISION",
        "growth": f"{market}_FORWARD_EPS",
        "breadth": f"{market}_BREADTH_MA200",
    }


def _feature_record(
    *,
    run_id: str,
    asof: str,
    name: str,
    series_id: str,
    version: str,
    value: float | None,
    input_rows: Iterable[Mapping[str, Any]],
    status: str,
    reason: str,
    full_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    ids = _audit_ids(input_rows)
    feature_hash = sha256_json({
        "feature_name": name,
        "series_id": series_id,
        "feature_as_of": asof,
        "feature_version": version,
        "input_observation_ids": ids,
    })
    full_hash = sha256_json(sorted({str(x) for x in (full_ids or ids)}))
    suffix = "" if full_hash == feature_hash else f" 完整输入集合哈希={full_hash}；审计列表保留关键边界与最近窗口。"
    return {
        "replay_run_id": run_id,
        "as_of_datetime": asof,
        "feature_name": name,
        "series_id": series_id,
        "feature_version": version,
        "value": value,
        "input_hash": feature_hash,
        "input_observation_ids": ids,
        "score_eligible": value is not None and status == "scored",
        "status": status,
        "reason": reason + suffix,
        "full_input_hash": full_hash,
    }


def _make_features(run_id: str, asof: str, market: str, histories: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    series = _series_map(market)
    close_sid = series["drawdown"]
    close_rows = histories.get(close_sid, [])
    values = [_finite(row.get("value")) for row in close_rows]
    values = [value for value in values if value is not None]
    features: list[dict[str, Any]] = []
    if values:
        high = max(values)
        high_rows = [row for row in close_rows if _finite(row.get("value")) == high]
        features.append(_feature_record(
            run_id=run_id, asof=asof, name="ATH", series_id=close_sid,
            version=REPLAY_PRICE_FEATURE_VERSION, value=high,
            input_rows=high_rows + close_rows[-1:], full_ids=[row.get("observation_version_id") for row in close_rows],
            status="scored", reason="截至当前 replay 日的严格/代理历史最高收盘；不读取未来行。",
        ))
    else:
        features.append(_feature_record(
            run_id=run_id, asof=asof, name="ATH", series_id=close_sid,
            version=REPLAY_PRICE_FEATURE_VERSION, value=None, input_rows=[], status="missing",
            reason="截至当前 replay 日没有可用收盘观察。",
        ))
    if len(values) >= 200:
        high = max(values)
        current = values[-1]
        drawdown = -(1 - current / high) if high else None
        ma_rows = close_rows[-200:]
        features.append(_feature_record(
            run_id=run_id, asof=asof, name="DRAW_DOWN", series_id=close_sid,
            version=REPLAY_PRICE_FEATURE_VERSION, value=drawdown, input_rows=close_rows,
            full_ids=[row.get("observation_version_id") for row in close_rows], status="scored",
            reason="回撤=当前收盘/截至当日历史最高收盘−1；计算窗口在 replay 日截断。",
        ))
        rsi = _rsi(values)
        rsi_rows = close_rows[-101:] if len(close_rows) > 101 else close_rows
        features.append(_feature_record(
            run_id=run_id, asof=asof, name="RSI14", series_id=close_sid,
            version=REPLAY_PRICE_FEATURE_VERSION, value=rsi, input_rows=rsi_rows,
            full_ids=[row.get("observation_version_id") for row in close_rows],
            status="scored" if rsi is not None else "insufficient_history",
            reason="Wilder RSI14；只使用截至 replay 日的价格变化，至少需要100个变化。",
        ))
        features.append(_feature_record(
            run_id=run_id, asof=asof, name="MA200", series_id=close_sid,
            version=REPLAY_PRICE_FEATURE_VERSION, value=sum(values[-200:]) / 200,
            input_rows=ma_rows, status="scored", reason="最近200个截至 replay 日的收盘均值。",
        ))
    else:
        reason = f"截至当前 replay 日只有 {len(values)} 条收盘观察；需要200条，拒绝用未来或另一轨道补齐。"
        features.extend([
            _feature_record(run_id=run_id, asof=asof, name="DRAW_DOWN", series_id=close_sid, version=REPLAY_PRICE_FEATURE_VERSION, value=None, input_rows=close_rows, status="insufficient_history", reason=reason),
            _feature_record(run_id=run_id, asof=asof, name="RSI14", series_id=close_sid, version=REPLAY_PRICE_FEATURE_VERSION, value=None, input_rows=close_rows, status="insufficient_history", reason=reason + " RSI14 还需要100个变化。"),
            _feature_record(run_id=run_id, asof=asof, name="MA200", series_id=close_sid, version=REPLAY_PRICE_FEATURE_VERSION, value=None, input_rows=close_rows, status="insufficient_history", reason=reason),
        ])
    for key, name in (("vxn", "VXN_PERCENTILE"), ("real", "REAL_YIELD_PERCENTILE"), ("nfci", "NFCI_PERCENTILE")):
        sid = series[key]
        rows = histories.get(sid, [])
        percentile, count, input_rows = _empirical_percentile(rows, normalize_date(asof))
        status = "scored" if percentile is not None else "insufficient_history" if rows else "missing"
        reason = f"截至当前 replay 日的此前 {count} 个完整月末经验分位；不使用当前月未来值。"
        if not rows:
            reason = "截至当前 replay 日没有可用观察；不使用未来或跨系列替代。"
        elif percentile is None:
            reason += "至少需要60个同口径月末。"
        features.append(_feature_record(
            run_id=run_id, asof=asof, name=name, series_id=sid,
            version=REPLAY_PERCENTILE_FEATURE_VERSION, value=percentile,
            input_rows=input_rows, status=status, reason=reason,
        ))
    return features


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return rows[-1] if rows else None


def _score_rows(
    *,
    mode: str,
    market: str,
    asof: str,
    histories: Mapping[str, list[dict[str, Any]]],
    features: list[dict[str, Any]],
    freshness_rules: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    series = _series_map(market)
    by_name = {row["feature_name"]: row for row in features}
    rows: list[dict[str, Any]] = []

    def add(key: str, value: Any = None, score: float | None = None, *, status: str = "missing", observed: str | None = None, reason: str = "", source: str = "PITRepository", origin: str | None = None) -> None:
        label, module, weight = ROW_META[key]
        formula, category, calculation = RULES[key]
        rows.append({
            "key": key, "label": label, "module": module, "weight": weight,
            "value": _finite(value) if _finite(value) is not None else value,
            "score": None if score is None else float(score), "status": status,
            "date": observed, "reason": reason, "source": source,
            "formula": formula, "category": category, "calculation": calculation,
            "eligibility_origin": origin,
        })

    close_sid = series["drawdown"]
    close_rows = histories.get(close_sid, [])
    close_latest = _latest(close_rows)
    draw = by_name.get("DRAW_DOWN")
    rsi = by_name.get("RSI14")
    if draw and draw.get("score_eligible"):
        value = _finite(draw.get("value"))
        dd = max(0.0, -(value or 0.0))
        add("drawdown", value, 20 * min(dd / 0.4, 1.0), status="scored", observed=close_latest.get("observation_date") if close_latest else None, origin=close_latest.get("eligibility_origin") if close_latest else None, reason="截至当日历史最高收盘计算；该价格输入已由当前 replay 轨道截断。")
    else:
        add("drawdown", draw.get("value") if draw else None, status="insufficient_history" if draw and draw.get("status") != "missing" else "missing", observed=close_latest.get("observation_date") if close_latest else None, origin=close_latest.get("eligibility_origin") if close_latest else None, reason=(draw or {}).get("reason", "没有截至当日的合格收盘历史。"))
    if rsi and rsi.get("score_eligible"):
        rv = _finite(rsi.get("value")); add("rsi", rv, 5 * max(0.0, min(1.0, (50.0 - (rv or 50.0)) / 20.0)), status="scored", observed=close_latest.get("observation_date") if close_latest else None, origin=close_latest.get("eligibility_origin") if close_latest else None, reason=rsi.get("reason", ""))
    else:
        add("rsi", rsi.get("value") if rsi else None, status="insufficient_history" if rsi and rsi.get("status") != "missing" else "missing", observed=close_latest.get("observation_date") if close_latest else None, origin=close_latest.get("eligibility_origin") if close_latest else None, reason=(rsi or {}).get("reason", "没有截至当日的 RSI 历史。"))

    for key, feature_name, invert in (("vxn", "VXN_PERCENTILE", False), ("real", "REAL_YIELD_PERCENTILE", True), ("nfci", "NFCI_PERCENTILE", True)):
        sid = series[key]; latest = _latest(histories.get(sid, [])); feature = by_name.get(feature_name)
        if feature and feature.get("score_eligible"):
            p = _finite(feature.get("value")); add(key, p, ROW_META[key][2] * ((1 - p) if invert else p), status="scored", observed=latest.get("observation_date") if latest else None, origin=latest.get("eligibility_origin") if latest else None, reason=feature.get("reason", ""))
        else:
            add(key, feature.get("value") if feature else None, status="insufficient_history" if feature and feature.get("status") != "missing" else "missing", observed=latest.get("observation_date") if latest else None, origin=latest.get("eligibility_origin") if latest else None, reason=(feature or {}).get("reason", "没有截至当日的同口径历史。"))

    vix_latest = _latest(histories.get(series["vix"], []))
    add("vix", vix_latest.get("value") if vix_latest else None, 0, status="observed" if vix_latest else "missing", observed=vix_latest.get("observation_date") if vix_latest else None, origin=vix_latest.get("eligibility_origin") if vix_latest else None, reason="背景指标，不进入 V2.0 总分。")

    # Proxy PE is useful for historical research only; strict PE remains
    # governed by score_eligible and the same 60-month rule.
    for key, sid in (("forward_pe", series["forward_pe"]), ("ttm_pe", series["ttm_pe"])):
        source_rows = histories.get(sid, [])
        latest = _latest(source_rows)
        p, count, input_rows = _empirical_percentile(source_rows, normalize_date(asof))
        if p is not None and latest:
            add(key, latest.get("value"), ROW_META[key][2] * (1 - p), status="scored", observed=latest.get("observation_date"), origin=latest.get("eligibility_origin"), reason=f"{mode} 截至当日的 {count} 个完整月末经验分位；研究代理不代表严格历史发布证明。")
        elif latest:
            add(key, latest.get("value"), status="insufficient_history", observed=latest.get("observation_date"), origin=latest.get("eligibility_origin"), reason=f"截至当日只有 {count} 个完整月末；至少需要60个，不能用未来数据补齐。")
        else:
            add(key, status="missing", reason="截至当日没有该估值系列观察；未知项不重分配权重。")

    # Current free EPS captures are rolling/unknown-period.  Only an explicit
    # fixed fiscal-period proxy may score; otherwise the item is visible as
    # missing and cannot become an accidental look-ahead label.
    for key, sid in (("revision", series["revision"]), ("growth", series["growth"])):
        candidates = histories.get(sid, [])
        latest = _latest(candidates)
        if latest and latest.get("metadata") and latest["metadata"].get("fiscal_year") and latest["metadata"].get("period") in {"FY", "forward_fiscal_year"}:
            value = _finite(latest.get("value"));
            if value is not None:
                if key == "revision": score = 15 if value >= .05 else 10 if value >= 0 else 5 if value >= -.05 else 0
                else: score = 10 if value >= .15 else 7 if value >= .05 else 3 if value >= 0 else 0
                add(key, value, score, status="scored", observed=latest.get("observation_date"), origin=latest.get("eligibility_origin"), reason="同财政期固定版本代理；只使用截至当日记录。")
                continue
        add(key, latest.get("value") if latest else None, status="candidate" if latest else "missing", observed=latest.get("observation_date") if latest else None, origin=latest.get("eligibility_origin") if latest else None, reason="截至当日没有可核验的同财政期固定预测版本；滚动 EPS 不冒充修正方向。")

    breadth_sid = series["breadth"]; breadth_latest = _latest(histories.get(breadth_sid, []))
    if breadth_latest:
        value = _finite(breadth_latest.get("value"))
        add("breadth", value, ROW_META["breadth"][2] * max(0.0, min(1.0, value or 0.0)), status="scored", observed=breadth_latest.get("observation_date"), origin=breadth_latest.get("eligibility_origin"), reason="研究轨允许使用截至当日的历史代理宽度；严格轨仍要求完整成员证明。") if mode == "RESEARCH_PROXY" else add("breadth", value, None, status="candidate", observed=breadth_latest.get("observation_date"), origin=breadth_latest.get("eligibility_origin"), reason="严格轨未证明当日完整成员和分母。")
    else:
        add("breadth", status="missing", reason="截至当日没有宽度观察；未知项不重分配权重。")

    # Freshness is a gate only; it never substitutes an older row silently.
    latest_by_series = {
        sid: _latest(histories.get(sid, []))
        for sid in (series["drawdown"], series["vxn"], series["vix"], series["real"], series["nfci"])
    }
    freshness = {
        sid: assess_series_freshness(sid, row, asof, rule=freshness_rules.get(sid))
        for sid, row in latest_by_series.items()
    }
    stale = [sid for sid, item in freshness.items() if item["status"] == "stale"]
    scored = [row for row in rows if row["score"] is not None and row["weight"] > 0]
    known = sum(float(row["score"]) for row in scored)
    coverage = sum(float(row["weight"]) for row in scored)
    mapping = {row["key"]: row for row in rows}
    valuation = sum(mapping[key]["score"] or 0 for key in ("forward_pe", "ttm_pe"))
    earnings = sum(mapping[key]["score"] or 0 for key in ("revision", "growth"))
    if stale:
        gate = "DATA_STALE"; decision = "DATA_STALE"
    elif coverage < 100:
        gate = "INSUFFICIENT_EVIDENCE"; decision = "INSUFFICIENT_EVIDENCE"
    else:
        gate = "PASS"
        if known < 40: decision = "NO_SIGNAL"
        elif known < 60: decision = "WATCH"
        elif known < 75: decision = "TACTICAL_BUY_ALLOWED"
        else: decision = "STRONG_OPPORTUNITY"
    missing = [
        {"key": row["key"], "label": row["label"], "weight": row["weight"], "status": row["status"], "reason": row["reason"]}
        for row in rows if row["score"] is None and row["weight"] > 0
    ]
    return {
        "rows": rows, "score": known, "coverage": coverage, "gate_status": gate,
        "decision": decision, "missing_items": missing,
        "reason": {
            "as_of_datetime": asof, "mode": mode,
            "strict_pit": mode == "STRICT_PIT", "research_only": mode == "RESEARCH_PROXY",
            "no_future_observations": True, "no_forward_outcomes": True,
            "freshness": freshness, "stale_series": stale,
            "core_scores": {"valuation": valuation, "earnings": earnings},
        },
    }


class ReplayEngine:
    """Frozen-run coordinator for one market and one replay mode."""

    def __init__(self, db_path: Path | str, *, market: str = "NDX"):
        self.db_path = Path(db_path)
        self.market = str(market).upper()
        self.repo = PITRepository(self.db_path)
        self.model = load_model()
        register_score_model(self.db_path, self.model)
        self.freshness_rules = load_freshness_rules_from_db(self.db_path)

    def _load_histories(self, mode: str, end_date: str) -> dict[str, _SeriesIndex]:
        mode = str(mode).upper()
        if mode not in REPLAY_MODES:
            raise ValueError("replay mode 只能是 STRICT_PIT 或 RESEARCH_PROXY")
        result: dict[str, _SeriesIndex] = {}
        for sid in dict.fromkeys(_series_map(self.market).values()):
            if mode == "STRICT_PIT":
                rows = self.repo.get_history_available(sid, _asof_day(end_date))
            else:
                rows = self.repo.get_history_proxy(sid, _asof_day(end_date))
            result[sid] = _SeriesIndex(rows)
        return result

    def _available_days(self, index: _SeriesIndex, start_date: str, end_date: str) -> list[str]:
        start = normalize_date(start_date); end = normalize_date(end_date)
        return [day for day in index.dates if start <= day <= end]

    def evaluate_date(self, *, run_id: str, mode: str, day: str, indexes: Mapping[str, _SeriesIndex]) -> dict[str, Any]:
        asof = _asof_day(day)
        histories: dict[str, list[dict[str, Any]]] = {}
        for sid, index in indexes.items():
            histories[sid] = index.until(day, asof_datetime=asof, strict=mode == "STRICT_PIT")
        features = _make_features(run_id, asof, self.market, histories)
        scored = _score_rows(mode=mode, market=self.market, asof=asof, histories=histories, features=features, freshness_rules=self.freshness_rules)
        feature_ids: list[str] = []
        input_ids: list[str] = []
        for feature in features:
            feature_id, _ = self.repo.append_replay_feature(feature)
            feature_ids.append(feature_id)
            input_ids.extend(feature.get("input_observation_ids", []))
        # Add the complete input hash to the reason without leaking the
        # underlying future rows.  Decisions retain the bounded audit IDs.
        feature_hash = sha256_json(sorted(feature_ids))
        decision = {
            "replay_run_id": run_id,
            "sequence_no": 0,  # filled by run() in chronological order
            "as_of_datetime": asof,
            "market": self.market,
            "mode": mode,
            "score_model_version": self.model["model_version"],
            "feature_hash": feature_hash,
            "input_observation_ids": sorted(set(input_ids)),
            "feature_ids": sorted(feature_ids),
            "coverage": scored["coverage"],
            "score": scored["score"],
            "gate_status": scored["gate_status"],
            "decision": scored["decision"],
            "reason": {**scored["reason"], "feature_model_versions": {"price": REPLAY_PRICE_FEATURE_VERSION, "percentile": REPLAY_PERCENTILE_FEATURE_VERSION}},
            "missing_items": scored["missing_items"],
        }
        decision["input_hash"] = sha256_json(decision["input_observation_ids"])
        decision["features"] = features
        decision["rows"] = scored["rows"]
        return decision

    def run(
        self,
        *,
        mode: str = "RESEARCH_PROXY",
        start_date: str | None = None,
        end_date: str | None = None,
        replay_run_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        mode = str(mode).upper()
        if mode not in REPLAY_MODES:
            raise ValueError("replay mode 只能是 STRICT_PIT 或 RESEARCH_PROXY")
        # Load a broad enough history first to derive the real date range.
        probe_end = normalize_date(end_date or date.today())
        indexes = self._load_histories(mode, probe_end)
        close_index = indexes[_series_map(self.market)["drawdown"]]
        if not close_index.dates and mode == "STRICT_PIT":
            # A strict run with no qualified historical close is still a
            # valid, auditable run.  It produces zero decisions rather than
            # borrowing a proxy or inventing a date range.
            start = normalize_date(start_date or date.today())
            end = normalize_date(end_date or start)
        elif not close_index.dates:
            raise ValueError("当前 replay 轨道没有收盘观察，无法生成交易日序列")
        else:
            start = normalize_date(start_date or (DEFAULT_PROXY_START if mode == "RESEARCH_PROXY" else close_index.dates[0]))
            end = normalize_date(end_date or close_index.dates[-1])
        days = self._available_days(close_index, start, end)
        if not days and mode == "STRICT_PIT":
            days = []
        elif not days:
            raise ValueError("指定范围没有可用交易日")
        # Re-read through the final end so an explicit end date cannot be
        # accidentally widened by a probe date.  A separate, evaluation-only
        # price index is retained through the latest available date so
        # forward returns do not get truncated at the decision end date.
        indexes = self._load_histories(mode, end)
        outcome_probe_end = max(end, close_index.dates[-1] if close_index.dates else end)
        outcome_indexes = self._load_histories(mode, outcome_probe_end)
        model_material = {
            "model_version": self.model["model_version"],
            "config_hash": self.model["config_hash"],
            "mode": mode, "market": self.market,
            "start_date": start, "end_date": end,
            "feature_model_versions": {"price": REPLAY_PRICE_FEATURE_VERSION, "percentile": REPLAY_PERCENTILE_FEATURE_VERSION},
        }
        series_snapshot = {}
        for sid, index in indexes.items():
            ids = [str(row.get("observation_version_id")) for row in index.rows if row.get("observation_version_id")]
            series_snapshot[sid] = {"count": len(index.rows), "min_date": index.dates[0] if index.dates else None, "max_date": index.dates[-1] if index.dates else None, "input_hash": sha256_json(ids)}
        data_snapshot = {"repository": "PITRepository", "mode_reader": "get_history_available" if mode == "STRICT_PIT" else "get_history_proxy", "series": series_snapshot}
        material = {**model_material, "data_snapshot": data_snapshot, "data_cutoff": _asof_day(end)}
        run_config_hash = sha256_json(material)
        run_id = replay_run_id or f"replay-{self.market.lower()}-{mode.lower()}-{start}-{end}-{run_config_hash[:12]}"
        run, created = self.repo.create_replay_run({
            "replay_run_id": run_id, "market": self.market, "mode": mode,
            "start_date": start, "end_date": end,
            "score_model_version": self.model["model_version"],
            "feature_model_versions": material["feature_model_versions"],
            "data_snapshot": data_snapshot,
            "data_cutoff": _asof_day(end),
            "run_config_hash": run_config_hash,
            "created_at": _utc_now(), "started_at": _utc_now(),
        })
        if not created and run.get("status") in {"COMPLETED", "FAILED"} and not force:
            report = self.repo.get_replay_report(run_id)
            return {"run": run, "report": report.get("report") if report else run.get("summary", {}), "reused": True}
        decisions: list[dict[str, Any]] = []
        try:
            for sequence, day in enumerate(days, start=1):
                result = self.evaluate_date(run_id=run_id, mode=mode, day=day, indexes=indexes)
                result["sequence_no"] = sequence
                result.pop("features", None); result.pop("rows", None)
                decision_id, _ = self.repo.append_replay_decision(result)
                result["replay_decision_id"] = decision_id
                decisions.append(result)
            # Future outcomes are intentionally a second pass and a separate
            # table.  ``evaluate_date`` has no access to this data.
            outcome_count = self._write_forward_outcomes(run_id, mode, decisions, outcome_indexes[_series_map(self.market)["drawdown"]])
            report = build_replay_report(self.repo, run_id, mode=mode, market=self.market, decisions=decisions)
            report["forward_outcomes_written"] = outcome_count
            self.repo.record_replay_report(run_id, report)
            completed = self.repo.complete_replay_run(run_id, status="COMPLETED", summary=report)
            return {"run": completed, "report": report, "reused": False}
        except Exception as exc:
            self.repo.complete_replay_run(run_id, status="FAILED", error={"error": str(exc)})
            raise

    def _write_forward_outcomes(self, run_id: str, mode: str, decisions: list[dict[str, Any]], price_index: _SeriesIndex) -> int:
        count = 0
        for decision in decisions:
            day = date.fromisoformat(normalize_date(decision["as_of_datetime"]))
            current = price_index.first_on_or_after(day)
            if not current or current["observation_date"] != day.isoformat():
                continue
            current_value = _finite(current.get("value"))
            if current_value in (None, 0):
                continue
            values: dict[str, float | None] = {}
            future_ids: list[str] = []
            for months, field in zip(HORIZONS, ("forward_1m", "forward_3m", "forward_6m", "forward_1y", "forward_3y", "forward_5y")):
                target = _add_months(day, months)
                row = price_index.first_on_or_after(target)
                if row is None:
                    values[field] = None
                else:
                    future = _finite(row.get("value")); values[field] = future / current_value - 1 if future is not None else None
                    if row.get("observation_version_id"): future_ids.append(str(row["observation_version_id"]))
            end = _add_months(day, 12)
            future_rows = [row for row in price_index.after(day, target=None) if row["observation_date"] <= end.isoformat()]
            future_values = [_finite(row.get("value")) for row in future_rows]
            future_values = [value for value in future_values if value is not None]
            values["max_drawdown_next_1y"] = min((value / current_value - 1 for value in future_values), default=None)
            values["max_gain_next_1y"] = max((value / current_value - 1 for value in future_values), default=None)
            missing = [field for field in ("forward_1m", "forward_3m", "forward_6m", "forward_1y", "forward_3y", "forward_5y") if values.get(field) is None]
            status = "COMPLETE" if not missing else "PARTIAL"
            outcome = {
                "replay_run_id": run_id,
                "replay_decision_id": sha256_json({"replay_run_id": run_id, "as_of_datetime": decision["as_of_datetime"]})[:32],
                "market": self.market, "decision_date": day.isoformat(),
                "source_series_id": _series_map(self.market)["drawdown"],
                "source_observation_version_ids": future_ids,
                **values, "status": status,
                "reason": "Evaluation Layer only; future rows are never read by the decision evaluator." + (f" 缺少 horizon: {', '.join(missing)}" if missing else ""),
            }
            self.repo.append_forward_outcome(outcome)
            count += 1
        return count


def _stats(values: Iterable[float]) -> dict[str, Any]:
    data = sorted(float(value) for value in values if _finite(value) is not None)
    if not data:
        return {"mean": None, "median": None, "p25": None, "p75": None, "sample_count": 0}
    def quantile(q: float) -> float:
        if len(data) == 1: return data[0]
        position = (len(data) - 1) * q
        lower = int(math.floor(position)); upper = int(math.ceil(position))
        if lower == upper: return data[lower]
        return data[lower] + (data[upper] - data[lower]) * (position - lower)
    return {"mean": sum(data) / len(data), "median": quantile(.5), "p25": quantile(.25), "p75": quantile(.75), "sample_count": len(data)}


def _bucket(score: float | None) -> str:
    if score is None: return "missing"
    score = float(score)
    if score < 40: return "0-40"
    if score < 60: return "40-60"
    if score < 70: return "60-70"
    if score < 80: return "70-80"
    if score < 90: return "80-90"
    return "90+"


def _coverage_bucket(coverage: float | None) -> str:
    if coverage is None: return "missing"
    value = float(coverage)
    if value < 40: return "0-40"
    if value < 60: return "40-60"
    if value < 80: return "60-80"
    if value < 100: return "80-99"
    return "100"


def _bucket_stats(decisions: list[dict[str, Any]], outcomes: Mapping[str, dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    result: dict[str, list[float]] = {key: [] for key in ("0-40", "40-60", "60-70", "70-80", "80-90", "90+")}
    for decision in decisions:
        outcome = outcomes.get(decision["replay_decision_id"])
        if not outcome: continue
        bucket = _bucket(decision.get("score"))
        value = _finite(outcome.get(field))
        if bucket in result and value is not None: result[bucket].append(value)
    return {key: _stats(values) for key, values in result.items()}


def build_replay_report(repo: PITRepository, run_id: str, *, mode: str, market: str, decisions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    decisions = decisions if decisions is not None else repo.get_replay_decisions(run_id, limit=100000)
    outcomes = repo.get_forward_outcomes(run_id, limit=100000)
    outcome_map = {str(item["replay_decision_id"]): item for item in outcomes}
    score_distribution: dict[str, int] = {key: 0 for key in ("0-40", "40-60", "60-70", "70-80", "80-90", "90+", "missing")}
    coverage_distribution: dict[str, int] = {}
    decision_distribution: dict[str, int] = {}
    for item in decisions:
        score_distribution[_bucket(item.get("score"))] += 1
        cb = _coverage_bucket(item.get("coverage")); coverage_distribution[cb] = coverage_distribution.get(cb, 0) + 1
        decision_distribution[item.get("decision", "UNKNOWN")] = decision_distribution.get(item.get("decision", "UNKNOWN"), 0) + 1
    validation = {
        field: _bucket_stats(decisions, outcome_map, field)
        for field in ("forward_6m", "forward_1y", "forward_3y", "forward_5y")
    }
    # Annual de-overlap view: at most one decision per calendar year.
    annual: dict[int, dict[str, Any]] = {}
    for item in decisions:
        year = int(str(item["as_of_datetime"])[:4])
        annual.setdefault(year, item)
    annual_decisions = list(annual.values())
    annual_validation = {field: _bucket_stats(annual_decisions, outcome_map, field) for field in validation}

    drawdown_values: list[dict[str, Any]] = []
    rsi_values: list[dict[str, Any]] = []
    for item in decisions:
        outcome = outcome_map.get(str(item["replay_decision_id"]))
        if not outcome: continue
        row_map = {row["key"]: row for row in item.get("rows", [])}
        # Persisted decisions do not carry rows to keep the ledger compact;
        # report users can still compare simple baselines using the score and
        # feature snapshots when available.  Use deterministic score proxies
        # only for rows embedded by direct callers.
        if row_map.get("drawdown", {}).get("value") is not None:
            drawdown_values.append({"score": row_map["drawdown"].get("score"), "outcome": outcome})
        if row_map.get("rsi", {}).get("value") is not None:
            rsi_values.append({"score": row_map["rsi"].get("score"), "outcome": outcome})

    # For persisted runs, recompute simple controls from the feature values
    # stored in replay_features.  This remains evaluation-only and does not
    # alter any replay decision.
    feature_rows = repo.get_replay_features(run_id)
    feature_by_date: dict[str, dict[str, dict[str, Any]]] = {}
    for feature in feature_rows:
        feature_by_date.setdefault(str(feature["as_of_datetime"]), {})[feature["feature_name"]] = feature
    controls: dict[str, Any] = {}
    for control_name, feature_name, transform in (
        ("drawdown_only", "DRAW_DOWN", lambda v: 20 * min(max(-(float(v)), 0) / .4, 1)),
        ("rsi_only", "RSI14", lambda v: 5 * max(0, min(1, (50 - float(v)) / 20))),
    ):
        pairs = []
        for decision in decisions:
            feats = feature_by_date.get(str(decision["as_of_datetime"]), {})
            value = _finite((feats.get(feature_name) or {}).get("value")); outcome = outcome_map.get(str(decision["replay_decision_id"]))
            if value is not None and outcome: pairs.append((transform(value), outcome))
        controls[control_name] = {
            "score_bucket_validation": {
                field: {
                    bucket: _stats([outcome.get(field) for score, outcome in pairs if _bucket(score) == bucket and _finite(outcome.get(field)) is not None])
                    for bucket in ("0-40", "40-60", "60-70", "70-80", "80-90", "90+")
                }
                for field in ("forward_6m", "forward_1y", "forward_3y", "forward_5y")
            },
            "sample_count": len(pairs),
        }

    rng = random.Random(20260914)
    scores = [item.get("score") for item in decisions]
    shuffled = scores[:]; rng.shuffle(shuffled)
    placebo_pairs = []
    for item, score in zip(decisions, shuffled):
        outcome = outcome_map.get(str(item["replay_decision_id"])); value = _finite(outcome.get("forward_1y")) if outcome else None
        if value is not None: placebo_pairs.append((_bucket(score), value))
    placebo = {bucket: _stats([value for b, value in placebo_pairs if b == bucket]) for bucket in ("0-40", "40-60", "60-70", "70-80", "80-90", "90+")}

    one_year = validation["forward_1y"]
    populated = [v for v in one_year.values() if v.get("sample_count", 0) >= 20]
    if len(populated) < 2:
        information_value = "INCONCLUSIVE"
    else:
        low = next((one_year[key]["mean"] for key in ("0-40", "40-60") if one_year[key]["mean"] is not None), None)
        high = next((one_year[key]["mean"] for key in ("80-90", "90+") if one_year[key]["mean"] is not None), None)
        placebo_means = [item["mean"] for item in placebo.values() if item["mean"] is not None]
        # A missing high-score tail means the frozen V2 model never produced
        # the comparison required for an information-value claim.  It is
        # inconclusive evidence, not evidence that the score has no value.
        if high is None or low is None:
            information_value = "INCONCLUSIVE"
            spread = None
        else:
            spread = high - low
        placebo_spread = max(placebo_means) - min(placebo_means) if len(placebo_means) >= 2 else float("inf")
        if spread is not None:
            information_value = "POSITIVE" if spread > .02 and spread > placebo_spread else "WEAK" if spread > 0 else "NONE"

    periods = {}
    for label, start, end in (("2000-2002", "2000-01-01", "2002-12-31"), ("2007-2009", "2007-01-01", "2009-12-31"), ("2018Q4", "2018-10-01", "2018-12-31"), ("2020", "2020-01-01", "2020-12-31"), ("2022", "2022-01-01", "2022-12-31"), ("2025-2026", "2025-01-01", "2026-12-31")):
        subset = [item for item in decisions if start <= str(item["as_of_datetime"])[:10] <= end]
        periods[label] = {"decision_count": len(subset), "high_score_count": sum(1 for item in subset if (item.get("score") or 0) >= 75), "decision_distribution": {key: sum(1 for item in subset if item.get("decision") == key) for key in sorted({x.get("decision") for x in subset}) if key}}

    return {
        "phase": "2A", "mode": mode, "market": market, "replay_run_id": run_id,
        "decision_count": len(decisions), "forward_outcome_count": len(outcomes),
        "score_distribution": score_distribution, "coverage_distribution": coverage_distribution,
        "decision_distribution": decision_distribution,
        "validation_all_daily": validation, "validation_annual_sample": annual_validation,
        "controls": controls, "random_placebo": {"seed": 20260914, "forward_1y": placebo},
        "period_audit": periods, "current_score_information_value": information_value,
        "overlap_warning": "日频 forward return 存在重叠；同时提供每年一条的去重视角。",
        "strict_pit": mode == "STRICT_PIT", "research_only": mode == "RESEARCH_PROXY",
        "future_outcomes_isolated": True, "lookahead_check": "Decision evaluator only reads PITRepository histories through current as_of; forward_outcomes is written in a later evaluation pass.",
    }


def replay_date_view(repo: PITRepository, replay_run_id: str, day: str) -> dict[str, Any] | None:
    """Return a blind Decision View; never attach forward outcomes."""

    run = repo.get_replay_run(replay_run_id)
    if not run:
        return None
    normalized = normalize_date(day)
    if not str(run["start_date"]) <= normalized <= str(run["end_date"]):
        return None
    decisions = repo.get_replay_decisions(replay_run_id, as_of=_asof_day(normalized), limit=2)
    if not decisions:
        return None
    decision = decisions[0]
    features = repo.get_replay_features(replay_run_id, as_of=_asof_day(normalized))
    return {
        "replay_run_id": replay_run_id, "as_of": normalized, "as_of_datetime": _asof_day(normalized),
        "mode": run["mode"], "market": run["market"],
        "strict_pit": run["mode"] == "STRICT_PIT", "research_only": run["mode"] == "RESEARCH_PROXY",
        "coverage": decision["coverage"], "score": decision["score"], "gate_status": decision["gate_status"],
        "decision": decision["decision"], "reason": decision["reason"], "missing_items": decision["missing_items"],
        "input_observation_version_ids": decision["input_observation_ids"], "feature_ids": decision["feature_ids"],
        "features": features, "future_hidden": True,
    }


def run_replay(
    db_path: Path | str,
    *,
    mode: str = "RESEARCH_PROXY",
    market: str = "NDX",
    start_date: str | None = None,
    end_date: str | None = None,
    replay_run_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Convenience API used by the CLI, tests and the dashboard server."""

    return ReplayEngine(db_path, market=market).run(
        mode=mode, start_date=start_date, end_date=end_date,
        replay_run_id=replay_run_id, force=force,
    )


__all__ = [
    "ReplayEngine", "REPLAY_FEATURE_VERSION", "REPLAY_MODES", "build_replay_report", "replay_date_view", "run_replay",
]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Phase 2A dual-track chronological replay")
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parent / "data" / "dashboard.sqlite3")
    parser.add_argument("--market", default="NDX")
    parser.add_argument("--mode", choices=REPLAY_MODES, default="RESEARCH_PROXY")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--run-id")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_replay(args.db, mode=args.mode, market=args.market, start_date=args.start_date, end_date=args.end_date, replay_run_id=args.run_id, force=args.force), ensure_ascii=False, indent=2))
