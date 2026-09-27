"""Versioned local PostGIS store for verified aggregate exposure results."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
from typing import Callable, Iterable, Sequence

from src.reporting.exposure_report import REPORT_ID, verify_exposure_report


DB_SCHEMA_VERSION = "1.0"
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_CATEGORIES = frozenset({
    "database_failure", "database_unavailable", "input_invalid",
    "input_verification_failed", "reconciliation_failed", "schema_drift",
    "version_collision",
})
_TABLES = frozenset({
    "schema_metadata", "report_versions", "exposure_headline",
    "annual_exposure", "road_category_exposure", "frequency_consistency",
})


class StoreError(RuntimeError):
    """Fixed-category error that never retains database or source text."""

    def __init__(self, category: str) -> None:
        self.category = category if category in _SAFE_CATEGORIES else "database_failure"
        super().__init__(self.category)

    def __repr__(self) -> str:
        return f"StoreError(category={self.category!r})"


@dataclass(frozen=True)
class AggregateSnapshot:
    report_id: str
    report_schema_version: str
    report_version: str
    policy_label: str
    crs_status: str
    manifest_relative_path: str
    manifest_sha256: str
    interpretation_scope: str
    caveats: tuple[str, ...]
    headline: tuple[tuple[str, int, int, int], ...]
    annual: tuple[tuple[int, str, int], ...]
    road_categories: tuple[tuple[str, int, int], ...]
    frequency: tuple[int, int, int, int, str]


@dataclass(frozen=True)
class LoadResult:
    report_id: str
    reused: bool
    headline_rows: int
    annual_rows: int
    road_category_rows: int
    frequency_rows: int


class ExposureStore:
    """Transactional aggregate store using a caller-supplied DB connection factory."""

    def __init__(self, connection_factory: Callable[[], object]) -> None:
        if not callable(connection_factory):
            raise StoreError("input_invalid")
        self._connection_factory = connection_factory

    def __repr__(self) -> str:
        return "ExposureStore(connection_factory=<redacted>)"

    @classmethod
    def from_environment(cls) -> "ExposureStore":
        host = os.environ.get("PHASE5_DB_HOST", "")
        port = os.environ.get("PHASE5_DB_PORT", "")
        name = os.environ.get("PHASE5_DB_NAME", "")
        user = os.environ.get("PHASE5_DB_USER", "")
        password = os.environ.get("PHASE5_DB_PASSWORD", "")
        if (host != "127.0.0.1" or not port.isdigit() or not 1 <= int(port) <= 65535
                or not name or not user or not password):
            raise StoreError("database_unavailable")
        try:
            import psycopg
        except Exception:
            raise StoreError("database_unavailable") from None

        def connect() -> object:
            return psycopg.connect(
                host=host, port=int(port), dbname=name, user=user, password=password,
                connect_timeout=10,
            )

        return cls(connect)

    def apply_schema(self) -> None:
        """Create schema only in an empty project namespace; refuse any drift."""
        try:
            with self._connection_factory() as connection:  # type: ignore[attr-defined]
                with connection.transaction():
                    with connection.cursor() as cursor:
                        cursor.execute(_SCHEMA_TABLES)
                        tables = frozenset(_first(row) for row in cursor.fetchall())
                        if not tables:
                            cursor.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
                        elif tables != _TABLES:
                            raise StoreError("schema_drift")
                        else:
                            cursor.execute(_SCHEMA_VERSION)
                            row = cursor.fetchone()
                            if row is None or _first(row) != DB_SCHEMA_VERSION:
                                raise StoreError("schema_drift")
        except StoreError:
            raise
        except Exception:
            raise StoreError("database_failure") from None

    def load_verified_report(
        self, *, processed_root: Path, report_id: str = REPORT_ID,
    ) -> LoadResult:
        snapshot = read_verified_snapshot(processed_root, report_id)
        reused = False
        try:
            with self._connection_factory() as connection:  # type: ignore[attr-defined]
                with connection.transaction():
                    with connection.cursor() as cursor:
                        cursor.execute(_SELECT_REPORT, (snapshot.report_id,))
                        if cursor.fetchone() is not None:
                            existing = _read_database_snapshot(cursor, snapshot.report_id)
                            if existing != snapshot:
                                raise StoreError("version_collision")
                            reused = True
                        else:
                            _insert_snapshot(cursor, snapshot)
                            inserted = _read_database_snapshot(cursor, snapshot.report_id)
                            if inserted != snapshot:
                                raise StoreError("reconciliation_failed")
        except StoreError:
            raise
        except Exception:
            raise StoreError("database_failure") from None
        return LoadResult(snapshot.report_id, reused, len(snapshot.headline),
                          len(snapshot.annual), len(snapshot.road_categories), 1)

    def health(self) -> bool:
        try:
            with self._connection_factory() as connection:  # type: ignore[attr-defined]
                with connection.cursor() as cursor:
                    cursor.execute("/* phase5:health */ SELECT 1")
                    return cursor.fetchone() is not None
        except Exception:
            raise StoreError("database_unavailable") from None

    def latest_report_id(self) -> str:
        row = self._fetchone(_LATEST_REPORT)
        if row is None or not _valid_report_id(_first(row)):
            raise StoreError("input_invalid")
        return str(_first(row))

    def metadata(self, report_id: str) -> dict[str, object]:
        report_id = _require_report_id(report_id)
        row = self._fetchone(_METADATA, (report_id,))
        if row is None:
            raise StoreError("input_invalid")
        return {
            "report_id": row[0], "report_version": row[1], "policy_label": row[2],
            "crs_status": row[3], "manifest_sha256": row[4],
            "interpretation_scope": row[5], "caveats": list(row[6]),
        }

    def summary(self, report_id: str) -> tuple[dict[str, object], ...]:
        rows = self._fetchall(_HEADLINE, (_require_report_id(report_id),))
        if len(rows) != 2:
            raise StoreError("input_invalid")
        return tuple({"infrastructure_type": row[0], "unit": "records",
                      "total_count": row[1], "exposed_count": row[2],
                      "non_exposed_count": row[3]} for row in rows)

    def annual(self, report_id: str) -> tuple[dict[str, object], ...]:
        rows = self._fetchall(_ANNUAL, (_require_report_id(report_id),))
        if len(rows) != 28:
            raise StoreError("input_invalid")
        return tuple({"year": row[0], "infrastructure_type": row[1],
                      "unit": "records", "exposed_count": row[2]} for row in rows)

    def road_categories(self, report_id: str) -> tuple[dict[str, object], ...]:
        rows = self._fetchall(_CATEGORIES, (_require_report_id(report_id),))
        if not rows:
            raise StoreError("input_invalid")
        return tuple({"road_category": row[0], "unit": "segments",
                      "total_count": row[1], "exposed_count": row[2]} for row in rows)

    def _fetchone(self, query: str, parameters: tuple[object, ...] = ()) -> object | None:
        try:
            with self._connection_factory() as connection:  # type: ignore[attr-defined]
                with connection.cursor() as cursor:
                    cursor.execute(query, parameters)
                    return cursor.fetchone()
        except StoreError:
            raise
        except Exception:
            raise StoreError("database_failure") from None

    def _fetchall(self, query: str, parameters: tuple[object, ...]) -> tuple[object, ...]:
        try:
            with self._connection_factory() as connection:  # type: ignore[attr-defined]
                with connection.cursor() as cursor:
                    cursor.execute(query, parameters)
                    return tuple(cursor.fetchall())
        except StoreError:
            raise
        except Exception:
            raise StoreError("database_failure") from None


def read_verified_snapshot(processed_root: Path, report_id: str = REPORT_ID) -> AggregateSnapshot:
    """Verify canonical Phase 4 outputs and return only validated aggregates."""
    report_id = _require_report_id(report_id)
    root = Path(processed_root).resolve()
    directory = root / "reports" / "pattani" / report_id
    summary_path = directory / "report_summary.json"
    manifest_path = directory / "report_manifest.json"
    try:
        directory.resolve(strict=True).relative_to(root)
        if (directory.is_symlink() or summary_path.is_symlink() or manifest_path.is_symlink()
                or not summary_path.is_file() or not manifest_path.is_file()):
            raise ValueError
    except Exception:
        raise StoreError("input_invalid") from None
    verified = verify_exposure_report(root, report_id)
    if not verified.complete:
        raise StoreError("input_verification_failed")
    summary = _load_json(summary_path)
    manifest = _load_json(manifest_path)
    return _snapshot(summary, manifest, manifest_path.relative_to(root).as_posix(),
                     hashlib.sha256(manifest_path.read_bytes()).hexdigest(), report_id)


def _snapshot(summary: object, manifest: object, relative_manifest: str,
              manifest_hash: str, report_id: str) -> AggregateSnapshot:
    try:
        if not isinstance(summary, dict) or not isinstance(manifest, dict): raise ValueError
        if summary.get("report_id") != report_id or manifest.get("report_id") != report_id: raise ValueError
        if summary.get("policy_label") != "exploratory_non_authoritative": raise ValueError
        if summary.get("crs_status") != "provider_unverified_exploratory_interpretation": raise ValueError
        headline_value = summary["headline"]
        headline = tuple((kind, *_counts(headline_value[kind])) for kind in ("healthcare", "roads"))
        if headline != (("healthcare", 138, 18, 120), ("roads", 32358, 4919, 27439)): raise ValueError
        years = summary["years"]
        road = summary["annual_road_exposed"]; healthcare = summary["annual_healthcare_exposed"]
        if years != list(range(2011, 2025)) or len(road) != 14 or len(healthcare) != 14: raise ValueError
        annual = tuple((year, kind, _count(values[index]))
                       for index, year in enumerate(years)
                       for kind, values in (("healthcare", healthcare), ("roads", road)))
        categories_value = summary["road_categories"]
        if not isinstance(categories_value, list): raise ValueError
        categories = tuple(sorted((_category(item) for item in categories_value), key=lambda item: item[0]))
        if sum(item[1] for item in categories) != 32358 or sum(item[2] for item in categories) != 4919: raise ValueError
        frequency_value = summary["frequency_consistency"]
        frequency = (_count(frequency_value["feature_count"]), _count(frequency_value["match_count"]),
                     _count(frequency_value["mismatch_count"]), _count(frequency_value["missing_or_invalid_count"]),
                     frequency_value["relationship_status"])
        if frequency != (112073, 112073, 0, 0, "observed_structural_relationship_only"): raise ValueError
        caveats_value = summary["caveats"]
        if (not isinstance(caveats_value, list) or not caveats_value
                or any(not _safe_text(value, 500) for value in caveats_value)): raise ValueError
        interpretation = manifest["interpretation_scope"]
        if not _safe_text(interpretation, 500): raise ValueError
        report_version = manifest["report_version"]; schema_version = manifest["schema_version"]
        if not _safe_text(report_version, 40) or not _safe_text(schema_version, 40): raise ValueError
        return AggregateSnapshot(report_id, schema_version, report_version,
            "exploratory_non_authoritative", "provider_unverified_exploratory_interpretation",
            relative_manifest, manifest_hash, interpretation, tuple(caveats_value),
            headline, annual, categories, frequency)
    except (KeyError, TypeError, ValueError, IndexError):
        raise StoreError("input_invalid") from None


def _insert_snapshot(cursor: object, value: AggregateSnapshot) -> None:
    cursor.execute(_INSERT_REPORT, (value.report_id, value.report_schema_version,
        value.report_version, value.policy_label, value.crs_status,
        value.manifest_relative_path, value.manifest_sha256,
        value.interpretation_scope, list(value.caveats)))
    cursor.executemany(_INSERT_HEADLINE, ((value.report_id, *row) for row in value.headline))
    cursor.executemany(_INSERT_ANNUAL, ((value.report_id, *row) for row in value.annual))
    cursor.executemany(_INSERT_CATEGORY, ((value.report_id, *row) for row in value.road_categories))
    cursor.execute(_INSERT_FREQUENCY, (value.report_id, *value.frequency))


def _read_database_snapshot(cursor: object, report_id: str) -> AggregateSnapshot:
    cursor.execute(_SELECT_REPORT_FULL, (report_id,)); metadata = cursor.fetchone()
    if metadata is None: raise StoreError("reconciliation_failed")
    cursor.execute(_SELECT_HEADLINE_DB, (report_id,)); headline = tuple(cursor.fetchall())
    cursor.execute(_SELECT_ANNUAL_DB, (report_id,)); annual = tuple(cursor.fetchall())
    cursor.execute(_SELECT_CATEGORIES_DB, (report_id,)); categories = tuple(cursor.fetchall())
    cursor.execute(_SELECT_FREQUENCY_DB, (report_id,)); frequency = cursor.fetchone()
    if frequency is None: raise StoreError("reconciliation_failed")
    return AggregateSnapshot(report_id, metadata[0], metadata[1], metadata[2], metadata[3],
        metadata[4], metadata[5], metadata[6], tuple(metadata[7]), tuple(headline),
        tuple(annual), tuple(categories), tuple(frequency))


def _load_json(path: Path) -> object:
    def duplicate_safe(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result: raise ValueError
            result[key] = value
        return result
    try:
        return json.loads(path.read_bytes(), object_pairs_hook=duplicate_safe,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except Exception:
        raise StoreError("input_invalid") from None


def _counts(value: object) -> tuple[int, int, int]:
    if not isinstance(value, dict): raise ValueError
    values = (_count(value["total"]), _count(value["exposed"]), _count(value["non_exposed"]))
    if values[0] != values[1] + values[2]: raise ValueError
    return values


def _category(value: object) -> tuple[str, int, int]:
    if not isinstance(value, dict) or not _safe_text(value.get("category"), 100): raise ValueError
    exposed = _count(value["ever_exposed"]); total = _count(value["total_records"])
    if exposed > total: raise ValueError
    return str(value["category"]), total, exposed


def _count(value: object) -> int:
    if type(value) is not int or value < 0: raise ValueError
    return value


def _safe_text(value: object, maximum: int) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and all(ord(character) >= 32 for character in value))


def _valid_report_id(value: object) -> bool:
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


def _require_report_id(value: object) -> str:
    if not _valid_report_id(value): raise StoreError("input_invalid")
    return str(value)


def _first(row: object) -> object:
    return row[0]  # type: ignore[index]


_SCHEMA_TABLES = "/* phase5:schema_tables */ SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'pattani_exposure'"
_SCHEMA_VERSION = "/* phase5:schema_version */ SELECT schema_version FROM pattani_exposure.schema_metadata WHERE singleton = true"
_SELECT_REPORT = "/* phase5:select_report */ SELECT report_id FROM pattani_exposure.report_versions WHERE report_id = %s"
_SELECT_REPORT_FULL = "/* phase5:select_report_full */ SELECT schema_version, report_version, policy_label, crs_status, manifest_relative_path, manifest_sha256, interpretation_scope, caveats FROM pattani_exposure.report_versions WHERE report_id = %s"
_SELECT_HEADLINE_DB = "/* phase5:select_headline_db */ SELECT infrastructure_type, total_count, exposed_count, non_exposed_count FROM pattani_exposure.exposure_headline WHERE report_id = %s ORDER BY infrastructure_type"
_SELECT_ANNUAL_DB = "/* phase5:select_annual_db */ SELECT year, infrastructure_type, exposed_count FROM pattani_exposure.annual_exposure WHERE report_id = %s ORDER BY year, infrastructure_type"
_SELECT_CATEGORIES_DB = "/* phase5:select_categories_db */ SELECT road_category, total_count, exposed_count FROM pattani_exposure.road_category_exposure WHERE report_id = %s ORDER BY road_category"
_SELECT_FREQUENCY_DB = "/* phase5:select_frequency_db */ SELECT feature_count, match_count, mismatch_count, missing_or_invalid_count, relationship_status FROM pattani_exposure.frequency_consistency WHERE report_id = %s"
_INSERT_REPORT = "/* phase5:insert_report */ INSERT INTO pattani_exposure.report_versions (report_id, schema_version, report_version, policy_label, crs_status, manifest_relative_path, manifest_sha256, interpretation_scope, caveats) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
_INSERT_HEADLINE = "/* phase5:insert_headline */ INSERT INTO pattani_exposure.exposure_headline (report_id, infrastructure_type, total_count, exposed_count, non_exposed_count) VALUES (%s,%s,%s,%s,%s)"
_INSERT_ANNUAL = "/* phase5:insert_annual */ INSERT INTO pattani_exposure.annual_exposure (report_id, year, infrastructure_type, exposed_count) VALUES (%s,%s,%s,%s)"
_INSERT_CATEGORY = "/* phase5:insert_category */ INSERT INTO pattani_exposure.road_category_exposure (report_id, road_category, total_count, exposed_count) VALUES (%s,%s,%s,%s)"
_INSERT_FREQUENCY = "/* phase5:insert_frequency */ INSERT INTO pattani_exposure.frequency_consistency (report_id, feature_count, match_count, mismatch_count, missing_or_invalid_count, relationship_status) VALUES (%s,%s,%s,%s,%s,%s)"
_LATEST_REPORT = "/* phase5:latest_report */ SELECT report_id FROM pattani_exposure.report_versions ORDER BY loaded_at_utc DESC, report_id DESC LIMIT 1"
_METADATA = "/* phase5:metadata */ SELECT report_id, report_version, policy_label, crs_status, manifest_sha256, interpretation_scope, caveats FROM pattani_exposure.report_versions WHERE report_id = %s"
_HEADLINE = "/* phase5:headline */ SELECT infrastructure_type, total_count, exposed_count, non_exposed_count FROM pattani_exposure.exposure_headline WHERE report_id = %s ORDER BY infrastructure_type"
_ANNUAL = "/* phase5:annual */ SELECT year, infrastructure_type, exposed_count FROM pattani_exposure.annual_exposure WHERE report_id = %s ORDER BY year, infrastructure_type"
_CATEGORIES = "/* phase5:categories */ SELECT road_category, total_count, exposed_count FROM pattani_exposure.road_category_exposure WHERE report_id = %s ORDER BY road_category"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load one verified local aggregate exposure report")
    parser.add_argument("--processed-root", type=Path, required=True)
    parser.add_argument("--report-id", default=REPORT_ID)
    parser.add_argument("--apply-schema", action="store_true")
    args = parser.parse_args(argv)
    try:
        store = ExposureStore.from_environment()
        if args.apply_schema: store.apply_schema()
        result = store.load_verified_report(processed_root=args.processed_root, report_id=args.report_id)
        print(json.dumps({"status": "complete", "report_id": result.report_id,
                          "reused": result.reused}, sort_keys=True))
        return 0
    except StoreError as error:
        print(json.dumps({"status": "failed", "category": error.category}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
