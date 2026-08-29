from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
import requests

from src.configuration import (
    ConfigurationError,
    GistdaConfig,
    OFFICIAL_GISTDA_API_BASE_URL,
)
from src.ingestion.gistda_client import (
    API_KEY_HEADER,
    FLOOD_FREQUENCY_PATH,
    GistdaClient,
    GistdaConnectionError,
    GistdaHTTPError,
    GistdaTimeoutError,
)


DUMMY_API_KEY = "dummy-client-test-key"


@dataclass
class FakeResponse:
    content: bytes = b'{"type":"FeatureCollection","features":[]}'
    status_code: int = 200
    headers: dict[str, str] = field(
        default_factory=lambda: {"Content-Type": "application/json"}
    )


class FakeSession:
    def __init__(
        self,
        response: FakeResponse | None = None,
        error: requests.RequestException | None = None,
    ) -> None:
        self.response = response if response is not None else FakeResponse()
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.close_count = 0

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response

    def close(self) -> None:
        self.close_count += 1


@pytest.fixture(autouse=True)
def prohibit_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_request(*_: object, **__: object) -> None:
        raise AssertionError("live network access is prohibited in client tests")

    monkeypatch.setattr(requests.sessions.Session, "request", fail_request)


def make_config() -> GistdaConfig:
    return GistdaConfig(
        api_base_url=OFFICIAL_GISTDA_API_BASE_URL,
        api_key=DUMMY_API_KEY,
        province_id="94",
    )


def test_success_preserves_request_contract_bytes_and_metadata() -> None:
    raw_content = b"{malformed-json-bytes"
    response = FakeResponse(
        content=raw_content,
        headers={"Content-Type": "application/unexpected"},
    )
    session = FakeSession(response=response)
    timeout = (1.5, 9.5)
    client = GistdaClient(make_config(), session=session, timeout=timeout)  # type: ignore[arg-type]

    result = client.get_flood_frequency(
        bbox="100,6,101,7",
        limit=10,
        offset=0,
        pv_idn="94",
        ap_idn="9401",
        tb_idn="940101",
    )

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == f"{OFFICIAL_GISTDA_API_BASE_URL}{FLOOD_FREQUENCY_PATH}"
    assert call["headers"] == {API_KEY_HEADER: DUMMY_API_KEY}
    assert call["params"] == {
        "bbox": "100,6,101,7",
        "limit": 10,
        "offset": 0,
        "pv_idn": "94",
        "ap_idn": "9401",
        "tb_idn": "940101",
    }
    assert DUMMY_API_KEY not in call["url"]
    assert DUMMY_API_KEY not in repr(call["params"])
    assert call["timeout"] == timeout
    assert result.content is raw_content
    assert result.status_code == 200
    assert result.content_type == "application/unexpected"
    assert raw_content.decode() not in repr(result)
    assert DUMMY_API_KEY not in repr(result)


def test_none_query_parameters_are_omitted() -> None:
    session = FakeSession()
    client = GistdaClient(make_config(), session=session)  # type: ignore[arg-type]

    client.get_flood_frequency(limit=1)

    assert session.calls[0]["params"] == {"limit": 1}


def test_unapproved_base_url_is_rejected_before_session_creation_or_use() -> None:
    rejected_url = "https://example.invalid/resources"
    config = GistdaConfig(
        api_base_url=rejected_url,
        api_key=DUMMY_API_KEY,
        province_id="94",
    )
    injected_session = FakeSession()

    with patch("src.ingestion.gistda_client.requests.Session") as session_factory:
        with pytest.raises(ConfigurationError) as exc_info:
            GistdaClient(config)

    with pytest.raises(ConfigurationError):
        GistdaClient(config, session=injected_session)  # type: ignore[arg-type]

    session_factory.assert_not_called()
    assert injected_session.calls == []
    assert rejected_url not in str(exc_info.value)
    assert DUMMY_API_KEY not in str(exc_info.value)


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 429, 500, 204])
def test_non_200_status_raises_safe_http_error(status_code: int) -> None:
    body = b"sensitive response body"
    session = FakeSession(response=FakeResponse(content=body, status_code=status_code))
    client = GistdaClient(make_config(), session=session)  # type: ignore[arg-type]

    with pytest.raises(GistdaHTTPError) as exc_info:
        client.get_flood_frequency()

    error = exc_info.value
    assert error.status_code == status_code
    assert error.content_type == "application/json"
    assert DUMMY_API_KEY not in str(error)
    assert DUMMY_API_KEY not in repr(error)
    assert body.decode() not in str(error)
    assert body.decode() not in repr(error)


@pytest.mark.parametrize(
    ("request_error", "expected_error"),
    [
        (requests.Timeout("unsafe transport detail"), GistdaTimeoutError),
        (requests.ConnectionError("unsafe transport detail"), GistdaConnectionError),
    ],
)
def test_transport_errors_are_mapped_without_chaining_or_credentials(
    request_error: requests.RequestException,
    expected_error: type[Exception],
) -> None:
    session = FakeSession(error=request_error)
    client = GistdaClient(make_config(), session=session)  # type: ignore[arg-type]

    with pytest.raises(expected_error) as exc_info:
        client.get_flood_frequency()

    assert exc_info.value.__cause__ is None
    assert DUMMY_API_KEY not in str(exc_info.value)
    assert DUMMY_API_KEY not in repr(exc_info.value)


@pytest.mark.parametrize("invalid_limit", [0, 10001, -1, "10", True])
def test_invalid_limit_is_rejected_before_session_call(invalid_limit: object) -> None:
    session = FakeSession()
    client = GistdaClient(make_config(), session=session)  # type: ignore[arg-type]

    with pytest.raises(ValueError) as exc_info:
        client.get_flood_frequency(limit=invalid_limit)  # type: ignore[arg-type]

    assert session.calls == []
    assert DUMMY_API_KEY not in str(exc_info.value)


@pytest.mark.parametrize("invalid_offset", [-1, "0", True])
def test_invalid_offset_is_rejected_before_session_call(invalid_offset: object) -> None:
    session = FakeSession()
    client = GistdaClient(make_config(), session=session)  # type: ignore[arg-type]

    with pytest.raises(ValueError) as exc_info:
        client.get_flood_frequency(offset=invalid_offset)  # type: ignore[arg-type]

    assert session.calls == []
    assert DUMMY_API_KEY not in str(exc_info.value)


def test_injected_session_is_not_closed() -> None:
    session = FakeSession()

    with GistdaClient(make_config(), session=session) as client:  # type: ignore[arg-type]
        client.get_flood_frequency()
    client.close()

    assert session.close_count == 0


def test_client_owned_session_is_closed_by_close() -> None:
    session = FakeSession()
    with patch("src.ingestion.gistda_client.requests.Session", return_value=session):
        client = GistdaClient(make_config())
        client.close()

    assert session.close_count == 1


def test_client_owned_session_is_closed_by_context_manager() -> None:
    session = FakeSession()
    with patch("src.ingestion.gistda_client.requests.Session", return_value=session):
        with GistdaClient(make_config()):
            pass

    assert session.close_count == 1
