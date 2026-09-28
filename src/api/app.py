"""Loopback-oriented FastAPI service for exploratory aggregate results."""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from src.dashboard import STATIC_DIRECTORY
from src.database.exposure_store import ExposureStore, StoreError


SERVICE_SCOPE = "exploratory geometric exposure aggregates"
REPORT_CAVEAT = "Geometric intersection is not confirmed disruption, risk, severity, accessibility, damage, or complete coverage."
ReportId = Annotated[
    str | None,
    Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
]


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"]
    service_scope: Literal["exploratory geometric exposure aggregates"]


class MetadataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str
    report_version: str
    policy_label: Literal["exploratory_non_authoritative"]
    crs_status: Literal["provider_unverified_exploratory_interpretation"]
    manifest_sha256: str
    interpretation_scope: str
    caveats: list[str]


class HeadlineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    infrastructure_type: Literal["healthcare", "roads"]
    unit: Literal["records"]
    total_count: int
    exposed_count: int
    non_exposed_count: int


class SummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str
    service_scope: Literal["exploratory geometric exposure aggregates"]
    count_meaning: Literal["records with at least one geometric intersection in the observed snapshot"]
    caveats: list[str]
    items: list[HeadlineItem]


class AnnualItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: int
    infrastructure_type: Literal["healthcare", "roads"]
    unit: Literal["records"]
    exposed_count: int


class AnnualResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str
    service_scope: Literal["exploratory geometric exposure aggregates"]
    count_meaning: Literal["records with at least one geometric intersection for the observed yearly flag"]
    caveats: list[str]
    items: list[AnnualItem]


class RoadCategoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    road_category: str
    unit: Literal["segments"]
    total_count: int
    exposed_count: int


class RoadCategoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str
    service_scope: Literal["exploratory geometric exposure aggregates"]
    count_meaning: Literal["road segments with at least one geometric intersection in the observed snapshot"]
    caveats: list[str]
    items: list[RoadCategoryItem]


def _default_provider() -> ExposureStore:
    return ExposureStore.from_environment()


def create_app(provider: Callable[[], ExposureStore] | None = None) -> FastAPI:
    """Create an API with an injected store provider; no connection occurs here."""
    selected_provider = provider or _default_provider

    def repository() -> Generator[ExposureStore, None, None]:
        yield selected_provider()

    application = FastAPI(
        title="Pattani Exploratory Exposure Aggregates",
        version="1.0",
        description=("Local read-only service for exploratory geometric exposure aggregates. "
                     "It is not disruption, risk, severity, accessibility, prediction, damage, "
                     "or complete coverage."),
    )

    @application.middleware("http")
    async def dashboard_security_headers(request: Request, call_next: Callable):
        response = await call_next(request)
        if request.url.path.startswith("/dashboard"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; font-src 'none'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
            )
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        return response

    @application.get("/dashboard/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIRECTORY / "index.html", media_type="text/html")

    @application.get("/dashboard/styles.css", include_in_schema=False)
    def dashboard_styles() -> FileResponse:
        return FileResponse(STATIC_DIRECTORY / "styles.css", media_type="text/css")

    @application.get("/dashboard/app.js", include_in_schema=False)
    def dashboard_script() -> FileResponse:
        return FileResponse(STATIC_DIRECTORY / "app.js", media_type="text/javascript")

    @application.exception_handler(StoreError)
    async def safe_store_error(_: Request, error: StoreError) -> JSONResponse:
        status = 503 if error.category in {"database_failure", "database_unavailable"} else 404
        return JSONResponse(status_code=status, content={"detail": error.category})

    @application.get("/health", response_model=HealthResponse)
    def health(store: ExposureStore = Depends(repository)) -> dict[str, object]:
        if not store.health():
            raise StoreError("database_unavailable")
        return {"status": "ok", "service_scope": SERVICE_SCOPE}

    @application.get("/v1/metadata", response_model=MetadataResponse)
    def metadata(report_id: ReportId = None,
                 store: ExposureStore = Depends(repository)) -> dict[str, object]:
        return store.metadata(report_id or store.latest_report_id())

    @application.get("/v1/exposure/summary", response_model=SummaryResponse)
    def summary(report_id: ReportId = None,
                store: ExposureStore = Depends(repository)) -> dict[str, object]:
        selected = report_id or store.latest_report_id()
        return {"report_id": selected, "service_scope": SERVICE_SCOPE,
                "count_meaning": "records with at least one geometric intersection in the observed snapshot",
                "caveats": [REPORT_CAVEAT],
                "items": list(store.summary(selected))}

    @application.get("/v1/exposure/annual", response_model=AnnualResponse)
    def annual(report_id: ReportId = None,
               store: ExposureStore = Depends(repository)) -> dict[str, object]:
        selected = report_id or store.latest_report_id()
        return {"report_id": selected, "service_scope": SERVICE_SCOPE,
                "count_meaning": "records with at least one geometric intersection for the observed yearly flag",
                "caveats": [REPORT_CAVEAT],
                "items": list(store.annual(selected))}

    @application.get("/v1/exposure/road-categories", response_model=RoadCategoryResponse)
    def categories(report_id: ReportId = None,
                   store: ExposureStore = Depends(repository)) -> dict[str, object]:
        selected = report_id or store.latest_report_id()
        return {"report_id": selected, "service_scope": SERVICE_SCOPE,
                "count_meaning": "road segments with at least one geometric intersection in the observed snapshot",
                "caveats": [REPORT_CAVEAT],
                "items": list(store.road_categories(selected))}

    return application


app = create_app()
