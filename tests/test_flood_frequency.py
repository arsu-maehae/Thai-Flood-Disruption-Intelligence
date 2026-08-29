from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import requests

from src.configuration import GistdaConfig, OFFICIAL_GISTDA_API_BASE_URL
from src.ingestion.flood_frequency import ingest_pattani_sample
from src.ingestion.gistda_client import GistdaHTTPError, GistdaResponse


DUMMY_KEY = "dummy-ingestion-test-key"
FIXED_TIME = datetime(2026, 8, 29, 3, 4, 5, 678901, tzinfo=timezone.utc)
RAW_BYTES = b'\x00{"unvalidated":true,"bytes":"preserved"}\xff'


class FakeClient:
    def __init__(
        self,
        response: GistdaResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response or GistdaResponse(
            content=RAW_BYTES,
            status_code=200,
            content_type="application/json",
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


def make_config(province_id: str = "94") -> GistdaConfig:
    return GistdaConfig(
        api_base_url=OFFICIAL_GISTDA_API_BASE_URL,
        api_key=DUMMY_KEY,
        province_id=province_id,
    )


def ingest(temp_root: Path, client: FakeClient, **kwargs: Any):
    return ingest_pattani_sample(
        config=make_config(),
        client=client,  # type: ignore[arg-type]
        output_root=temp_root,
        retrieved_at=FIXED_TIME,
        **kwargs,
    )


def test_ingestion_preserves_exact_bytes_and_writes_sanitized_metadata(
    tmp_path: Path,
) -> None:
    client = FakeClient()

    result = ingest(tmp_path, client)

    digest = hashlib.sha256(RAW_BYTES).hexdigest()
    assert client.calls == [{"pv_idn": "94", "limit": 10, "offset": 0}]
    assert result.created is True
    assert result.raw_path.read_bytes() == RAW_BYTES
    assert result.sha256 == digest
    assert result.raw_path.parent == tmp_path / "gistda/flood_freq/pattani"
    assert result.raw_path.name == (
        "20260829T030405678901Z__pv_idn-94__limit-10__offset-0"
        f"__sha256-{digest[:12]}.json"
    )
    assert result.metadata_path.name.endswith(".metadata.json")

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata == {
        "metadata_schema_version": "1.0",
        "retrieved_at_utc": "2026-08-29T03:04:05.678901Z",
        "provider": "GISTDA",
        "dataset": "Historical Flood Recurrence",
        "endpoint_path": "/features/flood-freq",
        "query_parameters": {"pv_idn": "94", "limit": 10, "offset": 0},
        "http_status": 200,
        "content_type": "application/json",
        "byte_count": len(RAW_BYTES),
        "sha256": digest,
        "relative_raw_artifact_path": result.raw_path.relative_to(tmp_path).as_posix(),
    }
    serialized_metadata = result.metadata_path.read_text(encoding="utf-8")
    assert DUMMY_KEY not in serialized_metadata
    assert "API-Key" not in serialized_metadata


def test_timestamp_is_converted_to_utc_and_filename_is_safe(tmp_path: Path) -> None:
    source_time = datetime(
        2026, 8, 29, 10, 4, 5, 678901, tzinfo=timezone(timedelta(hours=7))
    )
    client = FakeClient()

    result = ingest_pattani_sample(
        config=make_config(),
        client=client,  # type: ignore[arg-type]
        output_root=tmp_path,
        retrieved_at=source_time,
    )

    assert result.raw_path.name.startswith(
        "20260829T030405678901Z__pv_idn-94__limit-10__offset-0"
    )
    assert "/" not in result.raw_path.name


def test_atomic_publications_use_temporary_files_in_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link
    publications: list[tuple[Path, Path]] = []

    def recording_link(source: str, destination: Path) -> None:
        publications.append((Path(source), Path(destination)))
        real_link(source, destination)

    monkeypatch.setattr("src.ingestion.flood_frequency.os.link", recording_link)

    result = ingest(tmp_path, FakeClient())

    assert len(publications) == 2
    assert {destination for _, destination in publications} == {
        result.raw_path,
        result.metadata_path,
    }
    assert all(
        source.parent == destination.parent for source, destination in publications
    )
    assert all(source.name.endswith(".tmp") for source, _ in publications)


def test_identical_controlled_retrieval_is_idempotent(tmp_path: Path) -> None:
    first = ingest(tmp_path, FakeClient())
    raw_mtime = first.raw_path.stat().st_mtime_ns
    metadata_mtime = first.metadata_path.stat().st_mtime_ns

    second = ingest(tmp_path, FakeClient())

    assert second == type(first)(
        first.raw_path, first.metadata_path, first.sha256, created=False
    )
    assert second.raw_path.stat().st_mtime_ns == raw_mtime
    assert second.metadata_path.stat().st_mtime_ns == metadata_mtime


def test_existing_path_collision_is_not_overwritten(tmp_path: Path) -> None:
    first = ingest(tmp_path, FakeClient())
    first.raw_path.write_bytes(b"existing-different-content")

    with pytest.raises(FileExistsError):
        ingest(tmp_path, FakeClient())

    assert first.raw_path.read_bytes() == b"existing-different-content"


def test_racing_destination_is_not_overwritten_and_temps_are_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link
    raced_content = b"created-by-another-process"

    def racing_link(source: str, destination: Path) -> None:
        destination.write_bytes(raced_content)
        real_link(source, destination)

    monkeypatch.setattr("src.ingestion.flood_frequency.os.link", racing_link)

    with pytest.raises(FileExistsError):
        ingest(tmp_path, FakeClient())

    destination = tmp_path / "gistda/flood_freq/pattani"
    published = [path for path in destination.iterdir() if not path.name.endswith(".tmp")]
    assert len(published) == 1
    assert published[0].read_bytes() == raced_content
    assert [path for path in destination.iterdir() if path.name.endswith(".tmp")] == []


def test_metadata_publication_collision_rolls_back_created_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link
    metadata_from_other_process = b"external-metadata"
    publication_count = 0

    def collide_on_metadata(source: str, destination: Path) -> None:
        nonlocal publication_count
        publication_count += 1
        if publication_count == 2:
            destination.write_bytes(metadata_from_other_process)
        real_link(source, destination)

    monkeypatch.setattr(
        "src.ingestion.flood_frequency.os.link", collide_on_metadata
    )

    with pytest.raises(FileExistsError):
        ingest(tmp_path, FakeClient())

    destination = tmp_path / "gistda/flood_freq/pattani"
    remaining = list(destination.iterdir())
    assert len(remaining) == 1
    assert remaining[0].name.endswith(".metadata.json")
    assert remaining[0].read_bytes() == metadata_from_other_process
    assert [path for path in remaining if path.name.endswith(".tmp")] == []


def test_metadata_temp_creation_failure_cleans_raw_temp_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.ingestion import flood_frequency

    real_write_temp = flood_frequency._write_temp
    setup_error = OSError("metadata temporary-file setup failed")
    call_count = 0

    def fail_second_temp(directory: Path, target_name: str, content: bytes) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise setup_error
        return real_write_temp(directory, target_name, content)

    monkeypatch.setattr(flood_frequency, "_write_temp", fail_second_temp)

    with pytest.raises(OSError) as exc_info:
        ingest(tmp_path, FakeClient())

    assert exc_info.value is setup_error
    destination = tmp_path / "gistda/flood_freq/pattani"
    assert list(destination.iterdir()) == []


def test_wrong_province_is_rejected_before_client_or_filesystem(
    tmp_path: Path,
) -> None:
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


def test_production_clock_is_evaluated_after_client_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()

    def clock_after_response() -> datetime:
        assert client.calls == [{"pv_idn": "94", "limit": 10, "offset": 0}]
        return FIXED_TIME

    monkeypatch.setattr("src.ingestion.flood_frequency._utc_now", clock_after_response)

    result = ingest_pattani_sample(
        config=make_config(),
        client=client,  # type: ignore[arg-type]
        output_root=tmp_path,
    )

    assert result.raw_path.name.startswith("20260829T030405678901Z")


def test_naive_injected_timestamp_is_rejected_before_client_or_filesystem(
    tmp_path: Path,
) -> None:
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

    assert not tmp_path.exists() or list(tmp_path.rglob("*")) == []
