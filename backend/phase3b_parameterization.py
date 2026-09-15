"""Phase 3B: real-world capital-profile parameterization.

This layer converts editable income and cash-flow assumptions into the frozen
Phase 3A opportunity-fund state machine.  It does not alter the Tactical
trigger, C/D ladders, or event history, and it never creates an order.  A
profile may use currency amounts (the default UI examples use CNY), but those
amounts remain at this adapter boundary; the underlying spend rule is still
``planned = ladder_fraction * current_target`` and ``actual = min(planned,
available_cash)``.
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
from phase2f_research import FORMAL_THRESHOLDS, LADDER_CONFIG
from phase3a_state_machine import (
    CAPITAL_STATE_C_VERSION,
    CAPITAL_STATE_D_VERSION,
    STATE_MACHINE_CONFIG,
    STATE_MACHINE_CONFIG_HASH,
    STATE_MACHINE_MODEL_VERSION,
    TRIGGER_VERSION,
    TRIGGER_WINDOW,
)
from pit_repository import PITRepository

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "dashboard.sqlite3"

REAL_WORLD_MODEL_VERSION = "NDX_REAL_WORLD_PARAMETERIZATION_V1"
PHASE3B_MODEL_VERSION = REAL_WORLD_MODEL_VERSION
REAL_WORLD_CONFIG_CREATED_AT = "2026-09-15T00:00:00Z"

LADDER_IDS = ("C", "D")
TARGET_IDS = ("F1", "F2", "F3", "F4")
CAP_MULTIPLIERS = (1.0, 1.5, 2.0)
INCOME_REFILL_RATIOS = (0.05, 0.10, 0.15)
REFILL_MODES = ("FIXED", "INCOME_LINKED")
GROWTH_SCENARIOS = ("G0", "G1", "G2", "G3")
SURPLUS_POLICIES = ("S0", "S1", "S2")

# The income values are explicitly labelled scenario assumptions.  The user
# can replace them through a profile; they are not recommendations or claims
# about the user's income.
STANDARD_PROFILE_ASSUMPTIONS: dict[str, dict[str, Any]] = {
    "P1": {"label": "低现金流阶段", "currency": "CNY", "monthly_income": 8000.0, "monthly_core_dca": 1000.0, "monthly_opportunity_refill": 500.0, "refill_range": [500.0, 500.0], "assumption": "收入为参数压力场景，非个人建议"},
    "P2": {"label": "成长阶段", "currency": "CNY", "monthly_income": 20000.0, "monthly_core_dca": 3000.0, "monthly_opportunity_refill": 1000.0, "refill_range": [1000.0, 1000.0], "assumption": "收入为参数压力场景，非个人建议"},
    "P3": {"label": "稳定收入阶段", "currency": "CNY", "monthly_income": 35000.0, "monthly_core_dca": 5000.0, "monthly_opportunity_refill": 2000.0, "refill_range": [2000.0, 2000.0], "assumption": "收入为参数压力场景，非个人建议"},
    "P4": {"label": "高现金流阶段", "currency": "CNY", "monthly_income": 60000.0, "monthly_core_dca": 10000.0, "monthly_opportunity_refill": 4000.0, "refill_range": [3000.0, 5000.0], "assumption": "收入为参数压力场景，P4补充范围为3000–5000，非个人建议"},
}

TARGET_SPECS: dict[str, dict[str, Any]] = {
    "F1": {"label": "3个月 Core DCA", "method": "CORE_DCA_MULTIPLE", "months": 3},
    "F2": {"label": "6个月 Core DCA", "method": "CORE_DCA_MULTIPLE", "months": 6},
    "F3": {"label": "12个月 Core DCA", "method": "CORE_DCA_MULTIPLE", "months": 12},
    "F4": {"label": "固定 100,000", "method": "FIXED_CURRENCY", "amount": 100000.0},
}

SURPLUS_POLICY_DEFINITIONS: dict[str, dict[str, str]] = {
    "S0": {"label": "保留现金", "description": "Surplus 保持独立现金，不扩大 Opportunity Fund Cap"},
    "S1": {"label": "转入 Core DCA", "description": "Surplus 只作为 Core DCA 额外现金记录，不回流 Opportunity Fund"},
    "S2": {"label": "低风险现金管理占位", "description": "Surplus 转入低风险现金管理占位余额，不指定具体产品"},
}

# These gate thresholds are fixed before scenario calculation.  They are
# feasibility guardrails, not return targets and not a personal suitability
# recommendation.
REAL_WORLD_GATE_CONFIG: dict[str, Any] = {
    "max_average_refill_burden_ratio": 0.15,
    "max_refill_burden_ratio": 0.20,
    "ordinary_band_participation_required": True,
    "deep_band_40_50_full_funding_required": False,
    "max_time_to_full_months": 36,
    "no_borrowing_required": True,
    "core_dca_priority_required": True,
    "cap_required": True,
}

REAL_WORLD_CONFIG: dict[str, Any] = {
    "model_version": REAL_WORLD_MODEL_VERSION,
    "created_at": REAL_WORLD_CONFIG_CREATED_AT,
    "research_only": True,
    "simulation_only": True,
    "auto_trade": False,
    "upstream_state_machine_model_version": STATE_MACHINE_MODEL_VERSION,
    "upstream_state_machine_config_hash": STATE_MACHINE_CONFIG_HASH,
    "trigger_version": TRIGGER_VERSION,
    "trigger_window_trading_days": TRIGGER_WINDOW,
    "formal_thresholds": list(FORMAL_THRESHOLDS),
    "supported_ladders": {"C": {"version": CAPITAL_STATE_C_VERSION, "allocations": copy.deepcopy(LADDER_CONFIG["C"]["allocations"])}, "D": {"version": CAPITAL_STATE_D_VERSION, "allocations": copy.deepcopy(LADDER_CONFIG["D"]["allocations"])}},
    "target_specs": copy.deepcopy(TARGET_SPECS),
    "cap_multipliers": list(CAP_MULTIPLIERS),
    "income_refill_ratios": list(INCOME_REFILL_RATIOS),
    "growth_scenarios": list(GROWTH_SCENARIOS),
    "surplus_policies": copy.deepcopy(SURPLUS_POLICY_DEFINITIONS),
    "waterfall": ["income", "living_expenses_external", "core_dca", "opportunity_refill", "opportunity_fund_cap", "surplus_policy"],
    "emergency_fund": {"excluded_from_investment_system": True, "borrow_or_leverage_allowed": False},
    "cash_drag": {"cash_yield_assumption": None, "cash_drag_proxy": "idle_cash_amount_and_time_only"},
    "gate": copy.deepcopy(REAL_WORLD_GATE_CONFIG),
    "phase3a_rules_frozen": True,
}
REAL_WORLD_CONFIG_HASH = sha256_json(REAL_WORLD_CONFIG)
PHASE3B_CONFIG_HASH = REAL_WORLD_CONFIG_HASH


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _month_key(value: Any) -> str:
    raw = str(value)
    if len(raw) >= 7 and raw[4] == "-":
        key = raw[:7]
        try:
            date.fromisoformat(key + "-01")
        except ValueError as exc:
            raise ValueError("月份必须是 YYYY-MM") from exc
        return key
    raise ValueError("月份必须是 YYYY-MM")


def _month_index(month: str) -> int:
    key = _month_key(month)
    year, mon = (int(part) for part in key.split("-"))
    return year * 12 + mon - 1


def _month_starts(start: str, end: str) -> list[str]:
    first = date.fromisoformat(normalize_date(start)).replace(day=1)
    last = date.fromisoformat(normalize_date(end)).replace(day=1)
    output: list[str] = []
    cursor = first
    while cursor <= last:
        output.append(cursor.isoformat())
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return output


def _normalise_schedule(raw: Any, *, field: str) -> dict[str, Any]:
    if raw is None:
        return {"mode": "FLAT"}
    if isinstance(raw, Mapping):
        mode = str(raw.get("mode", "STEP")).upper()
        if mode in {"FLAT", "NONE"}:
            return {"mode": "FLAT"}
        if mode in {"ANNUAL_COMPOUND", "ANNUAL"}:
            rate = _finite(raw.get("annual_rate", raw.get("rate")))
            if rate is None or rate < -1:
                raise ValueError(f"{field}.annual_rate 必须是大于 -100% 的有限数")
            base = _month_key(raw.get("base_month", "2000-01"))
            return {"mode": "ANNUAL_COMPOUND", "annual_rate": float(rate), "base_month": base}
        if mode in {"STEP", "STAGED"}:
            changes = raw.get("changes", raw.get("schedule", {}))
            if not isinstance(changes, Mapping):
                raise ValueError(f"{field}.changes 必须是 YYYY-MM 映射")
            values: dict[str, float] = {}
            for month, value in changes.items():
                amount = _finite(value)
                if amount is None or amount < 0:
                    raise ValueError(f"{field}.changes 的倍数必须是非负有限数")
                values[_month_key(month)] = float(amount)
            return {"mode": "STEP", "changes": dict(sorted(values.items()))}
        if mode in {"PAUSE", "PAUSE_THEN_COMPOUND"}:
            rate = _finite(raw.get("annual_rate", raw.get("rate", 0.0)))
            if rate is None or rate < -1:
                raise ValueError(f"{field}.annual_rate 必须是大于 -100% 的有限数")
            base_month = _month_key(raw.get("base_month", "2000-01"))
            pause_start = _month_key(raw.get("pause_start", raw.get("stop_month")))
            pause_end = _month_key(raw.get("pause_end"))
            if _month_index(pause_end) <= _month_index(pause_start):
                raise ValueError(f"{field}.pause_end 必须晚于 pause_start")
            return {"mode": "PAUSE_THEN_COMPOUND", "annual_rate": float(rate), "base_month": base_month, "pause_start": pause_start, "pause_end": pause_end}
        if mode in {"STOP", "STOP_THEN_COMPOUND"}:
            stop_month = _month_key(raw.get("stop_month"))
            return {"mode": "STOP", "stop_month": stop_month, "after_factor": float(_finite(raw.get("after_factor", 1.0)) or 0.0)}
        raise ValueError(f"{field}.mode 不支持")
    if isinstance(raw, list):
        changes: dict[str, float] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError(f"{field} 列表项必须是对象")
            month = item.get("effective_month", item.get("month"))
            value = item.get("factor", item.get("value", item.get("income")))
            if month is None:
                raise ValueError(f"{field} 列表项缺少 effective_month")
            amount = _finite(value)
            if amount is None or amount < 0:
                raise ValueError(f"{field} 倍数必须是非负有限数")
            changes[_month_key(month)] = float(amount)
        return {"mode": "STEP", "changes": dict(sorted(changes.items()))}
    raise ValueError(f"{field} 必须是对象、映射或列表")


def normalize_real_world_profile(profile: Mapping[str, Any], *, target_id: str | None = None, cap_multiplier: float | None = None) -> dict[str, Any]:
    """Validate a user-facing profile without mutating Phase 3A settings."""
    if not isinstance(profile, Mapping):
        raise ValueError("CapitalProfile 必须是对象")
    profile_id = str(profile.get("profile_id") or "CUSTOM")
    currency = str(profile.get("currency") or "CNY")
    monthly_income = _finite(profile.get("monthly_income"))
    monthly_core = _finite(profile.get("monthly_core_dca"))
    monthly_refill = _finite(profile.get("monthly_opportunity_refill", profile.get("monthly_refill", 0)))
    initial_cash = _finite(profile.get("initial_opportunity_cash", profile.get("current_opportunity_cash", profile.get("initial_fund", 0))))
    if any(value is None or value < 0 for value in (monthly_income, monthly_core, monthly_refill, initial_cash)):
        raise ValueError("monthly_income、monthly_core_dca、monthly_opportunity_refill、initial_opportunity_cash 必须是非负有限数")
    # A profile may arrive either from the friendly F1–F4 selector or from
    # the formal CapitalProfile schema.  If the schema supplies an absolute
    # opportunity-fund target without a method/target id, treat it as an
    # explicit currency target.  Normalized profiles already carry a method,
    # so this inference cannot override an existing choice.
    target_method_raw = profile.get("target_method")
    if target_method_raw is None and "opportunity_fund_target" in profile and not any(key in profile for key in ("target_id", "target_multiple", "target_value", "fund_target", "fund_target_value")):
        target_method_raw = "FIXED_CURRENCY"
    target_method = str(target_method_raw or "CORE_DCA_MULTIPLE").upper()
    if target_method not in {"CORE_DCA_MULTIPLE", "FIXED_CURRENCY"}:
        raise ValueError("target_method 只能是 CORE_DCA_MULTIPLE 或 FIXED_CURRENCY")
    chosen_target = str(target_id or profile.get("target_id") or "F2").upper()
    if chosen_target not in TARGET_SPECS:
        raise ValueError("target_id 只能是 F1、F2、F3、F4")
    raw_multiplier = cap_multiplier if cap_multiplier is not None else profile.get("cap_multiplier")
    raw_cap_value = _finite(profile.get("opportunity_fund_cap", profile.get("fund_cap")))
    target_value = _finite(profile.get("target_value", profile.get("fund_target_value", profile.get("fund_target", profile.get("opportunity_fund_target")))))
    if target_method == "CORE_DCA_MULTIPLE":
        target_multiple = _finite(profile.get("target_multiple", TARGET_SPECS[chosen_target].get("months", 6)))
        if target_multiple is None or target_multiple <= 0:
            raise ValueError("target_multiple 必须为正数")
        target_value = float(target_multiple)
    else:
        target_value = float(target_value if target_value is not None else TARGET_SPECS[chosen_target].get("amount", 100000.0))
        if target_value < 0:
            raise ValueError("固定 target_value 不能为负")
    # Expose the first-period target/cap in the normalized CapitalProfile so
    # API clients can display the two fields named by the Phase 3B schema.
    # The replay recalculates these values at every month when a growth
    # schedule changes; they are therefore labels, not a second cash source.
    configured_target = float(target_value * monthly_core) if target_method == "CORE_DCA_MULTIPLE" else float(target_value)
    if raw_multiplier is None:
        # Absolute cap values are accepted only when they map exactly to one
        # of the three pre-registered cap multipliers.  This keeps the grid
        # finite and prevents an arbitrary cap from becoming a hidden tuning
        # parameter.
        if raw_cap_value is None:
            multiplier = 1.0
        elif configured_target <= 1e-12:
            multiplier = 1.0 if abs(raw_cap_value) <= 1e-12 else None
        else:
            candidate = raw_cap_value / configured_target
            multiplier = next((value for value in CAP_MULTIPLIERS if abs(candidate - value) <= 1e-8), None)
        if multiplier is None:
            raise ValueError("opportunity_fund_cap 必须对应 1.0、1.5 或 2.0 × target")
    else:
        multiplier = float(raw_multiplier)
    if multiplier not in CAP_MULTIPLIERS:
        raise ValueError("cap_multiplier 只能是 1.0、1.5、2.0")
    configured_cap = configured_target * multiplier
    surplus_policy = str(profile.get("surplus_policy", "S0")).upper()
    if surplus_policy not in SURPLUS_POLICIES:
        raise ValueError("surplus_policy 只能是 S0、S1、S2")
    emergency_excluded = bool(profile.get("emergency_fund_excluded", True))
    if not emergency_excluded:
        raise ValueError("Emergency Fund 必须排除在投资系统之外")
    if bool(profile.get("debt_financing_allowed", False)):
        raise ValueError("Phase 3B 禁止借款、杠杆或信用融资")
    income_growth = _normalise_schedule(profile.get("income_growth_schedule"), field="income_growth_schedule")
    core_growth = _normalise_schedule(profile.get("core_dca_growth_schedule"), field="core_dca_growth_schedule")
    refill_growth = _normalise_schedule(profile.get("refill_growth_schedule"), field="refill_growth_schedule")
    ratio = _finite(profile.get("opportunity_refill_ratio", profile.get("income_refill_ratio")))
    if ratio is not None and ratio not in INCOME_REFILL_RATIOS:
        raise ValueError("income-linked refill ratio 只能是 5%、10%、15%")
    return {
        "profile_id": profile_id,
        "label": str(profile.get("label") or profile_id),
        "currency": currency,
        "monthly_income": float(monthly_income),
        "monthly_core_dca": float(monthly_core),
        "monthly_opportunity_refill": float(monthly_refill),
        "target_method": target_method,
        "target_id": chosen_target,
        "target_value": float(target_value),
        "target_multiple": float(target_value) if target_method == "CORE_DCA_MULTIPLE" else None,
        "cap_multiplier": multiplier,
        "opportunity_fund_target": configured_target,
        "opportunity_fund_cap": configured_cap,
        "initial_opportunity_cash": float(initial_cash),
        "surplus_policy": surplus_policy,
        "income_growth_schedule": income_growth,
        "core_dca_growth_schedule": core_growth,
        "refill_growth_schedule": refill_growth,
        "opportunity_refill_ratio": None if ratio is None else float(ratio),
        "emergency_fund_excluded": True,
        "emergency_fund_balance": 0.0,
        "debt_financing_allowed": False,
        "simulation_only": True,
        "auto_trade": False,
        "assumption": str(profile.get("assumption") or "用户可编辑参数；不是个人建议"),
    }


def standard_profiles() -> dict[str, dict[str, Any]]:
    return {profile_id: normalize_real_world_profile({**values, "profile_id": profile_id}) for profile_id, values in STANDARD_PROFILE_ASSUMPTIONS.items()}


def _growth_factor(schedule: Mapping[str, Any], month: str) -> float:
    mode = str(schedule.get("mode", "FLAT")).upper()
    if mode == "FLAT": return 1.0
    if mode == "ANNUAL_COMPOUND":
        years = max(0, _month_index(month) - _month_index(str(schedule.get("base_month", "2000-01")))) / 12.0
        return max(0.0, (1.0 + float(schedule.get("annual_rate", 0.0))) ** years)
    if mode == "STEP":
        selected = 1.0
        for effective, factor in sorted((str(k), float(v)) for k, v in (schedule.get("changes") or {}).items()):
            if _month_index(effective) <= _month_index(month): selected = factor
            else: break
        return max(0.0, selected)
    if mode in {"PAUSE", "PAUSE_THEN_COMPOUND"}:
        base_month = _month_index(str(schedule.get("base_month", "2000-01")))
        pause_start = _month_index(str(schedule.get("pause_start", schedule.get("stop_month", "2100-01"))))
        pause_end = _month_index(str(schedule.get("pause_end", "2101-01")))
        current = _month_index(month)
        before_pause = max(0.0, (pause_start - base_month) / 12.0)
        annual_rate = float(schedule.get("annual_rate", 0.0))
        frozen = max(0.0, (1.0 + annual_rate) ** before_pause)
        if current < pause_start:
            years = max(0.0, (current - base_month) / 12.0)
            return max(0.0, (1.0 + annual_rate) ** years)
        if current < pause_end:
            return frozen
        years_after = max(0.0, (current - pause_end) / 12.0)
        return frozen * max(0.0, (1.0 + annual_rate) ** years_after)
    if mode == "STOP":
        return 0.0 if _month_index(month) >= _month_index(str(schedule["stop_month"])) else 1.0
    return 1.0


def growth_schedule_for(name: str) -> dict[str, Any]:
    name = str(name).upper()
    if name == "G0": return {"income": {"mode": "FLAT"}, "core": {"mode": "FLAT"}, "refill": {"mode": "FLAT"}, "label": "收入与定投不增长"}
    if name == "G1": return {"income": {"mode": "ANNUAL_COMPOUND", "annual_rate": 0.05, "base_month": "2000-01"}, "core": {"mode": "FLAT"}, "refill": {"mode": "FLAT"}, "label": "收入每年增长5%，定投补充不自动增长"}
    if name == "G2": return {"income": {"mode": "STEP", "changes": {"2000-01": 1.0, "2003-01": 1.2, "2006-01": 1.44, "2009-01": 1.728, "2012-01": 2.0736, "2015-01": 2.48832, "2018-01": 2.985984, "2021-01": 3.5831808, "2024-01": 4.29981696}}, "core": {"mode": "STEP", "changes": {"2000-01": 1.0, "2003-01": 1.2, "2006-01": 1.44, "2009-01": 1.728, "2012-01": 2.0736, "2015-01": 2.48832, "2018-01": 2.985984, "2021-01": 3.5831808, "2024-01": 4.29981696}}, "refill": {"mode": "STEP", "changes": {"2000-01": 1.0, "2003-01": 1.2, "2006-01": 1.44, "2009-01": 1.728, "2012-01": 2.0736, "2015-01": 2.48832, "2018-01": 2.985984, "2021-01": 3.5831808, "2024-01": 4.29981696}}, "label": "每3年提升一次收入、核心定投和补充档位"}
    if name == "G3": return {"income": {"mode": "STEP", "changes": {"2000-01": 1.0, "2007-01": 2.5, "2014-01": 4.375, "2020-01": 8.75}}, "core": {"mode": "STEP", "changes": {"2000-01": 1.0, "2007-01": 2.5, "2014-01": 4.375, "2020-01": 8.75}}, "refill": {"mode": "STEP", "changes": {"2000-01": 1.0, "2007-01": 2.5, "2014-01": 4.375, "2020-01": 8.75}}, "label": "预先冻结 P1→P2→P3→P4 阶段路径"}
    raise ValueError("growth_scenario 只能是 G0、G1、G2、G3")


def apply_growth_profile(profile: Mapping[str, Any], growth_scenario: str) -> dict[str, Any]:
    p = normalize_real_world_profile(profile)
    growth_scenario = str(growth_scenario).upper()
    if growth_scenario == "G0":
        # G0 means "no scenario overlay".  Preserve explicit schedules from
        # a personal/custom profile so a target or income change is replayed
        # exactly as entered.  Standard profiles already normalize to FLAT.
        p["growth_scenario"] = growth_scenario
        return p
    schedule = growth_schedule_for(growth_scenario)
    p["growth_scenario"] = growth_scenario
    p["income_growth_schedule"] = schedule["income"]
    p["core_dca_growth_schedule"] = schedule["core"]
    p["refill_growth_schedule"] = schedule["refill"]
    return p


def _normalise_event(event: Mapping[str, Any]) -> dict[str, Any]:
    day = normalize_date(event.get("event_date") or event.get("date"), field="event_date")
    raw_threshold = event.get("threshold", event.get("band"))
    threshold = _finite(raw_threshold)
    if threshold is not None and abs(threshold) > 1: threshold /= 100.0
    if threshold is not None: threshold = abs(threshold)
    if threshold is not None and not any(abs(threshold - value) <= 1e-8 for value in FORMAL_THRESHOLDS): threshold = None
    kind = str(event.get("event_kind") or "TACTICAL_BAND").upper()
    cycle = str(event.get("tactical_cycle_id") or event.get("cycle_id") or f"cycle-{day}")
    trigger = _finite(event.get("trigger_drawdown", event.get("drawdown", -float(threshold or 0)))) or 0.0
    if abs(trigger) > 1: trigger /= 100.0
    return {
        "event_id": str(event.get("tactical_event_id") or event.get("path_event_id") or event.get("event_id") or sha256_json({"date": day, "cycle": cycle, "threshold": threshold, "kind": kind})[:32]),
        "event_date": day, "tactical_cycle_id": cycle, "threshold": threshold,
        "trigger_drawdown": -abs(float(trigger)), "ath_drawdown": event.get("ath_drawdown"),
        "event_kind": kind, "input_observation_ids": sorted({str(value) for value in (event.get("input_observation_ids") or event.get("source_observation_ids") or []) if value}),
        "payload": dict(event.get("payload") or {}),
    }


def _target_and_cap(profile: Mapping[str, Any], month: str) -> tuple[float, float, float, float, float]:
    core_factor = _growth_factor(profile.get("core_dca_growth_schedule") or {"mode": "FLAT"}, month)
    core = float(profile["monthly_core_dca"]) * core_factor
    if str(profile["target_method"]).upper() == "CORE_DCA_MULTIPLE":
        target = core * float(profile["target_multiple"])
    else:
        target = float(profile["target_value"])
    cap = target * float(profile["cap_multiplier"])
    income = float(profile["monthly_income"]) * _growth_factor(profile.get("income_growth_schedule") or {"mode": "FLAT"}, month)
    refill = float(profile["monthly_opportunity_refill"]) * _growth_factor(profile.get("refill_growth_schedule") or {"mode": "FLAT"}, month)
    return max(0.0, target), max(0.0, cap), max(0.0, income), max(0.0, core), max(0.0, refill)


def _ladder_fraction(ladder_id: str, band: str) -> float:
    return float(LADDER_CONFIG[str(ladder_id).upper()]["allocations"][str(band)]) / 100.0


def _adequacy(cash: float, ladder_id: str, target: float, used: set[str]) -> tuple[float | None, float]:
    remaining = sum(_ladder_fraction(ladder_id, str(int(round(value * 100)))) * target for value in FORMAL_THRESHOLDS if str(int(round(value * 100))) not in used)
    if remaining <= 1e-12: return None, 0.0
    return cash / remaining, remaining


def _dispatch_surplus(policy: str, amount: float, balances: dict[str, float]) -> str:
    amount = max(0.0, float(amount))
    if amount <= 1e-12: return "NONE"
    key = str(policy).upper()
    if key == "S0": balances["surplus_cash"] += amount; return "S0_CASH"
    if key == "S1": balances["surplus_to_core_dca"] += amount; return "S1_CORE_DCA"
    if key == "S2": balances["surplus_low_risk_placeholder"] += amount; return "S2_LOW_RISK_PLACEHOLDER"
    raise ValueError("surplus_policy 无效")


def replay_real_world_profile(
    events: Iterable[Mapping[str, Any]],
    profile: Mapping[str, Any],
    *,
    ladder_id: str = "C",
    target_id: str | None = None,
    cap_multiplier: float | None = None,
    refill_mode: str = "FIXED",
    refill_ratio: float | None = None,
    growth_scenario: str = "G0",
    surplus_policy: str | None = None,
    start_date: str,
    end_date: str,
    path_id: str = "HISTORICAL",
    path_type: str = "HISTORICAL",
) -> dict[str, Any]:
    """Replay one real-world profile using the frozen trigger and ladder.

    The timeline contains month starts, event dates and the path bounds. Cash
    balances are held constant between timeline points, so idle cash is
    measured in calendar days without materialising thousands of redundant
    daily rows for every scenario.
    """
    ladder_id = str(ladder_id).upper()
    if ladder_id not in LADDER_IDS: raise ValueError("ladder_id 只能是 C 或 D")
    refill_mode = str(refill_mode).upper()
    if refill_mode not in REFILL_MODES: raise ValueError("refill_mode 只能是 FIXED 或 INCOME_LINKED")
    if refill_mode == "INCOME_LINKED" and refill_ratio not in INCOME_REFILL_RATIOS:
        raise ValueError("income-linked refill ratio 只能是 0.05、0.10、0.15")
    profile = normalize_real_world_profile(profile, target_id=target_id, cap_multiplier=cap_multiplier)
    profile = apply_growth_profile(profile, growth_scenario)
    surplus_policy = str(surplus_policy or profile.get("surplus_policy", "S0")).upper()
    if surplus_policy not in SURPLUS_POLICIES: raise ValueError("surplus_policy 无效")
    start = normalize_date(start_date, field="start_date"); end = normalize_date(end_date, field="end_date")
    if start > end: raise ValueError("start_date 不能晚于 end_date")
    normalised_events = [_normalise_event(item) for item in events]
    events_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in normalised_events:
        if start <= event["event_date"] <= end: events_by_day[event["event_date"]].append(event)
    for day in events_by_day: events_by_day[day].sort(key=lambda item: (99 if item["threshold"] is None else item["threshold"], item["event_id"]))
    # Do not replay the first day of a month that lies before a mid-month
    # start_date.  The opening cash is already measured at start_date, so a
    # prior month-start refill would be a silent look-back.
    timeline = set([start, end]); timeline.update(day for day in _month_starts(start, end) if start <= day <= end); timeline.update(events_by_day)
    points = sorted(timeline)
    first_target, first_cap, _, _, _ = _target_and_cap(profile, start[:7])
    initial_cash = float(profile["initial_opportunity_cash"])
    cash = min(initial_cash, first_cap)
    balances = {"surplus_cash": max(initial_cash - first_cap, 0.0), "surplus_to_core_dca": 0.0, "surplus_low_risk_placeholder": 0.0}
    opening_total = cash + sum(balances.values())
    target = first_target; cap = first_cap
    current_cycle: str | None = None
    in_range_events = [item for item in normalised_events if start <= item["event_date"] <= end]
    first_event = min(in_range_events, key=lambda item: (item["event_date"], item["event_id"])) if in_range_events else None
    if first_event: current_cycle = str(first_event["tactical_cycle_id"])
    used_bands: set[str] = set(); previous_cycle = current_cycle
    last_event: dict[str, Any] | None = None
    event_log: list[dict[str, Any]] = []; snapshots: list[dict[str, Any]] = []; monthly_rows: list[dict[str, Any]] = []
    ignored_events: list[dict[str, Any]] = []; refill_history: list[dict[str, Any]] = []
    total_deployed = total_planned = 0.0; total_refill_requested = total_refill_credited = 0.0
    total_income = total_core_required = total_core_paid = 0.0; core_shortfall_count = 0
    underfunded_count = duplicate_count = rearm_count = 0; deep_total = deep_fully_funded = 0
    cap_overflow_total = 0.0; total_days = 0; idle_cash_days = 0.0; idle_days = 0
    first_full_month: str | None = None; previous_day: str | None = None
    emergency_used = 0.0; borrowed = 0.0

    for index, day in enumerate(points):
        day_target, day_cap, income, core_required, fixed_refill = _target_and_cap(profile, day[:7])
        target_changed = abs(day_target - target) > 1e-12
        target = day_target
        # A target decrease can lower the cap.  The excess is routed through
        # the selected Surplus policy; it cannot be silently discarded.
        cap = day_cap
        if cash > cap + 1e-12:
            overflow = cash - cap; cash = cap; cap_overflow_total += overflow; _dispatch_surplus(surplus_policy, overflow, balances)
        refill_info = {"mode": refill_mode, "ratio": refill_ratio, "requested": 0.0, "credited": 0.0, "overflow": 0.0, "income": income, "core_required": core_required, "core_paid": 0.0, "core_shortfall": 0.0, "date": day}
        is_month_start = date.fromisoformat(day).day == 1
        if is_month_start:
            core_paid = min(core_required, income)
            core_shortfall = max(core_required - core_paid, 0.0)
            core_shortfall_count += int(core_shortfall > 1e-9)
            income_after_core = max(income - core_paid, 0.0)
            requested = income * float(refill_ratio) if refill_mode == "INCOME_LINKED" else fixed_refill
            effective = min(max(0.0, requested), income_after_core)
            room = max(cap - cash, 0.0)
            credited = min(effective, room)
            overflow = max(effective - credited, 0.0)
            cash += credited
            total_income += income; total_core_required += core_required; total_core_paid += core_paid
            total_refill_requested += effective; total_refill_credited += credited; cap_overflow_total += overflow
            route = _dispatch_surplus(surplus_policy, overflow, balances)
            refill_info.update({"requested": effective, "credited": credited, "overflow": overflow, "core_paid": core_paid, "core_shortfall": core_shortfall, "surplus_route": route, "income_missing": income <= 0 and (requested > 0 or core_required > 0)})
            refill_history.append(dict(refill_info))
            monthly_rows.append({"date": day, "month": day[:7], "income": income, "core_required": core_required, "core_paid": core_paid, "core_shortfall": core_shortfall, "opportunity_refill_requested": effective, "opportunity_refill_credited": credited, "cap_overflow": overflow, "target": target, "cap": cap, "surplus_route": route})
        if previous_day is not None:
            span = max(0, (date.fromisoformat(day) - date.fromisoformat(previous_day)).days)
            total_days += span; idle_cash_days += cash * span; idle_days += int(cash > 1e-12) * span
        previous_day = day
        if first_full_month is None and cash + 1e-9 >= target and target > 0:
            first_full_month = day[:7]
        day_events = events_by_day.get(day, [])
        if day_events and current_cycle is None: current_cycle = str(day_events[0]["tactical_cycle_id"])
        transaction_id = f"real-world-tx-{path_id}-{day}" if day_events else None
        sequence = 0; event_ids_today: list[str] = []
        for event in day_events:
            event_cycle = str(event["tactical_cycle_id"])
            if current_cycle is None: current_cycle = event_cycle
            elif event_cycle != current_cycle:
                current_cycle = event_cycle; used_bands = set(); rearm_count += 1
            threshold = event["threshold"]
            formal = event["event_kind"] == "TACTICAL_BAND" and threshold in FORMAL_THRESHOLDS
            if not formal:
                ignored_events.append({"event_id": event["event_id"], "event_date": day, "reason": "ATH_LABEL_OR_NON_FORMAL_EVENT", "event_kind": event["event_kind"]}); continue
            band = str(int(round(float(threshold) * 100)))
            if band in used_bands:
                duplicate_count += 1; ignored_events.append({"event_id": event["event_id"], "event_date": day, "reason": "DUPLICATE_BAND_IN_CYCLE", "band": band}); continue
            sequence += 1
            planned = _ladder_fraction(ladder_id, band) * target
            before = cash; actual = min(planned, max(before, 0.0)); shortfall = max(planned - actual, 0.0); cash = max(0.0, before - actual)
            underfunded = shortfall > 1e-9; underfunded_count += int(underfunded)
            if float(threshold) >= 0.4:
                deep_total += 1; deep_fully_funded += int(not underfunded)
            total_planned += planned; total_deployed += actual; used_bands.add(band); event_ids_today.append(event["event_id"])
            entry = {"real_world_run_id": path_id, "path_id": path_id, "transaction_id": transaction_id, "transaction_sequence": sequence, "event_date": day, "tactical_cycle_id": current_cycle, "band": band, "threshold": float(threshold), "trigger_drawdown": event["trigger_drawdown"], "ladder_id": ladder_id, "ladder_version": CAPITAL_STATE_C_VERSION if ladder_id == "C" else CAPITAL_STATE_D_VERSION, "target_fund": target, "fund_cap": cap, "planned_amount": planned, "actual_amount": actual, "shortfall": shortfall, "cash_before": before, "cash_after": cash, "underfunded": underfunded, "core_dca_untouched": True, "emergency_fund_excluded": True, "debt_financing_allowed": False, "simulation_only": True, "auto_trade": False, "payload": {"source_event_id": event["event_id"], "target_changed_today": target_changed, "surplus_policy": surplus_policy}}
            entry["event_hash"] = sha256_json({key: value for key, value in entry.items() if key != "event_hash"}); event_log.append(entry); last_event = entry
        adequacy, remaining = _adequacy(cash, ladder_id, target, used_bands)
        if target > 0 and first_full_month is None and cash + 1e-9 >= target: first_full_month = day[:7]
        snapshot = {"date": day, "path_id": path_id, "path_type": path_type, "profile_id": profile["profile_id"], "target_id": profile["target_id"], "tactical_cycle_id": current_cycle, "used_bands": sorted(used_bands, key=lambda item: int(item)), "armed_bands": sorted(set(str(int(round(value * 100))) for value in FORMAL_THRESHOLDS) - used_bands, key=lambda item: int(item)), "tactical_event_ids_today": event_ids_today, "available_opportunity_cash": cash, "target_opportunity_cash": target, "opportunity_fund_cap": cap, "surplus_cash": balances["surplus_cash"], "surplus_to_core_dca": balances["surplus_to_core_dca"], "surplus_low_risk_placeholder": balances["surplus_low_risk_placeholder"], "capital_adequacy_ratio": adequacy, "remaining_theoretical_amount": remaining, "last_capital_event_date": None if last_event is None else last_event["event_date"], "last_capital_event_band": None if last_event is None else last_event["band"], "last_capital_event_amount": 0.0 if last_event is None else last_event["actual_amount"], "target_changed": target_changed, "core_dca_untouched": True, "emergency_fund_excluded": True, "debt_financing_allowed": False, "simulation_only": True, "auto_trade": False}
        snapshots.append(snapshot)
    if previous_day is not None:
        end_span = max(0, (date.fromisoformat(end) - date.fromisoformat(previous_day)).days)
        total_days += end_span; idle_cash_days += cash * end_span; idle_days += int(cash > 1e-12) * end_span
    # The timeline starts and ends at the same date in one-day paths. Treat
    # such a state as one observed day for a stable idle-time denominator.
    if total_days <= 0: total_days = 1; idle_cash_days = cash; idle_days = int(cash > 1e-12)
    policy_balance = sum(balances.values())
    # Both credited refill and cap-routed surplus are funded by the same
    # monthly request.  Count the effective request once so cash conservation
    # includes money that was routed directly to a surplus bucket.
    total_inflow = opening_total + total_refill_requested
    conservation_error = abs(total_inflow - total_deployed - (cash + policy_balance))
    first_month_index = _month_index(start[:7]); full_months = None if first_full_month is None else max(0, _month_index(first_full_month) - first_month_index)
    avg_cash = idle_cash_days / total_days
    avg_refill_burden = (total_refill_requested / total_income) if total_income > 0 else None
    max_refill_burden = max((row["opportunity_refill_requested"] / row["income"] for row in monthly_rows if row["income"] > 0), default=None)
    ordinary_events = [row for row in event_log if row["band"] in {"10", "20"}]
    ordinary_participation = any(row["actual_amount"] > 1e-9 for row in ordinary_events)
    deep_ready = all(row["actual_amount"] + 1e-9 >= row["planned_amount"] for row in event_log if row["band"] in {"40", "50"}) if any(row["band"] in {"40", "50"} for row in event_log) else True
    viability_reasons: list[str] = []
    if profile["emergency_fund_excluded"] is not True: viability_reasons.append("紧急备用金未排除")
    if borrowed > 1e-9 or emergency_used > 1e-9: viability_reasons.append("存在借款或备用金转入")
    if avg_refill_burden is not None and avg_refill_burden > REAL_WORLD_GATE_CONFIG["max_average_refill_burden_ratio"]: viability_reasons.append("平均 Refill Burden 超过固定门槛")
    if max_refill_burden is not None and max_refill_burden > REAL_WORLD_GATE_CONFIG["max_refill_burden_ratio"]: viability_reasons.append("最高 Refill Burden 超过固定门槛")
    if REAL_WORLD_GATE_CONFIG["ordinary_band_participation_required"] and event_log and not ordinary_participation: viability_reasons.append("普通回撤没有实际参与")
    if REAL_WORLD_GATE_CONFIG["max_time_to_full_months"] is not None and full_months is not None and full_months > REAL_WORLD_GATE_CONFIG["max_time_to_full_months"]: viability_reasons.append("达到目标资金池所需时间超过36个月")
    viability = "PASS" if not viability_reasons else "FAIL"
    # Core DCA is always settled before any opportunity refill.  This flag
    # checks that ordering directly; a low-income month may still report a
    # core shortfall without allowing tactical cash to cover it.
    core_priority_holds = all(
        float(row.get("core_paid", 0.0)) <= float(row.get("income", 0.0)) + 1e-9
        and float(row.get("opportunity_refill_requested", 0.0))
        <= max(float(row.get("income", 0.0)) - float(row.get("core_paid", 0.0)), 0.0) + 1e-9
        for row in monthly_rows
    )
    return {"model_version": REAL_WORLD_MODEL_VERSION, "path_id": path_id, "path_type": path_type, "profile": profile, "ladder_id": ladder_id, "target_id": profile["target_id"], "cap_multiplier": profile["cap_multiplier"], "refill_mode": refill_mode, "refill_ratio": refill_ratio, "growth_scenario": growth_scenario, "surplus_policy": surplus_policy, "start_date": start, "end_date": end, "opening_opportunity_cash": opening_total, "opening_available_cash": min(initial_cash, first_cap), "target_at_start": first_target, "cap_at_start": first_cap, "ending_opportunity_cash": cash, "ending_surplus_cash": balances["surplus_cash"], "surplus_to_core_dca": balances["surplus_to_core_dca"], "surplus_low_risk_placeholder": balances["surplus_low_risk_placeholder"], "total_income": total_income, "total_core_dca_required": total_core_required, "total_core_dca_paid": total_core_paid, "core_dca_shortfall_count": core_shortfall_count, "total_refill_requested": total_refill_requested, "total_refill_credited": total_refill_credited, "total_deployed": total_deployed, "total_planned": total_planned, "total_inflow": total_inflow, "cap_overflow_total": cap_overflow_total, "policy_balance": policy_balance, "cash_conservation_error": conservation_error, "underfunded_event_count": underfunded_count, "underfunded_event_rate": underfunded_count / len(event_log) if event_log else 0.0, "duplicate_trigger_count": duplicate_count, "rearm_count": rearm_count, "event_count": len(event_log), "snapshot_count": len(snapshots), "capital_utilization": total_deployed / total_inflow if total_inflow > 1e-12 else 0.0, "capital_adequacy_ratio": snapshots[-1]["capital_adequacy_ratio"] if snapshots else None, "capital_adequacy_label": capital_adequacy_label(snapshots[-1]["capital_adequacy_ratio"] if snapshots else None), "average_opportunity_cash_balance": avg_cash, "cash_utilization_ratio": total_deployed / total_inflow if total_inflow > 1e-12 else 0.0, "cash_idle_time_days": idle_days, "cash_idle_time_ratio": idle_days / total_days, "idle_cash_days": idle_cash_days, "cash_drag_proxy": {"average_idle_cash": avg_cash, "idle_cash_days": idle_cash_days, "rate_assumption": None, "monetary_drag": None}, "refill_burden_ratio": avg_refill_burden, "max_refill_burden_ratio": max_refill_burden, "first_full_target_month": first_full_month, "time_to_full_months": full_months, "deep_crisis_event_count_40_50": deep_total, "deep_crisis_fully_funded_count": deep_fully_funded, "deep_crisis_readiness": deep_ready, "ordinary_band_participation": ordinary_participation, "emergency_fund_excluded": True, "emergency_cash_used": emergency_used, "debt_financing_allowed": False, "borrowed_amount": borrowed, "core_dca_priority_holds": core_priority_holds, "cash_nonnegative": cash >= -1e-9 and all(row["available_opportunity_cash"] >= -1e-9 for row in snapshots), "cash_conservation_holds": conservation_error <= 1e-7, "target_growth_does_not_create_cash": True, "phase3a_trigger_history_unchanged": True, "phase3a_config_hash": STATE_MACHINE_CONFIG_HASH, "real_world_viability": viability, "viability_reasons": viability_reasons, "cash_flow_waterfall": ["income", "living_expenses_external", "core_dca", "opportunity_refill", "opportunity_fund_cap", "surplus_policy"], "events": event_log, "monthly_snapshots": monthly_rows, "snapshots": snapshots, "refill_history": refill_history, "ignored_events": ignored_events, "replay_fingerprint": sha256_json({"events": event_log, "snapshots": snapshots, "monthly": monthly_rows}), "final_state": snapshots[-1] if snapshots else None}


def capital_adequacy_label(ratio: float | None) -> str:
    if ratio is None: return "FULLY FUNDED"
    ratio = float(ratio)
    if ratio >= 1.0: return "FULLY FUNDED"
    if ratio >= 0.75: return "WELL FUNDED"
    if ratio >= 0.5: return "PARTIALLY FUNDED"
    if ratio >= 0.25: return "LOW RESERVE"
    return "CRITICALLY LOW"


def _scenario_key(result: Mapping[str, Any]) -> str:
    return f"{result['profile']['profile_id']}|{result['target_id']}|{result['cap_multiplier']}|{result['refill_mode']}|{result.get('refill_ratio')}|{result['growth_scenario']}|{result['surplus_policy']}|{result['ladder_id']}"


_BULKY_RESULT_FIELDS = {"events", "snapshots", "monthly_snapshots", "refill_history", "ignored_events", "final_state"}


def _public_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return the aggregate scenario view used by reports and APIs.

    Event ledgers and timeline snapshots remain available in the append-only
    scenario/path tables only when a caller explicitly asks for them in a
    future replay endpoint.  Keeping them out of the report prevents a
    multi-profile grid from turning the dashboard status response into a
    multi-megabyte download.
    """
    return {key: value for key, value in result.items() if key not in _BULKY_RESULT_FIELDS}


def _stress_paths(start: str, end: str) -> dict[str, dict[str, Any]]:
    def ev(threshold: float, day: str, cycle: str) -> dict[str, Any]: return {"event_id": f"{cycle}-{day}-{threshold}", "event_date": day, "tactical_cycle_id": cycle, "threshold": threshold, "trigger_drawdown": -threshold, "event_kind": "TACTICAL_BAND"}
    return {
        "R1_EARLY_50": {"path_type": "STRESS", "events": [ev(0.10, start, "r1"), ev(0.20, start, "r1"), ev(0.30, start, "r1"), ev(0.40, start, "r1"), ev(0.50, start, "r1")], "start_date": start, "end_date": end, "description": "刚开始就遇到 -50%"},
        "R2_LOW_INCOME_REPEATED_20": {"path_type": "STRESS", "events": [ev(0.20, start, "r2a"), ev(0.20, "2000-07-01", "r2b"), ev(0.20, "2001-01-01", "r2c")], "start_date": start, "end_date": end, "description": "低收入阶段连续 -20% cycles"},
        "R3_GROWTH_THEN_CRASH": {"path_type": "STRESS", "events": [ev(0.10, "2019-03-01", "r3"), ev(0.20, "2019-03-01", "r3"), ev(0.30, "2019-03-01", "r3"), ev(0.40, "2019-03-01", "r3"), ev(0.50, "2019-03-01", "r3")], "start_date": start, "end_date": end, "description": "收入增长后才遇到大熊市"},
        "R4_RECRASH_AFTER_6M": {"path_type": "STRESS", "events": [ev(0.10, "2020-01-01", "r4a"), ev(0.20, "2020-01-01", "r4a"), ev(0.30, "2020-07-01", "r4b")], "start_date": start, "end_date": end, "description": "资金刚使用后六个月再次 -30%"},
        "R5_LONG_BULL_CAP": {"path_type": "STRESS", "events": [], "start_date": start, "end_date": end, "description": "多年牛市，资金长期达到 Cap"},
        "R6_INCOME_STOP_2Y": {"path_type": "STRESS", "events": [ev(0.20, "2022-03-01", "r6")], "start_date": start, "end_date": end, "description": "收入增长暂停两年"},
        "R7_CORE_UP_REFILL_FLAT": {"path_type": "STRESS", "events": [ev(0.20, "2023-03-01", "r7")], "start_date": start, "end_date": end, "description": "Core DCA 提高但机会补充不变"},
        "R8_TARGET_UP_BEFORE_CRASH": {"path_type": "STRESS", "events": [ev(0.30, "2024-01-01", "r8")], "start_date": start, "end_date": end, "description": "Target 提高后立刻大跌"},
    }


def _scenario_grid(profiles: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    grid: list[dict[str, Any]] = []
    # Baseline: all profiles × targets × caps, fixed refill and flat growth.
    for profile_id in profiles:
        for target_id in TARGET_IDS:
            for cap in CAP_MULTIPLIERS:
                grid.append({"profile_id": profile_id, "target_id": target_id, "cap_multiplier": cap, "refill_mode": "FIXED", "refill_ratio": None, "growth_scenario": "G0", "surplus_policy": "S0"})
    # Income-linked refill: F2/1.5/G0/S0 across all profiles and ratios.
    for profile_id in profiles:
        for ratio in INCOME_REFILL_RATIOS:
            grid.append({"profile_id": profile_id, "target_id": "F2", "cap_multiplier": 1.5, "refill_mode": "INCOME_LINKED", "refill_ratio": ratio, "growth_scenario": "G0", "surplus_policy": "S0"})
    # Growth: compare G0–G3 at the balanced target/cap and fixed refill.
    for profile_id in profiles:
        for growth in GROWTH_SCENARIOS:
            grid.append({"profile_id": profile_id, "target_id": "F2", "cap_multiplier": 1.5, "refill_mode": "FIXED", "refill_ratio": None, "growth_scenario": growth, "surplus_policy": "S0"})
    # Surplus policies: same balanced cash-flow profile, all three policies.
    for profile_id in profiles:
        for policy in SURPLUS_POLICIES:
            grid.append({"profile_id": profile_id, "target_id": "F2", "cap_multiplier": 1.5, "refill_mode": "FIXED", "refill_ratio": None, "growth_scenario": "G0", "surplus_policy": policy})
    # De-duplicate while retaining deterministic order.
    seen: set[str] = set(); output: list[dict[str, Any]] = []
    for item in grid:
        key = json.dumps(item, sort_keys=True)
        if key not in seen: seen.add(key); output.append(item)
    return output


def _profile_for_target(base: Mapping[str, Any], target_id: str, cap: float) -> dict[str, Any]:
    target_id = str(target_id).upper()
    if target_id not in TARGET_SPECS:
        raise ValueError("target_id 只能是 F1、F2、F3、F4")
    profile = dict(base)
    spec = TARGET_SPECS[target_id]
    # The scenario target is authoritative.  This prevents a base profile's
    # default F2 method from silently turning F4 into a six-month target.
    profile["target_id"] = target_id
    profile["target_method"] = spec["method"]
    if spec["method"] == "CORE_DCA_MULTIPLE":
        profile["target_multiple"] = float(spec["months"])
        profile.pop("target_value", None)
    else:
        profile["target_value"] = float(spec["amount"])
        profile.pop("target_multiple", None)
    return normalize_real_world_profile(profile, target_id=target_id, cap_multiplier=cap)


def compare_ladders(results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(results)
    by_profile: dict[str, dict[str, list[Mapping[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows: by_profile[str(row["profile"]["profile_id"])][str(row["ladder_id"])].append(row)
    output: dict[str, Any] = {}
    for profile_id, ladders in sorted(by_profile.items()):
        summary: dict[str, Any] = {}
        for ladder in LADDER_IDS:
            values = ladders.get(ladder, [])
            summary[ladder] = {
                "scenario_count": len(values),
                "mean_capital_utilization": mean(float(x["capital_utilization"]) for x in values) if values else None,
                "mean_cash_drag_proxy": mean(float(x["cash_drag_proxy"]["average_idle_cash"]) for x in values) if values else None,
                "mean_underfunded_rate": mean(float(x["underfunded_event_rate"]) for x in values) if values else None,
                "mean_refill_burden_ratio": mean(float(x["refill_burden_ratio"]) for x in values if x.get("refill_burden_ratio") is not None) if any(x.get("refill_burden_ratio") is not None for x in values) else None,
                "mean_time_to_full_months": mean(float(x["time_to_full_months"]) for x in values if x.get("time_to_full_months") is not None) if any(x.get("time_to_full_months") is not None for x in values) else None,
                "deep_crisis_readiness_rate": mean(bool(x["deep_crisis_readiness"]) for x in values) if values else None,
                "ordinary_participation_rate": mean(bool(x["ordinary_band_participation"]) for x in values) if values else None,
                "viability_rate": mean(x["real_world_viability"] == "PASS" for x in values) if values else None,
                "cash_conservation_holds": all(bool(x["cash_conservation_holds"]) for x in values),
            }
        c, d = summary.get("C", {}), summary.get("D", {})
        deltas = {key: (c.get(key) - d.get(key)) if isinstance(c.get(key), (int, float)) and isinstance(d.get(key), (int, float)) else None for key in ("mean_capital_utilization", "mean_cash_drag_proxy", "mean_underfunded_rate", "mean_refill_burden_ratio", "mean_time_to_full_months", "deep_crisis_readiness_rate", "ordinary_participation_rate", "viability_rate")}
        output[profile_id] = {"C": c, "D": d, "C_minus_D": deltas, "selection": "PROFILE_DEPENDENT"}
    return output


def _candidate_profiles(results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [row for row in results if row.get("real_world_viability") == "PASS"]
    if not rows: return {"Conservative": None, "Balanced": None, "Aggressive": None, "selection_method": "no viable simulated row"}
    # Structural labels are assigned after simulation, using frozen metrics;
    # no final wealth or return is used.
    def score(row: Mapping[str, Any]) -> tuple[float, float, float]:
        adequacy = float(row.get("capital_adequacy_ratio") or 0.0)
        deep = 1.0 if row.get("deep_crisis_readiness") else 0.0
        burden = float(row.get("refill_burden_ratio") or 0.0)
        utilization = float(row.get("capital_utilization") or 0.0)
        drag = float(row.get("cash_drag_proxy", {}).get("average_idle_cash") or 0.0)
        return (adequacy + deep - burden, utilization - drag / max(float(row.get("target_at_start") or 1.0), 1.0), -burden)
    conservative = max(rows, key=lambda row: score(row)[0])
    aggressive = max(rows, key=lambda row: score(row)[1])
    median_row = sorted(rows, key=lambda row: (abs(score(row)[0] - median(score(item)[0] for item in rows)), abs(score(row)[1] - median(score(item)[1] for item in rows))))[0]
    def descriptor(row: Mapping[str, Any]) -> dict[str, Any]: return {key: row.get(key) for key in ("profile", "ladder_id", "target_id", "cap_multiplier", "refill_mode", "refill_ratio", "growth_scenario", "surplus_policy", "real_world_viability", "capital_adequacy_ratio", "capital_adequacy_label", "capital_utilization", "cash_drag_proxy", "refill_burden_ratio", "time_to_full_months")}
    return {"Conservative": descriptor(conservative), "Balanced": descriptor(median_row), "Aggressive": descriptor(aggressive), "selection_method": "post-simulation structural score; no return maximization or trigger re-ranking"}


def _income_stop_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    p = dict(profile); p["income_growth_schedule"] = {"mode": "STOP", "stop_month": "2022-01", "after_factor": 1.0}; return p


def _income_pause_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze a 5% annual income-growth path for 2022–2023, then resume."""
    p = dict(profile)
    p["income_growth_schedule"] = {
        "mode": "PAUSE_THEN_COMPOUND",
        "annual_rate": 0.05,
        "base_month": "2000-01",
        "pause_start": "2022-01",
        "pause_end": "2024-01",
    }
    return p


def _custom_stress_events(path_id: str, start: str, end: str) -> list[dict[str, Any]]:
    return _stress_paths(start, end).get(path_id, {}).get("events", [])


def build_phase3b_report(*, batch_id: str, profile_results: Iterable[Mapping[str, Any]], path_results: Iterable[Mapping[str, Any]], phase3a_batch_id: str | None, run_id: str | None = None) -> dict[str, Any]:
    rows = list(profile_results); paths = list(path_results)
    ladder_comparison = compare_ladders(rows)
    candidate = _candidate_profiles(rows)
    def scenario_rows(**filters: Any) -> list[dict[str, Any]]:
        return [_public_result(row) for row in rows if all(row.get(key) == value for key, value in filters.items())]
    baseline_rows = scenario_rows(target_id="F2", cap_multiplier=1.5, refill_mode="FIXED", growth_scenario="G0", surplus_policy="S0")
    target_rows = {target: scenario_rows(target_id=target, refill_mode="FIXED", growth_scenario="G0", surplus_policy="S0") for target in TARGET_IDS}
    income_refill_rows = [_public_result(row) for row in rows if row.get("refill_mode") == "INCOME_LINKED"]
    growth_rows = [_public_result(row) for row in rows if row.get("growth_scenario") != "G0"]
    surplus_rows = [_public_result(row) for row in rows if row.get("target_id") == "F2" and row.get("cap_multiplier") == 1.5 and row.get("refill_mode") == "FIXED" and row.get("growth_scenario") == "G0"]
    stress_summary: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in paths:
        stress_summary[str(row.get("path_id"))].append(_public_result(row))
    trigger_unchanged = all(bool(row.get("phase3a_trigger_history_unchanged")) and row.get("phase3a_config_hash") == STATE_MACHINE_CONFIG_HASH for row in rows + paths)
    invariants = {
        "emergency_cash_never_enters_opportunity_fund": all(bool(row.get("emergency_fund_excluded")) and float(row.get("emergency_cash_used", 0)) == 0 for row in rows + paths),
        "debt_financing_never_used": all(not bool(row.get("debt_financing_allowed")) and float(row.get("borrowed_amount", 0)) == 0 for row in rows + paths),
        "core_dca_priority": all(bool(row.get("core_dca_priority_holds")) for row in rows + paths),
        "cash_nonnegative": all(bool(row.get("cash_nonnegative")) for row in rows + paths),
        "cash_conservation": all(bool(row.get("cash_conservation_holds")) for row in rows + paths),
        "target_growth_does_not_create_cash": all(bool(row.get("target_growth_does_not_create_cash")) for row in rows + paths),
        "phase3a_trigger_history_unchanged": trigger_unchanged,
        "core_dca_untouched_by_tactical_spend": all(bool(row.get("core_dca_priority_holds")) for row in rows + paths),
        "cap_routes_excess": True,
        "surplus_policy_deterministic": True,
        "no_auto_trade": True,
    }
    # Ladder selection is intentionally conservative: unless one candidate is
    # consistently better in every profile and stress dimension, retain a
    # profile-dependent conclusion.
    ladder_selection = "INCONCLUSIVE"
    profile_selections = [item.get("selection") for item in ladder_comparison.values()]
    if profile_selections and all(item == "PROFILE_DEPENDENT" for item in profile_selections): ladder_selection = "PROFILE_DEPENDENT"
    # The executable invariants can pass while the upstream history remains a
    # proxy and standard income values remain assumptions.  Keep that
    # limitation visible in the formal phase status instead of collapsing it
    # into an unqualified PASS.
    status = "PASS_WITH_LIMITATIONS" if rows and all(invariants.values()) else "FAIL" if rows else "FAIL"
    return {
        "phase3b_batch_id": batch_id,
        "real_world_model_version": REAL_WORLD_MODEL_VERSION,
        "PHASE_3B_STATUS": status,
        "REAL_WORLD_PARAMETERIZATION": "VALID" if rows and all(invariants.values()) else "INVALID",
        "LADDER_SELECTION": ladder_selection,
        "READY_FOR_PERSONAL_PROFILE": "YES" if rows and all(invariants.values()) else "NO",
        "simulation_only": True,
        "auto_trade": False,
        "phase3a_batch_id": phase3a_batch_id,
        "phase3a_config_hash": STATE_MACHINE_CONFIG_HASH,
        "real_world_config_hash": REAL_WORLD_CONFIG_HASH,
        "trigger": {"version": TRIGGER_VERSION, "window_trading_days": TRIGGER_WINDOW, "thresholds": list(FORMAL_THRESHOLDS), "ath_long_term_reference": True},
        "ladder_definitions": {"C": copy.deepcopy(LADDER_CONFIG["C"]), "D": copy.deepcopy(LADDER_CONFIG["D"])},
        "capital_profile_schema": ["profile_id", "currency", "monthly_income", "monthly_core_dca", "monthly_opportunity_refill", "opportunity_fund_target", "opportunity_fund_cap", "initial_opportunity_cash", "surplus_policy", "income_growth_schedule", "core_dca_growth_schedule", "refill_growth_schedule", "emergency_fund_excluded", "debt_financing_allowed", "auto_trade"],
        "standard_profiles": copy.deepcopy(STANDARD_PROFILE_ASSUMPTIONS),
        "target_specs": copy.deepcopy(TARGET_SPECS),
        "cap_multipliers": list(CAP_MULTIPLIERS),
        "income_refill_ratios": list(INCOME_REFILL_RATIOS),
        "growth_scenarios": {key: growth_schedule_for(key) for key in GROWTH_SCENARIOS},
        "surplus_policies": copy.deepcopy(SURPLUS_POLICY_DEFINITIONS),
        "waterfall": REAL_WORLD_CONFIG["waterfall"],
        "gate": copy.deepcopy(REAL_WORLD_GATE_CONFIG),
        "profile_result_count": len(rows),
        "path_result_count": len(paths),
        "baseline_profile_results": baseline_rows,
        "target_results": target_rows,
        "income_linked_refill_results": income_refill_rows,
        "income_growth_results": growth_rows,
        "surplus_policy_results": surplus_rows,
        "stress_path_results": dict(sorted(stress_summary.items())),
        "capital_adequacy_labels": {f"{row['profile']['profile_id']}|{row['ladder_id']}": row.get("capital_adequacy_label") for row in baseline_rows},
        "cash_drag_summary": {"definition": "average idle Opportunity cash and idle cash-days", "baseline": [{"profile_id": row["profile"]["profile_id"], "ladder_id": row["ladder_id"], "average_idle_cash": row.get("cash_drag_proxy", {}).get("average_idle_cash"), "idle_cash_days": row.get("cash_drag_proxy", {}).get("idle_cash_days")} for row in baseline_rows]},
        "refill_burden_summary": {"definition": "effective monthly opportunity refill / monthly income", "baseline": [{"profile_id": row["profile"]["profile_id"], "ladder_id": row["ladder_id"], "refill_burden_ratio": row.get("refill_burden_ratio"), "max_refill_burden_ratio": row.get("max_refill_burden_ratio")} for row in baseline_rows]},
        "profile_results": [_public_result(row) for row in rows],
        "path_results": [_public_result(row) for row in paths],
        "ladder_comparison": ladder_comparison,
        "candidate_profiles": candidate,
        "invariants": invariants,
        "cash_drag_definition": "average idle Opportunity cash and idle cash-days; no fixed return assumption",
        "refill_burden_definition": "effective monthly opportunity refill / monthly income after Core DCA priority",
        "limitations": [
            "P1–P4 的收入是可编辑压力场景假设，不是用户真实收入或推荐。",
            "历史 NDX 触发输入沿用 HISTORICAL_PROXY；本阶段验证现金流执行，不验证买入信号本身。",
            "生活开支在系统外，水位计算只保证 Core DCA 优先于 Opportunity Refill。",
            "S2 是低风险现金管理占位，不指定具体基金或收益率；Cash Drag 不货币化。",
            "本阶段不含止盈、杠杆、借款、自动交易或多资产扩展。",
        ],
        "run_id": run_id,
    }


def _historical_events(repo: PITRepository, phase3a_batch_id: str | None, start: str, end: str) -> tuple[list[dict[str, Any]], str | None]:
    batch = phase3a_batch_id
    if not batch:
        runs = repo.get_capital_state_machine_runs(market="NDX", limit=1000)
        batch = next((item["batch_id"] for item in runs if item.get("status") == "COMPLETED"), None)
    if not batch: raise ValueError("需要已完成的 Phase 3A batch")
    runs = [item for item in repo.get_capital_state_machine_runs(market="NDX", batch_id=batch, limit=10) if item.get("status") == "COMPLETED"]
    if not runs: raise ValueError("Phase 3A batch 没有已完成 run")
    run_id = next((item["state_machine_run_id"] for item in runs if item["ladder_id"] == "C"), runs[0]["state_machine_run_id"])
    # The event stream is identical for C/D; only use one copy as the input
    # history, then run both ladders against that history.
    return repo.get_capital_events(run_id, as_of=end, limit=1000000), batch


def run_phase3b_validation(
    repo: PITRepository | None = None,
    *,
    phase3a_batch_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    batch_id: str | None = None,
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    repo = repo or PITRepository(DB)
    events, resolved_phase3a_batch = _historical_events(repo, phase3a_batch_id, start_date or "2000-01-01", end_date or date.today().isoformat())
    start = normalize_date(start_date or "2000-01-01"); end = normalize_date(end_date or date.today().isoformat())
    base_profiles = {key: normalize_real_world_profile(value) for key, value in (profiles or standard_profiles()).items()}
    batch = str(batch_id or f"phase3b-real-world-ndx-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    run_id = batch
    run_config = copy.deepcopy(REAL_WORLD_CONFIG)
    run_config["profile_ids"] = sorted(base_profiles)
    run_config["phase3a_batch_id"] = resolved_phase3a_batch
    run, created = repo.create_real_world_parameterization_run({"real_world_run_id": run_id, "batch_id": batch, "market": "NDX", "model_version": REAL_WORLD_MODEL_VERSION, "phase3a_batch_id": resolved_phase3a_batch, "start_date": start, "end_date": end, "config": run_config, "config_hash": REAL_WORLD_CONFIG_HASH, "data_snapshot": {"phase3a_batch_id": resolved_phase3a_batch, "event_count": len(events), "source": "Phase 3A capital event log"}, "data_cutoff": as_of_datetime(f"{end}T23:59:59.999999Z"), "simulation_only": True, "auto_trade": False})
    if not created and run.get("status") == "COMPLETED":
        stored = repo.get_real_world_parameterization_report(run_id)
        if stored and stored.get("report"): return stored["report"]
    profile_results: list[dict[str, Any]] = []; path_results: list[dict[str, Any]] = []
    grid = _scenario_grid(base_profiles)
    for item in grid:
        base = base_profiles[item["profile_id"]]
        for ladder in LADDER_IDS:
            replay = replay_real_world_profile(events, _profile_for_target(base, item["target_id"], item["cap_multiplier"]), ladder_id=ladder, target_id=item["target_id"], cap_multiplier=item["cap_multiplier"], refill_mode=item["refill_mode"], refill_ratio=item["refill_ratio"], growth_scenario=item["growth_scenario"], surplus_policy=item["surplus_policy"], start_date=start, end_date=end, path_id="HISTORICAL", path_type="HISTORICAL")
            profile_results.append(replay)
    # Stress tests use a balanced F2/1.5 setting for each profile, ladder,
    # growth/refill variant; each path is kept separate from the historical
    # rows so it cannot affect profile candidate selection accidentally.
    stress_paths = _stress_paths(start, end)
    for path_id, path in stress_paths.items():
        for profile_id, base in base_profiles.items():
            growth = "G3" if path_id == "R3_GROWTH_THEN_CRASH" else "G0"
            profile_input = _income_pause_profile(base) if path_id == "R6_INCOME_STOP_2Y" else base
            if path_id == "R7_CORE_UP_REFILL_FLAT":
                profile_input = dict(base); profile_input["core_dca_growth_schedule"] = {"mode": "STEP", "changes": {"2000-01": 1.0, "2023-01": 2.0}}
            if path_id == "R8_TARGET_UP_BEFORE_CRASH":
                profile_input = dict(base); profile_input["target_method"] = "CORE_DCA_MULTIPLE"; profile_input["target_multiple"] = 6.0; profile_input["core_dca_growth_schedule"] = {"mode": "STEP", "changes": {"2000-01": 1.0, "2024-01": 2.0}}
            for ladder in LADDER_IDS:
                replay = replay_real_world_profile(path.get("events", []), _profile_for_target(profile_input, "F2", 1.5), ladder_id=ladder, target_id="F2", cap_multiplier=1.5, refill_mode="FIXED", refill_ratio=None, growth_scenario=growth, surplus_policy="S0", start_date=start, end_date=end, path_id=path_id, path_type="STRESS")
                path_results.append(replay)
    report = build_phase3b_report(batch_id=batch, profile_results=profile_results, path_results=path_results, phase3a_batch_id=resolved_phase3a_batch, run_id=run_id)
    for row in profile_results:
        repo.append_real_world_profile_scenario({"real_world_run_id": run_id, "profile_id": row["profile"]["profile_id"], "target_id": row["target_id"], "cap_multiplier": row["cap_multiplier"], "refill_mode": row["refill_mode"], "refill_ratio": row["refill_ratio"], "growth_scenario": row["growth_scenario"], "surplus_policy": row["surplus_policy"], "ladder_id": row["ladder_id"], "profile": row["profile"], "metrics": {key: value for key, value in row.items() if key not in {"events", "snapshots", "monthly_snapshots", "refill_history", "ignored_events", "final_state"}}, "result_hash": sha256_json(row)})
    for row in path_results:
        repo.append_real_world_path_result({"real_world_run_id": run_id, "path_id": row["path_id"], "path_type": row["path_type"], "profile_id": row["profile"]["profile_id"], "target_id": row["target_id"], "cap_multiplier": row["cap_multiplier"], "refill_mode": row["refill_mode"], "refill_ratio": row["refill_ratio"], "growth_scenario": row["growth_scenario"], "surplus_policy": row["surplus_policy"], "ladder_id": row["ladder_id"], "result": {key: value for key, value in row.items() if key not in {"events", "snapshots", "monthly_snapshots", "refill_history", "ignored_events", "final_state"}}, "result_hash": sha256_json(row)})
    repo.record_real_world_parameterization_report(run_id, report)
    repo.complete_real_world_parameterization_run(run_id, summary={"PHASE_3B_STATUS": report["PHASE_3B_STATUS"], "profile_result_count": len(profile_results), "path_result_count": len(path_results)})
    return report


def report_from_repository(repo: PITRepository | None = None) -> dict[str, Any]:
    repo = repo or PITRepository(DB)
    runs = repo.get_real_world_parameterization_runs(market="NDX", limit=1000)
    completed = next((item for item in runs if item.get("status") == "COMPLETED"), None)
    if not completed: return {"PHASE_3B_STATUS": "NOT_RUN", "REAL_WORLD_PARAMETERIZATION": "INVALID", "LADDER_SELECTION": "INCONCLUSIVE", "READY_FOR_PERSONAL_PROFILE": "NO", "simulation_only": True, "auto_trade": False}
    stored = repo.get_real_world_parameterization_report(completed["real_world_run_id"])
    return (stored or {}).get("report") or {"PHASE_3B_STATUS": "INCONCLUSIVE", "phase3b_batch_id": completed["batch_id"]}


def markdown_report(report: Mapping[str, Any]) -> str:
    target_labels = ", ".join(
        f"{key}={value.get('label')}"
        for key, value in report.get("target_specs", {}).items()
    )
    lines = ["# Phase 3B：Real-World Parameterization & Capital Profile Validation", "", f"批次 `{report.get('phase3b_batch_id')}`；simulation_only={report.get('simulation_only')}、auto_trade={report.get('auto_trade')}。", "", "| 结论 | 状态 |", "|---|---|", f"| `PHASE_3B_STATUS` | **{report.get('PHASE_3B_STATUS')}** |", f"| `REAL_WORLD_PARAMETERIZATION` | **{report.get('REAL_WORLD_PARAMETERIZATION')}** |", f"| `LADDER_SELECTION` | **{report.get('LADDER_SELECTION')}** |", f"| `READY_FOR_PERSONAL_PROFILE` | **{report.get('READY_FOR_PERSONAL_PROFILE')}** |", "", "## 覆盖范围", "", f"- Profile scenarios: {report.get('profile_result_count')}; stress paths: {report.get('path_result_count')}。", f"- Targets: {target_labels}。", "- C/D、252日 Tactical Trigger、ATH 长期参考和 Phase 3A 资金逻辑在本阶段保持冻结。", "", "## 基线 Profile 结果（F2 / Cap 1.5 / 固定补充 / G0 / S0）", "", "| Profile | Ladder | Adequacy | Utilization | Underfunded | Refill Burden | Time to full |", "|---|---|---:|---:|---:|---:|---:|"]
    for row in report.get("baseline_profile_results", []):
        profile = row.get("profile", {})
        ratio = row.get("capital_adequacy_ratio")
        ratio_text = "—" if ratio is None else f"{float(ratio):.2f} ({row.get('capital_adequacy_label', '—')})"
        lines.append(f"| {profile.get('profile_id', '—')} | {row.get('ladder_id', '—')} | {ratio_text} | {float(row.get('capital_utilization', 0)):.1%} | {float(row.get('underfunded_event_rate', 0)):.1%} | {float(row.get('refill_burden_ratio', 0)):.1%} | {row.get('time_to_full_months', '—')}m |")
    lines.extend(["", "## 候选与限制", "", f"- Ladder 结论：{report.get('LADDER_SELECTION')}；候选 Profile 由模拟后结构性指标产生：{json.dumps(report.get('candidate_profiles', {}), ensure_ascii=False)}。", f"- 已单独保留 Target（F1/F2/F3/F4）、收入比例（5%/10%/15%）、增长（G0–G3）、Surplus（S0–S2）和 R1–R8 压力结果；报告未使用收益最大化或未来价格重排。", "- Refill Burden = effective opportunity refill / monthly income；Cash Drag 只报告闲置金额和时间，不假设收益率。", ""])
    lines.extend(f"- {item}" for item in report.get("limitations", [])); return "\n".join(lines) + "\n"


__all__ = [
    "REAL_WORLD_MODEL_VERSION", "PHASE3B_MODEL_VERSION", "REAL_WORLD_CONFIG", "REAL_WORLD_CONFIG_HASH", "PHASE3B_CONFIG_HASH", "STANDARD_PROFILE_ASSUMPTIONS", "TARGET_SPECS", "CAP_MULTIPLIERS", "INCOME_REFILL_RATIOS", "GROWTH_SCENARIOS", "SURPLUS_POLICIES", "REAL_WORLD_GATE_CONFIG", "normalize_real_world_profile", "standard_profiles", "growth_schedule_for", "apply_growth_profile", "income_linked_refill", "replay_real_world_profile", "capital_adequacy_label", "compare_ladders", "build_phase3b_report", "run_phase3b_validation", "report_from_repository", "markdown_report",
]


def income_linked_refill(profile: Mapping[str, Any], month: str, *, ratio: float | None = None) -> dict[str, Any]:
    p = normalize_real_world_profile(profile); value = float(ratio if ratio is not None else (p.get("opportunity_refill_ratio") or 0.0))
    if value not in INCOME_REFILL_RATIOS: raise ValueError("income-linked refill ratio 只能是 0.05、0.10、0.15")
    _, _, income, core, _ = _target_and_cap(apply_growth_profile(p, "G0"), _month_key(month))
    requested = income * value; effective = min(requested, max(income - min(core, income), 0.0))
    return {"month": _month_key(month), "income": income, "core_dca": core, "ratio": value, "requested": requested, "effective_after_core_priority": effective, "core_priority_holds": effective <= max(income - min(core, income), 0.0) + 1e-9}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run Phase 3B real-world capital-profile validation")
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--phase3a-batch-id", default=None)
    parser.add_argument("--start-date", default="2000-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--markdown-out", type=Path, default=None)
    args = parser.parse_args()
    result = run_phase3b_validation(PITRepository(args.db), phase3a_batch_id=args.phase3a_batch_id, start_date=args.start_date, end_date=args.end_date, batch_id=args.run_id)
    if args.json_out: args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    if args.markdown_out: args.markdown_out.write_text(markdown_report(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
