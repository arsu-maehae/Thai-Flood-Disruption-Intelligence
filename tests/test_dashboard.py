from __future__ import annotations

import socket

from fastapi.testclient import TestClient
import pytest

from src.api.app import create_app


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
    def summary(self, report_id: str) -> tuple[dict[str, object], ...]: return ()
    def annual(self, report_id: str) -> tuple[dict[str, object], ...]: return ()
    def road_categories(self, report_id: str) -> tuple[dict[str, object], ...]: return ()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(lambda: FakeStore()))  # type: ignore[arg-type]


def test_dashboard_assets_are_local_safe_and_typed(client: TestClient) -> None:
    expected = {"/dashboard/": "text/html", "/dashboard/styles.css": "text/css", "/dashboard/app.js": "text/javascript"}
    for path, content_type in expected.items():
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(content_type)
        assert response.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "access-control-allow-origin" not in response.headers
    combined = " ".join(client.get(path).text for path in expected)
    assert not any(value in combined.casefold() for value in ("http://", "https://", "cdn", "googleapis", "mapbox", "leaflet"))


def test_html_is_semantic_accessible_and_caveated(client: TestClient) -> None:
    html = client.get("/dashboard/").text
    for token in ("<main", "<h1", "<h2", 'aria-live="polite"', 'id="roads-table"', "Skip to dashboard content",
                  "Healthcare address-text candidates", "Road segments", "not proof that no flood occurred"):
        assert token in html
    prohibited = ("disruption score", "risk score", "all roads are drivable", "complete provider coverage")
    assert all(token not in html.casefold() for token in prohibited)


def test_script_uses_safe_same_origin_contract_and_interactions(client: TestClient) -> None:
    script = client.get("/dashboard/app.js").text
    for route in ("/health", "/v1/metadata", "/v1/exposure/summary", "/v1/exposure/annual", "/v1/exposure/road-categories"):
        assert route in script
    assert "encodeURIComponent(metadata.report_id)" in script
    assert "textContent" in script and "createElement" in script
    assert 'element("table")' in script and 'element("caption"' in script
    assert "innerHTML" not in script and "localStorage" not in script and "sessionStorage" not in script
    assert all(value in script for value in ("year-select", "category-filter", "category-sort", "reset-controls"))
    assert "invalid_response" in script and "could not be loaded" in script


def test_annual_chart_has_a_definite_responsive_plot_height(client: TestClient) -> None:
    styles = client.get("/dashboard/styles.css").text
    chart_rule = next(rule for rule in styles.split("}") if ".bar-chart" in rule)
    assert "height: clamp(10rem, 24vw, 14rem)" in chart_rule
    assert "min-height" not in chart_rule
    script = client.get("/dashboard/app.js").text
    assert "item.exposed_count === 0 ? 0 : Math.max(2," in script
    assert "(item.exposed_count / maximum) * 100" in script


def test_unknown_assets_and_traversal_are_refused_with_safe_headers(client: TestClient) -> None:
    for path in ("/dashboard/missing.js", "/dashboard/%2e%2e/secret"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"
        assert "traceback" not in response.text.casefold()


def test_dashboard_adds_no_application_write_route(client: TestClient) -> None:
    for route in client.app.routes:
        assert not ({"POST", "PUT", "PATCH", "DELETE"} & set(getattr(route, "methods", set())))
    assert client.post("/dashboard/").status_code == 405
