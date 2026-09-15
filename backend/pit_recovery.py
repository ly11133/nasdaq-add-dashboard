"""Versioned historical Point-in-Time recovery rules.

The recovery audit is deliberately separate from the promotion operation.  A
source can be *capable* of a conservative vintage rule while every existing
row remains ``HISTORICAL_PROXY`` until the raw vintage response and its
evidence are actually archived.
"""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from typing import Any, Mapping

from data_contract import parse_datetime
from pit_repository import PITRepository


ROOT = Path(__file__).resolve().parent
RULE_PATH = ROOT / "pit_recovery_rules.json"
RECOVERY_RULE_SET_VERSION = "PIT_RECOVERY_RULES_V1"


def load_recovery_rules(path: Path | str = RULE_PATH) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("rule_set_version") != RECOVERY_RULE_SET_VERSION:
        raise ValueError("历史恢复规则集版本不匹配")
    series = payload.get("series")
    if not isinstance(series, list) or not series:
        raise ValueError("历史恢复规则缺少 series")
    required = {
        "series_id", "provider", "historical_source", "revision_behavior",
        "publication_semantics", "market_close_semantics", "vintage_support",
        "candidate_available_at_rule", "can_upgrade_historical", "classification",
        "confidence", "evidence", "rule_version",
    }
    for item in series:
        missing = required.difference(item)
        if missing:
            raise ValueError(f"{item.get('series_id', '?')} 恢复规则缺少：{', '.join(sorted(missing))}")
        if not isinstance(item["evidence"], list) or not item["evidence"]:
            raise ValueError(f"{item['series_id']} 缺少可审计 evidence")
    return payload


def audit_recovery_rules(db_path: Path | str, *, rule_path: Path | str = RULE_PATH) -> dict[str, Any]:
    """Persist the recovery assessment and report current applied upgrades."""

    payload = load_recovery_rules(rule_path)
    repo = PITRepository(db_path)
    audited = []
    for item in payload["series"]:
        audited.append(repo.record_recovery_audit({**item}))
    persisted = repo.get_recovery_audit()
    upgraded_by_series: dict[str, int] = {}
    with repo.connect() as conn:
        rows = conn.execute(
            """SELECT o.series_id, COUNT(*)
               FROM observations o JOIN observation_versions v ON v.observation_id=o.observation_id
               WHERE v.eligibility_origin IN ('PROVIDER_VINTAGE_VERIFIED','MANUAL_VERIFIED')
                 AND v.eligibility_rule_version IS NOT NULL
               GROUP BY o.series_id"""
        ).fetchall()
    upgraded_by_series = {str(row[0]): int(row[1]) for row in rows}
    classifications = {item["series_id"]: item["classification"] for item in payload["series"]}
    return {
        "rule_set_version": payload["rule_set_version"],
        "audited_series": audited,
        "persisted_audit_rows": len(persisted),
        "classification_counts": {
            classification: sum(value == classification for value in classifications.values())
            for classification in sorted(set(classifications.values()))
        },
        "upgraded_observation_counts": upgraded_by_series,
        "historical_upgrade_applied": sum(upgraded_by_series.values()),
        "historical_upgrade_note": "能力审计不等于自动升级；只有显式 vintage raw 与规则证据才能插入新资格版本。",
    }


def conservative_available_at(*, series_id: str, vintage_date: Any, delay_days: int = 1) -> str:
    """Compute the only automatic recovery timestamp currently permitted.

    This is intentionally limited to the documented NFCI conservative rule.
    It never invents a same-day release time.
    """

    if str(series_id) != "US_NFCI":
        raise ValueError("当前只有 US_NFCI 定义了保守 vintage 延迟规则")
    if int(delay_days) < 1:
        raise ValueError("NFCI 保守延迟至少为1天")
    base = parse_datetime(vintage_date, field="vintage_date")
    return (base + timedelta(days=int(delay_days))).replace(hour=0, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")


def promote_verified_historical_observation(
    db_path: Path | str,
    record: Mapping[str, Any],
    *,
    rule_version: str,
    evidence: Mapping[str, Any] | list[Any],
    vintage_date: Any | None = None,
) -> tuple[str, bool]:
    """Insert one explicitly evidenced historical observation version.

    The function is intentionally strict: it requires a persisted rule whose
    classification allows promotion, a non-empty evidence object, and a raw
    fetch already stored in the repository.  It never changes an old proxy
    row; a new observation version is appended instead.
    """

    if not evidence:
        raise ValueError("历史升级必须提供 evidence")
    repo = PITRepository(db_path)
    rules = [row for row in repo.get_recovery_audit(str(record["series_id"])) if row["rule_version"] == rule_version]
    if not rules:
        raise ValueError("未找到已持久化的 eligibility rule version")
    rule = rules[-1]
    if not rule["can_upgrade_historical"] or rule["classification"] not in {
        "STRICT_HISTORICAL_ELIGIBLE", "ELIGIBLE_WITH_CONSERVATIVE_DELAY",
    }:
        raise ValueError("当前规则不允许历史升级")
    normalized = dict(record)
    normalized["eligibility_origin"] = "PROVIDER_VINTAGE_VERIFIED"
    normalized["score_eligible"] = True
    normalized["eligibility_rule_version"] = rule_version
    normalized["eligibility_evidence"] = evidence
    if rule["classification"] == "ELIGIBLE_WITH_CONSERVATIVE_DELAY":
        if vintage_date is None:
            raise ValueError("保守延迟规则必须提供 vintage_date")
        normalized["available_at"] = conservative_available_at(series_id=str(record["series_id"]), vintage_date=vintage_date)
        normalized["vintage"] = str(vintage_date)[:10]
    if normalized.get("available_at") is None:
        raise ValueError("历史升级必须有可验证 available_at")
    # validate_contract_record inside record_observation_version checks that
    # available_at is no later than retrieved_at and that all hashes match.
    version = repo.record_observation_version(normalized, raw_fetch_id=str(record["raw_fetch_id"]), return_created=True)
    return version


__all__ = [
    "RECOVERY_RULE_SET_VERSION",
    "audit_recovery_rules",
    "conservative_available_at",
    "load_recovery_rules",
    "promote_verified_historical_observation",
]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Phase 1C historical PIT recovery audit")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "dashboard.sqlite3")
    args = parser.parse_args()
    print(json.dumps(audit_recovery_rules(args.db), ensure_ascii=False, indent=2))
