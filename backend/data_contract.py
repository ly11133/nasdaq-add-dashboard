"""Point-in-Time data contract used by the Phase 1A repository.

The contract deliberately keeps observation time, publication time, the time at
which this application could use a value, and our retrieval time separate. A
missing or unproven ``available_at`` never qualifies an observation for formal
historical scoring.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import hashlib
import json
import math
from typing import Any, Mapping


CONTRACT_VERSION = "PIT_DATA_CONTRACT_V1"
QUALIFICATION_CLASSES = {
    "PIT_ELIGIBLE",
    "PIT_PROXY",
    "CANDIDATE_ONLY",
    "UNAVAILABLE",
}

# Eligibility describes how this particular observation became usable.  It is
# deliberately separate from ``quality_status`` (the provider/data quality
# label) and from the series-level qualification class.  A single raw fetch
# may therefore contain both live-qualified observations and historical proxy
# observations.
ELIGIBILITY_ORIGINS = {
    "HISTORICAL_PROXY",
    "OBSERVED_LIVE",
    "PROVIDER_VINTAGE_VERIFIED",
    "MANUAL_VERIFIED",
    "CANDIDATE",
}

REQUIRED_FIELDS = (
    "series_id",
    "observation_date",
    "publication_at",
    "available_at",
    "retrieved_at",
    "source",
    "source_url",
    "source_version",
    "vintage",
    "value",
    "unit",
    "methodology",
    "quality_status",
    "score_eligible",
    "eligibility_origin",
    "raw_fetch_id",
    "raw_hash",
)


def canonical_json(value: Any) -> str:
    """Return stable JSON for hashes and reproducibility records."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_datetime(value: Any, *, field: str = "datetime", date_end: bool = False) -> datetime:
    """Parse an ISO date/datetime as UTC.

    Date-only ``as_of`` values represent the full UTC calendar day. Stored
    availability timestamps should normally be full timestamps; accepting a
    date keeps the import boundary compatible with the existing app while
    converting it deterministically.
    """

    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time.max if date_end else time.min)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} 不能为空")
        if len(text) == 10:
            try:
                parsed = date.fromisoformat(text)
            except ValueError as exc:
                raise ValueError(f"{field} 不是有效 ISO 日期") from exc
            result = datetime.combine(parsed, time.max if date_end else time.min)
        else:
            normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
            try:
                result = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise ValueError(f"{field} 不是有效 ISO 时间") from exc
    else:
        raise ValueError(f"{field} 必须是 ISO 日期或时间")
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def iso_utc(value: Any, *, field: str = "datetime", date_end: bool = False) -> str:
    return parse_datetime(value, field=field, date_end=date_end).isoformat().replace("+00:00", "Z")


def normalize_date(value: Any, *, field: str = "observation_date") -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()[:10]
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise ValueError(f"{field} 不是有效 YYYY-MM-DD 日期") from exc
    raise ValueError(f"{field} 必须是日期")


def validate_contract_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize one observation version.

    ``available_at`` may be null only for a non-eligible record. This is the
    central guard against promoting an observation merely because its economic
    date is old enough.
    """

    missing = [key for key in REQUIRED_FIELDS if key not in record]
    if missing:
        raise ValueError("PIT record 缺少字段：" + ", ".join(missing))
    result = dict(record)
    result["series_id"] = str(result["series_id"])
    result["observation_date"] = normalize_date(result["observation_date"])
    result["retrieved_at"] = iso_utc(result["retrieved_at"], field="retrieved_at")
    result["publication_at"] = None if result["publication_at"] in (None, "") else iso_utc(result["publication_at"], field="publication_at")
    result["available_at"] = None if result["available_at"] in (None, "") else iso_utc(result["available_at"], field="available_at")
    result["source"] = str(result["source"])
    result["source_url"] = str(result["source_url"] or "")
    result["source_version"] = None if result["source_version"] in (None, "") else str(result["source_version"])
    result["vintage"] = None if result["vintage"] in (None, "") else str(result["vintage"])
    result["unit"] = None if result["unit"] in (None, "") else str(result["unit"])
    result["methodology"] = str(result["methodology"])
    result["quality_status"] = str(result["quality_status"])
    metadata = result.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata 必须是对象")
    result["metadata"] = dict(metadata)
    result["eligibility_origin"] = str(result["eligibility_origin"]).upper()
    if result["eligibility_origin"] not in ELIGIBILITY_ORIGINS:
        raise ValueError("eligibility_origin 无效")
    result["raw_fetch_id"] = str(result["raw_fetch_id"])
    result["raw_hash"] = str(result["raw_hash"]).lower()
    if len(result["raw_hash"]) != 64 or any(c not in "0123456789abcdef" for c in result["raw_hash"]):
        raise ValueError("raw_hash 必须是64位 SHA256")
    if result["available_at"] is None and bool(result["score_eligible"]):
        raise ValueError("无法证明 available_at 时不得 score_eligible=true")
    if not isinstance(result["score_eligible"], bool):
        raise ValueError("score_eligible 必须是布尔值")
    result["score_eligible"] = result["score_eligible"]
    if result["eligibility_origin"] in {"OBSERVED_LIVE", "PROVIDER_VINTAGE_VERIFIED", "MANUAL_VERIFIED"} and result["available_at"] is None:
        raise ValueError("已核验观察必须提供 available_at")
    if result["score_eligible"] and result["eligibility_origin"] in {"HISTORICAL_PROXY", "CANDIDATE"}:
        raise ValueError("历史代理或候选观察不得 score_eligible=true")
    if result["eligibility_origin"] in {"HISTORICAL_PROXY", "CANDIDATE"} and result["score_eligible"]:
        raise ValueError("eligibility_origin 与 score_eligible 冲突")
    if not result["score_eligible"] and result["available_at"] is None:
        pass
    if result["available_at"] and result["retrieved_at"]:
        if parse_datetime(result["available_at"]) > parse_datetime(result["retrieved_at"]):
            raise ValueError("available_at 不能晚于 retrieved_at")
    if result["publication_at"] and result["available_at"]:
        if parse_datetime(result["publication_at"]) > parse_datetime(result["available_at"]):
            raise ValueError("publication_at 不能晚于 available_at")
    value = result["value"]
    if isinstance(value, bool):
        raise ValueError("value 不能是布尔值")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValueError("value 必须是有限数值")
    return result


def as_of_datetime(value: Any) -> str:
    """Normalize a query cutoff; date-only values include the entire day."""

    return iso_utc(value, field="as_of", date_end=True)
