"""Append-only Point-in-Time storage and query boundary for Phase 1A.

The repository is intentionally small. It owns the SQL boundary used by
future formal scoring, while the existing JSON snapshot tables remain as a
backward-compatible presentation cache. A query with an ``as_of`` cutoff can
only see an observation version whose proven ``available_at`` is no later than
that cutoff and whose qualification flag is true.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import shutil
import sqlite3
from functools import lru_cache
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urlparse

from data_contract import (
    CONTRACT_VERSION,
    ELIGIBILITY_ORIGINS,
    as_of_datetime,
    canonical_json,
    normalize_date,
    parse_datetime,
    sha256_json,
    validate_contract_record,
)


ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "migrations" / "001_pit_base.sql"
MIGRATION_PATHS = (
    ROOT / "migrations" / "001_pit_base.sql",
    ROOT / "migrations" / "002_qualification_alias.sql",
    ROOT / "migrations" / "003_score_models_append_only.sql",
    ROOT / "migrations" / "004_strict_pit.sql",
    ROOT / "migrations" / "005_observation_metadata.sql",
    ROOT / "migrations" / "006_phase1c.sql",
    ROOT / "migrations" / "007_phase2a.sql",
    ROOT / "migrations" / "008_phase2b.sql",
    ROOT / "migrations" / "009_phase2c.sql",
    ROOT / "migrations" / "010_phase2d.sql",
    ROOT / "migrations" / "011_phase2d_timing_outcomes.sql",
    ROOT / "migrations" / "012_phase2e_tactical_drawdown.sql",
    ROOT / "migrations" / "013_phase2f_capital_feasibility.sql",
    ROOT / "migrations" / "014_phase3a_capital_state_machine.sql",
    ROOT / "migrations" / "015_phase3b_real_world_parameterization.sql",
)
QUALIFICATION_PATH = ROOT / "pit_qualification.json"
SCORE_MODEL_PATH = ROOT / "score_model.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _backup_path(db_path: Path) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return backup_dir / f"{db_path.name}.{stamp}.bak"


def backup_database(db_path: Path) -> Path | None:
    """Create a SQLite-consistent backup before the first PIT migration."""

    db_path = Path(db_path)
    if not db_path.exists() or db_path.stat().st_size == 0:
        return None
    target = _backup_path(db_path)
    source = sqlite3.connect(str(db_path), timeout=30)
    destination = sqlite3.connect(str(target), timeout=30)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    return target


def migrate_database(db_path: Path, *, backup: bool = True) -> dict[str, Any]:
    """Apply the numbered schema without touching legacy snapshot data.

    The migration is idempotent. A backup is made once, immediately before
    version 1 is applied to an existing database. No ``UPDATE`` or ``DELETE``
    is used for append-only PIT tables.
    """

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    migration_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
    version = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0] if migration_table else 0
    created_backup = None
    try:
        pending = [(index + 1, path) for index, path in enumerate(MIGRATION_PATHS) if index + 1 > version]
        if pending and backup:
            created_backup = backup_database(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        for migration_version, path in pending:
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (migration_version, utc_now()))
            version = migration_version
        conn.commit()
    finally:
        conn.close()
    return {"schema_version": version, "backup_path": str(created_backup) if created_backup else None}


@lru_cache(maxsize=1)
def _qualification_index() -> dict[str, dict[str, Any]]:
    payload = _read_json(QUALIFICATION_PATH)
    return {item["series_id"]: item for item in payload.get("series", [])}


def _default_series(series_id: str, *, asset_id: str = "UNKNOWN", metric: str = "unknown", unit: str | None = None) -> dict[str, Any]:
    item = _qualification_index().get(series_id, {})
    return {
        "series_id": series_id,
        "display_name": item.get("label", series_id),
        "asset_id": asset_id,
        "metric": item.get("metric", metric),
        "unit": item.get("unit", unit),
        "methodology": item.get("reason", "No formal PIT qualification has been established."),
        "qualification_class": item.get("classification", "UNAVAILABLE"),
        "score_eligible_default": int(bool(item.get("score_eligible", False))),
        "live_observation_allowed": bool(item.get("live_observation_allowed", False)),
    }


def _live_observation_allowed(series_id: str) -> bool:
    """Return the frozen per-series live-capture policy.

    This policy is intentionally read from the versioned qualification file,
    rather than inferred from a successful HTTP response.  A current value
    can be observed live while its older bulk history remains a proxy.
    """

    return bool(_qualification_index().get(series_id, {}).get("live_observation_allowed", False))


class PITRepository:
    """Repository that is the only supported SQL reader for formal PIT data."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        migrate_database(self.db_path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def register_series(self, spec: Mapping[str, Any]) -> str:
        required = {"series_id", "display_name", "asset_id", "metric", "methodology", "qualification_class", "score_eligible_default"}
        missing = required.difference(spec)
        if missing:
            raise ValueError("data_series 缺少字段：" + ", ".join(sorted(missing)))
        if spec["qualification_class"] not in {"PIT_ELIGIBLE", "PIT_PROXY", "CANDIDATE_ONLY", "UNAVAILABLE"}:
            raise ValueError("无效 qualification_class")
        created = str(spec.get("created_at") or utc_now())
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO data_series(series_id, display_name, asset_id, metric, unit,
                   methodology, qualification_class, score_eligible_default, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(series_id) DO NOTHING""",
                (
                    str(spec["series_id"]),
                    str(spec["display_name"]),
                    str(spec["asset_id"]),
                    str(spec["metric"]),
                    spec.get("unit"),
                    str(spec["methodology"]),
                    str(spec["qualification_class"]),
                    int(bool(spec["score_eligible_default"])),
                    created,
                ),
            )
        return str(spec["series_id"])

    def ensure_series(self, series_id: str, *, asset_id: str = "UNKNOWN", metric: str = "unknown", unit: str | None = None, spec: Mapping[str, Any] | None = None) -> str:
        value = dict(_default_series(series_id, asset_id=asset_id, metric=metric, unit=unit))
        if spec:
            value.update(spec)
        return self.register_series(value)

    def record_raw_fetch(self, meta: Mapping[str, Any], *, raw_path: Path | str, raw_hash: str | None = None) -> str:
        """Append one raw response; identical path/hash writes are idempotent."""

        path = Path(raw_path)
        if not path.exists() or not path.is_file():
            raise ValueError(f"raw 文件不存在：{path}")
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest().lower()
        digest = (raw_hash or actual_digest).lower()
        if digest != actual_digest:
            raise ValueError("raw_hash 与文件内容不一致")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("raw_hash 必须是64位 SHA256")
        source = str(meta.get("source") or "unknown")
        retrieved = meta.get("retrieved_at_utc") or meta.get("retrieved_at")
        if retrieved is None:
            raise ValueError("raw_fetch 缺少 retrieved_at")
        retrieved = iso_timestamp(retrieved, "retrieved_at")
        source_url = str(meta.get("url") or meta.get("source_url") or "")
        raw_fetch_id = str(meta.get("raw_fetch_id") or hashlib.sha256(f"{path.parent.name}|{source}|{digest}".encode()).hexdigest()[:32])
        source_version = meta.get("source_version") or "collector_response"
        vintage = meta.get("vintage")
        if vintage is None:
            vintage = _vintage_from_url(source_url)
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO raw_fetches
                   (raw_fetch_id, source, source_url, source_version, vintage,
                    retrieved_at, http_status, raw_hash, raw_path, byte_count, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    raw_fetch_id,
                    source,
                    source_url,
                    None if source_version in (None, "") else str(source_version),
                    None if vintage in (None, "") else str(vintage),
                    retrieved,
                    meta.get("http_status"),
                    digest,
                    str(path.resolve()),
                    path.stat().st_size,
                    utc_now(),
                ),
            )
        return raw_fetch_id

    def record_observation_version(
        self,
        record: Mapping[str, Any],
        *,
        raw_fetch_id: str,
        series_spec: Mapping[str, Any] | None = None,
        return_created: bool = False,
    ) -> str | tuple[str, bool]:
        """Append a version; never updates a prior value."""

        normalized = validate_contract_record(record)
        series_id = normalized["series_id"]
        self.ensure_series(series_id, spec=series_spec)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version_id, created = self._insert_observation_version(
                conn,
                normalized,
                raw_fetch_id=raw_fetch_id,
                series_spec=series_spec,
            )
        return (version_id, created) if return_created else version_id

    def _insert_observation_version(
        self,
        conn: sqlite3.Connection,
        normalized: Mapping[str, Any],
        *,
        raw_fetch_id: str,
        series_spec: Mapping[str, Any] | None = None,
        dedupe_unchanged_source: bool = False,
    ) -> tuple[str, bool]:
        """Insert one already-normalized version using an existing transaction.

        ``archive_collection`` uses this seam to validate every observation
        while committing the whole collection in one SQLite transaction. The
        public single-record method keeps its original behavior; this helper
        does not update or delete append-only rows.
        """

        series_id = str(normalized["series_id"])
        observation_id = hashlib.sha256(f"{series_id}|{normalized['observation_date']}".encode()).hexdigest()[:32]
        value = normalized["value"]
        value_real = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        value_text = value if isinstance(value, str) else None
        value_json = None if value_real is not None or value_text is not None else canonical_json(value)
        series_row = conn.execute(
            "SELECT qualification_class, score_eligible_default FROM data_series WHERE series_id=?",
            (series_id,),
        ).fetchone()
        if not series_row:
            raise ValueError(f"data_series 不存在：{series_id}")
        if normalized["score_eligible"]:
            origin = normalized["eligibility_origin"]
            if origin in {"HISTORICAL_PROXY", "CANDIDATE"}:
                raise ValueError(f"{series_id} 的 {origin} 观察不得进入正式评分")
            live_allowed = _live_observation_allowed(series_id) or bool((series_spec or {}).get("live_observation_allowed", False))
            if origin == "OBSERVED_LIVE" and not live_allowed and (series_row[0] != "PIT_ELIGIBLE" or not series_row[1]):
                raise ValueError(f"{series_id} 尚未声明允许实时观察评分")
            # Provider/manual verification is an observation-level proof; it
            # may promote one version even when the series default remains a
            # proxy for ordinary public-history captures.
        raw_row = conn.execute("SELECT raw_hash FROM raw_fetches WHERE raw_fetch_id=?", (raw_fetch_id,)).fetchone()
        if not raw_row:
            raise ValueError(f"raw_fetch 不存在：{raw_fetch_id}")
        if raw_row[0] != normalized["raw_hash"]:
            raise ValueError("observation_version 的 raw_hash 与 raw_fetch 不一致")
        if dedupe_unchanged_source:
            # A daily refresh normally returns the same long historical
            # series with a new raw-fetch path.  Preserve the raw response,
            # but do not manufacture another observation version when the
            # same provider/source already has the same economic value and
            # eligibility state.  A changed value, publication metadata,
            # quality label, or live/proxy state still creates a new version.
            unchanged = conn.execute(
                """SELECT v.observation_version_id, v.value_real,
                          v.value_text, v.value_json, v.publication_at,
                          v.quality_status, v.methodology, v.score_eligible,
                          v.eligibility_origin
                   FROM observation_versions v
                   JOIN raw_fetches f ON f.raw_fetch_id=v.raw_fetch_id
                   WHERE v.observation_id=? AND f.source=?
                   ORDER BY v.version_no DESC
                   LIMIT 1""",
                (observation_id, normalized["source"]),
            ).fetchone()
            if unchanged:
                same_value = (
                    unchanged[1] == value_real
                    and unchanged[2] == value_text
                    and unchanged[3] == value_json
                )
                if (
                    same_value
                    and unchanged[4] == normalized["publication_at"]
                    and unchanged[5] == normalized["quality_status"]
                    and unchanged[6] == normalized["methodology"]
                    and bool(unchanged[7]) == bool(normalized["score_eligible"])
                    and unchanged[8] == normalized["eligibility_origin"]
                ):
                    return str(unchanged[0]), False
        conn.execute(
            """INSERT OR IGNORE INTO observations
               (observation_id, series_id, observation_date, unit, created_at)
               VALUES(?,?,?,?,?)""",
            (observation_id, series_id, normalized["observation_date"], normalized.get("unit"), utc_now()),
        )
        existing = conn.execute(
            "SELECT observation_version_id FROM observation_versions WHERE observation_id=? AND raw_fetch_id=?",
            (observation_id, raw_fetch_id),
        ).fetchone()
        if existing:
            return str(existing[0]), False
        version_no = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM observation_versions WHERE observation_id=?",
            (observation_id,),
        ).fetchone()[0]
        version_id = hashlib.sha256(f"{observation_id}|{raw_fetch_id}".encode()).hexdigest()[:32]
        conn.execute(
            """INSERT INTO observation_versions
               (observation_version_id, observation_id, version_no,
                publication_at, available_at, retrieved_at, source_version,
                vintage, value_real, value_text, value_json, methodology,
                quality_status, score_eligible, eligibility_origin,
                raw_fetch_id, raw_hash, inserted_at, metadata_json,
                eligibility_rule_version, eligibility_evidence_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                version_id,
                observation_id,
                version_no,
                normalized["publication_at"],
                normalized["available_at"],
                normalized["retrieved_at"],
                normalized["source_version"],
                normalized["vintage"],
                value_real,
                value_text,
                value_json,
                normalized["methodology"],
                normalized["quality_status"],
                int(normalized["score_eligible"]),
                normalized["eligibility_origin"],
                raw_fetch_id,
                normalized["raw_hash"],
                utc_now(),
                canonical_json(normalized["metadata"]),
                str(normalized.get("eligibility_rule_version") or (
                    "OBSERVED_LIVE_CAPTURE_V1" if normalized["eligibility_origin"] == "OBSERVED_LIVE"
                    else "UNVERSIONED_LEGACY_RECORD"
                )),
                canonical_json(normalized.get("eligibility_evidence") or {
                    "status": "not_supplied",
                    "warning": "legacy observation did not carry an explicit Phase 1C evidence object",
                }),
            ),
        )
        return version_id, True

    def get_latest_available(self, series_id: str, as_of: Any) -> dict[str, Any] | None:
        cutoff = as_of_datetime(as_of)
        with self.connect() as conn:
            row = conn.execute(
                """SELECT o.series_id, o.observation_date, v.*, s.display_name,
                          s.unit AS series_unit, s.qualification_class
                   FROM observations o
                   JOIN observation_versions v ON v.observation_id=o.observation_id
                   JOIN data_series s ON s.series_id=o.series_id
                   WHERE o.series_id=? AND o.observation_date<=date(?) AND v.score_eligible=1
                     AND v.eligibility_origin NOT IN ('HISTORICAL_PROXY', 'CANDIDATE')
                     AND v.available_at IS NOT NULL AND v.available_at<=?
                   ORDER BY o.observation_date DESC, v.available_at DESC, v.version_no DESC
                   LIMIT 1""",
                (series_id, cutoff, cutoff),
            ).fetchone()
        return _row_to_record(row) if row else None

    def get_history_available(self, series_id: str, as_of: Any, lookback: int | None = None) -> list[dict[str, Any]]:
        cutoff = as_of_datetime(as_of)
        query_params: list[Any] = [series_id, cutoff, cutoff]
        clause = ""
        if lookback is not None:
            if int(lookback) < 0:
                raise ValueError("lookback 不能为负数")
            cutoff_date = normalize_date(as_of)
            clause = " AND o.observation_date>=date(?, ?)"
            query_params.extend([cutoff_date, f"-{int(lookback)} days"])
        with self.connect() as conn:
            rows = conn.execute(
                f"""WITH eligible AS (
                        SELECT o.series_id, o.observation_date, v.*, s.display_name,
                               s.unit AS series_unit, s.qualification_class,
                               ROW_NUMBER() OVER (
                                   PARTITION BY v.observation_id
                                   ORDER BY v.available_at DESC, v.version_no DESC
                               ) AS pit_rank
                        FROM observations o
                        JOIN observation_versions v ON v.observation_id=o.observation_id
                        JOIN data_series s ON s.series_id=o.series_id
                        WHERE o.series_id=? AND o.observation_date<=date(?) AND v.score_eligible=1
                          AND v.eligibility_origin NOT IN ('HISTORICAL_PROXY', 'CANDIDATE')
                          AND v.available_at IS NOT NULL AND v.available_at<=?{clause}
                    )
                    SELECT * FROM eligible WHERE pit_rank=1
                    ORDER BY observation_date ASC, available_at ASC, version_no ASC""",
                query_params,
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get_observation_versions(self, series_id: str | None = None, observation_date: Any | None = None, as_of: Any | None = None, *, include_ineligible: bool = True) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if series_id is not None:
            clauses.append("o.series_id=?"); params.append(series_id)
        if observation_date is not None:
            clauses.append("o.observation_date=?"); params.append(normalize_date(observation_date))
        if as_of is not None:
            clauses.append("o.observation_date<=date(?) AND v.available_at IS NOT NULL AND v.available_at<=?"); params.extend([as_of_datetime(as_of), as_of_datetime(as_of)])
        if not include_ineligible:
            clauses.extend(["v.score_eligible=1", "v.eligibility_origin NOT IN ('HISTORICAL_PROXY', 'CANDIDATE')"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT o.series_id, o.observation_date, v.*, s.display_name,
                           s.unit AS series_unit, s.qualification_class
                    FROM observations o
                    JOIN observation_versions v ON v.observation_id=o.observation_id
                    JOIN data_series s ON s.series_id=o.series_id{where}
                    ORDER BY o.series_id, o.observation_date, v.version_no""",
                params,
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get_raw_provenance(self, raw_fetch_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM raw_fetches WHERE raw_fetch_id=?", (raw_fetch_id,)).fetchone()
        return dict(row) if row else None

    def append_decision(
        self,
        *,
        as_of: Any,
        market: str,
        mode: str,
        score_model_version: str,
        config_hash: str,
        input_version_ids: Iterable[str],
        coverage: float,
        score: float | None,
        gate_status: str,
        decision: str,
        reason: Mapping[str, Any] | None = None,
        missing_items: Iterable[Any] = (),
        data_cutoff: Any | None = None,
    ) -> dict[str, Any]:
        """Append one deterministic decision record.

        The input hash is calculated from a sorted, de-duplicated list of
        observation version ids.  The decision log is an audit ledger: it has
        no update/delete path and every strict evaluation, including an
        insufficient-evidence result, produces one row.
        """

        mode = str(mode).upper()
        if mode not in {"LEGACY", "RESEARCH_PROXY", "STRICT_PIT"}:
            raise ValueError("无效评估模式")
        coverage = float(coverage)
        if not 0 <= coverage <= 100:
            raise ValueError("coverage 必须在 0–100")
        model_version = str(score_model_version)
        config_hash = str(config_hash).lower()
        if len(config_hash) != 64:
            raise ValueError("config_hash 必须是64位 SHA256")
        ids = sorted({str(value) for value in input_version_ids})
        input_hash = sha256_json(ids)
        asof = as_of_datetime(as_of)
        cutoff = as_of_datetime(data_cutoff if data_cutoff is not None else as_of)
        reason_payload = dict(reason or {})
        missing_payload = list(missing_items)
        with self.connect() as conn:
            # Serialize the read-of-previous-hash and the following insert so
            # concurrent strict evaluations cannot create two chain branches.
            conn.execute("BEGIN IMMEDIATE")
            model_row = conn.execute("SELECT config_hash FROM score_models WHERE model_version=?", (model_version,)).fetchone()
            if not model_row:
                raise ValueError(f"未知 score_model_version：{model_version}")
            if str(model_row[0]) != config_hash:
                raise ValueError("decision 使用了错误的 score model config_hash")
            previous_row = conn.execute(
                "SELECT decision_hash FROM decision_log WHERE market=? ORDER BY created_at DESC, decision_id DESC LIMIT 1",
                (str(market),),
            ).fetchone()
            previous = str(previous_row[0]) if previous_row else None
            created = utc_now()
            material = {
                "as_of_datetime": asof,
                "market": str(market),
                "mode": mode,
                "score_model_version": model_version,
                "config_hash": config_hash,
                "data_cutoff": cutoff,
                "input_hash": input_hash,
                "coverage": coverage,
                "score": None if score is None else float(score),
                "gate_status": str(gate_status),
                "decision": str(decision),
                "reason_json": reason_payload,
                "missing_items_json": missing_payload,
                "created_at": created,
                "previous_decision_hash": previous,
            }
            decision_hash = sha256_json(material)
            decision_id = sha256_json({"decision_hash": decision_hash, "created_at": created})[:32]
            conn.execute(
                """INSERT INTO decision_log
                   (decision_id, as_of_datetime, market, mode, score_model_version,
                    config_hash, data_cutoff, input_hash, coverage, score,
                    gate_status, decision, reason_json, missing_items_json,
                    created_at, previous_decision_hash, decision_hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    asof,
                    str(market),
                    mode,
                    model_version,
                    config_hash,
                    cutoff,
                    input_hash,
                    coverage,
                    None if score is None else float(score),
                    str(gate_status),
                    str(decision),
                    canonical_json(reason_payload),
                    canonical_json(missing_payload),
                    created,
                    previous,
                    decision_hash,
                ),
            )
        return {
            **material,
            "decision_id": decision_id,
            "input_hash": input_hash,
            "decision_hash": decision_hash,
            "input_version_ids": ids,
        }

    def get_decision_log(self, market: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        if int(limit) <= 0:
            return []
        clauses = " WHERE market=?" if market is not None else ""
        params: list[Any] = [str(market)] if market is not None else []
        params.append(min(int(limit), 1000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM decision_log{clauses} ORDER BY created_at DESC, decision_id DESC LIMIT ?",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("reason_json", "missing_items_json"):
                try:
                    item[key] = json.loads(item[key])
                except (TypeError, json.JSONDecodeError):
                    pass
            result.append(item)
        return result

    def verify_decision_chain(self, market: str | None = None) -> dict[str, Any]:
        """Recompute decision hashes and previous links without mutation."""

        clauses = " WHERE market=?" if market is not None else ""
        params: list[Any] = [str(market)] if market is not None else []
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM decision_log{clauses} ORDER BY market, created_at, decision_id",
                params,
            ).fetchall()
        previous_by_market: dict[str, str | None] = {}
        errors: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                reason_payload = json.loads(item["reason_json"])
                missing_payload = json.loads(item["missing_items_json"])
            except (TypeError, json.JSONDecodeError):
                errors.append({"decision_id": item.get("decision_id"), "error": "invalid decision JSON"})
                continue
            expected_previous = previous_by_market.get(str(item["market"]))
            material = {
                "as_of_datetime": item["as_of_datetime"],
                "market": item["market"],
                "mode": item["mode"],
                "score_model_version": item["score_model_version"],
                "config_hash": item["config_hash"],
                "data_cutoff": item["data_cutoff"],
                "input_hash": item["input_hash"],
                "coverage": float(item["coverage"]),
                "score": None if item["score"] is None else float(item["score"]),
                "gate_status": item["gate_status"],
                "decision": item["decision"],
                "reason_json": reason_payload,
                "missing_items_json": missing_payload,
                "created_at": item["created_at"],
                "previous_decision_hash": item["previous_decision_hash"],
            }
            recomputed = sha256_json(material)
            if recomputed != item["decision_hash"]:
                errors.append({"decision_id": item["decision_id"], "error": "decision_hash mismatch"})
            if item["previous_decision_hash"] != expected_previous:
                errors.append({"decision_id": item["decision_id"], "error": "previous_decision_hash mismatch"})
            previous_by_market[str(item["market"])] = str(item["decision_hash"])
        return {"valid": not errors, "checked": len(rows), "errors": errors}

    def archive_collection(self, *, run_dir: Path | str, manifest: Iterable[Mapping[str, Any]], records: Iterable[Mapping[str, Any]], profile_id: str, audit: Mapping[str, Any] | None = None, as_of: Any | None = None, progress_callback=None) -> dict[str, Any]:
        """Archive every successful network response and parsed observation.

        The collector only knows that this application saw a value at
        ``retrieved_at``. It therefore stores that timestamp as the earliest
        *application availability* and leaves source publication time unknown
        unless a full timestamp is explicitly supplied.  Only the latest
        observation in a genuinely current run may be marked OBSERVED_LIVE;
        older rows in the same response stay HISTORICAL_PROXY.
        """

        run_dir = Path(run_dir)
        manifest = list(manifest)
        records = list(records)
        asof_date = normalize_date(as_of or (audit or {}).get("as_of") or max((r.get("observation_date") for r in records if r.get("observation_date")), default=utc_now()[:10]), field="as_of")
        fetch_ids: dict[str, str] = {}
        for meta in manifest:
            source = str(meta.get("source") or "")
            raw_file = run_dir / f"{source}.raw"
            if not source or not raw_file.exists() or not raw_file.is_file():
                continue
            fetch_meta = dict(meta)
            fetch_meta["raw_fetch_id"] = hashlib.sha256(f"{run_dir.name}|{source}|{meta.get('sha256','')}".encode()).hexdigest()[:32]
            fetch_ids[source] = self.record_raw_fetch(fetch_meta, raw_path=raw_file, raw_hash=meta.get("sha256"))
        inserted = 0
        duplicates = 0
        skipped = 0
        latest_by_series: dict[str, str] = {}
        for record in records:
            source = str(record.get("source") or "")
            metric = str(record.get("metric") or "")
            if source not in fetch_ids or not record.get("observation_date"):
                continue
            sid = series_id_for_record(profile_id, source, metric)
            observed = normalize_date(record.get("observation_date"), field="observation_date")
            latest_by_series[sid] = max(latest_by_series.get(sid, "0001-01-01"), observed)
        # A historical as-of replay performed today must not be promoted to
        # live merely because the collector retrieved it now.
        retrieved_dates = []
        for meta in manifest:
            if meta.get("source") in fetch_ids and meta.get("retrieved_at_utc"):
                retrieved_dates.append(normalize_date(meta["retrieved_at_utc"], field="retrieved_at"))
        live_run = bool(retrieved_dates) and asof_date >= max(retrieved_dates)
        # Build and validate the archive rows before opening the write
        # transaction.  The old implementation called the public
        # ``record_observation_version`` for every row.  That method is
        # intentionally safe for one-off writes, but it opens a connection,
        # ensures the series and commits once per observation.  A collection
        # can contain tens of thousands of rows, so use the same validation
        # and insert seam below with one connection/transaction instead.
        prepared: list[tuple[dict[str, Any], Mapping[str, Any], str]] = []
        series_specs: dict[str, Mapping[str, Any]] = {}
        for record in records:
            source = str(record.get("source") or "")
            raw_fetch_id = fetch_ids.get(source)
            if not raw_fetch_id:
                skipped += 1
                continue
            series_id = series_id_for_record(profile_id, source, str(record.get("metric") or ""))
            spec = _default_series(series_id, asset_id=profile_id, metric=str(record.get("metric") or ""), unit=record.get("unit"))
            observed = normalize_date(record.get("observation_date"), field="observation_date")
            retrieved = iso_timestamp(record.get("retrieved_at"), "retrieved_at")
            published = record.get("published_at")
            full_published = published if published and "T" in str(published) else None
            vintage = None if full_published else (str(published) if published not in (None, "") else None)
            latest = latest_by_series.get(series_id) == observed
            live_allowed = _live_observation_allowed(series_id)
            if live_run and latest and live_allowed:
                eligibility_origin = "OBSERVED_LIVE"
                score_eligible = True
                eligibility_rule_version = "OBSERVED_LIVE_CAPTURE_V1"
                eligibility_evidence = {
                    "rule": "available_at = application retrieval timestamp",
                    "scope": "current run latest observation only",
                    "source": source,
                    "retrieved_at": retrieved,
                    "historical_rows_in_same_response": "remain HISTORICAL_PROXY",
                }
            elif latest and spec.get("qualification_class") == "CANDIDATE_ONLY":
                eligibility_origin = "CANDIDATE"
                score_eligible = False
                eligibility_rule_version = "CANDIDATE_ONLY_CAPTURE_V1"
                eligibility_evidence = {
                    "rule": "candidate source has no verified point-in-time availability",
                    "source": source,
                    "available_at": None,
                }
            else:
                eligibility_origin = "HISTORICAL_PROXY"
                score_eligible = False
                eligibility_rule_version = "HISTORICAL_PROXY_CAPTURE_V1"
                eligibility_evidence = {
                    "rule": "bulk history retrieved now is not historical availability proof",
                    "source": source,
                    "retrieved_at": retrieved,
                    "available_at": None,
                }
            metadata = dict(record.get("metadata") or {}) if isinstance(record.get("metadata") or {}, Mapping) else {}
            metadata.setdefault("profile_id", str(profile_id).upper())
            metadata.setdefault("observation_date", observed)
            metadata.setdefault("retrieved_at", retrieved)
            if published not in (None, ""):
                metadata.setdefault("provider_date", str(published))
            archive_record = {
                "series_id": series_id,
                "observation_date": observed,
                "publication_at": full_published,
                # A bulk historical response proves only that the application
                # retrieved bytes now; it does not prove that the historical
                # value was available on its observation date.  Keep
                # available_at null for proxy/candidate rows.  Live rows use
                # the conservative retrieval timestamp below.
                "available_at": retrieved if eligibility_origin == "OBSERVED_LIVE" else None,
                "retrieved_at": retrieved,
                "source": source,
                "source_url": next((str(m.get("url") or "") for m in manifest if m.get("source") == source), ""),
                "source_version": str(record.get("parser_version") or "collector_parser_v1"),
                "vintage": vintage,
                "value": record.get("value"),
                "unit": record.get("unit") or spec.get("unit"),
                "methodology": str(record.get("basis") or spec.get("methodology") or ""),
                # Keep the provider/application quality label verbatim; it is
                # separate from the normalized eligibility origin.
                "quality_status": str(record.get("quality_status") or spec.get("qualification_class", "PIT_PROXY")),
                "score_eligible": score_eligible,
                "eligibility_origin": eligibility_origin,
                "eligibility_rule_version": eligibility_rule_version,
                "eligibility_evidence": eligibility_evidence,
                "metadata": metadata,
                "raw_fetch_id": raw_fetch_id,
                "raw_hash": next((str(m.get("sha256")) for m in manifest if m.get("source") == source), ""),
            }
            normalized = validate_contract_record(archive_record)
            prepared.append((normalized, spec, raw_fetch_id))
            series_specs.setdefault(series_id, spec)
        for series_id, spec in series_specs.items():
            self.ensure_series(series_id, spec=spec)
        if prepared:
            progress_step = max(1, len(prepared) // 100)
            if callable(progress_callback):
                try: progress_callback(0, len(prepared))
                except Exception: pass
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for row_index, (normalized, spec, raw_fetch_id) in enumerate(prepared, 1):
                    _, created = self._insert_observation_version(
                        conn,
                        normalized,
                        raw_fetch_id=raw_fetch_id,
                        series_spec=spec,
                        dedupe_unchanged_source=True,
                    )
                    if created:
                        inserted += 1
                    else:
                        duplicates += 1
                    if callable(progress_callback) and (row_index == len(prepared) or row_index % progress_step == 0):
                        try: progress_callback(row_index, len(prepared))
                        except Exception: pass
        return {
            "status": "archived",
            "contract_version": CONTRACT_VERSION,
            "run_directory": str(run_dir.resolve()),
            "raw_fetches": len(fetch_ids),
            "observations_archived": inserted,
            "observations_duplicate": duplicates,
            "observations_skipped": skipped,
            "score_eligible": sum(1 for r in records if latest_by_series.get(series_id_for_record(profile_id, str(r.get("source") or ""), str(r.get("metric") or ""))) == normalize_date(r.get("observation_date"), field="observation_date") and live_run and _live_observation_allowed(series_id_for_record(profile_id, str(r.get("source") or ""), str(r.get("metric") or "")))),
            "live_run": live_run,
            "archive_policy": "latest current observation only: OBSERVED_LIVE; older bulk history: HISTORICAL_PROXY; ambiguous series: CANDIDATE",
        }

    def record_recovery_audit(self, audit: Mapping[str, Any]) -> dict[str, Any]:
        """Append one versioned historical-PIT recovery assessment.

        The audit is evidence about a possible qualification rule, not a
        promotion operation.  A later rule revision gets a new
        ``rule_version`` and cannot mutate this row.
        """

        required = {
            "series_id", "provider", "historical_source", "revision_behavior",
            "publication_semantics", "market_close_semantics", "vintage_support",
            "candidate_available_at_rule", "can_upgrade_historical", "classification",
            "confidence", "evidence", "rule_version",
        }
        missing = required.difference(audit)
        if missing:
            raise ValueError("pit_recovery_audit 缺少字段：" + ", ".join(sorted(missing)))
        classification = str(audit["classification"])
        if classification not in {
            "STRICT_HISTORICAL_ELIGIBLE",
            "ELIGIBLE_WITH_CONSERVATIVE_DELAY",
            "PROXY_ONLY",
            "UNRESOLVED",
        }:
            raise ValueError("无效历史恢复分类")
        evidence = audit["evidence"]
        if not isinstance(evidence, (Mapping, list)):
            raise ValueError("evidence 必须是对象或数组")
        material = {
            "series_id": str(audit["series_id"]),
            "rule_version": str(audit["rule_version"]),
            "evidence": evidence,
        }
        audit_id = str(audit.get("audit_id") or sha256_json(material)[:32])
        audited_at = iso_timestamp(audit.get("audited_at") or utc_now(), "audited_at")
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO pit_recovery_audit
                   (audit_id, series_id, provider, historical_source,
                    revision_behavior, publication_semantics, market_close_semantics,
                    vintage_support, candidate_available_at_rule,
                    can_upgrade_historical, classification, confidence,
                    evidence_json, rule_version, audited_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    audit_id,
                    str(audit["series_id"]),
                    str(audit["provider"]),
                    str(audit["historical_source"]),
                    str(audit["revision_behavior"]),
                    str(audit["publication_semantics"]),
                    str(audit["market_close_semantics"]),
                    str(audit["vintage_support"]),
                    str(audit["candidate_available_at_rule"]),
                    int(bool(audit["can_upgrade_historical"])),
                    classification,
                    str(audit["confidence"]),
                    canonical_json(evidence),
                    str(audit["rule_version"]),
                    audited_at,
                ),
            )
        return {**dict(audit), "audit_id": audit_id, "audited_at": audited_at}

    def get_recovery_audit(self, series_id: str | None = None) -> list[dict[str, Any]]:
        clauses = " WHERE series_id=?" if series_id else ""
        params = [str(series_id)] if series_id else []
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM pit_recovery_audit{clauses} ORDER BY series_id, audited_at, audit_id",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["evidence"] = json.loads(item.pop("evidence_json"))
            except (TypeError, json.JSONDecodeError):
                item["evidence"] = {}
            item["can_upgrade_historical"] = bool(item["can_upgrade_historical"])
            result.append(item)
        return result

    def record_feature_snapshot(self, snapshot: Mapping[str, Any]) -> tuple[str, bool]:
        """Insert a deterministic feature snapshot without overwriting history."""

        required = {
            "feature_name", "series_id", "feature_as_of", "feature_version",
            "input_hash", "input_observation_ids", "score_eligible", "status", "reason",
        }
        missing = required.difference(snapshot)
        if missing:
            raise ValueError("feature_snapshot 缺少字段：" + ", ".join(sorted(missing)))
        input_hash = str(snapshot["input_hash"]).lower()
        if len(input_hash) != 64 or any(c not in "0123456789abcdef" for c in input_hash):
            raise ValueError("feature input_hash 必须是64位 SHA256")
        raw_ids = snapshot["input_observation_ids"]
        if isinstance(raw_ids, str):
            try:
                raw_ids = json.loads(raw_ids)
            except json.JSONDecodeError as exc:
                raise ValueError("input_observation_ids 必须是数组") from exc
        if not isinstance(raw_ids, Iterable):
            raise ValueError("input_observation_ids 必须是数组")
        ids = sorted({str(value) for value in raw_ids})
        if input_hash != sha256_json({
            "feature_name": str(snapshot["feature_name"]),
            "series_id": str(snapshot["series_id"]),
            "feature_as_of": as_of_datetime(snapshot["feature_as_of"]),
            "feature_version": str(snapshot["feature_version"]),
            "input_observation_ids": ids,
        }):
            raise ValueError("feature input_hash 与输入观察不一致")
        feature_as_of = as_of_datetime(snapshot["feature_as_of"])
        feature_version = str(snapshot["feature_version"])
        feature_id = str(snapshot.get("feature_id") or sha256_json({
            "feature_name": str(snapshot["feature_name"]),
            "series_id": str(snapshot["series_id"]),
            "feature_as_of": feature_as_of,
            "feature_version": feature_version,
            "input_hash": input_hash,
        })[:32])
        value = snapshot.get("value")
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("feature value 必须是数值或 null") from exc
        value_json = snapshot.get("value_json")
        if value_json is not None and not isinstance(value_json, str):
            value_json = canonical_json(value_json)
        created_at = iso_timestamp(snapshot.get("created_at") or utc_now(), "created_at")
        with self.connect() as conn:
            before = conn.execute("SELECT 1 FROM feature_snapshots WHERE feature_id=?", (feature_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO feature_snapshots
                   (feature_id, feature_name, series_id, feature_as_of,
                    as_of_datetime, feature_version, value, value_json,
                    input_hash, input_observation_ids_json, score_eligible,
                    status, reason, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    feature_id,
                    str(snapshot["feature_name"]),
                    str(snapshot["series_id"]),
                    feature_as_of,
                    as_of_datetime(snapshot.get("as_of_datetime") or snapshot["feature_as_of"]),
                    feature_version,
                    value,
                    value_json,
                    input_hash,
                    canonical_json(ids),
                    int(bool(snapshot["score_eligible"])),
                    str(snapshot["status"]),
                    str(snapshot["reason"]),
                    created_at,
                ),
            )
        return feature_id, before is None

    def get_feature_snapshots(
        self,
        *,
        feature_name: str | None = None,
        series_id: str | None = None,
        as_of: Any | None = None,
        score_eligible: bool | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if feature_name is not None:
            clauses.append("feature_name=?"); params.append(str(feature_name))
        if series_id is not None:
            clauses.append("series_id=?"); params.append(str(series_id))
        if as_of is not None:
            clauses.append("feature_as_of<=?"); params.append(as_of_datetime(as_of))
        if score_eligible is not None:
            clauses.append("score_eligible=?"); params.append(int(bool(score_eligible)))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM feature_snapshots{where} ORDER BY feature_as_of, feature_name, feature_id",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["score_eligible"] = bool(item["score_eligible"])
            try:
                item["input_observation_ids"] = json.loads(item.pop("input_observation_ids_json"))
            except (TypeError, json.JSONDecodeError):
                item["input_observation_ids"] = []
            if item.get("value_json"):
                try:
                    item["value_payload"] = json.loads(item["value_json"])
                except (TypeError, json.JSONDecodeError):
                    item["value_payload"] = item["value_json"]
            result.append(item)
        return result

    def record_fetch_attempt(self, attempt: Mapping[str, Any]) -> tuple[str, bool]:
        """Append one fetch attempt, including failures and cache fallbacks."""

        required = {"run_id", "attempted_at", "source", "source_url", "status", "retry_count"}
        missing = required.difference(attempt)
        if missing:
            raise ValueError("fetch_attempt 缺少字段：" + ", ".join(sorted(missing)))
        retry_count = int(attempt["retry_count"])
        if retry_count < 0:
            raise ValueError("retry_count 不能为负数")
        attempted_at = iso_timestamp(attempt["attempted_at"], "attempted_at")
        raw_hash = attempt.get("raw_hash")
        if raw_hash not in (None, ""):
            raw_hash = str(raw_hash).lower()
            if len(raw_hash) != 64 or any(c not in "0123456789abcdef" for c in raw_hash):
                raise ValueError("fetch_attempt raw_hash 必须是64位 SHA256")
        material = {
            "run_id": str(attempt["run_id"]), "source": str(attempt["source"]),
            "attempted_at": attempted_at, "retry_count": retry_count,
            "status": str(attempt["status"]), "raw_hash": raw_hash,
        }
        attempt_id = str(attempt.get("attempt_id") or sha256_json(material)[:32])
        created_at = iso_timestamp(attempt.get("created_at") or utc_now(), "created_at")
        with self.connect() as conn:
            before = conn.execute("SELECT 1 FROM fetch_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO fetch_attempts
                   (attempt_id, run_id, attempted_at, source, source_url,
                    status, error, retry_count, raw_fetch_id, http_status,
                    raw_hash, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id, str(attempt["run_id"]), attempted_at,
                    str(attempt["source"]), str(attempt["source_url"] or ""),
                    str(attempt["status"]), None if attempt.get("error") in (None, "") else str(attempt["error"]),
                    retry_count, attempt.get("raw_fetch_id"), attempt.get("http_status"), raw_hash, created_at,
                ),
            )
        return attempt_id, before is None

    def record_fetch_attempts(self, *, run_id: str, manifest: Iterable[Mapping[str, Any]], run_dir: Path | str | None = None) -> dict[str, int]:
        inserted = 0
        duplicates = 0
        run_dir = Path(run_dir) if run_dir else None
        for meta in manifest:
            source = str(meta.get("source") or "")
            if not source:
                continue
            status = str(meta.get("status") or (
                "CACHE_FALLBACK" if meta.get("cache_fallback") else
                "ERROR" if meta.get("error") else
                "SUCCESS"
            ))
            raw_fetch_id = None
            raw_path = run_dir / f"{source}.raw" if run_dir else None
            if raw_path and raw_path.exists() and meta.get("sha256"):
                raw_fetch_id = hashlib.sha256(f"{Path(run_dir).name}|{source}|{meta.get('sha256','')}".encode()).hexdigest()[:32]
            _, created = self.record_fetch_attempt({
                "run_id": str(run_id),
                "attempted_at": meta.get("attempted_at_utc") or meta.get("retrieved_at_utc") or utc_now(),
                "source": source,
                "source_url": meta.get("url") or "",
                "status": status,
                "error": meta.get("error") or meta.get("fallback_warning"),
                "retry_count": int(meta.get("retry_count") or 0),
                "raw_fetch_id": raw_fetch_id,
                "http_status": meta.get("http_status"),
                "raw_hash": meta.get("sha256"),
            })
            if created:
                inserted += 1
            else:
                duplicates += 1
        return {"inserted": inserted, "duplicates": duplicates, "total": inserted + duplicates}

    def get_fetch_attempts(self, run_id: str | None = None, *, limit: int = 500) -> list[dict[str, Any]]:
        clauses = " WHERE run_id=?" if run_id else ""
        params: list[Any] = [str(run_id)] if run_id else []
        params.append(min(max(int(limit), 1), 5000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM fetch_attempts{clauses} ORDER BY attempted_at, source, retry_count LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def record_archive_run(self, run: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"run_id", "profile_id", "as_of_datetime", "started_at", "status"}
        missing = required.difference(run)
        if missing:
            raise ValueError("archive_run 缺少字段：" + ", ".join(sorted(missing)))
        run_id = str(run["run_id"])
        with self.connect() as conn:
            before = conn.execute("SELECT 1 FROM archive_runs WHERE run_id=?", (run_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO archive_runs
                   (run_id, profile_id, as_of_datetime, started_at, finished_at,
                    status, strict_decision_hash, strict_coverage, chain_valid,
                    error_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, str(run["profile_id"]), as_of_datetime(run["as_of_datetime"]),
                    iso_timestamp(run["started_at"], "started_at"),
                    iso_timestamp(run["finished_at"], "finished_at") if run.get("finished_at") else None,
                    str(run["status"]), run.get("strict_decision_hash"),
                    None if run.get("strict_coverage") is None else float(run["strict_coverage"]),
                    None if run.get("chain_valid") is None else int(bool(run["chain_valid"])),
                    canonical_json(run.get("error") or run.get("error_json") or {}),
                    utc_now(),
                ),
            )
        return run_id, before is None

    def get_archive_runs(self, profile_id: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        clauses = " WHERE profile_id=?" if profile_id else ""
        params: list[Any] = [str(profile_id)] if profile_id else []
        params.append(min(max(int(limit), 1), 1000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM archive_runs{clauses} ORDER BY started_at DESC, run_id DESC LIMIT ?",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["error"] = json.loads(item.pop("error_json"))
            except (TypeError, json.JSONDecodeError):
                item["error"] = {}
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # Phase 2A replay repository boundary
    # ------------------------------------------------------------------

    def get_history_proxy(self, series_id: str, as_of: Any, lookback: int | None = None) -> list[dict[str, Any]]:
        """Return only historical proxy versions through the PIT repository.

        This is the research-only counterpart to ``get_history_available``.
        It deliberately selects ``HISTORICAL_PROXY`` rows and never includes
        candidate observations, legacy evidence, or a current CSV.  The
        caller still has to enforce the chronological cutoff for every
        derived feature; the SQL boundary only supplies rows dated on or
        before that cutoff.
        """

        cutoff = as_of_datetime(as_of)
        params: list[Any] = [str(series_id), cutoff]
        clause = ""
        if lookback is not None:
            if int(lookback) < 0:
                raise ValueError("lookback 不能为负数")
            clause = " AND o.observation_date>=date(?, ?)"
            params.extend([normalize_date(as_of), f"-{int(lookback)} days"])
        with self.connect() as conn:
            rows = conn.execute(
                f"""WITH proxy AS (
                        SELECT o.series_id, o.observation_date, v.*, s.display_name,
                               s.unit AS series_unit, s.qualification_class,
                               ROW_NUMBER() OVER (
                                   PARTITION BY v.observation_id
                                   ORDER BY v.version_no DESC, v.retrieved_at DESC,
                                            v.observation_version_id DESC
                               ) AS proxy_rank
                        FROM observations o
                        JOIN observation_versions v ON v.observation_id=o.observation_id
                        JOIN data_series s ON s.series_id=o.series_id
                        WHERE o.series_id=? AND o.observation_date<=date(?)
                          AND v.eligibility_origin='HISTORICAL_PROXY'
                          AND v.score_eligible=0{clause}
                    )
                    SELECT * FROM proxy WHERE proxy_rank=1
                    ORDER BY observation_date ASC, version_no ASC""",
                params,
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def create_replay_run(self, run: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """Create one frozen replay run; an existing id is never overwritten."""

        required = {
            "replay_run_id", "market", "mode", "start_date", "end_date",
            "score_model_version", "feature_model_versions", "data_snapshot",
            "data_cutoff", "run_config_hash",
        }
        missing = required.difference(run)
        if missing:
            raise ValueError("replay_run 缺少字段：" + ", ".join(sorted(missing)))
        mode = str(run["mode"]).upper()
        if mode not in {"STRICT_PIT", "RESEARCH_PROXY"}:
            raise ValueError("replay mode 只能是 STRICT_PIT 或 RESEARCH_PROXY")
        run_hash = str(run["run_config_hash"]).lower()
        if len(run_hash) != 64 or any(c not in "0123456789abcdef" for c in run_hash):
            raise ValueError("run_config_hash 必须是64位 SHA256")
        start_date = normalize_date(run["start_date"], field="start_date")
        end_date = normalize_date(run["end_date"], field="end_date")
        if start_date > end_date:
            raise ValueError("start_date 不能晚于 end_date")
        feature_versions = run["feature_model_versions"]
        if not isinstance(feature_versions, Mapping):
            raise ValueError("feature_model_versions 必须是对象")
        data_snapshot = run["data_snapshot"]
        if not isinstance(data_snapshot, (Mapping, list, str)):
            raise ValueError("data_snapshot 必须是对象、数组或字符串")
        replay_id = str(run["replay_run_id"])
        created_at = iso_timestamp(run.get("created_at") or utc_now(), "created_at")
        started_at = iso_timestamp(run.get("started_at") or created_at, "started_at")
        with self.connect() as conn:
            model = conn.execute("SELECT 1 FROM score_models WHERE model_version=?", (str(run["score_model_version"]),)).fetchone()
            if not model:
                raise ValueError(f"未知 score_model_version：{run['score_model_version']}")
            existing = conn.execute("SELECT * FROM replay_runs WHERE replay_run_id=?", (replay_id,)).fetchone()
            if existing:
                immutable = {
                    "market": str(existing["market"]),
                    "mode": str(existing["mode"]),
                    "start_date": str(existing["start_date"]),
                    "end_date": str(existing["end_date"]),
                    "score_model_version": str(existing["score_model_version"]),
                    "run_config_hash": str(existing["run_config_hash"]),
                }
                expected = {
                    "market": str(run["market"]),
                    "mode": mode,
                    "start_date": start_date,
                    "end_date": end_date,
                    "score_model_version": str(run["score_model_version"]),
                    "run_config_hash": run_hash,
                }
                if immutable != expected:
                    raise ValueError("同一 replay_run_id 的冻结配置不一致")
                return self._decode_replay_run(existing), False
            conn.execute(
                """INSERT INTO replay_runs
                   (replay_run_id, market, mode, start_date, end_date,
                    score_model_version, feature_model_versions_json,
                    data_snapshot_json, data_cutoff, run_config_hash,
                    status, created_at, started_at, completed_at,
                    summary_json, error_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    replay_id, str(run["market"]).upper(), mode, start_date,
                    end_date, str(run["score_model_version"]),
                    canonical_json(dict(feature_versions)), canonical_json(data_snapshot),
                    as_of_datetime(run["data_cutoff"]), run_hash, "RUNNING",
                    created_at, started_at, None, "{}", "{}",
                ),
            )
            row = conn.execute("SELECT * FROM replay_runs WHERE replay_run_id=?", (replay_id,)).fetchone()
        return self._decode_replay_run(row), True

    @staticmethod
    def _decode_replay_run(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target in (
            ("feature_model_versions_json", "feature_model_versions"),
            ("data_snapshot_json", "data_snapshot"),
            ("summary_json", "summary"),
            ("error_json", "error"),
        ):
            try:
                item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError):
                item[target] = {}
        return item

    def get_replay_run(self, replay_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM replay_runs WHERE replay_run_id=?", (str(replay_run_id),)).fetchone()
        return self._decode_replay_run(row) if row else None

    def get_replay_runs(self, *, market: str | None = None, mode: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if market is not None:
            clauses.append("market=?"); params.append(str(market).upper())
        if mode is not None:
            clauses.append("mode=?"); params.append(str(mode).upper())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(max(int(limit), 1), 1000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM replay_runs{where} ORDER BY created_at DESC, replay_run_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._decode_replay_run(row) for row in rows]

    def complete_replay_run(
        self,
        replay_run_id: str,
        *,
        status: str = "COMPLETED",
        summary: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        completed_at: Any | None = None,
    ) -> dict[str, Any]:
        status = str(status).upper()
        if status not in {"COMPLETED", "FAILED"}:
            raise ValueError("replay run 完成状态无效")
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM replay_runs WHERE replay_run_id=?", (str(replay_run_id),)).fetchone()
            if not row:
                raise ValueError("replay_run 不存在")
            if str(row[0]) != "RUNNING":
                raise ValueError("已结束的 replay_run 不可再次完成或修改")
            conn.execute(
                """UPDATE replay_runs
                   SET status=?, completed_at=?, summary_json=?, error_json=?
                   WHERE replay_run_id=? AND status='RUNNING'""",
                (
                    status, iso_timestamp(completed_at or utc_now(), "completed_at"),
                    canonical_json(summary or {}), canonical_json(error or {}),
                    str(replay_run_id),
                ),
            )
            updated = conn.execute("SELECT * FROM replay_runs WHERE replay_run_id=?", (str(replay_run_id),)).fetchone()
        return self._decode_replay_run(updated)

    def append_replay_feature(self, feature: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "replay_run_id", "as_of_datetime", "feature_name", "series_id",
            "feature_version", "input_observation_ids", "score_eligible", "status", "reason",
        }
        missing = required.difference(feature)
        if missing:
            raise ValueError("replay_feature 缺少字段：" + ", ".join(sorted(missing)))
        raw_ids = feature["input_observation_ids"]
        if isinstance(raw_ids, str):
            raw_ids = json.loads(raw_ids)
        if not isinstance(raw_ids, Iterable):
            raise ValueError("input_observation_ids 必须是数组")
        ids = sorted({str(x) for x in raw_ids})
        asof = as_of_datetime(feature["as_of_datetime"])
        expected_hash = sha256_json({
            "feature_name": str(feature["feature_name"]),
            "series_id": str(feature["series_id"]),
            "feature_as_of": asof,
            "feature_version": str(feature["feature_version"]),
            "input_observation_ids": ids,
        })
        input_hash = str(feature.get("input_hash") or expected_hash).lower()
        if input_hash != expected_hash:
            raise ValueError("replay feature input_hash 与输入观察不一致")
        feature_id = str(feature.get("feature_id") or sha256_json({
            "replay_run_id": str(feature["replay_run_id"]),
            "as_of_datetime": asof,
            "feature_name": str(feature["feature_name"]),
            "series_id": str(feature["series_id"]),
            "feature_version": str(feature["feature_version"]),
            "input_hash": input_hash,
        })[:32])
        value = feature.get("value")
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("replay feature value 必须是数值或 null") from exc
        value_json = feature.get("value_json")
        if value_json is not None and not isinstance(value_json, str):
            value_json = canonical_json(value_json)
        with self.connect() as conn:
            before = conn.execute("SELECT 1 FROM replay_features WHERE feature_id=?", (feature_id,)).fetchone()
            run = conn.execute("SELECT status, mode FROM replay_runs WHERE replay_run_id=?", (str(feature["replay_run_id"]),)).fetchone()
            if not run:
                raise ValueError("replay_run 不存在")
            if str(run[0]) != "RUNNING":
                raise ValueError("已结束 replay_run 不能追加 feature")
            conn.execute(
                """INSERT OR IGNORE INTO replay_features
                   (feature_id, replay_run_id, as_of_datetime, feature_name,
                    series_id, feature_version, value, value_json, input_hash,
                    input_observation_ids_json, score_eligible, status, reason, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    feature_id, str(feature["replay_run_id"]), asof,
                    str(feature["feature_name"]), str(feature["series_id"]),
                    str(feature["feature_version"]), value, value_json, input_hash,
                    canonical_json(ids), int(bool(feature["score_eligible"])),
                    str(feature["status"]), str(feature["reason"]),
                    iso_timestamp(feature.get("created_at") or utc_now(), "created_at"),
                ),
            )
        return feature_id, before is None

    def get_replay_features(self, replay_run_id: str, *, as_of: Any | None = None, feature_name: str | None = None) -> list[dict[str, Any]]:
        clauses = ["replay_run_id=?"]
        params: list[Any] = [str(replay_run_id)]
        if as_of is not None:
            clauses.append("as_of_datetime=?"); params.append(as_of_datetime(as_of))
        if feature_name is not None:
            clauses.append("feature_name=?"); params.append(str(feature_name))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM replay_features WHERE {' AND '.join(clauses)} ORDER BY as_of_datetime, feature_name, feature_id",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["input_observation_ids"] = json.loads(item.pop("input_observation_ids_json"))
            except (TypeError, json.JSONDecodeError):
                item["input_observation_ids"] = []
            if item.get("value_json"):
                try:
                    item["value_payload"] = json.loads(item["value_json"])
                except (TypeError, json.JSONDecodeError):
                    item["value_payload"] = item["value_json"]
            item["score_eligible"] = bool(item["score_eligible"])
            result.append(item)
        return result

    def append_replay_decision(self, decision: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "replay_run_id", "sequence_no", "as_of_datetime", "market", "mode",
            "score_model_version", "feature_hash", "input_observation_ids",
            "feature_ids", "coverage", "gate_status", "decision",
            "reason", "missing_items",
        }
        missing = required.difference(decision)
        if missing:
            raise ValueError("replay_decision 缺少字段：" + ", ".join(sorted(missing)))
        mode = str(decision["mode"]).upper()
        if mode not in {"STRICT_PIT", "RESEARCH_PROXY"}:
            raise ValueError("replay decision mode 无效")
        ids = sorted({str(x) for x in decision["input_observation_ids"]})
        feature_ids = sorted({str(x) for x in decision["feature_ids"]})
        expected_input_hash = sha256_json(ids)
        input_hash = str(decision.get("input_hash") or expected_input_hash).lower()
        if input_hash != expected_input_hash:
            raise ValueError("replay decision input_hash 与输入观察不一致")
        feature_hash = str(decision["feature_hash"]).lower()
        if len(feature_hash) != 64 or any(c not in "0123456789abcdef" for c in feature_hash):
            raise ValueError("feature_hash 必须是64位 SHA256")
        coverage = float(decision["coverage"])
        if not 0 <= coverage <= 100:
            raise ValueError("coverage 必须在 0–100")
        asof = as_of_datetime(decision["as_of_datetime"])
        replay_id = str(decision["replay_run_id"])
        decision_id = str(decision.get("replay_decision_id") or sha256_json({"replay_run_id": replay_id, "as_of_datetime": asof})[:32])
        with self.connect() as conn:
            run = conn.execute("SELECT status, mode, market, score_model_version FROM replay_runs WHERE replay_run_id=?", (replay_id,)).fetchone()
            if not run:
                raise ValueError("replay_run 不存在")
            if str(run[0]) != "RUNNING":
                raise ValueError("已结束 replay_run 不能追加 decision")
            if str(run[1]) != mode or str(run[2]) != str(decision["market"]).upper() or str(run[3]) != str(decision["score_model_version"]):
                raise ValueError("replay decision 与冻结 run 配置不一致")
            before = conn.execute("SELECT * FROM replay_decisions WHERE replay_decision_id=?", (decision_id,)).fetchone()
            if before:
                if str(before["input_hash"]) != input_hash or str(before["feature_hash"]) != feature_hash or str(before["decision"]) != str(decision["decision"]):
                    raise ValueError("同一 replay decision id 的结果不一致")
                return decision_id, False
            conn.execute(
                """INSERT INTO replay_decisions
                   (replay_decision_id, replay_run_id, sequence_no, as_of_datetime,
                    market, mode, score_model_version, input_hash, feature_hash,
                    input_observation_ids_json, feature_ids_json, coverage, score,
                    gate_status, decision, reason_json, missing_items_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id, replay_id, int(decision["sequence_no"]), asof,
                    str(decision["market"]).upper(), mode, str(decision["score_model_version"]),
                    input_hash, feature_hash, canonical_json(ids), canonical_json(feature_ids),
                    coverage, None if decision.get("score") is None else float(decision["score"]),
                    str(decision["gate_status"]), str(decision["decision"]),
                    canonical_json(decision.get("reason") or {}),
                    canonical_json(decision.get("missing_items") or []),
                    iso_timestamp(decision.get("created_at") or utc_now(), "created_at"),
                ),
            )
        return decision_id, True

    def get_replay_decisions(self, replay_run_id: str, *, as_of: Any | None = None, limit: int = 10000) -> list[dict[str, Any]]:
        clauses = ["replay_run_id=?"]
        params: list[Any] = [str(replay_run_id)]
        if as_of is not None:
            clauses.append("as_of_datetime=?"); params.append(as_of_datetime(as_of))
        params.append(min(max(int(limit), 1), 100000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM replay_decisions WHERE {' AND '.join(clauses)} ORDER BY sequence_no, as_of_datetime LIMIT ?",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for field, target in (
                ("input_observation_ids_json", "input_observation_ids"),
                ("feature_ids_json", "feature_ids"),
                ("reason_json", "reason"),
                ("missing_items_json", "missing_items"),
            ):
                try:
                    item[target] = json.loads(item.pop(field))
                except (TypeError, json.JSONDecodeError):
                    item[target] = [] if "ids" in target or target == "missing_items" else {}
            result.append(item)
        return result

    def append_forward_outcome(self, outcome: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "replay_run_id", "replay_decision_id", "market", "decision_date",
            "source_series_id", "source_observation_version_ids", "status", "reason",
        }
        missing = required.difference(outcome)
        if missing:
            raise ValueError("forward_outcome 缺少字段：" + ", ".join(sorted(missing)))
        ids = sorted({str(x) for x in outcome["source_observation_version_ids"]})
        outcome_id = str(outcome.get("outcome_id") or sha256_json({
            "replay_decision_id": str(outcome["replay_decision_id"]),
            "source_series_id": str(outcome["source_series_id"]),
        })[:32])
        with self.connect() as conn:
            link = conn.execute("SELECT replay_run_id FROM replay_decisions WHERE replay_decision_id=?", (str(outcome["replay_decision_id"]),)).fetchone()
            if not link or str(link[0]) != str(outcome["replay_run_id"]):
                raise ValueError("forward outcome 必须指向同一 replay decision")
            before = conn.execute("SELECT 1 FROM forward_outcomes WHERE outcome_id=?", (outcome_id,)).fetchone()
            values = [outcome.get(key) for key in ("forward_1m", "forward_3m", "forward_6m", "forward_1y", "forward_3y", "forward_5y", "max_drawdown_next_1y", "max_gain_next_1y")]
            conn.execute(
                """INSERT OR IGNORE INTO forward_outcomes
                   (outcome_id, replay_run_id, replay_decision_id, market,
                    decision_date, source_series_id, source_observation_version_ids_json,
                    forward_1m, forward_3m, forward_6m, forward_1y, forward_3y,
                    forward_5y, max_drawdown_next_1y, max_gain_next_1y, status,
                    reason, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    outcome_id, str(outcome["replay_run_id"]), str(outcome["replay_decision_id"]),
                    str(outcome["market"]).upper(), normalize_date(outcome["decision_date"], field="decision_date"),
                    str(outcome["source_series_id"]), canonical_json(ids), *values,
                    str(outcome["status"]), str(outcome["reason"]),
                    iso_timestamp(outcome.get("created_at") or utc_now(), "created_at"),
                ),
            )
        return outcome_id, before is None

    def get_forward_outcomes(self, replay_run_id: str, *, limit: int = 100000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM forward_outcomes WHERE replay_run_id=? ORDER BY decision_date, outcome_id LIMIT ?",
                (str(replay_run_id), min(max(int(limit), 1), 100000)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["source_observation_version_ids"] = json.loads(item.pop("source_observation_version_ids_json"))
            except (TypeError, json.JSONDecodeError):
                item["source_observation_version_ids"] = []
            result.append(item)
        return result

    def record_replay_report(self, replay_run_id: str, report: Mapping[str, Any]) -> tuple[str, bool]:
        payload = canonical_json(report)
        report_hash = sha256_json(report)
        report_id = sha256_json({"replay_run_id": str(replay_run_id), "report_hash": report_hash})[:32]
        with self.connect() as conn:
            before = conn.execute("SELECT 1 FROM replay_reports WHERE replay_run_id=?", (str(replay_run_id),)).fetchone()
            if before:
                return report_id, False
            conn.execute(
                "INSERT INTO replay_reports(report_id,replay_run_id,report_hash,report_json,created_at) VALUES(?,?,?,?,?)",
                (report_id, str(replay_run_id), report_hash, payload, utc_now()),
            )
        return report_id, True

    def get_replay_report(self, replay_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM replay_reports WHERE replay_run_id=?", (str(replay_run_id),)).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["report"] = json.loads(item.pop("report_json"))
        except (TypeError, json.JSONDecodeError):
            item["report"] = {}
        return item

    # Phase 2B is intentionally a separate repository surface.  None of the
    # methods below are called by strict evaluation or by replay_engine's V2
    # decision path.  A new proxy model/config creates a new run rather than
    # modifying an earlier research result.
    def create_proxy_research_run(self, run: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        required = {
            "proxy_run_id", "market", "proxy_model_version", "start_date", "end_date",
            "proxy_model_config", "config_hash", "data_snapshot", "data_cutoff",
        }
        missing = required.difference(run)
        if missing:
            raise ValueError("proxy_research_run 缺少字段：" + ", ".join(sorted(missing)))
        if str(run["proxy_model_version"]) != "NDX_PROXY_RESEARCH_V1":
            raise ValueError("proxy_model_version 必须是 NDX_PROXY_RESEARCH_V1")
        config_hash = str(run["config_hash"]).lower()
        if len(config_hash) != 64 or any(c not in "0123456789abcdef" for c in config_hash):
            raise ValueError("config_hash 必须是64位 SHA256")
        start_date = normalize_date(run["start_date"], field="start_date")
        end_date = normalize_date(run["end_date"], field="end_date")
        if start_date > end_date:
            raise ValueError("start_date 不能晚于 end_date")
        config = run["proxy_model_config"]
        snapshot = run["data_snapshot"]
        if not isinstance(config, Mapping):
            raise ValueError("proxy_model_config 必须是对象")
        if not isinstance(snapshot, (Mapping, list, str)):
            raise ValueError("data_snapshot 必须是对象、数组或字符串")
        proxy_id = str(run["proxy_run_id"])
        created_at = iso_timestamp(run.get("created_at") or utc_now(), "created_at")
        started_at = iso_timestamp(run.get("started_at") or created_at, "started_at")
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM proxy_research_runs WHERE proxy_run_id=?", (proxy_id,)).fetchone()
            if existing:
                immutable = {
                    "market": str(existing["market"]),
                    "proxy_model_version": str(existing["proxy_model_version"]),
                    "start_date": str(existing["start_date"]),
                    "end_date": str(existing["end_date"]),
                    "config_hash": str(existing["config_hash"]),
                }
                expected = {
                    "market": str(run["market"]).upper(),
                    "proxy_model_version": str(run["proxy_model_version"]),
                    "start_date": start_date,
                    "end_date": end_date,
                    "config_hash": config_hash,
                }
                if immutable != expected:
                    raise ValueError("同一 proxy_run_id 的冻结配置不一致")
                return self._decode_proxy_research_run(existing), False
            conn.execute(
                """INSERT INTO proxy_research_runs
                   (proxy_run_id, market, proxy_model_version, start_date, end_date,
                    proxy_model_config_json, config_hash, data_snapshot_json,
                    data_cutoff, status, created_at, started_at, completed_at,
                    summary_json, error_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    proxy_id, str(run["market"]).upper(), str(run["proxy_model_version"]),
                    start_date, end_date, canonical_json(dict(config)), config_hash,
                    canonical_json(snapshot), as_of_datetime(run["data_cutoff"]),
                    "RUNNING", created_at, started_at, None, "{}", "{}",
                ),
            )
            row = conn.execute("SELECT * FROM proxy_research_runs WHERE proxy_run_id=?", (proxy_id,)).fetchone()
        return self._decode_proxy_research_run(row), True

    @staticmethod
    def _decode_proxy_research_run(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target in (
            ("proxy_model_config_json", "proxy_model_config"),
            ("data_snapshot_json", "data_snapshot"),
            ("summary_json", "summary"),
            ("error_json", "error"),
        ):
            try:
                item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError):
                item[target] = {}
        return item

    def get_proxy_research_run(self, proxy_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM proxy_research_runs WHERE proxy_run_id=?", (str(proxy_run_id),)).fetchone()
        return self._decode_proxy_research_run(row) if row else None

    def get_proxy_research_runs(self, *, market: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if market is not None:
            clauses.append("market=?"); params.append(str(market).upper())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(max(int(limit), 1), 1000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM proxy_research_runs{where} ORDER BY created_at DESC, proxy_run_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._decode_proxy_research_run(row) for row in rows]

    def complete_proxy_research_run(
        self,
        proxy_run_id: str,
        *,
        status: str = "COMPLETED",
        summary: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        completed_at: Any | None = None,
    ) -> dict[str, Any]:
        status = str(status).upper()
        if status not in {"COMPLETED", "FAILED"}:
            raise ValueError("proxy research run 完成状态无效")
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM proxy_research_runs WHERE proxy_run_id=?", (str(proxy_run_id),)).fetchone()
            if not row:
                raise ValueError("proxy_research_run 不存在")
            if str(row[0]) != "RUNNING":
                raise ValueError("已结束的 proxy research run 不可再次完成或修改")
            conn.execute(
                """UPDATE proxy_research_runs
                   SET status=?, completed_at=?, summary_json=?, error_json=?
                   WHERE proxy_run_id=? AND status='RUNNING'""",
                (
                    status, iso_timestamp(completed_at or utc_now(), "completed_at"),
                    canonical_json(summary or {}), canonical_json(error or {}), str(proxy_run_id),
                ),
            )
            updated = conn.execute("SELECT * FROM proxy_research_runs WHERE proxy_run_id=?", (str(proxy_run_id),)).fetchone()
        return self._decode_proxy_research_run(updated)

    def append_proxy_signal(self, signal: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "proxy_run_id", "as_of_datetime", "market", "proxy_model_version",
            "proxy_available_weight", "proxy_b_available_weight", "feature_values",
            "feature_percentiles", "feature_status", "input_observation_ids",
        }
        missing = required.difference(signal)
        if missing:
            raise ValueError("proxy_signal 缺少字段：" + ", ".join(sorted(missing)))
        if str(signal["proxy_model_version"]) != "NDX_PROXY_RESEARCH_V1":
            raise ValueError("proxy_signal model version 无效")
        ids = sorted({str(x) for x in signal["input_observation_ids"]})
        expected_hash = sha256_json(ids)
        input_hash = str(signal.get("input_hash") or expected_hash).lower()
        if input_hash != expected_hash:
            raise ValueError("proxy_signal input_hash 与输入观察不一致")
        asof = as_of_datetime(signal["as_of_datetime"])
        signal_id = str(signal.get("proxy_signal_id") or sha256_json({"proxy_run_id": str(signal["proxy_run_id"]), "as_of_datetime": asof})[:32])
        with self.connect() as conn:
            run = conn.execute("SELECT status, market FROM proxy_research_runs WHERE proxy_run_id=?", (str(signal["proxy_run_id"]),)).fetchone()
            if not run:
                raise ValueError("proxy_research_run 不存在")
            if str(run[0]) != "RUNNING":
                raise ValueError("已结束 proxy research run 不能追加 signal")
            if str(run[1]) != str(signal["market"]).upper():
                raise ValueError("proxy_signal market 与 run 不一致")
            before = conn.execute("SELECT 1 FROM proxy_signal_observations WHERE proxy_signal_id=?", (signal_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO proxy_signal_observations
                   (proxy_signal_id, proxy_run_id, as_of_datetime, market,
                    proxy_model_version, proxy_raw_score, proxy_available_weight,
                    proxy_score_fraction, proxy_rank, proxy_percentile,
                    proxy_b_raw_score, proxy_b_available_weight,
                    proxy_b_score_fraction, proxy_b_rank, proxy_b_percentile,
                    feature_values_json, feature_percentiles_json,
                    feature_status_json, input_observation_ids_json, input_hash,
                    created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    signal_id, str(signal["proxy_run_id"]), asof,
                    str(signal["market"]).upper(), str(signal["proxy_model_version"]),
                    signal.get("proxy_raw_score"), float(signal["proxy_available_weight"]),
                    signal.get("proxy_score_fraction"), signal.get("proxy_rank"), signal.get("proxy_percentile"),
                    signal.get("proxy_b_raw_score"), float(signal["proxy_b_available_weight"]),
                    signal.get("proxy_b_score_fraction"), signal.get("proxy_b_rank"), signal.get("proxy_b_percentile"),
                    canonical_json(signal["feature_values"]), canonical_json(signal["feature_percentiles"]),
                    canonical_json(signal["feature_status"]), canonical_json(ids), input_hash,
                    iso_timestamp(signal.get("created_at") or utc_now(), "created_at"),
                ),
            )
        return signal_id, before is None

    def get_proxy_signals(self, proxy_run_id: str, *, limit: int = 100000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM proxy_signal_observations WHERE proxy_run_id=? ORDER BY as_of_datetime, proxy_signal_id LIMIT ?",
                (str(proxy_run_id), min(max(int(limit), 1), 500000)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for field, target, default in (
                ("feature_values_json", "feature_values", {}),
                ("feature_percentiles_json", "feature_percentiles", {}),
                ("feature_status_json", "feature_status", {}),
                ("input_observation_ids_json", "input_observation_ids", []),
            ):
                try:
                    item[target] = json.loads(item.pop(field))
                except (TypeError, json.JSONDecodeError):
                    item[target] = default
            result.append(item)
        return result

    def append_proxy_forward_outcome(self, outcome: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "proxy_run_id", "proxy_signal_id", "market", "decision_date",
            "source_series_id", "source_observation_version_ids", "status", "reason",
        }
        missing = required.difference(outcome)
        if missing:
            raise ValueError("proxy_forward_outcome 缺少字段：" + ", ".join(sorted(missing)))
        outcome_id = str(outcome.get("proxy_outcome_id") or sha256_json({"proxy_signal_id": str(outcome["proxy_signal_id"]), "source_series_id": str(outcome["source_series_id"])})[:32])
        ids = sorted({str(x) for x in outcome["source_observation_version_ids"]})
        with self.connect() as conn:
            link = conn.execute("SELECT proxy_run_id FROM proxy_signal_observations WHERE proxy_signal_id=?", (str(outcome["proxy_signal_id"]),)).fetchone()
            if not link or str(link[0]) != str(outcome["proxy_run_id"]):
                raise ValueError("proxy forward outcome 必须指向同一 signal")
            before = conn.execute("SELECT 1 FROM proxy_forward_outcomes WHERE proxy_outcome_id=?", (outcome_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO proxy_forward_outcomes
                   (proxy_outcome_id, proxy_run_id, proxy_signal_id, market,
                    decision_date, source_series_id, source_observation_version_ids_json,
                    forward_6m, forward_1y, forward_3y, forward_5y,
                    max_drawdown_next_1y, max_gain_next_1y, status, reason, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    outcome_id, str(outcome["proxy_run_id"]), str(outcome["proxy_signal_id"]),
                    str(outcome["market"]).upper(), normalize_date(outcome["decision_date"], field="decision_date"),
                    str(outcome["source_series_id"]), canonical_json(ids),
                    outcome.get("forward_6m"), outcome.get("forward_1y"), outcome.get("forward_3y"), outcome.get("forward_5y"),
                    outcome.get("max_drawdown_next_1y"), outcome.get("max_gain_next_1y"),
                    str(outcome["status"]), str(outcome["reason"]), iso_timestamp(outcome.get("created_at") or utc_now(), "created_at"),
                ),
            )
        return outcome_id, before is None

    def get_proxy_forward_outcomes(self, proxy_run_id: str, *, limit: int = 100000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM proxy_forward_outcomes WHERE proxy_run_id=? ORDER BY decision_date, proxy_outcome_id LIMIT ?",
                (str(proxy_run_id), min(max(int(limit), 1), 500000)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["source_observation_version_ids"] = json.loads(item.pop("source_observation_version_ids_json"))
            except (TypeError, json.JSONDecodeError):
                item["source_observation_version_ids"] = []
            result.append(item)
        return result

    def record_proxy_research_report(self, proxy_run_id: str, report: Mapping[str, Any]) -> tuple[str, bool]:
        payload = canonical_json(report)
        report_hash = sha256_json(report)
        report_id = sha256_json({"proxy_run_id": str(proxy_run_id), "report_hash": report_hash})[:32]
        with self.connect() as conn:
            before = conn.execute("SELECT 1 FROM proxy_research_reports WHERE proxy_run_id=?", (str(proxy_run_id),)).fetchone()
            if before:
                return report_id, False
            conn.execute(
                "INSERT INTO proxy_research_reports(proxy_report_id,proxy_run_id,report_hash,report_json,created_at) VALUES(?,?,?,?,?)",
                (report_id, str(proxy_run_id), report_hash, payload, utc_now()),
            )
        return report_id, True

    def get_proxy_research_report(self, proxy_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM proxy_research_reports WHERE proxy_run_id=?", (str(proxy_run_id),)).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["report"] = json.loads(item.pop("report_json"))
        except (TypeError, json.JSONDecodeError):
            item["report"] = {}
        return item

    # Phase 2C is a separate episode/event research surface.  Episode
    # construction, daily observable state and event clustering never read
    # any evaluation table.  These methods deliberately use append-only
    # tables so a later analysis cannot rewrite an earlier episode run.
    def create_episode_validation_run(self, run: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        required = {
            "episode_run_id", "market", "episode_model_version", "proxy_run_id",
            "start_date", "end_date", "episode_model_config", "config_hash",
            "data_snapshot", "data_cutoff",
        }
        missing = required.difference(run)
        if missing:
            raise ValueError("episode_validation_run 缺少字段：" + ", ".join(sorted(missing)))
        if str(run["episode_model_version"]) != "EPISODE_OPPORTUNITY_V1":
            raise ValueError("episode_model_version 必须是 EPISODE_OPPORTUNITY_V1")
        config_hash = str(run["config_hash"]).lower()
        if len(config_hash) != 64 or any(c not in "0123456789abcdef" for c in config_hash):
            raise ValueError("config_hash 必须是64位 SHA256")
        start_date = normalize_date(run["start_date"], field="start_date")
        end_date = normalize_date(run["end_date"], field="end_date")
        if start_date > end_date:
            raise ValueError("start_date 不能晚于 end_date")
        config = run["episode_model_config"]
        snapshot = run["data_snapshot"]
        if not isinstance(config, Mapping):
            raise ValueError("episode_model_config 必须是对象")
        if not isinstance(snapshot, (Mapping, list, str)):
            raise ValueError("data_snapshot 必须是对象、数组或字符串")
        episode_id = str(run["episode_run_id"])
        created_at = iso_timestamp(run.get("created_at") or utc_now(), "created_at")
        started_at = iso_timestamp(run.get("started_at") or created_at, "started_at")
        with self.connect() as conn:
            proxy = conn.execute("SELECT 1 FROM proxy_research_runs WHERE proxy_run_id=?", (str(run["proxy_run_id"]),)).fetchone()
            if not proxy:
                raise ValueError("episode_validation_run 必须引用已存在的 proxy_research_run")
            existing = conn.execute("SELECT * FROM episode_validation_runs WHERE episode_run_id=?", (episode_id,)).fetchone()
            if existing:
                immutable = {
                    "market": str(existing["market"]),
                    "episode_model_version": str(existing["episode_model_version"]),
                    "proxy_run_id": str(existing["proxy_run_id"]),
                    "start_date": str(existing["start_date"]),
                    "end_date": str(existing["end_date"]),
                    "config_hash": str(existing["config_hash"]),
                }
                expected = {
                    "market": str(run["market"]).upper(),
                    "episode_model_version": str(run["episode_model_version"]),
                    "proxy_run_id": str(run["proxy_run_id"]),
                    "start_date": start_date,
                    "end_date": end_date,
                    "config_hash": config_hash,
                }
                if immutable != expected:
                    raise ValueError("同一 episode_run_id 的冻结配置不一致")
                return self._decode_episode_validation_run(existing), False
            conn.execute(
                """INSERT INTO episode_validation_runs
                   (episode_run_id, market, episode_model_version, proxy_run_id,
                    start_date, end_date, episode_model_config_json, config_hash,
                    data_snapshot_json, data_cutoff, status, created_at,
                    started_at, completed_at, summary_json, error_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    episode_id, str(run["market"]).upper(), str(run["episode_model_version"]),
                    str(run["proxy_run_id"]), start_date, end_date,
                    canonical_json(dict(config)), config_hash, canonical_json(snapshot),
                    as_of_datetime(run["data_cutoff"]), "RUNNING", created_at,
                    started_at, None, "{}", "{}",
                ),
            )
            row = conn.execute("SELECT * FROM episode_validation_runs WHERE episode_run_id=?", (episode_id,)).fetchone()
        return self._decode_episode_validation_run(row), True

    @staticmethod
    def _decode_episode_validation_run(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target in (
            ("episode_model_config_json", "episode_model_config"),
            ("data_snapshot_json", "data_snapshot"),
            ("summary_json", "summary"),
            ("error_json", "error"),
        ):
            try:
                item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError):
                item[target] = {}
        return item

    def get_episode_validation_run(self, episode_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM episode_validation_runs WHERE episode_run_id=?", (str(episode_run_id),)).fetchone()
        return self._decode_episode_validation_run(row) if row else None

    def get_episode_validation_runs(self, *, market: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if market is not None:
            clauses.append("market=?"); params.append(str(market).upper())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(max(int(limit), 1), 1000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM episode_validation_runs{where} ORDER BY created_at DESC, episode_run_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._decode_episode_validation_run(row) for row in rows]

    def complete_episode_validation_run(
        self,
        episode_run_id: str,
        *,
        status: str = "COMPLETED",
        summary: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        completed_at: Any | None = None,
    ) -> dict[str, Any]:
        status = str(status).upper()
        if status not in {"COMPLETED", "FAILED"}:
            raise ValueError("episode validation run 完成状态无效")
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM episode_validation_runs WHERE episode_run_id=?", (str(episode_run_id),)).fetchone()
            if not row:
                raise ValueError("episode_validation_run 不存在")
            if str(row[0]) != "RUNNING":
                raise ValueError("已结束的 episode validation run 不可再次完成或修改")
            conn.execute(
                """UPDATE episode_validation_runs
                   SET status=?, completed_at=?, summary_json=?, error_json=?
                   WHERE episode_run_id=? AND status='RUNNING'""",
                (
                    status, iso_timestamp(completed_at or utc_now(), "completed_at"),
                    canonical_json(summary or {}), canonical_json(error or {}), str(episode_run_id),
                ),
            )
            updated = conn.execute("SELECT * FROM episode_validation_runs WHERE episode_run_id=?", (str(episode_run_id),)).fetchone()
        return self._decode_episode_validation_run(updated)

    @staticmethod
    def _require_episode_running(conn: sqlite3.Connection, episode_run_id: str) -> None:
        row = conn.execute("SELECT status FROM episode_validation_runs WHERE episode_run_id=?", (str(episode_run_id),)).fetchone()
        if not row:
            raise ValueError("episode_validation_run 不存在")
        if str(row[0]) != "RUNNING":
            raise ValueError("已结束 episode validation run 不能追加数据")

    def append_drawdown_episode(self, episode: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"episode_run_id", "episode_id", "market", "peak_date", "peak_value", "start_date", "complete", "data_end_date"}
        missing = required.difference(episode)
        if missing:
            raise ValueError("drawdown_episode 缺少字段：" + ", ".join(sorted(missing)))
        episode_id = str(episode["episode_id"])
        with self.connect() as conn:
            self._require_episode_running(conn, str(episode["episode_run_id"]))
            before = conn.execute("SELECT 1 FROM drawdown_episodes WHERE episode_id=?", (episode_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO drawdown_episodes
                   (episode_id, episode_run_id, market, peak_date, peak_value,
                    start_date, max_drawdown, max_drawdown_date, bottom_value,
                    bottom_date, recovery_date, duration_days, duration_trading_days,
                    complete, data_end_date, episode_payload_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    episode_id, str(episode["episode_run_id"]), str(episode["market"]).upper(),
                    normalize_date(episode["peak_date"], field="peak_date"), float(episode["peak_value"]),
                    normalize_date(episode["start_date"], field="start_date"), episode.get("max_drawdown"),
                    normalize_date(episode["max_drawdown_date"], field="max_drawdown_date") if episode.get("max_drawdown_date") else None,
                    episode.get("bottom_value"), normalize_date(episode["bottom_date"], field="bottom_date") if episode.get("bottom_date") else None,
                    normalize_date(episode["recovery_date"], field="recovery_date") if episode.get("recovery_date") else None,
                    episode.get("duration_days"), episode.get("duration_trading_days"), int(bool(episode["complete"])),
                    normalize_date(episode["data_end_date"], field="data_end_date"), canonical_json(episode.get("payload") or {}), utc_now(),
                ),
            )
        return episode_id, before is None

    def get_drawdown_episodes(self, episode_run_id: str, *, limit: int = 100000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM drawdown_episodes WHERE episode_run_id=? ORDER BY start_date, episode_id LIMIT ?",
                (str(episode_run_id), min(max(int(limit), 1), 500000)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try: item["payload"] = json.loads(item.pop("episode_payload_json"))
            except (TypeError, json.JSONDecodeError): item["payload"] = {}
            result.append(item)
        return result

    def append_episode_daily_state(self, state: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"episode_run_id", "episode_id", "as_of_date", "input_observation_ids", "input_hash"}
        missing = required.difference(state)
        if missing:
            raise ValueError("episode_daily_state 缺少字段：" + ", ".join(sorted(missing)))
        ids = sorted({str(x) for x in state["input_observation_ids"]})
        expected = sha256_json(ids)
        if str(state["input_hash"]).lower() != expected:
            raise ValueError("episode_daily_state input_hash 与输入观察不一致")
        state_id = str(state.get("state_id") or sha256_json({"episode_run_id": str(state["episode_run_id"]), "episode_id": str(state["episode_id"]), "as_of_date": normalize_date(state["as_of_date"])})[:32])
        with self.connect() as conn:
            self._require_episode_running(conn, str(state["episode_run_id"]))
            link = conn.execute("SELECT episode_run_id FROM drawdown_episodes WHERE episode_id=?", (str(state["episode_id"]),)).fetchone()
            if not link or str(link[0]) != str(state["episode_run_id"]):
                raise ValueError("episode_daily_state 必须指向同一 episode_run")
            before = conn.execute("SELECT 1 FROM episode_daily_states WHERE state_id=?", (state_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO episode_daily_states
                   (state_id, episode_run_id, episode_id, as_of_date,
                    current_drawdown, days_since_peak, current_proxy_score,
                    current_proxy_percentile, rsi14, distance_ma200, vxn,
                    real_yield, nfci, input_observation_ids_json, input_hash, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    state_id, str(state["episode_run_id"]), str(state["episode_id"]), normalize_date(state["as_of_date"]),
                    state.get("current_drawdown"), state.get("days_since_peak"), state.get("current_proxy_score"),
                    state.get("current_proxy_percentile"), state.get("rsi14"), state.get("distance_ma200"),
                    state.get("vxn"), state.get("real_yield"), state.get("nfci"), canonical_json(ids), expected, utc_now(),
                ),
            )
        return state_id, before is None

    def get_episode_daily_states(self, episode_run_id: str, *, episode_id: str | None = None, limit: int = 500000) -> list[dict[str, Any]]:
        clauses = ["episode_run_id=?"]; params: list[Any] = [str(episode_run_id)]
        if episode_id is not None: clauses.append("episode_id=?"); params.append(str(episode_id))
        params.append(min(max(int(limit), 1), 1000000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM episode_daily_states WHERE {' AND '.join(clauses)} ORDER BY as_of_date, state_id LIMIT ?", params
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try: item["input_observation_ids"] = json.loads(item.pop("input_observation_ids_json"))
            except (TypeError, json.JSONDecodeError): item["input_observation_ids"] = []
            result.append(item)
        return result

    def append_proxy_opportunity_event(self, event: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "episode_run_id", "episode_id", "proxy_run_id", "signal_variant", "event_start", "event_end",
            "first_signal_date", "peak_signal_date", "signal_max", "signal_mean", "first_signal_id", "peak_signal_id",
            "cluster_gap_trading_days",
        }
        missing = required.difference(event)
        if missing: raise ValueError("proxy_opportunity_event 缺少字段：" + ", ".join(sorted(missing)))
        if str(event["signal_variant"]) not in {"proxy_a", "proxy_b"}: raise ValueError("signal_variant 无效")
        if int(event["cluster_gap_trading_days"]) != 10: raise ValueError("Phase 2C 的 event cluster gap 固定为10个交易日")
        event_id = str(event["opportunity_event_id"])
        with self.connect() as conn:
            self._require_episode_running(conn, str(event["episode_run_id"]))
            link = conn.execute("SELECT episode_run_id FROM drawdown_episodes WHERE episode_id=?", (str(event["episode_id"]),)).fetchone()
            if not link or str(link[0]) != str(event["episode_run_id"]): raise ValueError("opportunity event 必须指向同一 episode_run")
            before = conn.execute("SELECT 1 FROM proxy_opportunity_events WHERE opportunity_event_id=?", (event_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO proxy_opportunity_events
                   (opportunity_event_id, episode_run_id, episode_id, proxy_run_id,
                    signal_variant, event_start, event_end, first_signal_date,
                    peak_signal_date, signal_max, signal_mean, drawdown_at_first_signal,
                    drawdown_at_peak_signal, first_signal_id, peak_signal_id,
                    cluster_gap_trading_days, event_payload_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, str(event["episode_run_id"]), str(event["episode_id"]), str(event["proxy_run_id"]),
                    str(event["signal_variant"]), normalize_date(event["event_start"]), normalize_date(event["event_end"]),
                    normalize_date(event["first_signal_date"]), normalize_date(event["peak_signal_date"]), float(event["signal_max"]),
                    float(event["signal_mean"]), event.get("drawdown_at_first_signal"), event.get("drawdown_at_peak_signal"),
                    str(event["first_signal_id"]), str(event["peak_signal_id"]), 10, canonical_json(event.get("payload") or {}), utc_now(),
                ),
            )
        return event_id, before is None

    def get_proxy_opportunity_events(self, episode_run_id: str, *, signal_variant: str | None = None, limit: int = 500000) -> list[dict[str, Any]]:
        clauses = ["episode_run_id=?"]; params: list[Any] = [str(episode_run_id)]
        if signal_variant is not None: clauses.append("signal_variant=?"); params.append(str(signal_variant))
        params.append(min(max(int(limit), 1), 1000000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM proxy_opportunity_events WHERE {' AND '.join(clauses)} ORDER BY event_start, opportunity_event_id LIMIT ?", params
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try: item["payload"] = json.loads(item.pop("event_payload_json"))
            except (TypeError, json.JSONDecodeError): item["payload"] = {}
            result.append(item)
        return result

    def append_drawdown_mechanical_event(self, event: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"episode_run_id", "episode_id", "threshold", "event_date", "event_price", "drawdown"}
        missing = required.difference(event)
        if missing: raise ValueError("drawdown_mechanical_event 缺少字段：" + ", ".join(sorted(missing)))
        threshold = float(event["threshold"])
        if threshold not in {0.1, 0.2, 0.3, 0.4, 0.5}: raise ValueError("机械回撤档位必须是10/20/30/40/50%")
        event_id = str(event.get("mechanical_event_id") or sha256_json({"episode_run_id": str(event["episode_run_id"]), "episode_id": str(event["episode_id"]), "threshold": threshold})[:32])
        with self.connect() as conn:
            self._require_episode_running(conn, str(event["episode_run_id"]))
            link = conn.execute("SELECT episode_run_id FROM drawdown_episodes WHERE episode_id=?", (str(event["episode_id"]),)).fetchone()
            if not link or str(link[0]) != str(event["episode_run_id"]): raise ValueError("mechanical event 必须指向同一 episode_run")
            before = conn.execute("SELECT 1 FROM drawdown_mechanical_events WHERE mechanical_event_id=?", (event_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO drawdown_mechanical_events
                   (mechanical_event_id, episode_run_id, episode_id, threshold,
                    event_date, event_price, drawdown, source_observation_id,
                    event_payload_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, str(event["episode_run_id"]), str(event["episode_id"]), threshold,
                    normalize_date(event["event_date"]), float(event["event_price"]), float(event["drawdown"]),
                    event.get("source_observation_id"), canonical_json(event.get("payload") or {}), utc_now(),
                ),
            )
        return event_id, before is None

    def get_drawdown_mechanical_events(self, episode_run_id: str, *, limit: int = 500000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM drawdown_mechanical_events WHERE episode_run_id=? ORDER BY event_date, threshold, mechanical_event_id LIMIT ?",
                (str(episode_run_id), min(max(int(limit), 1), 1000000)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try: item["payload"] = json.loads(item.pop("event_payload_json"))
            except (TypeError, json.JSONDecodeError): item["payload"] = {}
            result.append(item)
        return result

    def append_episode_event_evaluation(self, evaluation: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"episode_run_id", "episode_id", "event_type", "event_id", "entry_date", "entry_price", "timing_regret", "status", "reason"}
        missing = required.difference(evaluation)
        if missing: raise ValueError("episode_event_evaluation 缺少字段：" + ", ".join(sorted(missing)))
        event_type = str(evaluation["event_type"]).upper()
        if event_type not in {"COMPOSITE", "DRAWDOWN"}: raise ValueError("event_type 无效")
        evaluation_id = str(evaluation.get("evaluation_id") or sha256_json({"episode_run_id": str(evaluation["episode_run_id"]), "event_type": event_type, "event_id": str(evaluation["event_id"]), "entry_date": normalize_date(evaluation["entry_date"])})[:32])
        with self.connect() as conn:
            self._require_episode_running(conn, str(evaluation["episode_run_id"]))
            link = conn.execute("SELECT episode_run_id FROM drawdown_episodes WHERE episode_id=?", (str(evaluation["episode_id"]),)).fetchone()
            if not link or str(link[0]) != str(evaluation["episode_run_id"]): raise ValueError("evaluation 必须指向同一 episode_run")
            before = conn.execute("SELECT 1 FROM episode_event_evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO episode_event_evaluations
                   (evaluation_id, episode_run_id, episode_id, event_type, event_id,
                    signal_variant, entry_date, entry_price, forward_1y, forward_3y,
                    forward_5y, max_adverse_1y, max_favorable_1y, episode_bottom_price,
                    entry_efficiency, entry_to_bottom_pct, days_to_bottom,
                    timing_regret_json, fast_recovery, missed_rebound,
                    matched_mechanical_event_id, status, reason, evaluation_payload_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    evaluation_id, str(evaluation["episode_run_id"]), str(evaluation["episode_id"]), event_type,
                    str(evaluation["event_id"]), evaluation.get("signal_variant"), normalize_date(evaluation["entry_date"]),
                    float(evaluation["entry_price"]), evaluation.get("forward_1y"), evaluation.get("forward_3y"), evaluation.get("forward_5y"),
                    evaluation.get("max_adverse_1y"), evaluation.get("max_favorable_1y"), evaluation.get("episode_bottom_price"),
                    evaluation.get("entry_efficiency"), evaluation.get("entry_to_bottom_pct"), evaluation.get("days_to_bottom"),
                    canonical_json(evaluation.get("timing_regret") or {}), evaluation.get("fast_recovery"), evaluation.get("missed_rebound"),
                    evaluation.get("matched_mechanical_event_id"), str(evaluation["status"]), str(evaluation["reason"]), canonical_json(evaluation.get("payload") or {}), utc_now(),
                ),
            )
        return evaluation_id, before is None

    def get_episode_event_evaluations(self, episode_run_id: str, *, event_type: str | None = None, limit: int = 1000000) -> list[dict[str, Any]]:
        clauses = ["episode_run_id=?"]; params: list[Any] = [str(episode_run_id)]
        if event_type is not None: clauses.append("event_type=?"); params.append(str(event_type).upper())
        params.append(min(max(int(limit), 1), 1000000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM episode_event_evaluations WHERE {' AND '.join(clauses)} ORDER BY entry_date, evaluation_id LIMIT ?", params
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for field, target, default in (("timing_regret_json", "timing_regret", {}), ("evaluation_payload_json", "payload", {})):
                try: item[target] = json.loads(item.pop(field))
                except (TypeError, json.JSONDecodeError): item[target] = default
            result.append(item)
        return result

    def append_episode_at10_assessment(self, assessment: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"episode_run_id", "episode_id", "mechanical_event_id", "signal_variant", "trigger_date", "category", "reached_20", "reached_30", "reached_40"}
        missing = required.difference(assessment)
        if missing: raise ValueError("episode_at10_assessment 缺少字段：" + ", ".join(sorted(missing)))
        if str(assessment["category"]) not in {"AGREE", "DELAY", "STRONGLY_OPPOSE", "UNAVAILABLE"}: raise ValueError("at10 category 无效")
        assessment_id = str(assessment.get("assessment_id") or sha256_json({"episode_run_id": str(assessment["episode_run_id"]), "episode_id": str(assessment["episode_id"]), "signal_variant": str(assessment["signal_variant"])})[:32])
        with self.connect() as conn:
            self._require_episode_running(conn, str(assessment["episode_run_id"]))
            link = conn.execute("SELECT episode_run_id FROM drawdown_mechanical_events WHERE mechanical_event_id=?", (str(assessment["mechanical_event_id"]),)).fetchone()
            if not link or str(link[0]) != str(assessment["episode_run_id"]): raise ValueError("at10 assessment 必须指向同一 episode_run")
            before = conn.execute("SELECT 1 FROM episode_at10_assessments WHERE assessment_id=?", (assessment_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO episode_at10_assessments
                   (assessment_id, episode_run_id, episode_id, mechanical_event_id,
                    signal_variant, trigger_date, signal_percentile, category,
                    reached_20, reached_30, reached_40, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    assessment_id, str(assessment["episode_run_id"]), str(assessment["episode_id"]), str(assessment["mechanical_event_id"]),
                    str(assessment["signal_variant"]), normalize_date(assessment["trigger_date"]), assessment.get("signal_percentile"), str(assessment["category"]),
                    int(bool(assessment["reached_20"])), int(bool(assessment["reached_30"])), int(bool(assessment["reached_40"])), utc_now(),
                ),
            )
        return assessment_id, before is None

    def get_episode_at10_assessments(self, episode_run_id: str, *, limit: int = 100000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM episode_at10_assessments WHERE episode_run_id=? ORDER BY trigger_date, signal_variant, assessment_id LIMIT ?",
                (str(episode_run_id), min(max(int(limit), 1), 500000)),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_episode_validation_report(self, episode_run_id: str, report: Mapping[str, Any]) -> tuple[str, bool]:
        payload = canonical_json(report)
        report_hash = sha256_json(report)
        report_id = sha256_json({"episode_run_id": str(episode_run_id), "report_hash": report_hash})[:32]
        with self.connect() as conn:
            before = conn.execute("SELECT 1 FROM episode_validation_reports WHERE episode_run_id=?", (str(episode_run_id),)).fetchone()
            if before:
                return report_id, False
            conn.execute(
                "INSERT INTO episode_validation_reports(episode_report_id,episode_run_id,report_hash,report_json,created_at) VALUES(?,?,?,?,?)",
                (report_id, str(episode_run_id), report_hash, payload, utc_now()),
            )
        return report_id, True

    def get_episode_validation_report(self, episode_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM episode_validation_reports WHERE episode_run_id=?", (str(episode_run_id),)).fetchone()
        if not row: return None
        item = dict(row)
        try: item["report"] = json.loads(item.pop("report_json"))
        except (TypeError, json.JSONDecodeError): item["report"] = {}
        return item

    # Phase 2D is deliberately separate from the Phase 2C composite/event
    # tables.  A drawdown event is copied into an overlay run, then its
    # contemporaneous features are appended.  Future outcomes, conditional
    # summaries and model comparisons have their own append-only tables.
    def create_drawdown_overlay_run(self, run: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        required = {
            "overlay_run_id", "market", "overlay_model_version", "phase2c_run_id",
            "start_date", "end_date", "overlay_model_config", "config_hash",
            "data_snapshot", "data_cutoff",
        }
        missing = required.difference(run)
        if missing:
            raise ValueError("drawdown_overlay_validation_run 缺少字段：" + ", ".join(sorted(missing)))
        if str(run["overlay_model_version"]) != "NDX_DRAWDOWN_OVERLAY_V1":
            raise ValueError("overlay_model_version 必须是 NDX_DRAWDOWN_OVERLAY_V1")
        config_hash = str(run["config_hash"]).lower()
        if len(config_hash) != 64 or any(c not in "0123456789abcdef" for c in config_hash):
            raise ValueError("overlay config_hash 必须是64位 SHA256")
        start_date = normalize_date(run["start_date"], field="start_date")
        end_date = normalize_date(run["end_date"], field="end_date")
        if start_date > end_date:
            raise ValueError("start_date 不能晚于 end_date")
        config = run["overlay_model_config"]
        snapshot = run["data_snapshot"]
        if not isinstance(config, Mapping):
            raise ValueError("overlay_model_config 必须是对象")
        if not isinstance(snapshot, (Mapping, list, str)):
            raise ValueError("data_snapshot 必须是对象、数组或字符串")
        overlay_id = str(run["overlay_run_id"])
        created_at = iso_timestamp(run.get("created_at") or utc_now(), "created_at")
        started_at = iso_timestamp(run.get("started_at") or created_at, "started_at")
        with self.connect() as conn:
            phase2c = conn.execute(
                "SELECT status, market FROM episode_validation_runs WHERE episode_run_id=?",
                (str(run["phase2c_run_id"]),),
            ).fetchone()
            if not phase2c:
                raise ValueError("overlay run 必须引用已存在的 Phase 2C run")
            if str(phase2c["status"]) != "COMPLETED":
                raise ValueError("overlay run 只能引用已完成的 Phase 2C run")
            if str(phase2c["market"]) != str(run["market"]).upper():
                raise ValueError("overlay run market 与 Phase 2C run 不一致")
            existing = conn.execute(
                "SELECT * FROM drawdown_overlay_validation_runs WHERE overlay_run_id=?", (overlay_id,)
            ).fetchone()
            if existing:
                immutable = {
                    "market": str(existing["market"]),
                    "overlay_model_version": str(existing["overlay_model_version"]),
                    "phase2c_run_id": str(existing["phase2c_run_id"]),
                    "start_date": str(existing["start_date"]),
                    "end_date": str(existing["end_date"]),
                    "config_hash": str(existing["config_hash"]),
                }
                expected = {
                    "market": str(run["market"]).upper(),
                    "overlay_model_version": str(run["overlay_model_version"]),
                    "phase2c_run_id": str(run["phase2c_run_id"]),
                    "start_date": start_date,
                    "end_date": end_date,
                    "config_hash": config_hash,
                }
                if immutable != expected:
                    raise ValueError("同一 overlay_run_id 的冻结配置不一致")
                return self._decode_drawdown_overlay_run(existing), False
            conn.execute(
                """INSERT INTO drawdown_overlay_validation_runs
                   (overlay_run_id, market, overlay_model_version, phase2c_run_id,
                    start_date, end_date, overlay_model_config_json, config_hash,
                    data_snapshot_json, data_cutoff, status, created_at,
                    started_at, completed_at, summary_json, error_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    overlay_id, str(run["market"]).upper(), str(run["overlay_model_version"]),
                    str(run["phase2c_run_id"]), start_date, end_date,
                    canonical_json(dict(config)), config_hash, canonical_json(snapshot),
                    as_of_datetime(run["data_cutoff"]), "RUNNING", created_at, started_at,
                    None, "{}", "{}",
                ),
            )
            row = conn.execute(
                "SELECT * FROM drawdown_overlay_validation_runs WHERE overlay_run_id=?", (overlay_id,)
            ).fetchone()
        return self._decode_drawdown_overlay_run(row), True

    @staticmethod
    def _decode_drawdown_overlay_run(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target in (
            ("overlay_model_config_json", "overlay_model_config"),
            ("data_snapshot_json", "data_snapshot"),
            ("summary_json", "summary"),
            ("error_json", "error"),
        ):
            try:
                item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError):
                item[target] = {}
        return item

    def get_drawdown_overlay_run(self, overlay_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM drawdown_overlay_validation_runs WHERE overlay_run_id=?", (str(overlay_run_id),)
            ).fetchone()
        return self._decode_drawdown_overlay_run(row) if row else None

    def get_drawdown_overlay_runs(self, *, market: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if market is not None:
            clauses.append("market=?"); params.append(str(market).upper())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(max(int(limit), 1), 1000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM drawdown_overlay_validation_runs{where} ORDER BY created_at DESC, overlay_run_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._decode_drawdown_overlay_run(row) for row in rows]

    def complete_drawdown_overlay_run(
        self,
        overlay_run_id: str,
        *,
        status: str = "COMPLETED",
        summary: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        completed_at: Any | None = None,
    ) -> dict[str, Any]:
        status = str(status).upper()
        if status not in {"COMPLETED", "FAILED"}:
            raise ValueError("drawdown overlay run 完成状态无效")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status FROM drawdown_overlay_validation_runs WHERE overlay_run_id=?", (str(overlay_run_id),)
            ).fetchone()
            if not row:
                raise ValueError("drawdown_overlay_validation_run 不存在")
            if str(row[0]) != "RUNNING":
                raise ValueError("已结束的 drawdown overlay run 不可再次完成或修改")
            conn.execute(
                """UPDATE drawdown_overlay_validation_runs
                   SET status=?, completed_at=?, summary_json=?, error_json=?
                   WHERE overlay_run_id=? AND status='RUNNING'""",
                (
                    status, iso_timestamp(completed_at or utc_now(), "completed_at"),
                    canonical_json(summary or {}), canonical_json(error or {}), str(overlay_run_id),
                ),
            )
            updated = conn.execute(
                "SELECT * FROM drawdown_overlay_validation_runs WHERE overlay_run_id=?", (str(overlay_run_id),)
            ).fetchone()
        return self._decode_drawdown_overlay_run(updated)

    @staticmethod
    def _require_drawdown_overlay_running(conn: sqlite3.Connection, overlay_run_id: str) -> None:
        row = conn.execute(
            "SELECT status FROM drawdown_overlay_validation_runs WHERE overlay_run_id=?", (str(overlay_run_id),)
        ).fetchone()
        if not row:
            raise ValueError("drawdown_overlay_validation_run 不存在")
        if str(row[0]) != "RUNNING":
            raise ValueError("已结束 drawdown overlay run 不能追加数据")

    def append_drawdown_overlay_event(self, event: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "overlay_run_id", "phase2c_run_id", "mechanical_event_id", "episode_id", "market",
            "threshold", "drawdown_band", "event_date", "event_price", "drawdown",
            "rsi14_status", "distance_ma200_status", "vxn_status", "real_yield_status", "nfci_status",
            "input_observation_ids", "input_hash",
        }
        missing = required.difference(event)
        if missing:
            raise ValueError("drawdown_overlay_event 缺少字段：" + ", ".join(sorted(missing)))
        threshold = float(event["threshold"])
        if threshold not in {0.1, 0.2, 0.3, 0.4, 0.5}:
            raise ValueError("overlay event 的回撤档位无效")
        if str(event["drawdown_band"]) not in {"MILD", "MEDIUM", "DEEP"}:
            raise ValueError("overlay drawdown_band 无效")
        statuses = {str(event[name]) for name in ("rsi14_status", "distance_ma200_status", "vxn_status", "real_yield_status", "nfci_status")}
        if not statuses.issubset({"SUPPORTIVE", "NEUTRAL", "CAUTION", "UNAVAILABLE"}):
            raise ValueError("overlay status 无效")
        ids = sorted({str(x) for x in event["input_observation_ids"]})
        expected_hash = sha256_json(ids)
        if str(event["input_hash"]).lower() != expected_hash:
            raise ValueError("drawdown_overlay_event input_hash 与输入观察不一致")
        event_id = str(event.get("overlay_event_id") or sha256_json({
            "overlay_run_id": str(event["overlay_run_id"]), "mechanical_event_id": str(event["mechanical_event_id"]),
        })[:32])
        with self.connect() as conn:
            self._require_drawdown_overlay_running(conn, str(event["overlay_run_id"]))
            link = conn.execute(
                "SELECT episode_run_id, episode_id FROM drawdown_mechanical_events WHERE mechanical_event_id=?",
                (str(event["mechanical_event_id"]),),
            ).fetchone()
            if not link or str(link[0]) != str(event["phase2c_run_id"]) or str(link[1]) != str(event["episode_id"]):
                raise ValueError("overlay event 必须引用同一 Phase 2C mechanical event")
            before = conn.execute(
                "SELECT 1 FROM drawdown_overlay_events WHERE overlay_event_id=?", (event_id,)
            ).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO drawdown_overlay_events
                   (overlay_event_id, overlay_run_id, phase2c_run_id, mechanical_event_id,
                    episode_id, market, threshold, drawdown_band, event_date, event_price,
                    drawdown, current_drawdown, days_since_peak,
                    rsi14, rsi14_percentile, rsi14_opportunity_rank, rsi14_status,
                    distance_ma200, distance_ma200_percentile, distance_ma200_opportunity_rank, distance_ma200_status,
                    vxn, vxn_percentile, vxn_opportunity_rank, vxn_status,
                    real_yield, real_yield_percentile, real_yield_opportunity_rank, real_yield_status,
                    nfci, nfci_percentile, nfci_opportunity_rank, nfci_status,
                    source_state_id, input_observation_ids_json, input_hash, overlay_payload_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, str(event["overlay_run_id"]), str(event["phase2c_run_id"]), str(event["mechanical_event_id"]),
                    str(event["episode_id"]), str(event["market"]).upper(), threshold, str(event["drawdown_band"]),
                    normalize_date(event["event_date"], field="event_date"), float(event["event_price"]), float(event["drawdown"]),
                    event.get("current_drawdown"), event.get("days_since_peak"),
                    event.get("rsi14"), event.get("rsi14_percentile"), event.get("rsi14_opportunity_rank"), str(event["rsi14_status"]),
                    event.get("distance_ma200"), event.get("distance_ma200_percentile"), event.get("distance_ma200_opportunity_rank"), str(event["distance_ma200_status"]),
                    event.get("vxn"), event.get("vxn_percentile"), event.get("vxn_opportunity_rank"), str(event["vxn_status"]),
                    event.get("real_yield"), event.get("real_yield_percentile"), event.get("real_yield_opportunity_rank"), str(event["real_yield_status"]),
                    event.get("nfci"), event.get("nfci_percentile"), event.get("nfci_opportunity_rank"), str(event["nfci_status"]),
                    event.get("source_state_id"), canonical_json(ids), expected_hash, canonical_json(event.get("payload") or {}), utc_now(),
                ),
            )
        return event_id, before is None

    def get_drawdown_overlay_events(self, overlay_run_id: str, *, drawdown_band: str | None = None, limit: int = 500000) -> list[dict[str, Any]]:
        clauses = ["overlay_run_id=?"]; params: list[Any] = [str(overlay_run_id)]
        if drawdown_band is not None:
            clauses.append("drawdown_band=?"); params.append(str(drawdown_band))
        params.append(min(max(int(limit), 1), 1000000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM drawdown_overlay_events WHERE {' AND '.join(clauses)} ORDER BY event_date, threshold, overlay_event_id LIMIT ?",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try: item["input_observation_ids"] = json.loads(item.pop("input_observation_ids_json"))
            except (TypeError, json.JSONDecodeError): item["input_observation_ids"] = []
            try: item["payload"] = json.loads(item.pop("overlay_payload_json"))
            except (TypeError, json.JSONDecodeError): item["payload"] = {}
            result.append(item)
        return result

    def append_drawdown_overlay_evaluation(self, evaluation: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "overlay_run_id", "overlay_event_id", "phase2c_run_id", "episode_id", "event_date", "entry_price",
            "timing_regret", "future_observation_ids", "status", "reason",
        }
        missing = required.difference(evaluation)
        if missing:
            raise ValueError("drawdown_overlay_evaluation 缺少字段：" + ", ".join(sorted(missing)))
        eval_id = str(evaluation.get("overlay_evaluation_id") or sha256_json({
            "overlay_run_id": str(evaluation["overlay_run_id"]), "overlay_event_id": str(evaluation["overlay_event_id"]),
        })[:32])
        future_ids = sorted({str(x) for x in evaluation["future_observation_ids"]})
        with self.connect() as conn:
            self._require_drawdown_overlay_running(conn, str(evaluation["overlay_run_id"]))
            link = conn.execute(
                "SELECT overlay_run_id, phase2c_run_id, episode_id FROM drawdown_overlay_events WHERE overlay_event_id=?",
                (str(evaluation["overlay_event_id"]),),
            ).fetchone()
            if (not link or str(link[0]) != str(evaluation["overlay_run_id"])
                    or str(link[1]) != str(evaluation["phase2c_run_id"])
                    or str(link[2]) != str(evaluation["episode_id"])):
                raise ValueError("overlay evaluation 必须指向同一 overlay event")
            before = conn.execute(
                "SELECT 1 FROM drawdown_overlay_event_evaluations WHERE overlay_evaluation_id=?", (eval_id,)
            ).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO drawdown_overlay_event_evaluations
                   (overlay_evaluation_id, overlay_run_id, overlay_event_id, phase2c_run_id,
                    episode_id, event_date, entry_price, forward_1y, forward_3y, forward_5y,
                    max_adverse_1y, max_favorable_1y, episode_bottom_price, entry_efficiency,
                    entry_to_bottom_pct, days_to_bottom, timing_regret_json,
                    future_observation_ids_json, status, reason, evaluation_payload_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    eval_id, str(evaluation["overlay_run_id"]), str(evaluation["overlay_event_id"]),
                    str(evaluation["phase2c_run_id"]), str(evaluation["episode_id"]), normalize_date(evaluation["event_date"]),
                    float(evaluation["entry_price"]), evaluation.get("forward_1y"), evaluation.get("forward_3y"), evaluation.get("forward_5y"),
                    evaluation.get("max_adverse_1y"), evaluation.get("max_favorable_1y"), evaluation.get("episode_bottom_price"),
                    evaluation.get("entry_efficiency"), evaluation.get("entry_to_bottom_pct"), evaluation.get("days_to_bottom"),
                    canonical_json(evaluation.get("timing_regret") or {}), canonical_json(future_ids), str(evaluation["status"]),
                    str(evaluation["reason"]), canonical_json(evaluation.get("payload") or {}), utc_now(),
                ),
            )
        return eval_id, before is None

    def get_drawdown_overlay_evaluations(self, overlay_run_id: str, *, limit: int = 1000000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM drawdown_overlay_event_evaluations WHERE overlay_run_id=? ORDER BY event_date, overlay_evaluation_id LIMIT ?",
                (str(overlay_run_id), min(max(int(limit), 1), 1000000)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for field, target, default in (
                ("timing_regret_json", "timing_regret", {}),
                ("future_observation_ids_json", "future_observation_ids", []),
                ("evaluation_payload_json", "payload", {}),
            ):
                try: item[target] = json.loads(item.pop(field))
                except (TypeError, json.JSONDecodeError): item[target] = default
            result.append(item)
        return result

    def append_drawdown_overlay_conditional_result(self, result: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"overlay_run_id", "overlay_name", "drawdown_band", "sample_count", "available_count", "status", "result"}
        missing = required.difference(result)
        if missing: raise ValueError("drawdown_overlay_conditional_result 缺少字段：" + ", ".join(sorted(missing)))
        if str(result["overlay_name"]) not in {"RSI", "MA200_DISTANCE", "VXN", "REAL_YIELD", "NFCI"}:
            raise ValueError("overlay_name 无效")
        if str(result["drawdown_band"]) not in {"ALL", "MILD", "MEDIUM", "DEEP"}:
            raise ValueError("drawdown_band 无效")
        result_id = str(result.get("conditional_result_id") or sha256_json({
            "overlay_run_id": str(result["overlay_run_id"]), "overlay_name": str(result["overlay_name"]), "drawdown_band": str(result["drawdown_band"]),
        })[:32])
        with self.connect() as conn:
            self._require_drawdown_overlay_running(conn, str(result["overlay_run_id"]))
            before = conn.execute("SELECT 1 FROM drawdown_overlay_conditional_results WHERE conditional_result_id=?", (result_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO drawdown_overlay_conditional_results
                   (conditional_result_id, overlay_run_id, overlay_name, drawdown_band,
                    sample_count, available_count, status, result_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (result_id, str(result["overlay_run_id"]), str(result["overlay_name"]), str(result["drawdown_band"]), int(result["sample_count"]), int(result["available_count"]), str(result["status"]), canonical_json(result["result"]), utc_now()),
            )
        return result_id, before is None

    def get_drawdown_overlay_conditional_results(self, overlay_run_id: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM drawdown_overlay_conditional_results WHERE overlay_run_id=? ORDER BY overlay_name, drawdown_band LIMIT ?",
                (str(overlay_run_id), min(max(int(limit), 1), 10000)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try: item["result"] = json.loads(item.pop("result_json"))
            except (TypeError, json.JSONDecodeError): item["result"] = {}
            result.append(item)
        return result

    def append_drawdown_overlay_loeo_result(self, result: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"overlay_run_id", "overlay_name", "drawdown_band", "held_out_episode_id", "sample_count", "status", "result"}
        missing = required.difference(result)
        if missing: raise ValueError("drawdown_overlay_loeo_result 缺少字段：" + ", ".join(sorted(missing)))
        result_id = str(result.get("loeo_result_id") or sha256_json({
            "overlay_run_id": str(result["overlay_run_id"]), "overlay_name": str(result["overlay_name"]), "drawdown_band": str(result["drawdown_band"]), "held_out_episode_id": str(result["held_out_episode_id"]),
        })[:32])
        with self.connect() as conn:
            self._require_drawdown_overlay_running(conn, str(result["overlay_run_id"]))
            episode = conn.execute("SELECT 1 FROM drawdown_episodes WHERE episode_id=?", (str(result["held_out_episode_id"]),)).fetchone()
            if not episode: raise ValueError("loeo held_out_episode_id 不存在")
            before = conn.execute("SELECT 1 FROM drawdown_overlay_loeo_results WHERE loeo_result_id=?", (result_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO drawdown_overlay_loeo_results
                   (loeo_result_id, overlay_run_id, overlay_name, drawdown_band,
                    held_out_episode_id, sample_count, status, result_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (result_id, str(result["overlay_run_id"]), str(result["overlay_name"]), str(result["drawdown_band"]), str(result["held_out_episode_id"]), int(result["sample_count"]), str(result["status"]), canonical_json(result["result"]), utc_now()),
            )
        return result_id, before is None

    def get_drawdown_overlay_loeo_results(self, overlay_run_id: str, *, limit: int = 100000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM drawdown_overlay_loeo_results WHERE overlay_run_id=? ORDER BY overlay_name, drawdown_band, held_out_episode_id LIMIT ?",
                (str(overlay_run_id), min(max(int(limit), 1), 500000)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try: item["result"] = json.loads(item.pop("result_json"))
            except (TypeError, json.JSONDecodeError): item["result"] = {}
            result.append(item)
        return result

    def append_drawdown_overlay_model_result(self, result: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"overlay_run_id", "model_name", "drawdown_band", "outcome_name", "sample_count", "status", "result"}
        missing = required.difference(result)
        if missing: raise ValueError("drawdown_overlay_model_result 缺少字段：" + ", ".join(sorted(missing)))
        result_id = str(result.get("model_result_id") or sha256_json({
            "overlay_run_id": str(result["overlay_run_id"]), "model_name": str(result["model_name"]), "drawdown_band": str(result["drawdown_band"]), "outcome_name": str(result["outcome_name"]),
        })[:32])
        with self.connect() as conn:
            self._require_drawdown_overlay_running(conn, str(result["overlay_run_id"]))
            before = conn.execute("SELECT 1 FROM drawdown_overlay_model_results WHERE model_result_id=?", (result_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO drawdown_overlay_model_results
                   (model_result_id, overlay_run_id, model_name, drawdown_band,
                    outcome_name, sample_count, status, result_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (result_id, str(result["overlay_run_id"]), str(result["model_name"]), str(result["drawdown_band"]), str(result["outcome_name"]), int(result["sample_count"]), str(result["status"]), canonical_json(result["result"]), utc_now()),
            )
        return result_id, before is None

    def get_drawdown_overlay_model_results(self, overlay_run_id: str, *, limit: int = 10000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM drawdown_overlay_model_results WHERE overlay_run_id=? ORDER BY model_name, drawdown_band, outcome_name LIMIT ?",
                (str(overlay_run_id), min(max(int(limit), 1), 50000)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try: item["result"] = json.loads(item.pop("result_json"))
            except (TypeError, json.JSONDecodeError): item["result"] = {}
            result.append(item)
        return result

    def record_drawdown_overlay_report(self, overlay_run_id: str, report: Mapping[str, Any]) -> tuple[str, bool]:
        payload = canonical_json(report)
        report_hash = sha256_json(report)
        report_id = sha256_json({"overlay_run_id": str(overlay_run_id), "report_hash": report_hash})[:32]
        with self.connect() as conn:
            before = conn.execute("SELECT 1 FROM drawdown_overlay_validation_reports WHERE overlay_run_id=?", (str(overlay_run_id),)).fetchone()
            if before:
                return report_id, False
            conn.execute(
                "INSERT INTO drawdown_overlay_validation_reports(overlay_report_id,overlay_run_id,report_hash,report_json,created_at) VALUES(?,?,?,?,?)",
                (report_id, str(overlay_run_id), report_hash, payload, utc_now()),
            )
        return report_id, True

    def get_drawdown_overlay_report(self, overlay_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM drawdown_overlay_validation_reports WHERE overlay_run_id=?", (str(overlay_run_id),)).fetchone()
        if not row: return None
        item = dict(row)
        try: item["report"] = json.loads(item.pop("report_json"))
        except (TypeError, json.JSONDecodeError): item["report"] = {}
        return item

    # ------------------------------------------------------------------
    # Phase 2E tactical drawdown repository boundary
    # ------------------------------------------------------------------

    def create_tactical_drawdown_run(self, run: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        required = {
            "tactical_run_id", "market", "tactical_model_version", "phase2c_run_id",
            "start_date", "end_date", "tactical_model_config", "config_hash",
            "data_snapshot", "data_cutoff",
        }
        missing = required.difference(run)
        if missing:
            raise ValueError("tactical_drawdown_run 缺少字段：" + ", ".join(sorted(missing)))
        if str(run["tactical_model_version"]) != "NDX_TACTICAL_DRAWDOWN_V1":
            raise ValueError("tactical_model_version 必须是 NDX_TACTICAL_DRAWDOWN_V1")
        market = str(run["market"]).upper()
        if market != "NDX":
            raise ValueError("Phase 2E 当前只允许 NDX")
        config_hash = str(run["config_hash"]).lower()
        if len(config_hash) != 64 or any(char not in "0123456789abcdef" for char in config_hash):
            raise ValueError("tactical config_hash 必须是64位 SHA256")
        start_date = normalize_date(run["start_date"], field="start_date")
        end_date = normalize_date(run["end_date"], field="end_date")
        if start_date > end_date:
            raise ValueError("tactical start_date 不能晚于 end_date")
        tactical_id = str(run["tactical_run_id"])
        created_at = utc_now()
        started_at = iso_timestamp(run.get("started_at") or created_at, "started_at")
        with self.connect() as conn:
            upstream = conn.execute(
                "SELECT status, market FROM episode_validation_runs WHERE episode_run_id=?",
                (str(run["phase2c_run_id"]),),
            ).fetchone()
            if not upstream or str(upstream[0]) != "COMPLETED" or str(upstream[1]).upper() != market:
                raise ValueError("tactical run 必须引用同一市场的已完成 Phase 2C run")
            existing = conn.execute(
                "SELECT * FROM tactical_drawdown_validation_runs WHERE tactical_run_id=?", (tactical_id,)
            ).fetchone()
            if existing:
                item = self._decode_tactical_drawdown_run(existing)
                immutable = (
                    item["tactical_model_version"], item["phase2c_run_id"], item["start_date"], item["end_date"],
                    item["config_hash"], canonical_json(item["tactical_model_config"]),
                )
                expected = (
                    str(run["tactical_model_version"]), str(run["phase2c_run_id"]), start_date, end_date,
                    config_hash, canonical_json(dict(run["tactical_model_config"])),
                )
                if immutable != expected:
                    raise ValueError("同一 tactical_run_id 的冻结配置不一致")
                return item, False
            conn.execute(
                """INSERT INTO tactical_drawdown_validation_runs
                   (tactical_run_id, market, tactical_model_version, phase2c_run_id,
                    start_date, end_date, tactical_model_config_json, config_hash,
                    data_snapshot_json, data_cutoff, status, created_at, started_at,
                    completed_at, summary_json, error_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    tactical_id, market, str(run["tactical_model_version"]), str(run["phase2c_run_id"]),
                    start_date, end_date, canonical_json(dict(run["tactical_model_config"])), config_hash,
                    canonical_json(dict(run["data_snapshot"])), as_of_datetime(run["data_cutoff"]),
                    "RUNNING", created_at, started_at, None, "{}", "{}",
                ),
            )
            row = conn.execute(
                "SELECT * FROM tactical_drawdown_validation_runs WHERE tactical_run_id=?", (tactical_id,)
            ).fetchone()
        return self._decode_tactical_drawdown_run(row), True

    @staticmethod
    def _decode_tactical_drawdown_run(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target in (
            ("tactical_model_config_json", "tactical_model_config"),
            ("data_snapshot_json", "data_snapshot"),
            ("summary_json", "summary"),
            ("error_json", "error"),
        ):
            try:
                item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError):
                item[target] = {}
        return item

    def get_tactical_drawdown_run(self, tactical_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tactical_drawdown_validation_runs WHERE tactical_run_id=?", (str(tactical_run_id),)
            ).fetchone()
        return self._decode_tactical_drawdown_run(row) if row else None

    def get_tactical_drawdown_runs(self, *, market: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if market is not None:
            clauses.append("market=?"); params.append(str(market).upper())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(max(int(limit), 1), 1000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tactical_drawdown_validation_runs{where} ORDER BY created_at DESC, tactical_run_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._decode_tactical_drawdown_run(row) for row in rows]

    def complete_tactical_drawdown_run(
        self,
        tactical_run_id: str,
        *,
        status: str = "COMPLETED",
        summary: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        completed_at: Any | None = None,
    ) -> dict[str, Any]:
        status = str(status).upper()
        if status not in {"COMPLETED", "FAILED"}:
            raise ValueError("tactical drawdown run 完成状态无效")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status FROM tactical_drawdown_validation_runs WHERE tactical_run_id=?", (str(tactical_run_id),)
            ).fetchone()
            if not row:
                raise ValueError("tactical_drawdown_validation_run 不存在")
            if str(row[0]) != "RUNNING":
                raise ValueError("已结束的 tactical drawdown run 不可再次完成或修改")
            conn.execute(
                """UPDATE tactical_drawdown_validation_runs
                   SET status=?, completed_at=?, summary_json=?, error_json=?
                   WHERE tactical_run_id=? AND status='RUNNING'""",
                (
                    status, iso_timestamp(completed_at or utc_now(), "completed_at"),
                    canonical_json(summary or {}), canonical_json(error or {}), str(tactical_run_id),
                ),
            )
            updated = conn.execute(
                "SELECT * FROM tactical_drawdown_validation_runs WHERE tactical_run_id=?", (str(tactical_run_id),)
            ).fetchone()
        return self._decode_tactical_drawdown_run(updated)

    @staticmethod
    def _require_tactical_running(conn: sqlite3.Connection, tactical_run_id: str) -> None:
        row = conn.execute(
            "SELECT status FROM tactical_drawdown_validation_runs WHERE tactical_run_id=?", (str(tactical_run_id),)
        ).fetchone()
        if not row:
            raise ValueError("tactical_drawdown_validation_run 不存在")
        if str(row[0]) != "RUNNING":
            raise ValueError("已结束 tactical drawdown run 不能追加数据")

    @staticmethod
    def _decode_tactical_state(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target, default in (
            ("triggered_bands_json", "triggered_bands", []),
            ("input_observation_ids_json", "input_observation_ids", []),
            ("state_payload_json", "payload", {}),
        ):
            try: item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError): item[target] = default
        return item

    def append_tactical_drawdown_state(self, state: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "tactical_run_id", "tactical_cycle_id", "market", "as_of_date", "close_price",
            "rolling_high_252", "rolling_high_date", "tactical_peak_price", "tactical_peak_date",
            "tactical_drawdown", "days_since_peak", "new_tactical_peak", "input_observation_ids", "input_hash",
        }
        missing = required.difference(state)
        if missing: raise ValueError("tactical_drawdown_state 缺少字段：" + ", ".join(sorted(missing)))
        ids = sorted({str(value) for value in state["input_observation_ids"]})
        expected_hash = sha256_json(ids)
        if str(state["input_hash"]).lower() != expected_hash: raise ValueError("tactical state input_hash 不一致")
        if int(bool(state["new_tactical_peak"])) not in {0, 1}: raise ValueError("new_tactical_peak 无效")
        state_id = str(state.get("tactical_state_id") or sha256_json({"run": str(state["tactical_run_id"]), "cycle": str(state["tactical_cycle_id"]), "date": normalize_date(state["as_of_date"])})[:32])
        with self.connect() as conn:
            self._require_tactical_running(conn, str(state["tactical_run_id"]))
            before = conn.execute("SELECT 1 FROM tactical_drawdown_states WHERE tactical_state_id=?", (state_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO tactical_drawdown_states
                   (tactical_state_id, tactical_run_id, tactical_cycle_id, macro_episode_id,
                    market, as_of_date, close_price, rolling_high_252, rolling_high_date,
                    tactical_peak_price, tactical_peak_date, tactical_drawdown, days_since_peak,
                    new_tactical_peak, reset_reason, triggered_bands_json, source_observation_id,
                    input_observation_ids_json, input_hash, state_payload_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    state_id, str(state["tactical_run_id"]), str(state["tactical_cycle_id"]), state.get("macro_episode_id"),
                    str(state["market"]).upper(), normalize_date(state["as_of_date"]), float(state["close_price"]),
                    float(state["rolling_high_252"]), normalize_date(state["rolling_high_date"]),
                    float(state["tactical_peak_price"]), normalize_date(state["tactical_peak_date"]), float(state["tactical_drawdown"]),
                    int(state["days_since_peak"]), int(bool(state["new_tactical_peak"])), state.get("reset_reason"),
                    canonical_json(state.get("triggered_bands") or []), state.get("source_observation_id"), canonical_json(ids), expected_hash,
                    canonical_json(state.get("payload") or {}), utc_now(),
                ),
            )
        return state_id, before is None

    def get_tactical_drawdown_states(self, tactical_run_id: str, *, limit: int = 1000000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tactical_drawdown_states WHERE tactical_run_id=? ORDER BY as_of_date, tactical_state_id LIMIT ?",
                (str(tactical_run_id), min(max(int(limit), 1), 2000000)),
            ).fetchall()
        return [self._decode_tactical_state(row) for row in rows]

    def append_tactical_drawdown_cycle(self, cycle: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "tactical_run_id", "market", "peak_date", "peak_price", "start_date", "end_date",
            "recovery_state", "data_end_date",
        }
        missing = required.difference(cycle)
        if missing: raise ValueError("tactical_drawdown_cycle 缺少字段：" + ", ".join(sorted(missing)))
        cycle_id = str(cycle.get("tactical_cycle_id") or sha256_json({"run": str(cycle["tactical_run_id"]), "peak_date": normalize_date(cycle["peak_date"]), "peak_price": round(float(cycle["peak_price"]), 10)})[:32])
        with self.connect() as conn:
            self._require_tactical_running(conn, str(cycle["tactical_run_id"]))
            if cycle.get("macro_episode_id"):
                parent = conn.execute("SELECT 1 FROM drawdown_episodes WHERE episode_id=?", (str(cycle["macro_episode_id"]),)).fetchone()
                if not parent: raise ValueError("tactical cycle 的 macro_episode_id 不存在")
            before = conn.execute("SELECT 1 FROM tactical_drawdown_cycles WHERE tactical_cycle_id=?", (cycle_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO tactical_drawdown_cycles
                   (tactical_cycle_id, tactical_run_id, macro_episode_id, market,
                    peak_date, peak_price, start_date, end_date, max_drawdown,
                    max_drawdown_date, recovery_state, data_end_date, cycle_payload_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cycle_id, str(cycle["tactical_run_id"]), cycle.get("macro_episode_id"), str(cycle["market"]).upper(),
                    normalize_date(cycle["peak_date"]), float(cycle["peak_price"]), normalize_date(cycle["start_date"]),
                    normalize_date(cycle["end_date"]), cycle.get("max_drawdown"), normalize_date(cycle["max_drawdown_date"]) if cycle.get("max_drawdown_date") else None,
                    str(cycle["recovery_state"]), normalize_date(cycle["data_end_date"]), canonical_json(cycle.get("payload") or {}), utc_now(),
                ),
            )
        return cycle_id, before is None

    @staticmethod
    def _decode_tactical_cycle(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        try: item["payload"] = json.loads(item.pop("cycle_payload_json"))
        except (TypeError, json.JSONDecodeError): item["payload"] = {}
        return item

    def get_tactical_drawdown_cycles(self, tactical_run_id: str, *, limit: int = 100000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tactical_drawdown_cycles WHERE tactical_run_id=? ORDER BY start_date, tactical_cycle_id LIMIT ?",
                (str(tactical_run_id), min(max(int(limit), 1), 500000)),
            ).fetchall()
        return [self._decode_tactical_cycle(row) for row in rows]

    def append_tactical_drawdown_event(self, event: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "tactical_run_id", "tactical_cycle_id", "market", "threshold", "drawdown_band",
            "event_date", "event_price", "drawdown", "rsi14_status", "distance_ma200_status",
            "vxn_status", "real_yield_status", "nfci_status", "input_observation_ids", "input_hash",
        }
        missing = required.difference(event)
        if missing: raise ValueError("tactical_drawdown_event 缺少字段：" + ", ".join(sorted(missing)))
        threshold = float(event["threshold"])
        if threshold not in {0.1, 0.2, 0.3, 0.4, 0.5}: raise ValueError("tactical event 的回撤档位无效")
        if str(event["drawdown_band"]) not in {"MILD", "MEDIUM", "DEEP"}: raise ValueError("tactical drawdown_band 无效")
        statuses = {str(event[name]) for name in ("rsi14_status", "distance_ma200_status", "vxn_status", "real_yield_status", "nfci_status")}
        if not statuses.issubset({"SUPPORTIVE", "NEUTRAL", "CAUTION", "UNAVAILABLE"}): raise ValueError("tactical overlay status 无效")
        ids = sorted({str(value) for value in event["input_observation_ids"]})
        expected_hash = sha256_json(ids)
        if str(event["input_hash"]).lower() != expected_hash: raise ValueError("tactical event input_hash 不一致")
        event_id = str(event.get("tactical_event_id") or sha256_json({"run": str(event["tactical_run_id"]), "cycle": str(event["tactical_cycle_id"]), "threshold": threshold})[:32])
        with self.connect() as conn:
            self._require_tactical_running(conn, str(event["tactical_run_id"]))
            cycle = conn.execute("SELECT tactical_run_id FROM tactical_drawdown_cycles WHERE tactical_cycle_id=?", (str(event["tactical_cycle_id"]),)).fetchone()
            if not cycle or str(cycle[0]) != str(event["tactical_run_id"]): raise ValueError("tactical event 必须引用同一 run 的 cycle")
            before = conn.execute("SELECT 1 FROM tactical_drawdown_events WHERE tactical_event_id=?", (event_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO tactical_drawdown_events
                   (tactical_event_id, tactical_run_id, tactical_cycle_id, macro_episode_id,
                    market, threshold, drawdown_band, event_date, event_price, drawdown,
                    rsi14, rsi14_percentile, rsi14_opportunity_rank, rsi14_status,
                    distance_ma200, distance_ma200_percentile, distance_ma200_opportunity_rank, distance_ma200_status,
                    vxn, vxn_percentile, vxn_opportunity_rank, vxn_status,
                    real_yield, real_yield_percentile, real_yield_opportunity_rank, real_yield_status,
                    nfci, nfci_percentile, nfci_opportunity_rank, nfci_status,
                    source_state_id, source_observation_id, input_observation_ids_json, input_hash, event_payload_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, str(event["tactical_run_id"]), str(event["tactical_cycle_id"]), event.get("macro_episode_id"), str(event["market"]).upper(),
                    threshold, str(event["drawdown_band"]), normalize_date(event["event_date"]), float(event["event_price"]), float(event["drawdown"]),
                    event.get("rsi14"), event.get("rsi14_percentile"), event.get("rsi14_opportunity_rank"), str(event["rsi14_status"]),
                    event.get("distance_ma200"), event.get("distance_ma200_percentile"), event.get("distance_ma200_opportunity_rank"), str(event["distance_ma200_status"]),
                    event.get("vxn"), event.get("vxn_percentile"), event.get("vxn_opportunity_rank"), str(event["vxn_status"]),
                    event.get("real_yield"), event.get("real_yield_percentile"), event.get("real_yield_opportunity_rank"), str(event["real_yield_status"]),
                    event.get("nfci"), event.get("nfci_percentile"), event.get("nfci_opportunity_rank"), str(event["nfci_status"]),
                    event.get("source_state_id"), event.get("source_observation_id"), canonical_json(ids), expected_hash, canonical_json(event.get("payload") or {}), utc_now(),
                ),
            )
        return event_id, before is None

    @staticmethod
    def _decode_tactical_event(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target, default in (
            ("input_observation_ids_json", "input_observation_ids", []),
            ("event_payload_json", "payload", {}),
        ):
            try: item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError): item[target] = default
        return item

    def get_tactical_drawdown_events(self, tactical_run_id: str, *, drawdown_band: str | None = None, limit: int = 500000) -> list[dict[str, Any]]:
        clauses = ["tactical_run_id=?"]; params: list[Any] = [str(tactical_run_id)]
        if drawdown_band is not None: clauses.append("drawdown_band=?"); params.append(str(drawdown_band))
        params.append(min(max(int(limit), 1), 1000000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tactical_drawdown_events WHERE {' AND '.join(clauses)} ORDER BY event_date, threshold, tactical_event_id LIMIT ?", params
            ).fetchall()
        return [self._decode_tactical_event(row) for row in rows]

    def append_tactical_drawdown_evaluation(self, evaluation: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "tactical_run_id", "tactical_event_id", "tactical_cycle_id", "event_date", "entry_price",
            "timing_regret", "future_observation_ids", "status", "reason",
        }
        missing = required.difference(evaluation)
        if missing: raise ValueError("tactical_drawdown_evaluation 缺少字段：" + ", ".join(sorted(missing)))
        eval_id = str(evaluation.get("tactical_evaluation_id") or sha256_json({"run": str(evaluation["tactical_run_id"]), "event": str(evaluation["tactical_event_id"])})[:32])
        future_ids = sorted({str(value) for value in evaluation["future_observation_ids"]})
        with self.connect() as conn:
            self._require_tactical_running(conn, str(evaluation["tactical_run_id"]))
            link = conn.execute("SELECT tactical_run_id, tactical_cycle_id, macro_episode_id FROM tactical_drawdown_events WHERE tactical_event_id=?", (str(evaluation["tactical_event_id"]),)).fetchone()
            if (not link or str(link[0]) != str(evaluation["tactical_run_id"]) or str(link[1]) != str(evaluation["tactical_cycle_id"])):
                raise ValueError("tactical evaluation 必须指向同一 tactical event")
            before = conn.execute("SELECT 1 FROM tactical_drawdown_event_evaluations WHERE tactical_evaluation_id=?", (eval_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO tactical_drawdown_event_evaluations
                   (tactical_evaluation_id, tactical_run_id, tactical_event_id, tactical_cycle_id,
                    macro_episode_id, event_date, entry_price, forward_1y, forward_3y, forward_5y,
                    max_adverse_1y, max_favorable_1y, cycle_bottom_price, entry_efficiency,
                    entry_to_bottom_pct, days_to_bottom, timing_regret_json, future_observation_ids_json,
                    recovery_date, recovery_days, status, reason, evaluation_payload_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    eval_id, str(evaluation["tactical_run_id"]), str(evaluation["tactical_event_id"]), str(evaluation["tactical_cycle_id"]),
                    evaluation.get("macro_episode_id"), normalize_date(evaluation["event_date"]), float(evaluation["entry_price"]),
                    evaluation.get("forward_1y"), evaluation.get("forward_3y"), evaluation.get("forward_5y"), evaluation.get("max_adverse_1y"),
                    evaluation.get("max_favorable_1y"), evaluation.get("cycle_bottom_price"), evaluation.get("entry_efficiency"), evaluation.get("entry_to_bottom_pct"),
                    evaluation.get("days_to_bottom"), canonical_json(evaluation.get("timing_regret") or {}), canonical_json(future_ids),
                    evaluation.get("recovery_date"), evaluation.get("recovery_days"), str(evaluation["status"]), str(evaluation["reason"]), canonical_json(evaluation.get("payload") or {}), utc_now(),
                ),
            )
        return eval_id, before is None

    @staticmethod
    def _decode_tactical_evaluation(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target, default in (
            ("timing_regret_json", "timing_regret", {}),
            ("future_observation_ids_json", "future_observation_ids", []),
            ("evaluation_payload_json", "payload", {}),
        ):
            try: item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError): item[target] = default
        return item

    def get_tactical_drawdown_evaluations(self, tactical_run_id: str, *, limit: int = 1000000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tactical_drawdown_event_evaluations WHERE tactical_run_id=? ORDER BY event_date, tactical_evaluation_id LIMIT ?",
                (str(tactical_run_id), min(max(int(limit), 1), 1000000)),
            ).fetchall()
        return [self._decode_tactical_evaluation(row) for row in rows]

    def _append_tactical_stat_result(self, table: str, id_column: str, result: Mapping[str, Any], required: set[str], columns: str, values: tuple[Any, ...], result_id: str) -> tuple[str, bool]:
        missing = required.difference(result)
        if missing: raise ValueError(f"{table} 缺少字段：" + ", ".join(sorted(missing)))
        with self.connect() as conn:
            self._require_tactical_running(conn, str(result["tactical_run_id"]))
            before = conn.execute(f"SELECT 1 FROM {table} WHERE {id_column}=?", (result_id,)).fetchone()
            conn.execute(f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({','.join(['?'] * len(values))})", values)
        return result_id, before is None

    def append_tactical_overlay_conditional_result(self, result: Mapping[str, Any]) -> tuple[str, bool]:
        result_id = str(result.get("conditional_result_id") or sha256_json({"run": str(result["tactical_run_id"]), "overlay": str(result["overlay_name"]), "band": str(result["drawdown_band"])})[:32])
        return self._append_tactical_stat_result(
            "tactical_overlay_conditional_results", "conditional_result_id", result,
            {"tactical_run_id", "overlay_name", "drawdown_band", "sample_count", "available_count", "status", "result"},
            "conditional_result_id, tactical_run_id, overlay_name, drawdown_band, sample_count, available_count, status, result_json, created_at",
            (result_id, str(result["tactical_run_id"]), str(result["overlay_name"]), str(result["drawdown_band"]), int(result["sample_count"]), int(result["available_count"]), str(result["status"]), canonical_json(result["result"]), utc_now()), result_id,
        )

    def append_tactical_overlay_lome_result(self, result: Mapping[str, Any]) -> tuple[str, bool]:
        result_id = str(result.get("lome_result_id") or sha256_json({"run": str(result["tactical_run_id"]), "overlay": str(result["overlay_name"]), "band": str(result["drawdown_band"]), "held_out": str(result["held_out_macro_episode_id"])})[:32])
        return self._append_tactical_stat_result(
            "tactical_overlay_lome_results", "lome_result_id", result,
            {"tactical_run_id", "overlay_name", "drawdown_band", "held_out_macro_episode_id", "sample_count", "status", "result"},
            "lome_result_id, tactical_run_id, overlay_name, drawdown_band, held_out_macro_episode_id, sample_count, status, result_json, created_at",
            (result_id, str(result["tactical_run_id"]), str(result["overlay_name"]), str(result["drawdown_band"]), str(result["held_out_macro_episode_id"]), int(result["sample_count"]), str(result["status"]), canonical_json(result["result"]), utc_now()), result_id,
        )

    def append_tactical_overlay_model_result(self, result: Mapping[str, Any]) -> tuple[str, bool]:
        result_id = str(result.get("model_result_id") or sha256_json({"run": str(result["tactical_run_id"]), "model": str(result["model_name"]), "band": str(result["drawdown_band"]), "outcome": str(result["outcome_name"])})[:32])
        return self._append_tactical_stat_result(
            "tactical_overlay_model_results", "model_result_id", result,
            {"tactical_run_id", "model_name", "drawdown_band", "outcome_name", "sample_count", "status", "result"},
            "model_result_id, tactical_run_id, model_name, drawdown_band, outcome_name, sample_count, status, result_json, created_at",
            (result_id, str(result["tactical_run_id"]), str(result["model_name"]), str(result["drawdown_band"]), str(result["outcome_name"]), int(result["sample_count"]), str(result["status"]), canonical_json(result["result"]), utc_now()), result_id,
        )

    @staticmethod
    def _decode_tactical_result(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        try: item["result"] = json.loads(item.pop("result_json"))
        except (TypeError, json.JSONDecodeError): item["result"] = {}
        return item

    def _get_tactical_results(self, table: str, tactical_run_id: str, order_by: str, limit: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM {table} WHERE tactical_run_id=? ORDER BY {order_by} LIMIT ?", (str(tactical_run_id), min(max(int(limit), 1), 1000000))).fetchall()
        return [self._decode_tactical_result(row) for row in rows]

    def get_tactical_overlay_conditional_results(self, tactical_run_id: str, *, limit: int = 10000) -> list[dict[str, Any]]:
        return self._get_tactical_results("tactical_overlay_conditional_results", tactical_run_id, "overlay_name, drawdown_band", limit)

    def get_tactical_overlay_lome_results(self, tactical_run_id: str, *, limit: int = 500000) -> list[dict[str, Any]]:
        return self._get_tactical_results("tactical_overlay_lome_results", tactical_run_id, "overlay_name, drawdown_band, held_out_macro_episode_id", limit)

    def get_tactical_overlay_model_results(self, tactical_run_id: str, *, limit: int = 100000) -> list[dict[str, Any]]:
        return self._get_tactical_results("tactical_overlay_model_results", tactical_run_id, "model_name, drawdown_band, outcome_name", limit)

    def record_tactical_drawdown_report(self, tactical_run_id: str, report: Mapping[str, Any]) -> tuple[str, bool]:
        payload = canonical_json(report)
        report_hash = sha256_json(report)
        report_id = sha256_json({"tactical_run_id": str(tactical_run_id), "report_hash": report_hash})[:32]
        with self.connect() as conn:
            before = conn.execute("SELECT 1 FROM tactical_drawdown_validation_reports WHERE tactical_run_id=?", (str(tactical_run_id),)).fetchone()
            if before: return report_id, False
            conn.execute(
                "INSERT INTO tactical_drawdown_validation_reports(tactical_report_id,tactical_run_id,report_hash,report_json,created_at) VALUES(?,?,?,?,?)",
                (report_id, str(tactical_run_id), report_hash, payload, utc_now()),
            )
        return report_id, True

    def get_tactical_drawdown_report(self, tactical_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tactical_drawdown_validation_reports WHERE tactical_run_id=?", (str(tactical_run_id),)).fetchone()
        if not row: return None
        item = dict(row)
        try: item["report"] = json.loads(item.pop("report_json"))
        except (TypeError, json.JSONDecodeError): item["report"] = {}
        return item

    # ------------------------------------------------------------------
    # Phase 2F capital ladder feasibility repository boundary
    # ------------------------------------------------------------------

    def create_capital_feasibility_run(self, run: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        required = {
            "capital_run_id", "market", "capital_model_version", "phase2e_run_id",
            "start_date", "end_date", "capital_model_config", "config_hash",
            "data_snapshot", "data_cutoff",
        }
        missing = required.difference(run)
        if missing:
            raise ValueError("capital_feasibility_run 缺少字段：" + ", ".join(sorted(missing)))
        if str(run["capital_model_version"]) != "NDX_CAPITAL_LADDER_FEASIBILITY_V1":
            raise ValueError("capital_model_version 必须是 NDX_CAPITAL_LADDER_FEASIBILITY_V1")
        market = str(run["market"]).upper()
        if market != "NDX":
            raise ValueError("Phase 2F 当前只允许 NDX")
        config_hash = str(run["config_hash"]).lower()
        if len(config_hash) != 64 or any(char not in "0123456789abcdef" for char in config_hash):
            raise ValueError("capital config_hash 必须是64位 SHA256")
        start_date = normalize_date(run["start_date"], field="start_date")
        end_date = normalize_date(run["end_date"], field="end_date")
        if start_date > end_date:
            raise ValueError("capital start_date 不能晚于 end_date")
        capital_id = str(run["capital_run_id"])
        created_at = utc_now()
        started_at = iso_timestamp(run.get("started_at") or created_at, "started_at")
        with self.connect() as conn:
            upstream = conn.execute(
                "SELECT status, market FROM tactical_drawdown_validation_runs WHERE tactical_run_id=?",
                (str(run["phase2e_run_id"]),),
            ).fetchone()
            if not upstream or str(upstream[0]) != "COMPLETED" or str(upstream[1]).upper() != market:
                raise ValueError("capital run 必须引用同一市场的已完成 Phase 2E run")
            existing = conn.execute(
                "SELECT * FROM capital_feasibility_validation_runs WHERE capital_run_id=?", (capital_id,)
            ).fetchone()
            if existing:
                item = self._decode_capital_feasibility_run(existing)
                immutable = (
                    item["capital_model_version"], item["phase2e_run_id"], item["start_date"], item["end_date"],
                    item["config_hash"], canonical_json(item["capital_model_config"]),
                )
                expected = (
                    str(run["capital_model_version"]), str(run["phase2e_run_id"]), start_date, end_date,
                    config_hash, canonical_json(dict(run["capital_model_config"])),
                )
                if immutable != expected:
                    raise ValueError("同一 capital_run_id 的冻结配置不一致")
                return item, False
            conn.execute(
                """INSERT INTO capital_feasibility_validation_runs
                   (capital_run_id, market, capital_model_version, phase2e_run_id,
                    start_date, end_date, capital_model_config_json, config_hash,
                    data_snapshot_json, data_cutoff, status, created_at, started_at,
                    completed_at, summary_json, error_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    capital_id, market, str(run["capital_model_version"]), str(run["phase2e_run_id"]),
                    start_date, end_date, canonical_json(dict(run["capital_model_config"])), config_hash,
                    canonical_json(dict(run["data_snapshot"])), as_of_datetime(run["data_cutoff"]),
                    "RUNNING", created_at, started_at, None, "{}", "{}",
                ),
            )
            row = conn.execute(
                "SELECT * FROM capital_feasibility_validation_runs WHERE capital_run_id=?", (capital_id,)
            ).fetchone()
        return self._decode_capital_feasibility_run(row), True

    @staticmethod
    def _decode_capital_feasibility_run(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target in (
            ("capital_model_config_json", "capital_model_config"),
            ("data_snapshot_json", "data_snapshot"),
            ("summary_json", "summary"),
            ("error_json", "error"),
        ):
            try: item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError): item[target] = {}
        return item

    def get_capital_feasibility_run(self, capital_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM capital_feasibility_validation_runs WHERE capital_run_id=?", (str(capital_run_id),)
            ).fetchone()
        return self._decode_capital_feasibility_run(row) if row else None

    def get_capital_feasibility_runs(self, *, market: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if market is not None:
            clauses.append("market=?"); params.append(str(market).upper())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(max(int(limit), 1), 1000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM capital_feasibility_validation_runs{where} ORDER BY created_at DESC, capital_run_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._decode_capital_feasibility_run(row) for row in rows]

    def complete_capital_feasibility_run(
        self,
        capital_run_id: str,
        *,
        status: str = "COMPLETED",
        summary: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        completed_at: Any | None = None,
    ) -> dict[str, Any]:
        status = str(status).upper()
        if status not in {"COMPLETED", "FAILED"}:
            raise ValueError("capital feasibility run 完成状态无效")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status FROM capital_feasibility_validation_runs WHERE capital_run_id=?", (str(capital_run_id),)
            ).fetchone()
            if not row:
                raise ValueError("capital_feasibility_run 不存在")
            if str(row[0]) != "RUNNING":
                raise ValueError("已结束的 capital feasibility run 不可再次完成或修改")
            conn.execute(
                """UPDATE capital_feasibility_validation_runs
                   SET status=?, completed_at=?, summary_json=?, error_json=?
                   WHERE capital_run_id=? AND status='RUNNING'""",
                (
                    status, iso_timestamp(completed_at or utc_now(), "completed_at"),
                    canonical_json(summary or {}), canonical_json(error or {}), str(capital_run_id),
                ),
            )
            updated = conn.execute(
                "SELECT * FROM capital_feasibility_validation_runs WHERE capital_run_id=?", (str(capital_run_id),)
            ).fetchone()
        return self._decode_capital_feasibility_run(updated)

    @staticmethod
    def _require_capital_running(conn: sqlite3.Connection, capital_run_id: str) -> None:
        row = conn.execute(
            "SELECT status FROM capital_feasibility_validation_runs WHERE capital_run_id=?", (str(capital_run_id),)
        ).fetchone()
        if not row:
            raise ValueError("capital_feasibility_run 不存在")
        if str(row[0]) != "RUNNING":
            raise ValueError("已结束 capital feasibility run 不能追加数据")

    def append_capital_scenario_result(self, result: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "capital_run_id", "path_type", "path_id", "ladder_id", "replenishment_id", "cap_id",
            "initial_units", "m_to_opportunity_units", "sample_event_count", "status", "result",
        }
        missing = required.difference(result)
        if missing:
            raise ValueError("capital scenario 缺少字段：" + ", ".join(sorted(missing)))
        if str(result["path_type"]) not in {"HISTORICAL", "SYNTHETIC", "SEQUENCE", "EXTREME_EXTENSION"}:
            raise ValueError("capital scenario path_type 无效")
        if str(result["ladder_id"]) not in {"A", "B", "C", "D"}:
            raise ValueError("capital scenario ladder_id 无效")
        if str(result["replenishment_id"]) not in {"R0", "R1", "R2"}:
            raise ValueError("capital scenario replenishment_id 无效")
        if str(result["cap_id"]) not in {"CAP_6M", "CAP_12M", "CAP_24M"}:
            raise ValueError("capital scenario cap_id 无效")
        result_id = str(result.get("scenario_result_id") or sha256_json({
            "run": str(result["capital_run_id"]), "path_type": str(result["path_type"]), "path_id": str(result["path_id"]),
            "ladder": str(result["ladder_id"]), "replenishment": str(result["replenishment_id"]), "cap": str(result["cap_id"]),
        })[:32])
        with self.connect() as conn:
            self._require_capital_running(conn, str(result["capital_run_id"]))
            before = conn.execute("SELECT 1 FROM capital_feasibility_scenario_results WHERE scenario_result_id=?", (result_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO capital_feasibility_scenario_results
                   (scenario_result_id, capital_run_id, path_type, path_id, ladder_id,
                    replenishment_id, cap_id, initial_units, m_to_opportunity_units,
                    sample_event_count, status, result_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result_id, str(result["capital_run_id"]), str(result["path_type"]), str(result["path_id"]), str(result["ladder_id"]),
                    str(result["replenishment_id"]), str(result["cap_id"]), float(result["initial_units"]),
                    float(result["m_to_opportunity_units"]), int(result["sample_event_count"]), str(result["status"]),
                    canonical_json(result["result"]), utc_now(),
                ),
            )
        return result_id, before is None

    @staticmethod
    def _decode_capital_result(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        try: item["result"] = json.loads(item.pop("result_json"))
        except (TypeError, json.JSONDecodeError): item["result"] = {}
        return item

    def get_capital_feasibility_scenarios(self, capital_run_id: str, *, path_type: str | None = None, limit: int = 100000) -> list[dict[str, Any]]:
        clauses = ["capital_run_id=?"]; params: list[Any] = [str(capital_run_id)]
        if path_type is not None:
            clauses.append("path_type=?"); params.append(str(path_type))
        params.append(min(max(int(limit), 1), 500000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM capital_feasibility_scenario_results WHERE {' AND '.join(clauses)} ORDER BY path_type, path_id, ladder_id, replenishment_id, cap_id LIMIT ?",
                params,
            ).fetchall()
        return [self._decode_capital_result(row) for row in rows]

    def append_capital_ledger_entry(self, entry: Mapping[str, Any]) -> tuple[str, bool]:
        required = {
            "capital_run_id", "scenario_result_id", "path_type", "path_id", "event_sequence", "event_date",
            "event_kind", "cash_before_units", "required_units", "deployed_units", "cash_after_units",
            "replenishment_units", "surplus_units", "underfunded_event", "ath_state_ignored",
        }
        missing = required.difference(entry)
        if missing:
            raise ValueError("capital ledger 缺少字段：" + ", ".join(sorted(missing)))
        if str(entry["event_kind"]) not in {"TACTICAL_BAND", "EXTENSION_OBSERVATION"}:
            raise ValueError("capital ledger event_kind 无效")
        if float(entry["cash_before_units"]) < -1e-9 or float(entry["deployed_units"]) < -1e-9 or float(entry["cash_after_units"]) < -1e-9:
            raise ValueError("capital ledger 现金不能为负")
        if int(bool(entry["underfunded_event"])) not in {0, 1} or int(bool(entry["ath_state_ignored"])) not in {0, 1}:
            raise ValueError("capital ledger 标志无效")
        entry_id = str(entry.get("ledger_entry_id") or sha256_json({
            "scenario": str(entry["scenario_result_id"]), "sequence": int(entry["event_sequence"]),
        })[:32])
        with self.connect() as conn:
            self._require_capital_running(conn, str(entry["capital_run_id"]))
            scenario = conn.execute("SELECT capital_run_id FROM capital_feasibility_scenario_results WHERE scenario_result_id=?", (str(entry["scenario_result_id"]),)).fetchone()
            if not scenario or str(scenario[0]) != str(entry["capital_run_id"]):
                raise ValueError("capital ledger 必须引用同一 run 的 scenario")
            before = conn.execute("SELECT 1 FROM capital_feasibility_ledger WHERE ledger_entry_id=?", (entry_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO capital_feasibility_ledger
                   (ledger_entry_id, capital_run_id, scenario_result_id, path_type, path_id,
                    event_sequence, event_date, tactical_cycle_id, macro_episode_id,
                    threshold, event_kind, cash_before_units, required_units,
                    deployed_units, cash_after_units, replenishment_units, surplus_units,
                    underfunded_event, ath_state_ignored, input_observation_ids_json,
                    ledger_payload_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    entry_id, str(entry["capital_run_id"]), str(entry["scenario_result_id"]), str(entry["path_type"]), str(entry["path_id"]),
                    int(entry["event_sequence"]), normalize_date(entry["event_date"]), entry.get("tactical_cycle_id"), entry.get("macro_episode_id"),
                    entry.get("threshold"), str(entry["event_kind"]), float(entry["cash_before_units"]), float(entry["required_units"]),
                    float(entry["deployed_units"]), float(entry["cash_after_units"]), float(entry.get("replenishment_units") or 0),
                    float(entry.get("surplus_units") or 0), int(bool(entry["underfunded_event"])), int(bool(entry["ath_state_ignored"])),
                    canonical_json(sorted({str(value) for value in (entry.get("input_observation_ids") or [])})),
                    canonical_json(entry.get("payload") or {}), utc_now(),
                ),
            )
        return entry_id, before is None

    @staticmethod
    def _decode_capital_ledger(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target, default in (
            ("input_observation_ids_json", "input_observation_ids", []),
            ("ledger_payload_json", "payload", {}),
        ):
            try: item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError): item[target] = default
        return item

    def get_capital_feasibility_ledger(self, capital_run_id: str, *, scenario_result_id: str | None = None, limit: int = 1000000) -> list[dict[str, Any]]:
        clauses = ["capital_run_id=?"]; params: list[Any] = [str(capital_run_id)]
        if scenario_result_id is not None:
            clauses.append("scenario_result_id=?"); params.append(str(scenario_result_id))
        params.append(min(max(int(limit), 1), 2000000))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM capital_feasibility_ledger WHERE {' AND '.join(clauses)} ORDER BY path_type, path_id, event_date, event_sequence, ledger_entry_id LIMIT ?",
                params,
            ).fetchall()
        return [self._decode_capital_ledger(row) for row in rows]

    def record_capital_feasibility_report(self, capital_run_id: str, report: Mapping[str, Any]) -> tuple[str, bool]:
        payload = canonical_json(report)
        report_hash = sha256_json(report)
        report_id = sha256_json({"capital_run_id": str(capital_run_id), "report_hash": report_hash})[:32]
        with self.connect() as conn:
            before = conn.execute("SELECT 1 FROM capital_feasibility_reports WHERE capital_run_id=?", (str(capital_run_id),)).fetchone()
            if before:
                return report_id, False
            conn.execute(
                "INSERT INTO capital_feasibility_reports(capital_report_id,capital_run_id,report_hash,report_json,created_at) VALUES(?,?,?,?,?)",
                (report_id, str(capital_run_id), report_hash, payload, utc_now()),
            )
        return report_id, True

    def get_capital_feasibility_report(self, capital_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM capital_feasibility_reports WHERE capital_run_id=?", (str(capital_run_id),)).fetchone()
        if not row:
            return None
        item = dict(row)
        try: item["report"] = json.loads(item.pop("report_json"))
        except (TypeError, json.JSONDecodeError): item["report"] = {}
        return item

    # ------------------------------------------------------------------
    # Phase 3A deterministic capital-allocation state machine repository
    # ------------------------------------------------------------------

    def create_capital_state_machine_run(self, run: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        required = {
            "state_machine_run_id", "batch_id", "market", "state_machine_model_version",
            "ladder_id", "ladder_version", "trigger_version", "start_date", "end_date",
            "capital_profile", "state_machine_config", "config_hash", "data_snapshot",
            "data_cutoff", "simulation_only", "auto_trade",
        }
        missing = required.difference(run)
        if missing:
            raise ValueError("capital_state_machine_run 缺少字段：" + ", ".join(sorted(missing)))
        if str(run["state_machine_model_version"]) != "NDX_CAPITAL_ALLOCATION_STATE_MACHINE_V1":
            raise ValueError("state_machine_model_version 必须是 NDX_CAPITAL_ALLOCATION_STATE_MACHINE_V1")
        market = str(run["market"]).upper()
        if market != "NDX":
            raise ValueError("Phase 3A 当前只允许 NDX")
        ladder_id = str(run["ladder_id"]).upper()
        if ladder_id not in {"C", "D"}:
            raise ValueError("Phase 3A ladder_id 只能是 C 或 D")
        if str(run["trigger_version"]) != "NDX_TACTICAL_DRAWDOWN_V1":
            raise ValueError("Phase 3A 必须使用 NDX_TACTICAL_DRAWDOWN_V1")
        config_hash = str(run["config_hash"]).lower()
        if len(config_hash) != 64 or any(char not in "0123456789abcdef" for char in config_hash):
            raise ValueError("state machine config_hash 必须是64位 SHA256")
        start_date = normalize_date(run["start_date"], field="start_date")
        end_date = normalize_date(run["end_date"], field="end_date")
        if start_date > end_date:
            raise ValueError("capital state machine start_date 不能晚于 end_date")
        if not isinstance(run["capital_profile"], Mapping) or not isinstance(run["state_machine_config"], Mapping):
            raise ValueError("capital_profile 与 state_machine_config 必须是对象")
        if not isinstance(run["data_snapshot"], (Mapping, list, str)):
            raise ValueError("data_snapshot 必须是对象、数组或字符串")
        run_id = str(run["state_machine_run_id"])
        created_at = utc_now()
        started_at = iso_timestamp(run.get("started_at") or created_at, "started_at")
        phase2e_run_id = run.get("phase2e_run_id")
        phase2f_run_id = run.get("phase2f_run_id")
        with self.connect() as conn:
            if phase2e_run_id:
                upstream = conn.execute("SELECT status, market FROM tactical_drawdown_validation_runs WHERE tactical_run_id=?", (str(phase2e_run_id),)).fetchone()
                if not upstream or str(upstream[0]) != "COMPLETED" or str(upstream[1]).upper() != market:
                    raise ValueError("state machine 必须引用同一市场的已完成 Phase 2E run")
            if phase2f_run_id:
                feasibility = conn.execute("SELECT status, market, phase2e_run_id FROM capital_feasibility_validation_runs WHERE capital_run_id=?", (str(phase2f_run_id),)).fetchone()
                if not feasibility or str(feasibility[0]) != "COMPLETED" or str(feasibility[1]).upper() != market:
                    raise ValueError("state machine 的 Phase 2F 引用无效")
                if phase2e_run_id and str(feasibility[2]) != str(phase2e_run_id):
                    raise ValueError("state machine 的 Phase 2F run 必须来自同一个 Phase 2E run")
            existing = conn.execute("SELECT * FROM capital_state_machine_runs WHERE state_machine_run_id=?", (run_id,)).fetchone()
            if existing:
                item = self._decode_capital_state_machine_run(existing)
                immutable = (
                    item["batch_id"], item["market"], item["state_machine_model_version"], item["ladder_id"], item["ladder_version"],
                    item["trigger_version"], item.get("phase2e_run_id"), item.get("phase2f_run_id"), item["start_date"], item["end_date"],
                    item["config_hash"], canonical_json(item["capital_profile"]), canonical_json(item["state_machine_config"]),
                    int(item["simulation_only"]), int(item["auto_trade"]),
                )
                expected = (
                    str(run["batch_id"]), market, str(run["state_machine_model_version"]), ladder_id, str(run["ladder_version"]),
                    str(run["trigger_version"]), str(phase2e_run_id) if phase2e_run_id else None, str(phase2f_run_id) if phase2f_run_id else None,
                    start_date, end_date, config_hash, canonical_json(dict(run["capital_profile"])), canonical_json(dict(run["state_machine_config"])),
                    int(bool(run["simulation_only"])), int(bool(run["auto_trade"])),
                )
                if immutable != expected:
                    raise ValueError("同一 state_machine_run_id 的冻结配置不一致")
                return item, False
            conn.execute(
                """INSERT INTO capital_state_machine_runs
                   (state_machine_run_id, batch_id, market, state_machine_model_version,
                    ladder_id, ladder_version, trigger_version, phase2e_run_id,
                    phase2f_run_id, start_date, end_date, capital_profile_json,
                    state_machine_config_json, config_hash, data_snapshot_json,
                    data_cutoff, simulation_only, auto_trade, status, created_at,
                    started_at, completed_at, summary_json, error_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, str(run["batch_id"]), market, str(run["state_machine_model_version"]), ladder_id, str(run["ladder_version"]),
                    str(run["trigger_version"]), str(phase2e_run_id) if phase2e_run_id else None, str(phase2f_run_id) if phase2f_run_id else None,
                    start_date, end_date, canonical_json(dict(run["capital_profile"])), canonical_json(dict(run["state_machine_config"])),
                    config_hash, canonical_json(run["data_snapshot"]), as_of_datetime(run["data_cutoff"]), int(bool(run["simulation_only"])),
                    int(bool(run["auto_trade"])), "RUNNING", created_at, started_at, None, "{}", "{}",
                ),
            )
            row = conn.execute("SELECT * FROM capital_state_machine_runs WHERE state_machine_run_id=?", (run_id,)).fetchone()
        return self._decode_capital_state_machine_run(row), True

    @staticmethod
    def _decode_capital_state_machine_run(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target, default in (("capital_profile_json", "capital_profile", {}), ("state_machine_config_json", "state_machine_config", {}), ("data_snapshot_json", "data_snapshot", {}), ("summary_json", "summary", {}), ("error_json", "error", {})):
            try: item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError): item[target] = default
        item["simulation_only"] = bool(item.get("simulation_only")); item["auto_trade"] = bool(item.get("auto_trade"))
        return item

    def get_capital_state_machine_run(self, state_machine_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM capital_state_machine_runs WHERE state_machine_run_id=?", (str(state_machine_run_id),)).fetchone()
        return self._decode_capital_state_machine_run(row) if row else None

    def get_capital_state_machine_runs(self, *, market: str | None = None, ladder_id: str | None = None, batch_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []; params: list[Any] = []
        if market is not None: clauses.append("market=?"); params.append(str(market).upper())
        if ladder_id is not None: clauses.append("ladder_id=?"); params.append(str(ladder_id).upper())
        if batch_id is not None: clauses.append("batch_id=?"); params.append(str(batch_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""; params.append(min(max(int(limit), 1), 1000))
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM capital_state_machine_runs{where} ORDER BY created_at DESC, state_machine_run_id DESC LIMIT ?", params).fetchall()
        return [self._decode_capital_state_machine_run(row) for row in rows]

    def complete_capital_state_machine_run(self, state_machine_run_id: str, *, status: str = "COMPLETED", summary: Mapping[str, Any] | None = None, error: Mapping[str, Any] | None = None, completed_at: Any | None = None) -> dict[str, Any]:
        status = str(status).upper()
        if status not in {"COMPLETED", "FAILED"}: raise ValueError("capital state machine 完成状态无效")
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM capital_state_machine_runs WHERE state_machine_run_id=?", (str(state_machine_run_id),)).fetchone()
            if not row: raise ValueError("capital_state_machine_run 不存在")
            if str(row[0]) != "RUNNING": raise ValueError("已结束的 capital state machine run 不可再次完成或修改")
            conn.execute("UPDATE capital_state_machine_runs SET status=?, completed_at=?, summary_json=?, error_json=? WHERE state_machine_run_id=? AND status='RUNNING'", (status, iso_timestamp(completed_at or utc_now(), "completed_at"), canonical_json(summary or {}), canonical_json(error or {}), str(state_machine_run_id)))
            updated = conn.execute("SELECT * FROM capital_state_machine_runs WHERE state_machine_run_id=?", (str(state_machine_run_id),)).fetchone()
        return self._decode_capital_state_machine_run(updated)

    @staticmethod
    def _require_capital_state_machine_running(conn: sqlite3.Connection, run_id: str) -> None:
        row = conn.execute("SELECT status FROM capital_state_machine_runs WHERE state_machine_run_id=?", (str(run_id),)).fetchone()
        if not row: raise ValueError("capital_state_machine_run 不存在")
        if str(row[0]) != "RUNNING": raise ValueError("已结束 capital state machine run 不能追加数据")

    def append_capital_event(self, event: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"state_machine_run_id", "transaction_id", "transaction_sequence", "event_date", "tactical_cycle_id", "band", "trigger_drawdown", "ladder_version", "planned_amount", "actual_amount", "shortfall", "cash_before", "cash_after", "fund_target", "fund_cap", "monthly_refill", "underfunded", "input_hash", "event_hash"}
        missing = required.difference(event)
        if missing: raise ValueError("capital_event 缺少字段：" + ", ".join(sorted(missing)))
        if str(event["band"]) not in {"10", "20", "30", "40", "50"}: raise ValueError("capital_event band 无效")
        amounts = [float(event[name]) for name in ("planned_amount", "actual_amount", "shortfall", "cash_before", "cash_after", "fund_target", "fund_cap", "monthly_refill")]
        if any(value < -1e-9 for value in amounts): raise ValueError("capital_event 金额不能为负")
        if float(event["actual_amount"]) > float(event["planned_amount"]) + 1e-9: raise ValueError("capital_event actual_amount 不能大于 planned_amount")
        if float(event["actual_amount"]) > float(event["cash_before"]) + 1e-9: raise ValueError("capital_event actual_amount 不能大于 cash_before")
        for name in ("input_hash", "event_hash"):
            digest = str(event[name]).lower()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest): raise ValueError(f"capital_event {name} 必须是64位 SHA256")
        event_id = str(event.get("capital_event_id") or sha256_json({"run": str(event["state_machine_run_id"]), "transaction": str(event["transaction_id"]), "sequence": int(event["transaction_sequence"]), "event_hash": str(event["event_hash"])})[:32])
        with self.connect() as conn:
            self._require_capital_state_machine_running(conn, str(event["state_machine_run_id"]))
            before = conn.execute("SELECT 1 FROM capital_event_log WHERE capital_event_id=?", (event_id,)).fetchone()
            conn.execute("""INSERT OR IGNORE INTO capital_event_log
                (capital_event_id, state_machine_run_id, transaction_id, transaction_sequence,
                 event_date, tactical_cycle_id, band, trigger_drawdown, ath_drawdown,
                 ladder_version, planned_amount, actual_amount, shortfall, cash_before,
                 cash_after, fund_target, fund_cap, monthly_refill, underfunded, input_hash,
                 previous_event_hash, event_hash, event_payload_json, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                event_id, str(event["state_machine_run_id"]), str(event["transaction_id"]), int(event["transaction_sequence"]), normalize_date(event["event_date"]), str(event["tactical_cycle_id"]), str(event["band"]), float(event["trigger_drawdown"]), event.get("ath_drawdown"), str(event["ladder_version"]), float(event["planned_amount"]), float(event["actual_amount"]), float(event["shortfall"]), float(event["cash_before"]), float(event["cash_after"]), float(event["fund_target"]), float(event["fund_cap"]), float(event["monthly_refill"]), int(bool(event["underfunded"])), str(event["input_hash"]).lower(), event.get("previous_event_hash"), str(event["event_hash"]).lower(), canonical_json(event.get("payload") or {}), utc_now(),
            ))
        return event_id, before is None

    @staticmethod
    def _decode_capital_event(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        try: item["payload"] = json.loads(item.pop("event_payload_json"))
        except (TypeError, json.JSONDecodeError): item["payload"] = {}
        item["underfunded"] = bool(item.get("underfunded"))
        return item

    def get_capital_events(self, state_machine_run_id: str, *, as_of: Any | None = None, limit: int = 100000) -> list[dict[str, Any]]:
        clauses = ["state_machine_run_id=?"]; params: list[Any] = [str(state_machine_run_id)]
        if as_of is not None: clauses.append("event_date<=?"); params.append(normalize_date(as_of))
        params.append(min(max(int(limit), 1), 1000000))
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM capital_event_log WHERE {' AND '.join(clauses)} ORDER BY event_date, transaction_id, transaction_sequence, capital_event_id LIMIT ?", params).fetchall()
        return [self._decode_capital_event(row) for row in rows]

    def append_capital_daily_snapshot(self, snapshot: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"state_machine_run_id", "as_of_date", "tactical_cycle_id", "tactical_drawdown", "available_opportunity_cash", "target_opportunity_cash", "opportunity_fund_cap", "monthly_refill_rate", "surplus_cash", "ladder_version", "trigger_version", "capital_model_version", "core_dca_untouched", "state_hash"}
        missing = required.difference(snapshot)
        if missing: raise ValueError("capital_daily_snapshot 缺少字段：" + ", ".join(sorted(missing)))
        if str(snapshot["trigger_version"]) != "NDX_TACTICAL_DRAWDOWN_V1": raise ValueError("capital_daily_snapshot trigger_version 无效")
        if str(snapshot["capital_model_version"]) != "NDX_CAPITAL_ALLOCATION_STATE_MACHINE_V1": raise ValueError("capital_daily_snapshot capital_model_version 无效")
        for name in ("available_opportunity_cash", "target_opportunity_cash", "opportunity_fund_cap", "monthly_refill_rate", "surplus_cash", "last_capital_event_amount"):
            if float(snapshot.get(name) or 0) < -1e-9: raise ValueError("capital_daily_snapshot 金额不能为负")
        digest = str(snapshot["state_hash"]).lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest): raise ValueError("capital_daily_snapshot state_hash 必须是64位 SHA256")
        snapshot_id = str(snapshot.get("capital_snapshot_id") or sha256_json({"run": str(snapshot["state_machine_run_id"]), "date": normalize_date(snapshot["as_of_date"]), "hash": digest})[:32])
        with self.connect() as conn:
            self._require_capital_state_machine_running(conn, str(snapshot["state_machine_run_id"]))
            before = conn.execute("SELECT 1 FROM capital_daily_snapshots WHERE capital_snapshot_id=?", (snapshot_id,)).fetchone()
            conn.execute("""INSERT OR IGNORE INTO capital_daily_snapshots
                (capital_snapshot_id, state_machine_run_id, as_of_date, tactical_cycle_id,
                 ath_drawdown, tactical_drawdown, current_band, used_bands_json,
                 armed_bands_json, available_opportunity_cash, target_opportunity_cash,
                 opportunity_fund_cap, monthly_refill_rate, surplus_cash,
                 last_capital_event_date, last_capital_event_band, last_capital_event_amount,
                 ladder_version, trigger_version, capital_model_version,
                 capital_adequacy_ratio, next_trigger_json, core_dca_untouched, state_hash,
                 previous_state_hash, snapshot_payload_json, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                snapshot_id, str(snapshot["state_machine_run_id"]), normalize_date(snapshot["as_of_date"]), str(snapshot["tactical_cycle_id"]), snapshot.get("ath_drawdown"), float(snapshot["tactical_drawdown"]), snapshot.get("current_band"), canonical_json([str(x) for x in (snapshot.get("used_bands") or [])]), canonical_json([str(x) for x in (snapshot.get("armed_bands") or [])]), float(snapshot["available_opportunity_cash"]), float(snapshot["target_opportunity_cash"]), float(snapshot["opportunity_fund_cap"]), float(snapshot["monthly_refill_rate"]), float(snapshot["surplus_cash"]), snapshot.get("last_capital_event_date"), snapshot.get("last_capital_event_band"), float(snapshot.get("last_capital_event_amount") or 0), str(snapshot["ladder_version"]), str(snapshot["trigger_version"]), str(snapshot["capital_model_version"]), snapshot.get("capital_adequacy_ratio"), canonical_json(snapshot.get("next_trigger") or {}), int(bool(snapshot["core_dca_untouched"])), digest, snapshot.get("previous_state_hash"), canonical_json(snapshot.get("payload") or {}), utc_now(),
            ))
        return snapshot_id, before is None

    @staticmethod
    def _decode_capital_snapshot(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target, default in (("used_bands_json", "used_bands", []), ("armed_bands_json", "armed_bands", []), ("next_trigger_json", "next_trigger", {}), ("snapshot_payload_json", "payload", {})):
            try: item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError): item[target] = default
        item["core_dca_untouched"] = bool(item.get("core_dca_untouched"))
        return item

    def get_capital_snapshots(self, state_machine_run_id: str, *, as_of: Any | None = None, limit: int = 100000) -> list[dict[str, Any]]:
        clauses = ["state_machine_run_id=?"]; params: list[Any] = [str(state_machine_run_id)]
        if as_of is not None: clauses.append("as_of_date<=?"); params.append(normalize_date(as_of))
        params.append(min(max(int(limit), 1), 2000000))
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM capital_daily_snapshots WHERE {' AND '.join(clauses)} ORDER BY as_of_date, capital_snapshot_id LIMIT ?", params).fetchall()
        return [self._decode_capital_snapshot(row) for row in rows]

    def get_capital_snapshot(self, state_machine_run_id: str, as_of: Any) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM capital_daily_snapshots WHERE state_machine_run_id=? AND as_of_date=?", (str(state_machine_run_id), normalize_date(as_of))).fetchone()
        return self._decode_capital_snapshot(row) if row else None

    def record_capital_state_machine_report(self, state_machine_run_id: str, report: Mapping[str, Any]) -> tuple[str, bool]:
        payload = canonical_json(report); report_hash = sha256_json(report); report_id = sha256_json({"state_machine_run_id": str(state_machine_run_id), "report_hash": report_hash})[:32]
        with self.connect() as conn:
            before = conn.execute("SELECT 1 FROM capital_state_machine_reports WHERE state_machine_run_id=?", (str(state_machine_run_id),)).fetchone()
            if before: return report_id, False
            conn.execute("INSERT INTO capital_state_machine_reports(capital_report_id,state_machine_run_id,report_hash,report_json,created_at) VALUES(?,?,?,?,?)", (report_id, str(state_machine_run_id), report_hash, payload, utc_now()))
        return report_id, True

    def get_capital_state_machine_report(self, state_machine_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM capital_state_machine_reports WHERE state_machine_run_id=?", (str(state_machine_run_id),)).fetchone()
        if not row: return None
        item = dict(row)
        try: item["report"] = json.loads(item.pop("report_json"))
        except (TypeError, json.JSONDecodeError): item["report"] = {}
        return item

    # ------------------------------------------------------------------
    # Phase 3B real-world capital-profile repository
    # ------------------------------------------------------------------

    def create_real_world_parameterization_run(self, run: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        required = {
            "real_world_run_id", "batch_id", "market", "model_version", "start_date", "end_date",
            "config", "config_hash", "data_snapshot", "data_cutoff", "simulation_only", "auto_trade",
        }
        missing = required.difference(run)
        if missing:
            raise ValueError("real_world_parameterization_run 缺少字段：" + ", ".join(sorted(missing)))
        if str(run["model_version"]) != "NDX_REAL_WORLD_PARAMETERIZATION_V1":
            raise ValueError("model_version 必须是 NDX_REAL_WORLD_PARAMETERIZATION_V1")
        if str(run["market"]).upper() != "NDX":
            raise ValueError("Phase 3B 当前只允许 NDX")
        config_hash = str(run["config_hash"]).lower()
        if len(config_hash) != 64 or any(char not in "0123456789abcdef" for char in config_hash):
            raise ValueError("real_world config_hash 必须是64位 SHA256")
        start_date = normalize_date(run["start_date"], field="start_date")
        end_date = normalize_date(run["end_date"], field="end_date")
        if start_date > end_date:
            raise ValueError("real_world start_date 不能晚于 end_date")
        if not isinstance(run["config"], Mapping):
            raise ValueError("real_world config 必须是对象")
        if not isinstance(run["data_snapshot"], (Mapping, list, str)):
            raise ValueError("data_snapshot 必须是对象、数组或字符串")
        run_id = str(run["real_world_run_id"])
        created_at = utc_now()
        started_at = iso_timestamp(run.get("started_at") or created_at, "started_at")
        phase3a_batch_id = run.get("phase3a_batch_id")
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM real_world_parameterization_runs WHERE real_world_run_id=?", (run_id,)).fetchone()
            if existing:
                item = self._decode_real_world_parameterization_run(existing)
                immutable = (
                    item["batch_id"], item["market"], item["model_version"], item.get("phase3a_batch_id"),
                    item["start_date"], item["end_date"], item["config_hash"],
                    canonical_json(item["config"]), int(item["simulation_only"]), int(item["auto_trade"]),
                )
                expected = (
                    str(run["batch_id"]), "NDX", str(run["model_version"]), str(phase3a_batch_id) if phase3a_batch_id else None,
                    start_date, end_date, config_hash, canonical_json(dict(run["config"])),
                    int(bool(run["simulation_only"])), int(bool(run["auto_trade"])),
                )
                if immutable != expected:
                    raise ValueError("同一 real_world_run_id 的冻结配置不一致")
                return item, False
            conn.execute(
                """INSERT INTO real_world_parameterization_runs
                   (real_world_run_id, batch_id, market, model_version, phase3a_batch_id,
                    start_date, end_date, config_json, config_hash, data_snapshot_json,
                    data_cutoff, simulation_only, auto_trade, status, created_at,
                    started_at, completed_at, summary_json, error_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, str(run["batch_id"]), "NDX", str(run["model_version"]),
                    str(phase3a_batch_id) if phase3a_batch_id else None, start_date, end_date,
                    canonical_json(dict(run["config"])), config_hash, canonical_json(run["data_snapshot"]),
                    as_of_datetime(run["data_cutoff"]), int(bool(run["simulation_only"])), int(bool(run["auto_trade"])),
                    "RUNNING", created_at, started_at, None, "{}", "{}",
                ),
            )
            row = conn.execute("SELECT * FROM real_world_parameterization_runs WHERE real_world_run_id=?", (run_id,)).fetchone()
        return self._decode_real_world_parameterization_run(row), True

    @staticmethod
    def _decode_real_world_parameterization_run(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target, default in (
            ("config_json", "config", {}), ("data_snapshot_json", "data_snapshot", {}),
            ("summary_json", "summary", {}), ("error_json", "error", {}),
        ):
            try: item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError): item[target] = default
        item["simulation_only"] = bool(item.get("simulation_only")); item["auto_trade"] = bool(item.get("auto_trade"))
        return item

    def get_real_world_parameterization_run(self, real_world_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM real_world_parameterization_runs WHERE real_world_run_id=?", (str(real_world_run_id),)).fetchone()
        return self._decode_real_world_parameterization_run(row) if row else None

    def get_real_world_parameterization_runs(self, *, market: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []; params: list[Any] = []
        if market is not None: clauses.append("market=?"); params.append(str(market).upper())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(min(max(int(limit), 1), 1000))
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM real_world_parameterization_runs{where} ORDER BY created_at DESC, real_world_run_id DESC LIMIT ?", params).fetchall()
        return [self._decode_real_world_parameterization_run(row) for row in rows]

    def complete_real_world_parameterization_run(self, real_world_run_id: str, *, status: str = "COMPLETED", summary: Mapping[str, Any] | None = None, error: Mapping[str, Any] | None = None, completed_at: Any | None = None) -> dict[str, Any]:
        status = str(status).upper()
        if status not in {"COMPLETED", "FAILED"}:
            raise ValueError("real_world_parameterization 完成状态无效")
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM real_world_parameterization_runs WHERE real_world_run_id=?", (str(real_world_run_id),)).fetchone()
            if not row: raise ValueError("real_world_parameterization_run 不存在")
            if str(row[0]) != "RUNNING": raise ValueError("已结束的 real_world_parameterization_run 不可再次完成或修改")
            conn.execute(
                "UPDATE real_world_parameterization_runs SET status=?, completed_at=?, summary_json=?, error_json=? WHERE real_world_run_id=? AND status='RUNNING'",
                (status, iso_timestamp(completed_at or utc_now(), "completed_at"), canonical_json(summary or {}), canonical_json(error or {}), str(real_world_run_id)),
            )
            updated = conn.execute("SELECT * FROM real_world_parameterization_runs WHERE real_world_run_id=?", (str(real_world_run_id),)).fetchone()
        return self._decode_real_world_parameterization_run(updated)

    @staticmethod
    def _require_real_world_running(conn: sqlite3.Connection, run_id: str) -> None:
        row = conn.execute("SELECT status FROM real_world_parameterization_runs WHERE real_world_run_id=?", (str(run_id),)).fetchone()
        if not row: raise ValueError("real_world_parameterization_run 不存在")
        if str(row[0]) != "RUNNING": raise ValueError("已结束 real_world_parameterization_run 不能追加数据")

    def append_real_world_profile_scenario(self, scenario: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"real_world_run_id", "profile_id", "target_id", "cap_multiplier", "refill_mode", "growth_scenario", "surplus_policy", "ladder_id", "profile", "metrics", "result_hash"}
        missing = required.difference(scenario)
        if missing: raise ValueError("real_world_profile_scenario 缺少字段：" + ", ".join(sorted(missing)))
        if str(scenario["ladder_id"]).upper() not in {"C", "D"}: raise ValueError("real_world ladder_id 只能是 C 或 D")
        if float(scenario["cap_multiplier"]) not in {1.0, 1.5, 2.0}: raise ValueError("cap_multiplier 只能是 1.0、1.5、2.0")
        if str(scenario["refill_mode"]).upper() not in {"FIXED", "INCOME_LINKED"}: raise ValueError("refill_mode 无效")
        if str(scenario["surplus_policy"]).upper() not in {"S0", "S1", "S2"}: raise ValueError("surplus_policy 无效")
        digest = str(scenario["result_hash"]).lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest): raise ValueError("result_hash 必须是64位 SHA256")
        scenario_id = str(scenario.get("scenario_id") or sha256_json({"run": str(scenario["real_world_run_id"]), "profile": str(scenario["profile_id"]), "target": str(scenario["target_id"]), "cap": float(scenario["cap_multiplier"]), "mode": str(scenario["refill_mode"]), "ratio": scenario.get("refill_ratio"), "growth": str(scenario["growth_scenario"]), "surplus": str(scenario["surplus_policy"]), "ladder": str(scenario["ladder_id"]).upper()})[:32])
        with self.connect() as conn:
            self._require_real_world_running(conn, str(scenario["real_world_run_id"]))
            before = conn.execute("SELECT 1 FROM real_world_profile_scenarios WHERE scenario_id=?", (scenario_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO real_world_profile_scenarios
                   (scenario_id, real_world_run_id, profile_id, target_id, cap_multiplier,
                    refill_mode, refill_ratio, growth_scenario, surplus_policy, ladder_id,
                    profile_json, metrics_json, result_hash, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (scenario_id, str(scenario["real_world_run_id"]), str(scenario["profile_id"]), str(scenario["target_id"]), float(scenario["cap_multiplier"]), str(scenario["refill_mode"]).upper(), scenario.get("refill_ratio"), str(scenario["growth_scenario"]), str(scenario["surplus_policy"]).upper(), str(scenario["ladder_id"]).upper(), canonical_json(scenario["profile"]), canonical_json(scenario["metrics"]), digest, utc_now()),
            )
        return scenario_id, before is None

    @staticmethod
    def _decode_real_world_profile_scenario(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, target, default in (("profile_json", "profile", {}), ("metrics_json", "metrics", {})):
            try: item[target] = json.loads(item.pop(field))
            except (TypeError, json.JSONDecodeError): item[target] = default
        return item

    def get_real_world_profile_scenarios(self, real_world_run_id: str, *, profile_id: str | None = None, ladder_id: str | None = None, limit: int = 100000) -> list[dict[str, Any]]:
        clauses = ["real_world_run_id=?"]; params: list[Any] = [str(real_world_run_id)]
        if profile_id is not None: clauses.append("profile_id=?"); params.append(str(profile_id))
        if ladder_id is not None: clauses.append("ladder_id=?"); params.append(str(ladder_id).upper())
        params.append(min(max(int(limit), 1), 200000))
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM real_world_profile_scenarios WHERE {' AND '.join(clauses)} ORDER BY profile_id, target_id, cap_multiplier, growth_scenario, surplus_policy, ladder_id LIMIT ?", params).fetchall()
        return [self._decode_real_world_profile_scenario(row) for row in rows]

    def append_real_world_path_result(self, result: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"real_world_run_id", "path_id", "path_type", "profile_id", "target_id", "cap_multiplier", "refill_mode", "growth_scenario", "surplus_policy", "ladder_id", "result", "result_hash"}
        missing = required.difference(result)
        if missing: raise ValueError("real_world_path_result 缺少字段：" + ", ".join(sorted(missing)))
        if str(result["path_type"]).upper() not in {"HISTORICAL", "STRESS"}: raise ValueError("path_type 无效")
        if str(result["ladder_id"]).upper() not in {"C", "D"}: raise ValueError("real_world path ladder_id 无效")
        digest = str(result["result_hash"]).lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest): raise ValueError("path result_hash 必须是64位 SHA256")
        path_result_id = str(result.get("path_result_id") or sha256_json({"run": str(result["real_world_run_id"]), "path": str(result["path_id"]), "profile": str(result["profile_id"]), "target": str(result["target_id"]), "cap": float(result["cap_multiplier"]), "mode": str(result["refill_mode"]), "ratio": result.get("refill_ratio"), "growth": str(result["growth_scenario"]), "surplus": str(result["surplus_policy"]), "ladder": str(result["ladder_id"]).upper()})[:32])
        with self.connect() as conn:
            self._require_real_world_running(conn, str(result["real_world_run_id"]))
            before = conn.execute("SELECT 1 FROM real_world_path_results WHERE path_result_id=?", (path_result_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO real_world_path_results
                   (path_result_id, real_world_run_id, path_id, path_type, profile_id,
                    target_id, cap_multiplier, refill_mode, refill_ratio, growth_scenario,
                    surplus_policy, ladder_id, result_json, result_hash, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (path_result_id, str(result["real_world_run_id"]), str(result["path_id"]), str(result["path_type"]).upper(), str(result["profile_id"]), str(result["target_id"]), float(result["cap_multiplier"]), str(result["refill_mode"]).upper(), result.get("refill_ratio"), str(result["growth_scenario"]), str(result["surplus_policy"]).upper(), str(result["ladder_id"]).upper(), canonical_json(result["result"]), digest, utc_now()),
            )
        return path_result_id, before is None

    @staticmethod
    def _decode_real_world_path_result(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        try: item["result"] = json.loads(item.pop("result_json"))
        except (TypeError, json.JSONDecodeError): item["result"] = {}
        return item

    def get_real_world_path_results(self, real_world_run_id: str, *, path_id: str | None = None, limit: int = 100000) -> list[dict[str, Any]]:
        clauses = ["real_world_run_id=?"]; params: list[Any] = [str(real_world_run_id)]
        if path_id is not None: clauses.append("path_id=?"); params.append(str(path_id))
        params.append(min(max(int(limit), 1), 200000))
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM real_world_path_results WHERE {' AND '.join(clauses)} ORDER BY path_id, profile_id, target_id, cap_multiplier, growth_scenario, surplus_policy, ladder_id LIMIT ?", params).fetchall()
        return [self._decode_real_world_path_result(row) for row in rows]

    def record_real_world_parameterization_report(self, real_world_run_id: str, report: Mapping[str, Any]) -> tuple[str, bool]:
        payload = canonical_json(report); report_hash = sha256_json(report); report_id = sha256_json({"real_world_run_id": str(real_world_run_id), "report_hash": report_hash})[:32]
        with self.connect() as conn:
            before = conn.execute("SELECT 1 FROM real_world_parameterization_reports WHERE real_world_run_id=?", (str(real_world_run_id),)).fetchone()
            if before: return report_id, False
            conn.execute("INSERT INTO real_world_parameterization_reports(report_id,real_world_run_id,report_hash,report_json,created_at) VALUES(?,?,?,?,?)", (report_id, str(real_world_run_id), report_hash, payload, utc_now()))
        return report_id, True

    def get_real_world_parameterization_report(self, real_world_run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM real_world_parameterization_reports WHERE real_world_run_id=?", (str(real_world_run_id),)).fetchone()
        if not row: return None
        item = dict(row)
        try: item["report"] = json.loads(item.pop("report_json"))
        except (TypeError, json.JSONDecodeError): item["report"] = {}
        return item

    def record_gate_check(self, check: Mapping[str, Any]) -> tuple[str, bool]:
        required = {"gate_name", "status", "triggered_at", "timezone_name", "details"}
        missing = required.difference(check)
        if missing:
            raise ValueError("gate_check 缺少字段：" + ", ".join(sorted(missing)))
        gate_name = str(check["gate_name"])
        status = str(check["status"]).upper()
        if gate_name not in {"GATE_A_SCHEDULED_RUN", "GATE_B_FAILURE_RECOVERY"}:
            raise ValueError("gate_name 无效")
        if status not in {"PASS", "PASS_WITH_LIMITATIONS", "FAIL"}:
            raise ValueError("gate status 无效")
        triggered = iso_timestamp(check["triggered_at"], "triggered_at")
        gate_id = str(check.get("gate_check_id") or sha256_json({"gate_name": gate_name, "triggered_at": triggered, "status": status})[:32])
        with self.connect() as conn:
            before = conn.execute("SELECT 1 FROM phase2a_gate_checks WHERE gate_check_id=?", (gate_id,)).fetchone()
            conn.execute(
                """INSERT OR IGNORE INTO phase2a_gate_checks
                   (gate_check_id, gate_name, status, triggered_at, timezone_name,
                    details_json, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (gate_id, gate_name, status, triggered, str(check["timezone_name"]), canonical_json(check["details"]), utc_now()),
            )
        return gate_id, before is None

    def get_gate_checks(self, gate_name: str | None = None) -> list[dict[str, Any]]:
        clauses = " WHERE gate_name=?" if gate_name else ""
        params = [str(gate_name)] if gate_name else []
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM phase2a_gate_checks{clauses} ORDER BY triggered_at, gate_check_id", params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json"))
            except (TypeError, json.JSONDecodeError):
                item["details"] = {}
            result.append(item)
        return result


def iso_timestamp(value: Any, field: str) -> str:
    if value is None:
        raise ValueError(f"{field} 不能为空")
    return parse_datetime(value, field=field).isoformat().replace("+00:00", "Z")


def _vintage_from_url(source_url: str) -> str | None:
    if not source_url:
        return None
    values = parse_qs(urlparse(source_url).query).get("vintage_date")
    return values[0] if values else None


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    if item.get("value_real") is not None:
        item["value"] = item["value_real"]
    elif item.get("value_text") is not None:
        item["value"] = item["value_text"]
    elif item.get("value_json") is not None:
        item["value"] = json.loads(item["value_json"])
    if item.get("metadata_json") is not None:
        try:
            item["metadata"] = json.loads(item["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            item["metadata"] = {}
    if item.get("eligibility_evidence_json") is not None:
        try:
            item["eligibility_evidence"] = json.loads(item["eligibility_evidence_json"])
        except (TypeError, json.JSONDecodeError):
            item["eligibility_evidence"] = {}
    item["score_eligible"] = bool(item.get("score_eligible"))
    return item


def series_id_for_record(profile_id: str, source: str, metric: str) -> str:
    profile = str(profile_id).upper()
    mapping = {
        ("NDX", "fred_NASDAQ100", "NASDAQ100"): "NDX_CLOSE",
        ("NDX", "fred_VXNCLS", "VXNCLS"): "NDX_VXN",
        ("NDX", "fred_VIXCLS", "VIXCLS"): "VIX",
        ("NDX", "fred_DFII10", "DFII10"): "US10Y_REAL",
        ("NDX", "alfred_NFCI", "NFCI"): "US_NFCI",
        ("NDX", "hom_pe", "forward"): "NDX_FORWARD_PE",
        ("NDX", "hom_pe", "forwardOwn"): "NDX_FORWARD_PE_OWN",
        ("NDX", "hom_pe", "trailing"): "NDX_TTM_PE",
        ("NDX", "siblis_forward", "forward_pe"): "NDX_FORWARD_PE",
        ("NDX", "siblis_trailing", "ttm_pe"): "NDX_TTM_PE",
        ("NDX", "hom_breadth", "pct200"): "NDX_BREADTH_MA200",
        ("NDX", "siblis_eps_page", "eps_ntm_indexed"): "NDX_FORWARD_EPS",
        ("NDX", "siblis_eps_page", "eps_ttm_indexed"): "NDX_TTM_EPS_INDEX",
    }
    if (profile, source, metric) in mapping:
        return mapping[(profile, source, metric)]
    if profile != "NDX" and metric == "YAHOO_PRICE":
        return f"{profile}_CLOSE"
    if metric == "breadth" or metric in {"pct50", "pct200"}:
        return f"{profile}_BREADTH_MA200"
    return f"{profile}_{_safe_identifier(metric).upper()}"


def register_score_model(db_path: Path | str, model: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Insert the frozen model once; reject any attempt to mutate its version."""

    model = dict(model or _read_json(SCORE_MODEL_PATH))
    required = {"model_version", "created_at", "component_config", "weights", "thresholds", "gate_rules", "eligibility_rules"}
    missing = required.difference(model)
    if missing:
        raise ValueError("score model 缺少字段：" + ", ".join(sorted(missing)))
    material = {key: model[key] for key in sorted(required)}
    config_hash = sha256_json(material)
    db_path = Path(db_path)
    migrate_database(db_path)
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        old = conn.execute("SELECT config_hash FROM score_models WHERE model_version=?", (model["model_version"],)).fetchone()
        if old and old[0] != config_hash:
            raise ValueError(f"score_model_version {model['model_version']} 已存在，不能覆盖")
        if not old:
            conn.execute(
                """INSERT INTO score_models
                   (model_version, created_at, component_config, weights,
                    thresholds, gate_rules, eligibility_rules, config_hash)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    model["model_version"],
                    model["created_at"],
                    canonical_json(model["component_config"]),
                    canonical_json(model["weights"]),
                    canonical_json(model["thresholds"]),
                    canonical_json(model["gate_rules"]),
                    canonical_json(model["eligibility_rules"]),
                    config_hash,
                ),
            )
    return {**model, "config_hash": config_hash}


def score_output_fingerprint(model_version: str, inputs: Any, output: Any) -> str:
    return sha256_json({"model_version": model_version, "inputs": inputs, "output": output})


__all__ = [
    "PITRepository",
    "migrate_database",
    "backup_database",
    "register_score_model",
    "score_output_fingerprint",
    "series_id_for_record",
]
