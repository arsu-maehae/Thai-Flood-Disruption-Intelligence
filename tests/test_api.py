from __future__ import annotations

import socket

from fastapi.testclient import TestClient
import pytest

from src.api.app import SERVICE_SCOPE, create_app
from src.database.exposure_store import StoreError


REPORT_ID = "exploratory-exposure-report-v1-20260927-01"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr("dotenv.main.dotenv_values", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("configuration")))


class FakeStore:
    def health(self) -> bool: return True
    def latest_report_id(self) -> str: return REPORT_ID
    def metadata(self, report_id: str) -> dict[str, object]:
        return {"report_id": report_id, "report_version": "1.0",
            "policy_label": "exploratory_non_authoritative",
            "crs_status": "provider_unverified_exploratory_interpretation",
            "manifest_sha256": "a" * 64,
            "interpretation_scope": "exploratory geometric exposure aggregates only",
            "caveats": ["Geometric intersection is not disruption or risk"]}
    def summary(self, report_id: str) -> tuple[dict[str, object], ...]:
        return ({"infrastructure_type": "healthcare", "unit": "records", "total_count": 138,
                 "exposed_count": 18, "non_exposed_count": 120},
                {"infrastructure_type": "roads", "unit": "records", "total_count": 32358,
                 "exposed_count": 4919, "non_exposed_count": 27439})
    def annual(self, report_id: str) -> tuple[dict[str, object], ...]:
        return tuple({"year": year, "infrastructure_type": kind, "unit": "records", "exposed_count": 0}
                     for year in range(2011, 2025) for kind in ("healthcare", "roads"))
    def road_categories(self, report_id: str) -> tuple[dict[str, object], ...]:
        return ({"road_category": "primary", "unit": "segments", "total_count": 100,
                 "exposed_count": 10},
                {"road_category": "service", "unit": "segments", "total_count": 200,
                 "exposed_count": 20})


def test_read_only_endpoints_are_stable_ordered_and_safe() -> None:
    application = create_app(lambda: FakeStore())  # type: ignore[arg-type]
    client = TestClient(application)
    assert client.get("/health").json() == {"status": "ok", "service_scope": SERVICE_SCOPE}
    metadata = client.get("/v1/metadata").json()
    assert metadata["report_id"] == REPORT_ID and metadata["policy_label"] == "exploratory_non_authoritative"
    summary = client.get("/v1/exposure/summary").json()
    assert [item["infrastructure_type"] for item in summary["items"]] == ["healthcare", "roads"]
    assert "geometric intersection" in summary["count_meaning"] and summary["caveats"]
    annual = client.get("/v1/exposure/annual", params={"report_id": REPORT_ID}).json()
    assert [(item["year"], item["infrastructure_type"]) for item in annual["items"]] == [
        (year, kind) for year in range(2011, 2025) for kind in ("healthcare", "roads")]
    categories = client.get("/v1/exposure/road-categories").json()
    assert [item["road_category"] for item in categories["items"]] == ["primary", "service"]
    for route in application.routes:
        assert not ({"POST", "PUT", "PATCH", "DELETE"} & set(getattr(route, "methods", set())))
    combined = " ".join(response.text for response in [client.get("/health"), client.get("/v1/exposure/summary")]).casefold()
    assert all(term not in combined for term in ("password", "connection", "coordinates", "geometry", "properties"))
    assert "access-control-allow-origin" not in client.get("/health").headers


def test_invalid_report_id_and_database_failure_are_safe() -> None:
    client = TestClient(create_app(lambda: FakeStore()))  # type: ignore[arg-type]
    assert client.get("/v1/metadata", params={"report_id": "../unsafe"}).status_code == 422

    class FailedStore(FakeStore):
        def health(self) -> bool:
            raise StoreError("database_unavailable")

    response = TestClient(create_app(lambda: FailedStore())).get("/health")  # type: ignore[arg-type]
    assert response.status_code == 503 and response.json() == {"detail": "database_unavailable"}
    assert "secret" not in response.text and "traceback" not in response.text.casefold()
