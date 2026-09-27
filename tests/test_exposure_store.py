from __future__ import annotations

import copy
from pathlib import Path
import socket

import pytest

import src.database.exposure_store as store_module
from src.database.exposure_store import ExposureStore, StoreError


REPORT_ID = "exploratory-exposure-report-v1-20260927-01"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr("dotenv.main.dotenv_values", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("configuration")))


class _Verified:
    complete = True


class FakeDatabase:
    def __init__(self) -> None:
        self.state = {"reports": {}, "headline": {}, "annual": {}, "categories": {}, "frequency": {}}
        self.fail_marker: str | None = None

    def connect(self) -> "FakeConnection":
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.working: dict[str, dict] | None = None

    @property
    def state(self) -> dict[str, dict]:
        return self.working if self.working is not None else self.database.state

    def __enter__(self) -> "FakeConnection": return self
    def __exit__(self, *args: object) -> None: return None
    def cursor(self) -> "FakeCursor": return FakeCursor(self)
    def transaction(self) -> "FakeTransaction": return FakeTransaction(self)


class FakeTransaction:
    def __init__(self, connection: FakeConnection) -> None: self.connection = connection
    def __enter__(self) -> None:
        self.connection.working = copy.deepcopy(self.connection.database.state)
    def __exit__(self, kind: object, value: object, trace: object) -> None:
        if kind is None:
            self.connection.database.state = self.connection.working  # type: ignore[assignment]
        self.connection.working = None


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection; self.one: object | None = None; self.rows: list[object] = []
    def __enter__(self) -> "FakeCursor": return self
    def __exit__(self, *args: object) -> None: return None
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        marker = _marker(query)
        if marker == self.connection.database.fail_marker: raise RuntimeError("secret database text")
        state = self.connection.state; report_id = str(parameters[0]) if parameters else ""
        self.one = None; self.rows = []
        if marker == "health": self.one = (1,)
        elif marker == "select_report": self.one = (report_id,) if report_id in state["reports"] else None
        elif marker == "insert_report": state["reports"][report_id] = tuple(parameters[1:])
        elif marker == "insert_frequency": state["frequency"][report_id] = tuple(parameters[1:])
        elif marker == "select_report_full": self.one = state["reports"].get(report_id)
        elif marker == "select_headline_db": self.rows = sorted(state["headline"].get(report_id, []))
        elif marker == "select_annual_db": self.rows = sorted(state["annual"].get(report_id, []))
        elif marker == "select_categories_db": self.rows = sorted(state["categories"].get(report_id, []))
        elif marker == "select_frequency_db": self.one = state["frequency"].get(report_id)
        elif marker == "latest_report": self.one = (sorted(state["reports"])[-1],) if state["reports"] else None
        elif marker == "metadata":
            value = state["reports"].get(report_id)
            if value: self.one = (report_id, value[1], value[2], value[3], value[5], value[6], value[7])
        elif marker == "headline": self.rows = sorted(state["headline"].get(report_id, []))
        elif marker == "annual": self.rows = sorted(state["annual"].get(report_id, []))
        elif marker == "categories": self.rows = sorted(state["categories"].get(report_id, []))
        else: raise AssertionError(marker)
    def executemany(self, query: str, parameters: object) -> None:
        marker = _marker(query)
        if marker == self.connection.database.fail_marker: raise RuntimeError("secret database text")
        table = {"insert_headline": "headline", "insert_annual": "annual", "insert_category": "categories"}[marker]
        for value in parameters:  # type: ignore[union-attr]
            report_id, *row = value
            self.connection.state[table].setdefault(report_id, []).append(tuple(row))
    def fetchone(self) -> object | None: return self.one
    def fetchall(self) -> list[object]: return self.rows


def _marker(query: str) -> str:
    return query.split("phase5:", 1)[1].split(" ", 1)[0].split("*/", 1)[0]


def _report(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = root / "reports/pattani" / REPORT_ID; directory.mkdir(parents=True)
    summary = {
        "report_id": REPORT_ID, "policy_label": "exploratory_non_authoritative",
        "crs_status": "provider_unverified_exploratory_interpretation",
        "headline": {"roads": {"total": 32358, "exposed": 4919, "non_exposed": 27439},
                     "healthcare": {"total": 138, "exposed": 18, "non_exposed": 120}},
        "years": list(range(2011, 2025)),
        "annual_road_exposed": [74, 15, 1009, 232, 15, 42, 4042, 0, 121, 548, 698, 648, 629, 840],
        "annual_healthcare_exposed": [0, 0, 2, 1, 0, 0, 15, 0, 0, 3, 3, 4, 3, 6],
        "road_categories": [{"category": "primary", "total_records": 32358, "ever_exposed": 4919}],
        "frequency_consistency": {"feature_count": 112073, "match_count": 112073,
            "mismatch_count": 0, "missing_or_invalid_count": 0,
            "relationship_status": "observed_structural_relationship_only"},
        "caveats": ["Geometric intersection only", "Not complete coverage"],
    }
    manifest = {"report_id": REPORT_ID, "schema_version": "1.0", "report_version": "1.0",
                "interpretation_scope": "exploratory geometric exposure aggregates only"}
    import json
    (directory / "report_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (directory / "report_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(store_module, "verify_exposure_report", lambda *args, **kwargs: _Verified())
    return directory


def test_deterministic_load_reconciliation_reuse_and_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _report(tmp_path, monkeypatch); database = FakeDatabase(); store = ExposureStore(database.connect)
    result = store.load_verified_report(processed_root=tmp_path)
    assert not result.reused and (result.headline_rows, result.annual_rows, result.frequency_rows) == (2, 28, 1)
    assert store.load_verified_report(processed_root=tmp_path).reused
    database.state["reports"][REPORT_ID] = (*database.state["reports"][REPORT_ID][:5], "0" * 64,
                                                *database.state["reports"][REPORT_ID][6:])
    with pytest.raises(StoreError) as raised: store.load_verified_report(processed_root=tmp_path)
    assert raised.value.category == "version_collision"


def test_transaction_rolls_back_and_invalid_input_never_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _report(tmp_path, monkeypatch); database = FakeDatabase(); database.fail_marker = "insert_frequency"
    with pytest.raises(StoreError) as raised: ExposureStore(database.connect).load_verified_report(processed_root=tmp_path)
    assert raised.value.category == "database_failure" and not database.state["reports"]
    database.fail_marker = None
    summary = (directory / "report_summary.json").read_text(encoding="utf-8").replace('"total": 32358', '"total": true')
    (directory / "report_summary.json").write_text(summary, encoding="utf-8")
    with pytest.raises(StoreError) as invalid: ExposureStore(database.connect).load_verified_report(processed_root=tmp_path)
    assert invalid.value.category == "input_invalid" and not database.state["reports"]


def test_schema_constraints_and_safe_errors() -> None:
    schema = Path("src/database/schema.sql").read_text(encoding="utf-8")
    for clause in ("CREATE EXTENSION IF NOT EXISTS postgis", "year BETWEEN 2011 AND 2024",
                   "PRIMARY KEY (report_id, year, infrastructure_type)",
                   "PRIMARY KEY (report_id, road_category)", "reject_mutation"):
        assert clause in schema
    error = StoreError("database_failure")
    assert str(error) == "database_failure" and "password" not in repr(error)
    assert "secret" not in repr(ExposureStore(lambda: None))


def test_nonfinite_json_and_unknown_report_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _report(tmp_path, monkeypatch)
    summary = (directory / "report_summary.json").read_text(encoding="utf-8")
    (directory / "report_summary.json").write_text(
        summary.replace('"annual_road_exposed": [74', '"annual_road_exposed": [NaN'),
        encoding="utf-8",
    )
    database = FakeDatabase(); store = ExposureStore(database.connect)
    with pytest.raises(StoreError) as invalid:
        store.load_verified_report(processed_root=tmp_path)
    assert invalid.value.category == "input_invalid" and not database.state["reports"]
    with pytest.raises(StoreError) as missing:
        store.summary("valid-but-unknown")
    assert missing.value.category == "input_invalid"
