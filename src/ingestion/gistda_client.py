"""HTTP access for the documented GISTDA flood-frequency endpoint."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

import requests

from src.configuration import (
    ConfigurationError,
    GistdaConfig,
    OFFICIAL_GISTDA_API_BASE_URL,
)


FLOOD_FREQUENCY_PATH: Final = "/features/flood-freq"
API_KEY_HEADER: Final = "API-Key"
DEFAULT_TIMEOUT: Final = (5.0, 30.0)


class GistdaClientError(RuntimeError):
    """Base exception for credential-safe GISTDA client failures."""


class GistdaTimeoutError(GistdaClientError):
    """Raised when a request exceeds its connect or read timeout."""

    def __init__(self) -> None:
        super().__init__("GISTDA request timed out")


class GistdaConnectionError(GistdaClientError):
    """Raised when a request cannot be completed at the transport layer."""

    def __init__(self) -> None:
        super().__init__("GISTDA connection failed")


class GistdaHTTPError(GistdaClientError):
    """Raised for an HTTP error response without retaining its body."""

    def __init__(self, status_code: int, content_type: str | None) -> None:
        self.status_code = status_code
        self.content_type = content_type
        super().__init__(f"GISTDA request failed with HTTP status {status_code}")


@dataclass(frozen=True)
class GistdaResponse:
    """Exact response bytes and minimal immutable HTTP metadata."""

    content: bytes = field(repr=False)
    status_code: int
    content_type: str | None


class GistdaClient:
    """Small, injectable client for GISTDA historical flood recurrence data."""

    def __init__(
        self,
        config: GistdaConfig,
        *,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    ) -> None:
        if config.api_base_url != OFFICIAL_GISTDA_API_BASE_URL:
            raise ConfigurationError(
                "GISTDA_API_BASE_URL must match the official base URL"
            )
        if len(timeout) != 2 or any(value <= 0 for value in timeout):
            raise ValueError("timeout must contain positive connect and read values")

        self._config = config
        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self._timeout = timeout

    @property
    def endpoint_url(self) -> str:
        """Return the endpoint URL derived from validated configuration."""

        return f"{self._config.api_base_url.rstrip('/')}{FLOOD_FREQUENCY_PATH}"

    def get_flood_frequency(
        self,
        *,
        bbox: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        pv_idn: str | None = None,
        ap_idn: str | None = None,
        tb_idn: str | None = None,
    ) -> GistdaResponse:
        """Execute one request using only documented query parameters."""

        params = self._build_query_params(
            bbox=bbox,
            limit=limit,
            offset=offset,
            pv_idn=pv_idn,
            ap_idn=ap_idn,
            tb_idn=tb_idn,
        )

        try:
            response = self._session.get(
                self.endpoint_url,
                headers={API_KEY_HEADER: self._config.api_key},
                params=params,
                timeout=self._timeout,
            )
        except requests.Timeout:
            raise GistdaTimeoutError() from None
        except requests.RequestException:
            raise GistdaConnectionError() from None

        content_type = response.headers.get("Content-Type")
        if response.status_code != 200:
            raise GistdaHTTPError(response.status_code, content_type)

        return GistdaResponse(
            content=response.content,
            status_code=response.status_code,
            content_type=content_type,
        )

    def close(self) -> None:
        """Close a session created by this client."""

        if self._owns_session:
            self._session.close()

    def __enter__(self) -> GistdaClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _build_query_params(
        *,
        bbox: str | None,
        limit: int | None,
        offset: int | None,
        pv_idn: str | None,
        ap_idn: str | None,
        tb_idn: str | None,
    ) -> dict[str, str | int]:
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10000
        ):
            raise ValueError("limit must be an integer from 1 through 10000")
        if offset is not None and (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
        ):
            raise ValueError("offset must be a non-negative integer")

        values: tuple[tuple[str, str | int | None], ...] = (
            ("bbox", bbox),
            ("limit", limit),
            ("offset", offset),
            ("pv_idn", pv_idn),
            ("ap_idn", ap_idn),
            ("tb_idn", tb_idn),
        )
        return {name: value for name, value in values if value is not None}
