from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import zipfile

import pytest
import requests

from src.ingestion import infrastructure_acquisition as acquisition
from src.ingestion.infrastructure_acquisition import (
    AcquisitionError,
    ApprovedResourceSpec,
    ArchiveLimits,
    CandidateResourceEvidence,
    DGA_CATALOG_URL,
    DGA_CSV_DOWNLOAD_URL,
    DGA_CSV_RESOURCE_ID,
    DRR_CATALOG_URL,
    DRR_RESOURCE_ID,
    GEOFABRIK_COVERAGE_LABEL,
    GEOFABRIK_PROVIDER_LABEL,
    GEOFABRIK_THAILAND_CATALOG_URL,
    GEOFABRIK_THAILAND_DOWNLOAD_CAP,
    GEOFABRIK_THAILAND_EXPECTED_BYTES,
    GEOFABRIK_THAILAND_PBF_SPEC,
    GEOFABRIK_THAILAND_PBF_URL,
    GEOFABRIK_THAILAND_PROVIDER_MD5,
    GEOFABRIK_THAILAND_RESOURCE_ID,
    acquire_resource,
    revalidate_selected_metadata,
)


FIXED_TIME = datetime(2026, 9, 23, 8, 30, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status: int = 200,
        content_type: str = "text/csv",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.content = content
        self.status_code = status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.closed = False

    def iter_content(self, chunk_size: int = 1) -> object:
        return (
            self.content[index : index + chunk_size]
            for index in range(0, len(self.content), chunk_size)
        )

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


class FailingSession:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        raise self.error


def dga_evidence() -> CandidateResourceEvidence:
    return CandidateResourceEvidence(
        source_key="dga_healthcare",
        resource_id=DGA_CSV_RESOURCE_ID,
        catalog_url=DGA_CATALOG_URL,
        candidate_download_url=DGA_CSV_DOWNLOAD_URL,
        expected_format="csv",
    )


def dga_spec(max_bytes: int = 10_000) -> ApprovedResourceSpec:
    return ApprovedResourceSpec.approve(
        dga_evidence(),
        approved_media_types=("text/csv", "application/csv"),
        max_bytes=max_bytes,
        approval_reference="phase3b-review",
    )


def archive_limits(**changes: object) -> ArchiveLimits:
    values: dict[str, object] = {
        "max_members": 20,
        "max_member_uncompressed_bytes": 10_000,
        "max_total_uncompressed_bytes": 30_000,
        "max_compression_ratio": 100.0,
    }
    values.update(changes)
    return ArchiveLimits(**values)  # type: ignore[arg-type]


def drr_spec(content_url: str, limits: ArchiveLimits | None = None) -> ApprovedResourceSpec:
    evidence = CandidateResourceEvidence(
        source_key="drr_roads",
        resource_id=DRR_RESOURCE_ID,
        catalog_url=DRR_CATALOG_URL,
        candidate_download_url=content_url,
        expected_format="zip",
    )
    return ApprovedResourceSpec.approve(
        evidence,
        approved_media_types=("application/zip", "application/x-zip-compressed"),
        max_bytes=100_000,
        approval_reference="phase3b-review",
        archive_limits=limits or archive_limits(),
    )


def make_zip(
    names: tuple[str, ...] = ("roads.shp", "roads.shx", "roads.dbf"),
    *,
    symlink: bool = False,
    payload: bytes = b"safe-content",
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=compression) as archive:
        for name in names:
            if symlink and name == names[0]:
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, b"target")
            else:
                archive.writestr(name, payload)
    return target.getvalue()


def encode_varint(value: int) -> bytes:
    result = bytearray()
    while True:
        current = value & 0x7F
        value >>= 7
        result.append(current | (0x80 if value else 0))
        if not value:
            return bytes(result)


def make_osm_pbf(blob: bytes = b"synthetic-header-block") -> bytes:
    blob_header = (
        b"\x0a"
        + encode_varint(len(b"OSMHeader"))
        + b"OSMHeader"
        + b"\x18"
        + encode_varint(len(blob))
    )
    return len(blob_header).to_bytes(4, "big") + blob_header + blob


def synthetic_osm_spec(
    monkeypatch: pytest.MonkeyPatch,
    expected_content: bytes,
    *,
    max_bytes: int | None = None,
) -> ApprovedResourceSpec:
    expected_md5 = hashlib.md5(expected_content, usedforsecurity=False).hexdigest()
    cap = max_bytes if max_bytes is not None else max(len(expected_content), 1)
    monkeypatch.setattr(acquisition, "GEOFABRIK_THAILAND_EXPECTED_BYTES", len(expected_content))
    monkeypatch.setattr(acquisition, "GEOFABRIK_THAILAND_PROVIDER_MD5", expected_md5)
    monkeypatch.setattr(acquisition, "GEOFABRIK_THAILAND_DOWNLOAD_CAP", cap)
    return replace(
        GEOFABRIK_THAILAND_PBF_SPEC,
        expected_bytes=len(expected_content),
        expected_md5=expected_md5,
        max_bytes=cap,
    )


def acquire_osm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    *,
    response_content: bytes | None = None,
    headers: dict[str, str] | None = None,
    content_type: str = "application/octet-stream",
    status: int = 200,
    max_bytes: int | None = None,
):
    spec = synthetic_osm_spec(monkeypatch, content, max_bytes=max_bytes)
    body = content if response_content is None else response_content
    session = FakeSession(
        [
            FakeResponse(
                body,
                status=status,
                content_type=content_type,
                headers=headers,
            )
        ]
    )
    result = acquire_resource(
        spec,
        session,  # type: ignore[arg-type]
        tmp_path,
        minimum_free_bytes=0,
        clock=lambda: FIXED_TIME,
    )
    return result, session


def mark_zip_encrypted(content: bytes) -> bytes:
    result = bytearray(content)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while True:
            index = result.find(signature, start)
            if index < 0:
                break
            flags = int.from_bytes(result[index + flag_offset : index + flag_offset + 2], "little")
            result[index + flag_offset : index + flag_offset + 2] = (flags | 1).to_bytes(2, "little")
            start = index + 4
    return bytes(result)


def acquire_csv(tmp_path: Path, session: FakeSession, **kwargs: object):
    return acquire_resource(
        dga_spec(),
        session,  # type: ignore[arg-type]
        tmp_path,
        minimum_free_bytes=0,
        clock=lambda: FIXED_TIME,
        **kwargs,
    )


def create_required_symlink(target: Path, link: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create required link: {type(error).__name__}")


def test_metadata_revalidation_is_bounded_streamed_and_non_authorizing() -> None:
    drr_url = (
        "https://datagov.mot.go.th/dataset/roads/resource/"
        f"{DRR_RESOURCE_ID}/download/roads.zip"
    )
    drr_html = f'<a href="{drr_url}">download</a>'.encode()
    dga_html = f'<a href="{DGA_CSV_DOWNLOAD_URL}">download</a>'.encode()
    session = FakeSession(
        [
            FakeResponse(drr_html, content_type="text/html; charset=utf-8"),
            FakeResponse(dga_html, content_type="text/html"),
        ]
    )

    result = revalidate_selected_metadata(session)  # type: ignore[arg-type]

    assert result.request_count == 2
    assert result.authorizes_acquisition is False
    assert [item.candidate_download_url for item in result.candidates] == [
        drr_url,
        DGA_CSV_DOWNLOAD_URL,
    ]
    assert [call[0] for call in session.calls] == [DRR_CATALOG_URL, DGA_CATALOG_URL]
    assert all(call[1]["stream"] is True for call in session.calls)
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert all(call[1]["timeout"] == (10.0, 30.0) for call in session.calls)


def test_metadata_cap_and_redirect_stop_without_second_request() -> None:
    oversized = FakeSession([FakeResponse(b"x" * 11, content_type="text/html")])
    with pytest.raises(AcquisitionError, match="^metadata_response_too_large$"):
        revalidate_selected_metadata(  # type: ignore[arg-type]
            oversized, response_limit=10
        )
    assert len(oversized.calls) == 1

    redirected = FakeSession([FakeResponse(b"", status=302, content_type="text/html")])
    with pytest.raises(AcquisitionError, match="^metadata_redirect_rejected$"):
        revalidate_selected_metadata(redirected)  # type: ignore[arg-type]
    assert len(redirected.calls) == 1


def test_unresolved_candidate_cannot_be_approved() -> None:
    unresolved = CandidateResourceEvidence(
        "drr_roads", DRR_RESOURCE_ID, DRR_CATALOG_URL, None, "zip"
    )
    with pytest.raises(AcquisitionError, match="^download_url_unresolved$"):
        ApprovedResourceSpec.approve(
            unresolved,
            approved_media_types=("application/zip",),
            max_bytes=100,
            approval_reference="review",
            archive_limits=archive_limits(),
        )


@pytest.mark.parametrize(
    ("base", "changes"),
    [
        ("dga", {"resource_id": "11111111-1111-1111-1111-111111111111"}),
        ("dga", {"catalog_url": "https://data.go.th/th/dataset/different"}),
        (
            "dga",
            {
                "download_url": (
                    "https://data.go.th/dataset/00170665-bda1-4f4a-ad7c-52dac7abc7a5/"
                    f"resource/{DGA_CSV_RESOURCE_ID}/download/different.csv"
                )
            },
        ),
        ("drr", {"resource_id": "11111111-1111-1111-1111-111111111111"}),
        ("drr", {"catalog_url": "https://datagov.mot.go.th/th/dataset/different"}),
        ("drr", {"download_url": "https://datagov.mot.go.th/download/roads.zip"}),
    ],
)
def test_direct_spec_construction_cannot_bypass_exact_resource_binding(
    base: str, changes: dict[str, object]
) -> None:
    drr_url = (
        "https://datagov.mot.go.th/dataset/roads/resource/"
        f"{DRR_RESOURCE_ID}/download/roads.zip"
    )
    original = dga_spec() if base == "dga" else drr_spec(drr_url)
    with pytest.raises(ValueError, match="^resource_spec_invalid$"):
        replace(original, **changes)


def test_geofabrik_spec_is_exact_and_immutable() -> None:
    spec = GEOFABRIK_THAILAND_PBF_SPEC
    assert spec.resource_id == GEOFABRIK_THAILAND_RESOURCE_ID
    assert spec.catalog_url == GEOFABRIK_THAILAND_CATALOG_URL
    assert spec.download_url == GEOFABRIK_THAILAND_PBF_URL
    assert spec.expected_bytes == GEOFABRIK_THAILAND_EXPECTED_BYTES
    assert spec.expected_md5 == GEOFABRIK_THAILAND_PROVIDER_MD5
    assert spec.max_bytes == GEOFABRIK_THAILAND_DOWNLOAD_CAP
    assert spec.provider_label == GEOFABRIK_PROVIDER_LABEL
    assert spec.coverage_label == GEOFABRIK_COVERAGE_LABEL
    with pytest.raises(ValueError, match="^resource_spec_invalid$"):
        replace(spec, download_url="https://download.geofabrik.de/asia/other.osm.pbf")
    with pytest.raises(ValueError, match="^resource_spec_invalid$"):
        replace(spec, catalog_url="https://download.geofabrik.de/asia/index.html")


def test_osm_pbf_acquisition_reconciles_hashes_bytes_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = make_osm_pbf()
    result, session = acquire_osm(
        tmp_path,
        monkeypatch,
        content,
        headers={"Content-Length": str(len(content))},
    )

    sha256 = hashlib.sha256(content).hexdigest()
    md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
    assert result.sha256 == sha256
    assert result.byte_count == len(content)
    assert result.content_type == "application/octet-stream"
    assert result.artifact_path.name == (
        f"resource-{GEOFABRIK_THAILAND_RESOURCE_ID}__sha256-{sha256}.osm.pbf"
    )
    assert result.artifact_path.read_bytes() == content
    assert result.artifact_path.parent.relative_to(tmp_path).as_posix() == (
        "infrastructure/roads/osm/geofabrik"
    )
    metadata = json.loads(result.metadata_path.read_bytes())
    assert metadata["provider"] == GEOFABRIK_PROVIDER_LABEL
    assert metadata["coverage"] == GEOFABRIK_COVERAGE_LABEL
    assert metadata["approved_download_url"] == GEOFABRIK_THAILAND_PBF_URL
    assert metadata["byte_count"] == len(content)
    assert metadata["expected_byte_count"] == len(content)
    assert metadata["provider_md5"] == md5
    assert metadata["sha256"] == sha256
    assert metadata["request_count"] == 1
    assert metadata["redirect_count"] == 0
    assert metadata["archive_inspection"] == {
        "applied": True,
        "blob_header_bytes": 13,
        "declared_first_blob_bytes": 22,
        "extracted": False,
        "first_blob_type": "OSMHeader",
        "format": "osm.pbf",
        "max_blob_header_bytes": 65536,
        "max_declared_first_blob_bytes": 33554432,
        "semantic_parsing_applied": False,
    }
    assert session.calls == [
        (
            GEOFABRIK_THAILAND_PBF_URL,
            {"stream": True, "allow_redirects": False, "timeout": (10.0, 120.0)},
        )
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resource_id", "thailand-latest.osm.pbf"),
        ("download_url", "https://example.invalid/asia/thailand-260923.osm.pbf"),
        ("download_url", "https://download.geofabrik.de/asia/other.osm.pbf"),
        ("expected_bytes", 1),
        ("expected_md5", "0" * 32),
    ],
)
def test_osm_pbf_exact_binding_rejects_changes(field: str, value: object) -> None:
    with pytest.raises(ValueError, match="^resource_spec_invalid$"):
        replace(GEOFABRIK_THAILAND_PBF_SPEC, **{field: value})


def test_osm_pbf_missing_content_length_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = make_osm_pbf()
    result, _ = acquire_osm(tmp_path, monkeypatch, content)
    assert result.byte_count == len(content)


@pytest.mark.parametrize(
    ("headers", "category"),
    [
        ({"Content-Length": "invalid"}, "content_length_invalid"),
        ({"Content-Length": "1000"}, "download_too_large"),
    ],
)
def test_osm_pbf_invalid_or_oversized_declared_length_is_rejected_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    category: str,
) -> None:
    content = make_osm_pbf()
    spec = synthetic_osm_spec(monkeypatch, content)

    class UnreadableResponse(FakeResponse):
        def iter_content(self, chunk_size: int = 1) -> object:
            raise AssertionError("body must not be read")

    session = FakeSession(
        [
            UnreadableResponse(
                content,
                content_type="application/octet-stream",
                headers=headers,
            )
        ]
    )
    with pytest.raises(AcquisitionError, match=f"^{category}$"):
        acquire_resource(
            spec,
            session,  # type: ignore[arg-type]
            tmp_path,
            minimum_free_bytes=0,
            clock=lambda: FIXED_TIME,
        )


def test_osm_pbf_declared_exact_length_mismatch_is_rejected_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = make_osm_pbf()
    spec = synthetic_osm_spec(monkeypatch, content)

    class UnreadableResponse(FakeResponse):
        def iter_content(self, chunk_size: int = 1) -> object:
            raise AssertionError("body must not be read")

    session = FakeSession(
        [
            UnreadableResponse(
                content,
                content_type="application/octet-stream",
                headers={"Content-Length": str(len(content) - 1)},
            )
        ]
    )
    with pytest.raises(AcquisitionError, match="^content_length_mismatch$"):
        acquire_resource(
            spec,
            session,  # type: ignore[arg-type]
            tmp_path,
            minimum_free_bytes=0,
            clock=lambda: FIXED_TIME,
        )


def test_osm_pbf_streaming_cap_is_enforced_without_declared_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = make_osm_pbf(b"a")
    response = expected + b"overflow"
    spec = synthetic_osm_spec(monkeypatch, expected, max_bytes=len(expected))
    session = FakeSession(
        [FakeResponse(response, content_type="application/octet-stream")]
    )
    with pytest.raises(AcquisitionError, match="^download_too_large$"):
        acquire_resource(
            spec,
            session,  # type: ignore[arg-type]
            tmp_path,
            minimum_free_bytes=0,
            clock=lambda: FIXED_TIME,
        )


def test_osm_pbf_wrong_length_checksum_type_and_redirect_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = make_osm_pbf()
    cases = (
        (content[:-1], "application/octet-stream", 200, "download_length_mismatch"),
        (content[:-1] + b"x", "application/octet-stream", 200, "provider_checksum_mismatch"),
        (content, "text/plain", 200, "download_content_type_rejected"),
        (content, "application/octet-stream", 302, "download_redirect_rejected"),
    )
    for index, (body, content_type, status, category) in enumerate(cases):
        case_root = tmp_path / str(index)
        with pytest.raises(AcquisitionError, match=f"^{category}$"):
            acquire_osm(
                case_root,
                monkeypatch,
                content,
                response_content=body,
                content_type=content_type,
                status=status,
            )


@pytest.mark.parametrize(
    "content",
    [
        b"\x00",
        b"\x00\x00\x00\x10short",
        make_osm_pbf().replace(b"OSMHeader", b"OSMData__", 1),
    ],
)
def test_osm_pbf_invalid_or_truncated_header_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    spec = synthetic_osm_spec(monkeypatch, content)
    with pytest.raises(AcquisitionError, match="^pbf_header_rejected$"):
        acquire_resource(
            spec,
            FakeSession(
                [FakeResponse(content, content_type="application/octet-stream")]
            ),  # type: ignore[arg-type]
            tmp_path,
            minimum_free_bytes=0,
            clock=lambda: FIXED_TIME,
        )
    destination = tmp_path / "infrastructure/roads/osm/geofabrik"
    assert not destination.exists() or list(destination.iterdir()) == []


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (requests.Timeout("private timeout"), "download_timeout"),
        (requests.ConnectionError("private connection"), "download_connection_failed"),
    ],
)
def test_osm_pbf_request_failures_are_safe_and_publish_nothing(
    tmp_path: Path,
    error: Exception,
    category: str,
) -> None:
    session = FailingSession(error)
    with pytest.raises(AcquisitionError, match=f"^{category}$") as caught:
        acquire_resource(
            GEOFABRIK_THAILAND_PBF_SPEC,
            session,  # type: ignore[arg-type]
            tmp_path,
            minimum_free_bytes=0,
            clock=lambda: FIXED_TIME,
        )
    assert "private" not in str(caught.value) + repr(caught.value)
    assert len(session.calls) == 1
    assert not list(tmp_path.rglob("*.pbf"))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_timeout_and_compression_ratio_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="^archive_limits_invalid$"):
        archive_limits(max_compression_ratio=value)

    session = FakeSession([])
    with pytest.raises(AcquisitionError, match="^timeout_invalid$"):
        revalidate_selected_metadata(session, timeout=(value, 1.0))  # type: ignore[arg-type]
    assert session.calls == []


def test_csv_acquisition_preserves_bytes_metadata_and_ignores_type_parameters(
    tmp_path: Path,
) -> None:
    content = b"column_a,column_b\n1,2\n"
    session = FakeSession(
        [FakeResponse(content, content_type="text/csv; charset=utf-8")]
    )

    result = acquire_csv(tmp_path, session)

    digest = hashlib.sha256(content).hexdigest()
    assert result.sha256 == digest
    assert result.byte_count == len(content)
    assert result.content_type == "text/csv"
    assert result.reused is False
    assert result.artifact_path.read_bytes() == content
    assert digest in result.artifact_path.name
    metadata = json.loads(result.metadata_path.read_bytes())
    assert metadata["sha256"] == digest
    assert metadata["byte_count"] == len(content)
    assert metadata["redirect_count"] == 0
    assert metadata["archive_inspection"] == {"applied": False}
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["timeout"] == (10.0, 120.0)


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (FakeResponse(b"x", status=302), "download_redirect_rejected"),
        (FakeResponse(b"x", status=500), "download_http_error"),
        (FakeResponse(b"x", content_type="application/octet-stream"), "download_content_type_rejected"),
        (FakeResponse(b"12345", headers={"Content-Length": "10001"}), "download_too_large"),
    ],
)
def test_download_refusals_publish_nothing(
    tmp_path: Path, response: FakeResponse, category: str
) -> None:
    session = FakeSession([response])
    with pytest.raises(AcquisitionError, match=f"^{category}$"):
        acquire_csv(tmp_path, session)
    destination = tmp_path / "infrastructure/healthcare/dga"
    assert not destination.exists() or list(destination.iterdir()) == []
    assert len(session.calls) == 1


def test_streaming_cap_is_enforced_without_content_length(tmp_path: Path) -> None:
    content = b"01234567890"
    session = FakeSession([FakeResponse(content)])

    with pytest.raises(AcquisitionError, match="^download_too_large$") as caught:
        acquire_resource(
            dga_spec(max_bytes=10),
            session,  # type: ignore[arg-type]
            tmp_path,
            minimum_free_bytes=0,
            clock=lambda: FIXED_TIME,
        )

    assert caught.value.request_count == 1
    assert not caught.value.artifact_published
    destination = tmp_path / "infrastructure/healthcare/dga"
    assert list(destination.iterdir()) == []


def test_complete_pair_is_reused_only_after_exact_verification(tmp_path: Path) -> None:
    content = b"a,b\n1,2\n"
    first = acquire_csv(tmp_path, FakeSession([FakeResponse(content)]))
    artifact_mtime = first.artifact_path.stat().st_mtime_ns
    second = acquire_csv(tmp_path, FakeSession([FakeResponse(content)]))
    assert second.reused is True
    assert second.artifact_path.stat().st_mtime_ns == artifact_mtime

    metadata_bytes = first.metadata_path.read_bytes()
    first.metadata_path.write_bytes(metadata_bytes + b" ")
    with pytest.raises(AcquisitionError, match="^destination_collision$"):
        acquire_csv(tmp_path, FakeSession([FakeResponse(content)]))
    assert first.metadata_path.read_bytes() == metadata_bytes + b" "


def test_incomplete_pair_is_rejected_without_overwrite(tmp_path: Path) -> None:
    content = b"a\n1\n"
    first = acquire_csv(tmp_path, FakeSession([FakeResponse(content)]))
    first.metadata_path.unlink()
    before = first.artifact_path.read_bytes()

    with pytest.raises(AcquisitionError, match="^destination_collision$"):
        acquire_csv(tmp_path, FakeSession([FakeResponse(content)]))

    assert first.artifact_path.read_bytes() == before
    assert not first.metadata_path.exists()


def test_destination_directory_escape_is_rejected_before_request_or_write(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "root"
    outside = tmp_path / "outside"
    output_root.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"unchanged")
    create_required_symlink(outside, output_root / "infrastructure", directory=True)
    session = FakeSession([])

    with pytest.raises(AcquisitionError, match="^destination_escape_rejected$") as caught:
        acquire_resource(
            dga_spec(),
            session,  # type: ignore[arg-type]
            output_root,
            minimum_free_bytes=0,
            clock=lambda: FIXED_TIME,
        )

    assert session.calls == []
    assert sentinel.read_bytes() == b"unchanged"
    assert list(outside.iterdir()) == [sentinel]
    assert "outside" not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize("linked_member", ["artifact", "metadata"])
def test_symlinked_reusable_pair_member_is_rejected_without_outside_modification(
    tmp_path: Path, linked_member: str
) -> None:
    content = b"a,b\n1,2\n"
    output_root = tmp_path / "root"
    first = acquire_csv(output_root, FakeSession([FakeResponse(content)]))
    outside = tmp_path / f"outside-{linked_member}.bin"
    outside_bytes = b"outside-must-remain-unchanged"
    outside.write_bytes(outside_bytes)
    linked_path = first.artifact_path if linked_member == "artifact" else first.metadata_path
    linked_path.unlink()
    create_required_symlink(outside, linked_path, directory=False)

    with pytest.raises(AcquisitionError, match="^reuse_path_rejected$") as caught:
        acquire_csv(output_root, FakeSession([FakeResponse(content)]))

    assert linked_path.is_symlink()
    assert outside.read_bytes() == outside_bytes
    assert not any(path.name.endswith(".tmp") for path in first.artifact_path.parent.iterdir())
    safe_error = str(caught.value) + repr(caught.value)
    assert str(outside) not in safe_error
    assert outside_bytes.decode() not in safe_error


def test_safe_zip_records_prj_presence_without_extraction_or_interpretation(
    tmp_path: Path,
) -> None:
    content = make_zip(("roads.shp", "roads.shx", "roads.dbf", "roads.prj"))
    url = (
        "https://datagov.mot.go.th/dataset/roads/resource/"
        f"{DRR_RESOURCE_ID}/download/roads.zip"
    )
    result = acquire_resource(
        drr_spec(url),
        FakeSession([FakeResponse(content, content_type="application/zip")]),  # type: ignore[arg-type]
        tmp_path,
        minimum_free_bytes=0,
        clock=lambda: FIXED_TIME,
    )

    metadata = json.loads(result.metadata_path.read_bytes())
    inspection = metadata["archive_inspection"]
    assert inspection["required_shapefile_components_present"] is True
    assert inspection["prj_present"] is True
    assert inspection["crs_interpreted"] is False
    assert inspection["extracted"] is False
    assert {path.suffix for path in result.artifact_path.parent.iterdir()} == {".zip", ".json"}


@pytest.mark.parametrize(
    ("content", "limits", "category"),
    [
        (make_zip(("../roads.shp", "roads.shx", "roads.dbf")), archive_limits(), "archive_path_rejected"),
        (make_zip(symlink=True), archive_limits(), "archive_symlink_rejected"),
        (make_zip(("roads.shp", "ROADS.SHP", "roads.shx", "roads.dbf")), archive_limits(), "archive_duplicate_member"),
        (make_zip(), archive_limits(max_members=2), "archive_limits_exceeded"),
        (make_zip(("roads.shp", "roads.shx")), archive_limits(), "shapefile_components_missing"),
        (
            make_zip(("roads.shp", "roads.shx", "roads.dbf", "nested.zip")),
            archive_limits(),
            "nested_archive_rejected",
        ),
        (mark_zip_encrypted(make_zip()), archive_limits(), "archive_encryption_rejected"),
        (
            make_zip(compression=zipfile.ZIP_BZIP2),
            archive_limits(),
            "archive_compression_rejected",
        ),
        (make_zip(), archive_limits(max_member_uncompressed_bytes=5), "archive_limits_exceeded"),
        (make_zip(), archive_limits(max_total_uncompressed_bytes=20), "archive_limits_exceeded"),
        (
            make_zip(payload=b"A" * 1000),
            archive_limits(max_compression_ratio=2.0),
            "archive_limits_exceeded",
        ),
    ],
)
def test_unsafe_archives_are_rejected_before_publication(
    tmp_path: Path, content: bytes, limits: ArchiveLimits, category: str
) -> None:
    url = (
        "https://datagov.mot.go.th/dataset/roads/resource/"
        f"{DRR_RESOURCE_ID}/download/roads.zip"
    )
    with pytest.raises(AcquisitionError, match=f"^{category}$"):
        acquire_resource(
            drr_spec(url, limits),
            FakeSession([FakeResponse(content, content_type="application/zip")]),  # type: ignore[arg-type]
            tmp_path,
            minimum_free_bytes=0,
            clock=lambda: FIXED_TIME,
        )
    destination = tmp_path / "infrastructure/roads/drr"
    assert list(destination.iterdir()) == []


def test_publication_failure_before_artifact_reports_clean_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_link(source: object, destination: object) -> None:
        raise OSError("private detail")

    monkeypatch.setattr(acquisition.os, "link", fail_link)
    with pytest.raises(AcquisitionError, match="^publication_failed$") as caught:
        acquire_csv(tmp_path, FakeSession([FakeResponse(b"a\n")]))

    error = caught.value
    assert not error.artifact_published
    assert not error.metadata_published
    assert not error.rollback_attempted
    assert not error.cleanup_failed
    assert "private detail" not in repr(error)
    assert not any(path.name.endswith(".tmp") for path in tmp_path.rglob("*"))


def test_metadata_publication_failure_rolls_back_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link
    count = 0

    def fail_second(source: object, destination: object) -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("private detail")
        real_link(source, destination)

    monkeypatch.setattr(acquisition.os, "link", fail_second)
    with pytest.raises(AcquisitionError, match="^publication_failed$") as caught:
        acquire_csv(tmp_path, FakeSession([FakeResponse(b"a\n")]))

    error = caught.value
    assert error.artifact_published
    assert not error.metadata_published
    assert error.rollback_attempted and error.rollback_succeeded is True
    assert list((tmp_path / "infrastructure/healthcare/dga").iterdir()) == []


def test_metadata_publication_and_rollback_failure_preserve_flags_and_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link
    real_unlink = Path.unlink
    count = 0

    def fail_second_link(source: object, destination: object) -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("secret metadata publication detail")
        real_link(source, destination)

    def fail_artifact_rollback(path: Path, *args: object, **kwargs: object) -> None:
        if path.suffix == ".csv" and not path.name.endswith(".tmp"):
            raise OSError("secret rollback detail")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(acquisition.os, "link", fail_second_link)
    monkeypatch.setattr(Path, "unlink", fail_artifact_rollback)
    with pytest.raises(AcquisitionError, match="^publication_failed$") as caught:
        acquire_csv(tmp_path, FakeSession([FakeResponse(b"a\n")]))

    error = caught.value
    assert error.artifact_published and not error.metadata_published
    assert error.rollback_attempted and error.rollback_succeeded is False
    assert error.cleanup_failed is False
    assert "secret" not in str(error) + repr(error)
    destination = tmp_path / "infrastructure/healthcare/dga"
    assert len(list(destination.glob("*.csv"))) == 1
    assert not list(destination.glob("*.metadata.json"))


def test_prepublication_cleanup_failure_preserves_temp_and_safe_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_unlink = Path.unlink

    def fail_download_temp_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if path.name.startswith(".download."):
            raise OSError("dummy-secret cleanup detail")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_download_temp_cleanup)
    with pytest.raises(AcquisitionError, match="^download_too_large$") as caught:
        acquire_resource(
            dga_spec(max_bytes=2),
            FakeSession([FakeResponse(b"too-large")]),  # type: ignore[arg-type]
            tmp_path,
            minimum_free_bytes=0,
            clock=lambda: FIXED_TIME,
        )

    error = caught.value
    assert error.cleanup_failed
    assert not error.artifact_published and not error.metadata_published
    assert not error.rollback_attempted and error.rollback_succeeded is None
    assert "dummy-secret" not in str(error) + repr(error)
    destination = tmp_path / "infrastructure/healthcare/dga"
    assert len(list(destination.glob(".download.*.tmp"))) == 1
    assert not list(destination.glob("*.csv"))


def test_filesystem_failures_use_fixed_safe_categories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def private_failure(*args: object, **kwargs: object) -> object:
        raise OSError("dummy-secret filesystem detail")

    monkeypatch.setattr(acquisition, "_free_bytes_for", private_failure)
    with pytest.raises(AcquisitionError, match="^free_space_check_failed$") as caught:
        acquire_csv(tmp_path, FakeSession([]))
    assert "dummy-secret" not in str(caught.value) + repr(caught.value)

    monkeypatch.setattr(acquisition, "_free_bytes_for", lambda root: 10_000)
    monkeypatch.setattr(Path, "mkdir", private_failure)
    with pytest.raises(AcquisitionError, match="^directory_creation_failed$") as caught:
        acquire_csv(tmp_path, FakeSession([]))
    assert "dummy-secret" not in str(caught.value) + repr(caught.value)


def test_temporary_write_failure_uses_fixed_safe_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_temporary(*args: object, **kwargs: object) -> object:
        raise OSError("dummy-secret temporary detail")

    monkeypatch.setattr(acquisition.tempfile, "NamedTemporaryFile", fail_temporary)
    with pytest.raises(AcquisitionError, match="^temporary_write_failed$") as caught:
        acquire_csv(tmp_path, FakeSession([FakeResponse(b"a\n")]))

    error = caught.value
    assert not error.artifact_published and not error.metadata_published
    assert not error.cleanup_failed
    assert "dummy-secret" not in str(error) + repr(error)


def test_cleanup_failure_after_pair_publication_preserves_complete_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acquisition, "_cleanup_paths", lambda *paths: False)

    with pytest.raises(AcquisitionError, match="^cleanup_failed$") as caught:
        acquire_csv(tmp_path, FakeSession([FakeResponse(b"a\n")]))

    error = caught.value
    assert error.artifact_published and error.metadata_published
    assert error.cleanup_failed
    destination = tmp_path / "infrastructure/healthcare/dga"
    assert len([path for path in destination.iterdir() if not path.name.endswith(".tmp")]) == 2


def test_retry_enabled_requests_session_is_rejected_before_request(tmp_path: Path) -> None:
    session = requests_session = __import__("requests").Session()
    adapter = __import__("requests").adapters.HTTPAdapter(max_retries=1)
    requests_session.mount("https://", adapter)
    with pytest.raises(AcquisitionError, match="^retry_policy_rejected$"):
        acquire_resource(
            dga_spec(), session, tmp_path, minimum_free_bytes=0, clock=lambda: FIXED_TIME
        )
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []
    session.close()
