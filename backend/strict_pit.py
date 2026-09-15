"""Strict point-in-time opportunity evaluation (Phase 1B).

This module is intentionally independent from ``server.build``.  It reads
only score-eligible observation versions through :class:`PITRepository`; it
never opens the legacy ``evidence`` table, the current-history CSV, or a
proxy snapshot.  If the eligible history is too short, the result is an
explicit ``INSUFFICIENT_EVIDENCE`` decision and the evaluation is still
written to the append-only decision log.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable

from data_contract import as_of_datetime, normalize_date
from pit_repository import PITRepository, register_score_model
from score_model import load_model
from verification import RULES
from freshness import evaluate_freshness, load_freshness_rules_from_db


MODES = ("LEGACY", "RESEARCH_PROXY", "STRICT_PIT")
STRICT_MODE = "STRICT_PIT"

NDX_SERIES = {
    "drawdown": "NDX_CLOSE",
    "vxn": "NDX_VXN",
    "vix": "VIX",
    "real": "US10Y_REAL",
    "nfci": "US_NFCI",
    "forward_pe": "NDX_FORWARD_PE",
    "ttm_pe": "NDX_TTM_PE",
    "revision": "NDX_EPS_REVISION",
    "growth": "NDX_FORWARD_EPS",
    "breadth": "NDX_BREADTH_MA200",
}

ROW_META = {
    "drawdown": ("历史高点回撤", "pressure", 20),
    "vxn": ("Nasdaq-100波动率 VXN", "pressure", 5),
    "vix": ("VIX（背景）", "background", 0),
    "real": ("美国10年实际利率", "macro", 5),
    "nfci": ("金融条件 NFCI", "macro", 5),
    "forward_pe": ("Forward PE", "valuation", 20),
    "ttm_pe": ("TTM PE", "valuation", 10),
    "revision": ("同财政期 EPS 修正", "earnings", 15),
    "growth": ("下一财政年 EPS 增长", "earnings", 10),
    "breadth": ("成分股站上 MA200 比例", "breadth", 5),
    "rsi": ("RSI(14)", "technical", 5),
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _rsi(values: list[float]) -> float | None:
    # Match the frozen dashboard's Wilder warm-up policy: at least 100 daily
    # changes are required before an RSI can be scored.
    changes = [b - a for a, b in zip(values, values[1:])]
    if len(changes) < 100:
        return None
    gain = sum(max(x, 0) for x in changes[:14]) / 14
    loss = sum(max(-x, 0) for x in changes[:14]) / 14
    for change in changes[14:]:
        gain = (gain * 13 + max(change, 0)) / 14
        loss = (loss * 13 + max(-change, 0)) / 14
    if gain == loss == 0:
        return 50.0
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def _monthly_values(rows: list[dict[str, Any]], cutoff_date: str, limit: int = 120) -> list[float]:
    """Return the prior month-end values used by the frozen percentile rule."""

    cutoff_month = date.fromisoformat(cutoff_date).replace(day=1)
    month_end: dict[tuple[int, int], float] = {}
    for row in rows:
        observed = row.get("observation_date")
        try:
            observed_date = date.fromisoformat(str(observed)[:10])
        except (TypeError, ValueError):
            continue
        if observed_date >= cutoff_month:
            continue
        value = _number(row.get("value"))
        if value is not None:
            month_end[(observed_date.year, observed_date.month)] = value
    return [month_end[key] for key in sorted(month_end)[-limit:]]


def _percentile(rows: list[dict[str, Any]], cutoff_date: str) -> tuple[float | None, int]:
    values = _monthly_values(rows, cutoff_date)
    if len(values) < 60:
        return None, len(values)
    latest = _number(rows[-1].get("value")) if rows else None
    if latest is None:
        return None, len(values)
    # The dashboard's historical percentile is a conservative empirical CDF.
    return sum(value <= latest for value in values) / len(values), len(values)


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return rows[-1] if rows else None


def _reason(row: dict[str, Any], default: str) -> str:
    return str(row.get("methodology") or default)


def _valuation_proof_complete(row: dict[str, Any], key: str) -> bool:
    """Require an explicit provider/period contract before using PE values.

    ``score_eligible`` proves the time boundary, while metadata proves that a
    valuation observation has a stable period and aggregation identity.  A
    free page labelled only ``provider_defined`` or ``rolling`` remains a
    candidate even if somebody supplied it with an otherwise valid timestamp.
    """

    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return False
    required = ("provider_date", "period", "index_identity", "method_version")
    if any(not str(metadata.get(field) or "").strip() for field in required):
        return False
    period = str(metadata.get("period") or "").strip().lower()
    if period in {"provider_defined", "rolling", "ntm", "ltm"}:
        return False
    expected = "forward" if key == "forward_pe" else "ttm"
    return expected in period


def _breadth_proof_complete(row: dict[str, Any]) -> bool:
    """Accept breadth only with structured same-day membership evidence."""

    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return False
    return (
        metadata.get("membership_complete") is True
        and metadata.get("member_inputs_complete") is True
        and bool(str(metadata.get("constituent_date") or "").strip())
        and bool(str(metadata.get("membership_source") or "").strip())
        and bool(str(metadata.get("membership_retrieved_at") or "").strip())
        and bool(str(metadata.get("method_version") or "").strip())
    )


def evaluate_strict_pit(
    db_path: Path | str,
    as_of: Any,
    *,
    market: str = "NDX",
    score_model_version: str | None = None,
) -> dict[str, Any]:
    """Evaluate one as-of cutoff through the strict PIT path and log it."""

    market = str(market).upper()
    cutoff = as_of_datetime(as_of)
    cutoff_date = normalize_date(as_of, field="as_of")
    model = load_model()
    if score_model_version and score_model_version != model["model_version"]:
        raise ValueError("strict 评估只能使用当前冻结 score model")
    model_record = register_score_model(db_path, model)
    repo = PITRepository(db_path)
    input_ids: list[str] = []
    histories: dict[str, list[dict[str, Any]]] = {}

    def history(series_id: str) -> list[dict[str, Any]]:
        if series_id not in histories:
            values = repo.get_history_available(series_id, cutoff)
            histories[series_id] = values
            input_ids.extend(str(row["observation_version_id"]) for row in values)
        return histories[series_id]

    series_map = dict(NDX_SERIES)
    if market != "NDX":
        # Never borrow NDX macro/valuation observations for another market.
        # The collector namespaces non-NDX captures by profile, so an
        # unavailable local series remains missing rather than being silently
        # substituted with a cross-market value.
        series_map = {
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
    rows: list[dict[str, Any]] = []

    def add(key: str, value: Any = None, score: float | None = None, *, status: str = "missing", reason: str = "", observed: str | None = None, source: str = "PITRepository", origin: str | None = None) -> None:
        label, module, weight = ROW_META[key]
        formula, category, calculation = RULES[key]
        numeric = _number(value)
        rows.append({
            "key": key,
            "label": label,
            "module": module,
            "weight": weight,
            "value": numeric if numeric is not None else value,
            "score": None if score is None else float(score),
            "status": status,
            "date": observed,
            "reason": reason,
            "source": source,
            "formula": formula,
            "category": category,
            "calculation": calculation,
            "eligibility_origin": origin,
        })

    # Price pressure and RSI require a real history.  A single live close is
    # retained as a quote but cannot be mistaken for an all-time drawdown.
    close_rows = history(series_map["drawdown"])
    close_values = [v for v in (_number(row.get("value")) for row in close_rows) if v is not None]
    close_latest = _latest(close_rows)
    quote = {
        "close": _number(close_latest.get("value")) if close_latest else None,
        "date": close_latest.get("observation_date") if close_latest else None,
        "eligible_rows": len(close_rows),
    }
    if len(close_values) >= 200:
        high = max(close_values)
        current = close_values[-1]
        drawdown = 1 - current / high if high else None
        rsi = _rsi(close_values)
        ma200 = sum(close_values[-200:]) / 200
        observed = close_latest.get("observation_date") if close_latest else None
        origin = close_latest.get("eligibility_origin") if close_latest else None
        add("drawdown", -drawdown if drawdown is not None else None, 20 * min(max(drawdown or 0, 0) / 0.4, 1), status="scored", observed=observed, origin=origin, reason="严格 PIT 收盘历史；每个版本 available_at <= as_of。")
        add("rsi", rsi, 5 * max(0, min(1, (50 - rsi) / 20)) if rsi is not None else None, status="scored" if rsi is not None else "insufficient_history", observed=observed, origin=origin, reason="Wilder RSI14；严格路径要求至少100个变化且价格历史已通过 PIT 过滤。")
        quote.update({"high": high, "drawdown": -drawdown if drawdown is not None else None, "rsi": rsi, "ma200": ma200})
    else:
        reason = f"严格路径仅取得 {len(close_values)} 条可用收盘观察，绘制报价但需要至少200条才计算回撤/MA200，不能用历史代理补齐。"
        origin = close_latest.get("eligibility_origin") if close_latest else None
        add("drawdown", reason=reason, status="insufficient_history", observed=quote["date"], origin=origin)
        add("rsi", reason=reason + " RSI 还需要至少100个变化。", status="insufficient_history", observed=quote["date"], origin=origin)

    def add_percentile(key: str, *, invert: bool, minimum: int = 60) -> None:
        series_id = series_map[key]
        values = history(series_id)
        latest = _latest(values)
        if key in {"forward_pe", "ttm_pe"}:
            # A latest value with unclear period/aggregation must not be
            # replaced by an older qualified value: that would hide a current
            # provider-method change behind a historical percentile.
            if latest and not _valuation_proof_complete(latest, key):
                add(key, latest.get("value"), status="candidate", observed=latest.get("observation_date"), origin=latest.get("eligibility_origin"), reason="已取得时点观察，但 provider_date、期间、指数身份或方法版本未形成可核验的固定估值口径；保持候选，不计分。")
                return
            values = [row for row in values if _valuation_proof_complete(row, key)]
            latest = _latest(values)
        p, monthly_count = _percentile(values, cutoff_date)
        observed = latest.get("observation_date") if latest else None
        if p is not None and latest:
            score = ROW_META[key][2] * ((1 - p) if invert else p)
            add(key, latest.get("value"), score, status="scored", observed=observed, origin=latest.get("eligibility_origin"), reason=f"严格 PIT 历史 {monthly_count} 个完整月末；经验分位 {p:.1%}。")
        elif latest:
            add(key, latest.get("value"), status="insufficient_history", observed=observed, origin=latest.get("eligibility_origin"), reason=f"已取得实时合格观察，但严格 PIT 只有 {monthly_count} 个完整月末（原始 {len(values)} 条），至少需要 {minimum} 个同口径月末数据；不回退历史代理。")
        else:
            add(key, reason=f"严格 PIT 没有 available_at <= as_of 的合格观察；候选/历史代理不计分。", status="missing")

    add_percentile("vxn", invert=False)
    add_percentile("real", invert=True)
    add_percentile("nfci", invert=True)

    # VIX is retained as a background observation and has zero weight.
    vix_rows = history(series_map["vix"])
    vix_latest = _latest(vix_rows)
    if vix_latest:
        add("vix", vix_latest.get("value"), 0, status="observed", observed=vix_latest.get("observation_date"), origin=vix_latest.get("eligibility_origin"), reason="背景指标，不参与总分；仅显示严格 PIT 最新观察。")
    else:
        add("vix", reason="没有严格 PIT 合格观察。", status="missing")

    # Valuation requires a same-series 60-month history.  Current public PE
    # and EPS captures are intentionally candidate-only, so no proxy is used.
    add_percentile("forward_pe", invert=True)
    add_percentile("ttm_pe", invert=True)
    for key in ("revision", "growth", "breadth"):
        values = history(series_map[key])
        latest = _latest(values)
        if latest and key == "breadth" and len(values) >= 1:
            # Text such as "complete" is not a proof.  Require structured
            # same-day member and price-input metadata before breadth scores.
            complete_membership = _breadth_proof_complete(latest)
            if complete_membership:
                add(key, latest.get("value"), ROW_META[key][2] * max(0, min(1, _number(latest.get("value")) or 0)), status="scored", observed=latest.get("observation_date"), origin=latest.get("eligibility_origin"), reason="严格 PIT 当日完整成员口径。")
            else:
                add(key, latest.get("value"), status="candidate", observed=latest.get("observation_date"), origin=latest.get("eligibility_origin"), reason="实时宽度未证明当日完整成分名单、成员输入和方法版本；候选观察不计分。")
        elif latest:
            add(key, latest.get("value"), status="insufficient_history", observed=latest.get("observation_date"), origin=latest.get("eligibility_origin"), reason="严格 PIT 尚未取得同财政期、同方法的可比预测版本；滚动 EPS 代理不替代修正证据。")
        else:
            add(key, reason="严格 PIT 没有合格观察；候选数据不计分。", status="missing")

    scored = [row for row in rows if row["score"] is not None and row["weight"] > 0]
    known = sum(float(row["score"]) for row in scored)
    coverage = sum(float(row["weight"]) for row in scored)
    mapping = {row["key"]: row for row in rows}
    valuation = sum(mapping[key]["score"] or 0 for key in ("forward_pe", "ttm_pe"))
    earnings = sum(mapping[key]["score"] or 0 for key in ("revision", "growth"))
    # A stale observation is not silently reused as today's evidence.  A
    # missing series still maps to INSUFFICIENT_EVIDENCE through the coverage
    # gate; a present-but-expired series gets the more specific DATA_STALE
    # status.  Weekly NFCI is allowed to remain fresh within its 14-day rule.
    freshness_series = {
        series_map[key]: _latest(histories.get(series_map[key], []))
        for key in ("drawdown", "vxn", "vix", "real", "nfci")
    }
    freshness = evaluate_freshness(
        freshness_series,
        cutoff,
        load_freshness_rules_from_db(db_path),
    )
    stale_series = [series_id for series_id, item in freshness.items() if item["status"] == "stale"]
    if stale_series:
        gate_status = "DATA_STALE"
        decision = "DATA_STALE"
    elif coverage < 100:
        gate_status = "INSUFFICIENT_EVIDENCE"
        decision = "INSUFFICIENT_EVIDENCE"
    else:
        gate_status = "PASS"
        decision = "额外加仓吸引力偏弱" if known < 40 else "证据分歧，继续观察" if known < 60 else "支持适度加仓" if valuation >= 15 and earnings >= 15 else "核心证据未通过"
        if known >= 75 and valuation >= 18 and earnings >= 20:
            decision = "加仓证据较强"

    missing = [
        {"key": row["key"], "label": row["label"], "weight": row["weight"], "status": row["status"], "reason": row["reason"]}
        for row in rows if row["score"] is None and row["weight"] > 0
    ]
    input_ids = sorted(set(input_ids))
    reason = {
        "mode": STRICT_MODE,
        "no_legacy_fallback": True,
        "as_of_datetime": cutoff,
        "eligible_series_rows": {series_id: len(values) for series_id, values in histories.items()},
        "eligibility_rule": "score_eligible=true AND available_at <= as_of_datetime",
        "proxy_policy": "HISTORICAL_PROXY/CANDIDATE 永不进入 STRICT_PIT",
        "freshness": freshness,
        "stale_series": stale_series,
    }
    logged = repo.append_decision(
        as_of=cutoff,
        market=market,
        mode=STRICT_MODE,
        score_model_version=model_record["model_version"],
        config_hash=model_record["config_hash"],
        input_version_ids=input_ids,
        coverage=coverage,
        score=known,
        gate_status=gate_status,
        decision=decision,
        reason=reason,
        missing_items=missing,
        data_cutoff=cutoff,
    )
    return {
        "mode": STRICT_MODE,
        "market": market,
        "as_of": cutoff_date,
        "as_of_datetime": cutoff,
        "score_model_version": model_record["model_version"],
        "config_hash": model_record["config_hash"],
        "quote": quote,
        "rows": rows,
        "score": known,
        "known": known,
        "coverage": coverage,
        "gate_status": gate_status,
        "decision": decision,
        "missing_items": missing,
        "input_version_ids": input_ids,
        "input_hash": logged["input_hash"],
        "decision_id": logged["decision_id"],
        "decision_hash": logged["decision_hash"],
        "decision_log": logged,
        "reason": reason,
        "freshness": freshness,
    }


__all__ = ["MODES", "STRICT_MODE", "evaluate_strict_pit"]
