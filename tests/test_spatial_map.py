from __future__ import annotations

import socket

from fastapi.testclient import TestClient
import pytest

from src.api.app import create_app
from src.api import spatial as spatial_module
from src.api.spatial import REPORT_ID, SpatialPayloadError, build_spatial_payload


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr("dotenv.main.dotenv_values", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("configuration")))


class FakeStore:
    def health(self) -> bool: return True
    def latest_report_id(self) -> str: return REPORT_ID


class FakeSpatial:
    def __init__(self, payload: dict[str, object] | None = None, failure: str | None = None) -> None:
        self._payload = payload
        self._failure = failure

    def payload(self, report_id: str) -> dict[str, object]:
        if self._failure:
            raise SpatialPayloadError(self._failure)
        assert report_id == REPORT_ID
        assert self._payload is not None
        return self._payload


def _records(count: int) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    records = [{"feature_sequence": index, "highway": "service", "geometry": {
        "type": "LineString", "coordinates": [[100.0 + index / 100, 6.0], [100.01 + index / 100, 6.01]]}}
        for index in range(count)]
    exposure = [{"source_sequence": index, "intersects_flood": index % 2 == 0} for index in range(count)]
    return records, exposure


def _health() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    from src.transformation.healthcare_candidates import LATITUDE_FIELD, LONGITUDE_FIELD
    return ([{"candidate_sequence": 0, "source_fields": {LONGITUDE_FIELD: "101.2", LATITUDE_FIELD: "6.7"}}],
            [{"source_sequence": 0, "intersects_flood": True}])


def test_payload_is_deterministic_bounded_and_identifier_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spatial_module, "MAX_ROAD_FEATURES", 4)
    roads, exposure = _records(7); healthcare, health_exposure = _health()
    first = build_spatial_payload(roads, exposure, healthcare, health_exposure)
    second = build_spatial_payload(roads, exposure, healthcare, health_exposure)
    assert first == second
    metadata = first["roads"]["metadata"]  # type: ignore[index]
    assert metadata["population_counts"] == {"total": 7, "exposed": 4, "non_exposed": 3}  # type: ignore[index]
    assert metadata["displayed_counts"] == {"total": 4, "exposed": 4, "non_exposed": 0}  # type: ignore[index]
    assert metadata["deterministic_selection_policy"] == "all_exposed_then_evenly_spaced_non_exposed_context"  # type: ignore[index]
    assert metadata["representative_sample"] is False  # type: ignore[index]
    assert metadata["prevalence_inference_allowed"] is False  # type: ignore[index]
    assert first["roads"]["truncated"] is True  # type: ignore[index]
    assert first["healthcare"]["returned_count"] == 1  # type: ignore[index]
    rendered = str(first).casefold()
    assert all(value not in rendered for value in (
        "osm_way_id", "candidate_sequence", "source_sequence", "source_fields", "highway",
        "api-key", "password", "filesystem", "database", "connection",
    ))


def test_spatial_route_is_read_only_bounded_and_rejects_query_drift() -> None:
    roads, exposure = _records(2); healthcare, health_exposure = _health()
    payload = build_spatial_payload(roads, exposure, healthcare, health_exposure)
    client = TestClient(create_app(lambda: FakeStore(), lambda: FakeSpatial(payload)))  # type: ignore[arg-type]
    response = client.get("/v1/spatial/infrastructure", params={"report_id": REPORT_ID})
    assert response.status_code == 200 and response.json() == payload
    assert client.get("/v1/spatial/infrastructure").status_code == 404
    assert client.get("/v1/spatial/infrastructure", params={"report_id": REPORT_ID, "limit": "1"}).status_code == 404
    assert client.post("/v1/spatial/infrastructure").status_code == 405
    assert "access-control-allow-origin" not in response.headers


def test_spatial_failure_is_fixed_safe_and_dashboard_has_local_accessible_map() -> None:
    client = TestClient(create_app(lambda: FakeStore(), lambda: FakeSpatial(failure="payload_unavailable")))  # type: ignore[arg-type]
    response = client.get("/v1/spatial/infrastructure", params={"report_id": REPORT_ID})
    assert response.status_code == 503 and response.json() == {"detail": "payload_unavailable"}
    html = client.get("/dashboard/").text
    script = client.get("/dashboard/app.js").text
    styles = client.get("/dashboard/styles.css").text
    assert all(token in html for token in ('id="exposure-map"', 'tabindex="0"', "Exploratory and non-authoritative", "No external tiles"))
    for disclosure in (
        "All 4,919 ever-exposed road segments are displayed",
        "bounded deterministic subset (5,081 of 27,439)",
        "Visual proportions must not be interpreted as exposure prevalence",
        "authoritative aggregate remains 15.2% exposed",
        "all-years/ever-exposed map",
    ):
        assert disclosure in html
    assert all(token in script for token in (
        "/v1/spatial/infrastructure", "ResizeObserver", "ArrowLeft", "Home",
        "representative_sample", "prevalence_inference_allowed",
        "The map remains the unchanged all-years/ever-exposed view",
    ))
    year_handler = script.split('byId("year-select").addEventListener', 1)[1].split('byId("map-zoom-in")', 1)[0]
    assert "renderMap" not in year_handler and "state.map." not in year_handler
    assert all(token in styles for token in (".map-frame", "#exposure-map", "height: clamp"))
    combined = (html + script + styles).casefold()
    assert all(token not in combined for token in ("leaflet", "mapbox", "openstreetmap.org", "https://", "innerhtml"))


def test_invalid_geometry_and_lineage_are_rejected_safely() -> None:
    roads, exposure = _records(1); healthcare, health_exposure = _health()
    roads[0]["geometry"] = {"type": "Point", "coordinates": [100.0, 6.0]}
    with pytest.raises(SpatialPayloadError, match="^payload_invalid$") as caught:
        build_spatial_payload(roads, exposure, healthcare, health_exposure)
    assert "coordinates" not in repr(caught.value)
