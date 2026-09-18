from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlsplit

import pytest
import requests

from src.configuration import GistdaConfig, OFFICIAL_GISTDA_API_BASE_URL
from src.ingestion import flood_frequency
from src.ingestion.flood_frequency import (
    SourceSanitizationError,
    ingest_pattani_sample,
)
from src.ingestion.gistda_client import GistdaHTTPError, GistdaResponse


DUMMY_KEY = "dummy-ingestion-test-key"
FIXED_TIME = datetime(2026, 8, 29, 3, 4, 5, 678901, tzinfo=timezone.utc)


def synthetic_response_bytes() -> bytes:
    payload = {
        "type": "FeatureCollection",
        "links": [
            {
                "href": (
                    "https://api.example.test/features/flood-freq"
                    f"?api_key={DUMMY_KEY}&limit=10&safe=hello%20world"
                ),
                "rel": "self",
            },
            {
                "href": (
                    "https://api.example.test/features/flood-freq"
                    "?api_key=encoded-secret&offset=0"
                ),
                "rel": "alternate",
            },
            {
                "href": (
                    "https://api.example.test/features/flood-freq"
                    "?API_KEY=case-secret&pv_idn=94"
                ),
                "rel": "next",
            },
        ],
        "api_key": "direct-secret",
        "nested": {"API-Key": "nested-secret", "safe": "preserved"},
        "features": [],
        "numberReturned": 0,
        "numberMatched": 0,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class FakeClient:
    def __init__(
        self,
        response: GistdaResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response or GistdaResponse(
            content=synthetic_response_bytes(),
            status_code=200,
            content_type="application/geo+json",
        )
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def get_flood_frequency(self, **kwargs: Any) -> GistdaResponse:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture(autouse=True)
def prohibit_external_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*_: object, **__: object) -> None:
        raise AssertionError("live network access is prohibited")

    def fail_dotenv(*_: object, **__: object) -> None:
        raise AssertionError("dotenv access is prohibited")

    monkeypatch.setattr(requests.sessions.Session, "request", fail_network)
    monkeypatch.setattr("src.configuration.dotenv_values", fail_dotenv)
    monkeypatch.setattr("src.configuration.load_config", fail_dotenv)


def make_config(province_id: str = "94") -> GistdaConfig:
    return GistdaConfig(
        api_base_url=OFFICIAL_GISTDA_API_BASE_URL,
        api_key=DUMMY_KEY,
        province_id=province_id,
    )


def ingest(temp_root: Path, client: FakeClient):
    return ingest_pattani_sample(
        config=make_config(),
        client=client,  # type: ignore[arg-type]
        output_root=temp_root,
        retrieved_at=FIXED_TIME,
    )


def percent_encode(value: str, depth: int) -> str:
    for _ in range(depth):
        value = "".join(f"%{byte:02X}" for byte in value.encode("utf-8"))
    return value


def clean_payload() -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": [], "numberReturned": 0, "links": []}


def assert_credential_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any],
    *, api_key: str = DUMMY_KEY, offending: str = "",
    content_type: str = "application/json",
) -> None:
    response_bytes = json.dumps(payload).encode("utf-8")
    client = FakeClient(response=GistdaResponse(response_bytes, 200, content_type))
    config = GistdaConfig(OFFICIAL_GISTDA_API_BASE_URL, api_key, "94")
    output_root = tmp_path / "must-not-be-created"

    def forbid_filesystem(*_: Any, **__: Any) -> None:
        raise AssertionError("rejected credentials reached filesystem activity")

    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "exists", forbid_filesystem)
        guarded.setattr(Path, "mkdir", forbid_filesystem)
        guarded.setattr(Path, "unlink", forbid_filesystem)
        guarded.setattr(flood_frequency.tempfile, "NamedTemporaryFile", forbid_filesystem)
        guarded.setattr(flood_frequency, "_write_pair_atomically", forbid_filesystem)
        with pytest.raises(SourceSanitizationError) as caught:
            ingest_pattani_sample(config=config, client=client, output_root=output_root,
                                  retrieved_at=FIXED_TIME)
    assert type(caught.value) is SourceSanitizationError
    message = str(caught.value) + repr(caught.value)
    assert api_key not in message
    if offending:
        assert offending not in message
    for depth in (1, 2, 3):
        assert percent_encode(api_key, depth) not in message
    assert client.calls == [{"pv_idn": "94", "limit": 10, "offset": 0}]
    assert not output_root.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("depth", [0, 1, 2, 3, 6])
@pytest.mark.parametrize("location", ["nested_value", "object_key"])
def test_configured_key_is_rejected_at_every_encoding_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int, location: str,
) -> None:
    encoded = percent_encode(DUMMY_KEY, depth)
    candidate = f"prefix-{encoded}-suffix"
    payload = clean_payload()
    payload["nested"] = [{"safe": [candidate]}] if location == "nested_value" else {candidate: "safe"}
    assert_credential_rejection(tmp_path, monkeypatch, payload, offending=candidate)


@pytest.mark.parametrize("key", ["dummy form key", "dummy+percent%25key", "dummy ไทย+key"])
@pytest.mark.parametrize("depth", [0, 1, 2, 4])
def test_form_encoding_checks_intermediate_key_before_plus_or_percent_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, depth: int,
) -> None:
    encoded = key
    for _ in range(depth):
        encoded = quote_plus(encoded, safe="")
    payload = clean_payload()
    payload["nested"] = {"safe": f"prefix-{encoded}-suffix"}
    assert_credential_rejection(tmp_path, monkeypatch, payload, api_key=key, offending=encoded)


@pytest.mark.parametrize("name", ["api_key", "API_KEY", "aPi_KeY", "api-key", "API-Key",
                                  "aPi-kEy", "authorization", "AUTHORIZATION", "aUtHoRiZaTiOn"])
@pytest.mark.parametrize("depth", [1, 2, 4])
def test_encoded_credential_object_names_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, depth: int,
) -> None:
    encoded_name = percent_encode(name, depth)
    payload = clean_payload()
    payload["nested"] = [{encoded_name: "unrelated-secret"}]
    assert_credential_rejection(tmp_path, monkeypatch, payload, offending=encoded_name)


@pytest.mark.parametrize("name", ["authorization", "AUTHORIZATION", "aUtHoRiZaTiOn"])
def test_literal_authorization_field_is_rejected_not_silently_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    payload = clean_payload()
    payload["nested"] = {name: "unrelated-secret"}
    assert_credential_rejection(tmp_path, monkeypatch, payload, offending="unrelated-secret")


@pytest.mark.parametrize("name", ["api_key", "API_KEY", "api-key", "API-Key", "authorization", "AUTHORIZATION"])
@pytest.mark.parametrize("depth", [1, 2, 4])
@pytest.mark.parametrize("field", ["href", "unrelated_url"])
def test_encoded_query_names_are_rejected_inside_and_outside_href(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, depth: int, field: str,
) -> None:
    encoded_name = percent_encode(name, depth)
    url = f"https://example.test/source?{encoded_name}=unrelated-secret&offset=0&safe=yes"
    payload = clean_payload()
    payload["nested"] = {field: url}
    assert_credential_rejection(tmp_path, monkeypatch, payload, offending=url)


@pytest.mark.parametrize("name", ["api_key", "API_KEY", "api-key", "API-Key", "authorization", "AUTHORIZATION"])
@pytest.mark.parametrize("depth", [0, 1, 3])
def test_credential_url_in_remaining_string_is_rejected_even_when_whole_url_encoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, depth: int,
) -> None:
    url = f"https://example.test/source?{name}=unrelated-secret&safe=yes"
    candidate = percent_encode(url, depth)
    payload = clean_payload()
    payload["nested"] = [candidate]
    assert_credential_rejection(tmp_path, monkeypatch, payload, offending=candidate)


@pytest.mark.parametrize("name", ["api-key", "API-Key", "authorization", "AUTHORIZATION"])
def test_unsupported_literal_credential_query_names_in_href_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    payload = clean_payload()
    url = f"https://example.test/source?{name}=unrelated-secret"
    payload["links"] = [{"href": url}]
    assert_credential_rejection(tmp_path, monkeypatch, payload, offending=url)


@pytest.mark.parametrize("depth", [0, 1, 3])
def test_supported_literal_fields_and_query_names_still_remove_encoded_key_values(tmp_path: Path, depth: int) -> None:
    encoded = percent_encode(DUMMY_KEY, depth)
    payload = clean_payload()
    payload["aPi_KeY"] = encoded
    payload["nested"] = {"aPi-kEy": encoded}
    payload["links"] = [{"href": f"https://example.test/source?ApI_KeY={encoded}&safe=hello%20world"}]
    client = FakeClient(response=GistdaResponse(json.dumps(payload).encode(), 200, "application/json"))
    result = ingest(tmp_path, client)
    stored = json.loads(result.artifact_path.read_bytes())
    assert "aPi_KeY" not in stored and stored["nested"] == {}
    assert dict(parse_qsl(urlsplit(stored["links"][0]["href"]).query)) == {"safe": "hello world"}
    assert json.loads(result.metadata_path.read_bytes())["removed_credential_count"] == 3
    assert DUMMY_KEY.encode() not in result.artifact_path.read_bytes()


def test_safe_encoded_strings_object_keys_and_query_parameters_remain_accepted(tmp_path: Path) -> None:
    payload = clean_payload()
    url = "https://example.test/source?%73afe=hello+world&limit=10&offset=0&safe_api_key_suffix=yes"
    payload["nested"] = {
        "%2573afe_name": "%2568ello+world",
        "authorization_notes": "ordinary text",
        "safe_api_key_suffix": "ordinary text",
        "unrelated_url": percent_encode(url, 2),
    }
    payload["links"] = [{"href": url}]
    response_bytes = json.dumps(payload).encode()
    result = ingest(tmp_path, FakeClient(response=GistdaResponse(response_bytes, 200, "application/json")))
    stored = json.loads(result.artifact_path.read_bytes())
    assert stored["nested"] == payload["nested"]
    assert dict(parse_qsl(urlsplit(stored["links"][0]["href"]).query)) == {
        "safe": "hello world", "limit": "10", "offset": "0", "safe_api_key_suffix": "yes",
    }
    assert result.stored_artifact_sha256 == hashlib.sha256(result.artifact_path.read_bytes()).hexdigest()


@pytest.mark.parametrize("depth", [0, 1, 3])
def test_credentials_in_metadata_raise_safe_error_before_filesystem_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int,
) -> None:
    content_type = f"application/json; note={percent_encode(DUMMY_KEY, depth)}"
    assert_credential_rejection(tmp_path, monkeypatch, clean_payload(),
                                offending=content_type, content_type=content_type)


@pytest.mark.parametrize("depth", [0, 1, 3])
def test_encoded_key_in_safe_named_href_parameter_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int,
) -> None:
    payload = clean_payload()
    encoded = percent_encode(DUMMY_KEY, depth)
    payload["links"] = [{"href": f"https://example.test/source?note={encoded}&offset=0"}]
    assert_credential_rejection(tmp_path, monkeypatch, payload, offending=encoded)


@pytest.mark.parametrize("depth", [1, 3])
def test_url_decoding_preserves_literal_plus_in_partially_encoded_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int,
) -> None:
    key = "dummy+percent-key"
    encoded = percent_encode("dummy", depth) + "+percent-key"
    payload = clean_payload()
    payload["note"] = encoded
    assert_credential_rejection(tmp_path, monkeypatch, payload, api_key=key, offending=encoded)


def test_mixed_url_and_form_layers_cannot_hide_space_and_literal_pluses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "dummy space+tail+key"
    encoded = "dummy+space%2Btail%252Bkey"
    payload = clean_payload()
    payload["note"] = encoded
    assert_credential_rejection(tmp_path, monkeypatch, payload, api_key=key, offending=encoded)


@pytest.mark.parametrize("name", ["a%70i_key", "%2561pi_key", "API%2DKey", "Authorizatio%256E"])
def test_partially_encoded_credential_names_are_rejected_in_keys_and_href(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    payload = clean_payload()
    payload["nested"] = {name: "unrelated-secret"}
    assert_credential_rejection(tmp_path, monkeypatch, payload, offending=name)
    del payload["nested"]
    payload["links"] = [{"href": f"https://example.test/source?{name}=unrelated-secret"}]
    assert_credential_rejection(tmp_path, monkeypatch, payload, offending=name)


def test_rejection_leaves_preexisting_artifacts_immutable(tmp_path: Path) -> None:
    ingest(tmp_path, FakeClient())
    original_files = {
        path.relative_to(tmp_path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*") if path.is_file()
    }
    payload = clean_payload()
    payload["nested"] = {"note": percent_encode(DUMMY_KEY, 3)}
    client = FakeClient(response=GistdaResponse(json.dumps(payload).encode(), 200, "application/json"))
    with pytest.raises(SourceSanitizationError):
        ingest(tmp_path, client)
    current_files = {
        path.relative_to(tmp_path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*") if path.is_file()
    }
    assert current_files == original_files
    assert not list(tmp_path.rglob("*.tmp"))


def test_sanitized_artifact_removes_credentials_and_preserves_safe_parameters(
    tmp_path: Path,
) -> None:
    original_bytes = synthetic_response_bytes()
    client = FakeClient()

    result = ingest(tmp_path, client)

    stored_bytes = result.artifact_path.read_bytes()
    stored = json.loads(stored_bytes)
    metadata_bytes = result.metadata_path.read_bytes()
    metadata = json.loads(metadata_bytes)
    assert client.calls == [{"pv_idn": "94", "limit": 10, "offset": 0}]
    assert result.created is True
    assert result.artifact_path.name.endswith(".sanitized.json")
    assert result.artifact_path.parent == tmp_path / "gistda/flood_freq/pattani"
    assert original_bytes != stored_bytes
    assert DUMMY_KEY.encode() not in stored_bytes
    assert DUMMY_KEY.encode() not in metadata_bytes
    assert "api_key" not in {key.casefold() for key in stored}
    assert "api-key" not in {key.casefold() for key in stored["nested"]}
    assert stored["nested"]["safe"] == "preserved"
    expected = json.loads(original_bytes)
    del expected["api_key"]
    del expected["nested"]["API-Key"]
    for link, href in zip(expected["links"], [
        "https://api.example.test/features/flood-freq?limit=10&safe=hello+world",
        "https://api.example.test/features/flood-freq?offset=0",
        "https://api.example.test/features/flood-freq?pv_idn=94",
    ]):
        link["href"] = href
    assert stored_bytes == (json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    queries = [dict(parse_qsl(urlsplit(link["href"]).query)) for link in stored["links"]]
    assert queries == [
        {"limit": "10", "safe": "hello world"},
        {"offset": "0"},
        {"pv_idn": "94"},
    ]
    assert all("api_key" not in {name.casefold() for name in query} for query in queries)

    original_digest = hashlib.sha256(original_bytes).hexdigest()
    stored_digest = hashlib.sha256(stored_bytes).hexdigest()
    assert metadata["artifact_type"] == "sanitized_source_response"
    assert metadata["sanitization_schema_version"] == "1.0"
    assert metadata["sanitization_applied"] is True
    assert metadata["removed_credential_field_names"] == ["API-Key", "api_key"]
    assert metadata["removed_credential_query_parameter_names"] == [
        "API_KEY",
        "api_key",
    ]
    assert metadata["removed_credential_count"] == 5
    assert metadata["original_response_sha256"] == original_digest
    assert metadata["original_response_byte_count"] == len(original_bytes)
    assert metadata["stored_artifact_sha256"] == stored_digest
    assert metadata["stored_artifact_byte_count"] == len(stored_bytes)
    assert metadata["original_response_persisted"] is False
    assert metadata["original_response_disposition"] == (
        "The original response body was not persisted"
    )
    assert "links" not in metadata
    assert result.stored_artifact_sha256 == stored_digest
    assert f"__sha256-{stored_digest[:12]}.sanitized.json" in result.artifact_path.name


def test_malformed_json_persists_nothing(tmp_path: Path) -> None:
    client = FakeClient(
        response=GistdaResponse(b"not-json", 200, "application/json")
    )

    with pytest.raises(SourceSanitizationError) as exc_info:
        ingest(tmp_path, client)

    assert list(tmp_path.iterdir()) == []
    assert DUMMY_KEY not in str(exc_info.value)


def test_failed_sanitization_verification_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def leave_credentials(value: Any, _report: Any) -> Any:
        return value

    monkeypatch.setattr(flood_frequency, "_sanitize_value", leave_credentials)

    with pytest.raises(SourceSanitizationError):
        ingest(tmp_path, FakeClient())

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("encoded", [False, True])
def test_remaining_key_in_unrelated_string_fails_without_artifacts_or_leakage(
    tmp_path: Path,
    encoded: bool,
) -> None:
    credential_value = (
        "".join(f"%{ord(character):02X}" for character in DUMMY_KEY)
        if encoded
        else DUMMY_KEY
    )
    payload = {
        "type": "FeatureCollection",
        "links": [],
        "nested": {"unrelated": f"prefix-{credential_value}-suffix"},
        "features": [],
    }
    response_bytes = json.dumps(payload).encode("utf-8")
    client = FakeClient(
        response=GistdaResponse(response_bytes, 200, "application/json")
    )

    with pytest.raises(SourceSanitizationError) as exc_info:
        ingest(tmp_path, client)

    message = str(exc_info.value)
    assert DUMMY_KEY not in message
    assert credential_value not in message
    assert list(tmp_path.iterdir()) == []


def test_timestamp_is_normalized_and_filename_is_safe(tmp_path: Path) -> None:
    source_time = datetime(
        2026, 8, 29, 10, 4, 5, 678901, tzinfo=timezone(timedelta(hours=7))
    )

    result = ingest_pattani_sample(
        config=make_config(),
        client=FakeClient(),  # type: ignore[arg-type]
        output_root=tmp_path,
        retrieved_at=source_time,
    )

    assert result.artifact_path.name.startswith(
        "20260829T030405678901Z__pv_idn-94__limit-10__offset-0"
    )
    assert "/" not in result.artifact_path.name


def test_identical_controlled_retrieval_is_idempotent(tmp_path: Path) -> None:
    first = ingest(tmp_path, FakeClient())
    artifact_mtime = first.artifact_path.stat().st_mtime_ns
    metadata_mtime = first.metadata_path.stat().st_mtime_ns

    second = ingest(tmp_path, FakeClient())

    assert second.created is False
    assert second.artifact_path == first.artifact_path
    assert second.metadata_path == first.metadata_path
    assert second.stored_artifact_sha256 == first.stored_artifact_sha256
    assert second.artifact_path.stat().st_mtime_ns == artifact_mtime
    assert second.metadata_path.stat().st_mtime_ns == metadata_mtime


def test_existing_mismatched_collision_is_not_overwritten(tmp_path: Path) -> None:
    first = ingest(tmp_path, FakeClient())
    collision_bytes = b"existing-different-content"
    first.artifact_path.write_bytes(collision_bytes)

    with pytest.raises(FileExistsError):
        ingest(tmp_path, FakeClient())

    assert first.artifact_path.read_bytes() == collision_bytes


def test_atomic_no_replace_publication_and_temp_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link
    raced_content = b"created-by-another-process"

    def racing_link(source: str, destination: Path) -> None:
        destination.write_bytes(raced_content)
        real_link(source, destination)

    monkeypatch.setattr(flood_frequency.os, "link", racing_link)

    with pytest.raises(FileExistsError):
        ingest(tmp_path, FakeClient())

    destination = tmp_path / "gistda/flood_freq/pattani"
    remaining = list(destination.iterdir())
    assert len(remaining) == 1
    assert remaining[0].read_bytes() == raced_content
    assert not any(path.name.endswith(".tmp") for path in remaining)


def test_metadata_collision_rolls_back_created_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link
    external_metadata = b"external-metadata"
    count = 0

    def collide_second(source: str, destination: Path) -> None:
        nonlocal count
        count += 1
        if count == 2:
            destination.write_bytes(external_metadata)
        real_link(source, destination)

    monkeypatch.setattr(flood_frequency.os, "link", collide_second)

    with pytest.raises(FileExistsError):
        ingest(tmp_path, FakeClient())

    remaining = list((tmp_path / "gistda/flood_freq/pattani").iterdir())
    assert len(remaining) == 1
    assert remaining[0].name.endswith(".metadata.json")
    assert remaining[0].read_bytes() == external_metadata


def test_metadata_temp_failure_cleans_artifact_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write_temp = flood_frequency._write_temp
    setup_error = OSError("metadata temp setup failed")
    count = 0

    def fail_second(directory: Path, target_name: str, content: bytes) -> str:
        nonlocal count
        count += 1
        if count == 2:
            raise setup_error
        return real_write_temp(directory, target_name, content)

    monkeypatch.setattr(flood_frequency, "_write_temp", fail_second)

    with pytest.raises(OSError) as exc_info:
        ingest(tmp_path, FakeClient())

    assert exc_info.value is setup_error
    assert list((tmp_path / "gistda/flood_freq/pattani").iterdir()) == []


def test_wrong_province_fails_before_client_or_filesystem(tmp_path: Path) -> None:
    client = FakeClient()

    with pytest.raises(ValueError) as exc_info:
        ingest_pattani_sample(
            config=make_config("not-pattani"),
            client=client,  # type: ignore[arg-type]
            output_root=tmp_path,
            retrieved_at=FIXED_TIME,
        )

    assert client.calls == []
    assert list(tmp_path.iterdir()) == []
    assert DUMMY_KEY not in str(exc_info.value)


def test_production_clock_runs_after_successful_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()

    def clock_after_response() -> datetime:
        assert client.calls == [{"pv_idn": "94", "limit": 10, "offset": 0}]
        return FIXED_TIME

    monkeypatch.setattr(flood_frequency, "_utc_now", clock_after_response)

    result = ingest_pattani_sample(
        config=make_config(),
        client=client,  # type: ignore[arg-type]
        output_root=tmp_path,
    )

    assert result.artifact_path.name.startswith("20260829T030405678901Z")


def test_naive_timestamp_fails_before_client_or_filesystem(tmp_path: Path) -> None:
    client = FakeClient()

    with pytest.raises(ValueError, match="timezone-aware"):
        ingest_pattani_sample(
            config=make_config(),
            client=client,  # type: ignore[arg-type]
            output_root=tmp_path,
            retrieved_at=datetime(2026, 8, 29, 3, 4, 5),
        )

    assert client.calls == []
    assert list(tmp_path.iterdir()) == []


def test_http_failure_creates_no_artifacts(tmp_path: Path) -> None:
    client = FakeClient(error=GistdaHTTPError(500, "application/json"))

    with pytest.raises(GistdaHTTPError):
        ingest(tmp_path, client)

    assert list(tmp_path.iterdir()) == []
