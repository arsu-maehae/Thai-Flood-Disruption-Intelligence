from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests

from src.configuration import GistdaConfig, OFFICIAL_GISTDA_API_BASE_URL
from src.ingestion.flood_frequency import (
    SourcePageStructureError,
    SourceSanitizationError,
)
from src.ingestion.gistda_client import GistdaResponse
from src.ingestion.pagination import PaginationPolicyError, paginate_pattani
from tests.test_flood_frequency import DUMMY_KEY, FIXED_TIME


NUMBER_MATCHED_ABSENT = object()


def page_bytes(
    ids: list[str | None],
    *,
    number_returned: object | None = None,
    number_matched: object = 5,
    next_offset: int = 999999,
) -> bytes:
    features = [
        ({"type": "Feature", "properties": {}} if value is None else {"id": value, "type": "Feature", "properties": {}})
        for value in ids
    ]
    payload = {
        "type": "FeatureCollection",
        "features": features,
        "numberReturned": len(features) if number_returned is None else number_returned,
        "links": [
            {
                "rel": "next",
                "href": (
                    "https://api.example.test/features/flood-freq"
                    f"?api_key={DUMMY_KEY}&offset={next_offset}&limit=1"
                ),
            }
        ],
    }
    if number_matched is not NUMBER_MATCHED_ABSENT:
        payload["numberMatched"] = number_matched
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class SequencedClient:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get_flood_frequency(self, **kwargs: Any) -> GistdaResponse:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("pagination requested an unexpected page")
        return GistdaResponse(
            self.responses.pop(0),
            200,
            "application/geo+json",
        )


@pytest.fixture(autouse=True)
def prohibit_external_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*_: object, **__: object) -> None:
        raise AssertionError("live network access is prohibited")

    def fail_dotenv(*_: object, **__: object) -> None:
        raise AssertionError("dotenv access is prohibited")

    monkeypatch.setattr(requests.sessions.Session, "request", fail_network)
    monkeypatch.setattr("src.configuration.dotenv_values", fail_dotenv)


def config() -> GistdaConfig:
    return GistdaConfig(
        api_base_url=OFFICIAL_GISTDA_API_BASE_URL,
        api_key=DUMMY_KEY,
        province_id="94",
    )


def paginate(tmp_path: Path, client: SequencedClient, *, limit: int = 2, max_pages: int = 10):
    return paginate_pattani(
        config=config(),
        client=client,  # type: ignore[arg-type]
        output_root=tmp_path,
        limit=limit,
        max_pages=max_pages,
        retrieved_at=FIXED_TIME,
    )


def assert_artifacts_are_credential_safe(result: Any) -> None:
    for page in result.pages:
        artifact_bytes = page.artifact_path.read_bytes()
        metadata_bytes = page.metadata_path.read_bytes()
        assert DUMMY_KEY.encode() not in artifact_bytes
        assert DUMMY_KEY.encode() not in metadata_bytes
        payload = json.loads(artifact_bytes)
        for link in payload["links"]:
            names = {
                name.casefold()
                for name, _ in parse_qsl(urlsplit(link["href"]).query)
            }
            assert "api_key" not in names


def test_full_pages_then_partial_page_use_exact_offsets_and_ignore_links(
    tmp_path: Path,
) -> None:
    client = SequencedClient(
        [
            page_bytes(["a", "b"], next_offset=700),
            page_bytes(["c", "d"], next_offset=800),
            page_bytes(["e"], next_offset=900),
        ]
    )

    result = paginate(tmp_path, client)

    assert result.requested_offsets == (0, 2, 4)
    assert [call["offset"] for call in client.calls] == [0, 2, 4]
    assert all(call["limit"] == 2 and call["pv_idn"] == "94" for call in client.calls)
    assert result.stop_reason == "partial_page"
    assert result.total_features == 5
    assert result.observed_number_matched == 5
    assert result.duplicate_id_check_complete is True
    assert result.to_dict()["page_count"] == 3
    assert_artifacts_are_credential_safe(result)


def test_empty_first_page_stops_immediately(tmp_path: Path) -> None:
    client = SequencedClient([page_bytes([], number_matched=0)])

    result = paginate(tmp_path, client)

    assert result.requested_offsets == (0,)
    assert result.stop_reason == "empty_page"
    assert result.total_features == 0


def test_empty_final_page_after_full_page(tmp_path: Path) -> None:
    client = SequencedClient([page_bytes(["a", "b"], number_matched=2), page_bytes([], number_matched=2)])

    result = paginate(tmp_path, client)

    assert result.requested_offsets == (0, 2)
    assert result.stop_reason == "empty_page"


def test_repeated_page_content_fails(tmp_path: Path) -> None:
    client = SequencedClient([page_bytes(["a", "b"]), page_bytes(["a", "b"])])

    with pytest.raises(PaginationPolicyError, match="repeated"):
        paginate(tmp_path, client)


def test_duplicate_ids_across_pages_fail(tmp_path: Path) -> None:
    client = SequencedClient([page_bytes(["a", "b"]), page_bytes(["b", "c"])])

    with pytest.raises(PaginationPolicyError, match="duplicate"):
        paginate(tmp_path, client)


def test_missing_ids_report_incomplete_duplicate_verification(tmp_path: Path) -> None:
    client = SequencedClient([page_bytes(["a", None])])

    result = paginate(tmp_path, client, limit=3)

    assert result.stop_reason == "partial_page"
    assert result.duplicate_id_check_complete is False
    assert result.missing_feature_id_count == 1


@pytest.mark.parametrize(
    ("label", "feature"),
    [
        ("missing", {"type": "Feature"}),
        ("null", {"id": None, "type": "Feature"}),
        ("empty", {"id": "", "type": "Feature"}),
        ("boolean", {"id": True, "type": "Feature"}),
        ("numeric", {"id": 7, "type": "Feature"}),
        ("list", {"id": ["a"], "type": "Feature"}),
        ("object", {"id": {"value": "a"}, "type": "Feature"}),
    ],
    ids=lambda item: item if isinstance(item, str) else None,
)
def test_unusable_feature_ids_increment_incomplete_count(
    tmp_path: Path, label: str, feature: dict[str, object]
) -> None:
    payload = {
        "features": [feature],
        "numberReturned": 1,
        "numberMatched": 1,
    }
    client = SequencedClient([json.dumps(payload).encode("utf-8")])

    result = paginate(tmp_path, client, limit=2)

    assert result.missing_feature_id_count == 1, label
    assert result.duplicate_id_check_complete is False
    assert result.pages[0].feature_ids == ()


@pytest.mark.parametrize("feature", [None, True, 7, "feature", [], ["item"]])
def test_non_object_features_fail(
    tmp_path: Path, feature: object
) -> None:
    payload = {
        "features": [feature],
        "numberReturned": 1,
        "numberMatched": 1,
    }
    client = SequencedClient([json.dumps(payload).encode("utf-8")])

    with pytest.raises(SourcePageStructureError, match="JSON object"):
        paginate(tmp_path, client, limit=2)

    assert not client.responses
    assert list(tmp_path.iterdir()) == []


def test_changing_number_matched_fails(tmp_path: Path) -> None:
    client = SequencedClient(
        [page_bytes(["a", "b"], number_matched=4), page_bytes(["c"], number_matched=5)]
    )

    with pytest.raises(PaginationPolicyError, match="numberMatched"):
        paginate(tmp_path, client)


def test_number_matched_missing_then_present_fails(tmp_path: Path) -> None:
    client = SequencedClient(
        [
            page_bytes(["a", "b"], number_matched=NUMBER_MATCHED_ABSENT),
            page_bytes(["c"], number_matched=3),
        ]
    )

    with pytest.raises(PaginationPolicyError, match="presence"):
        paginate(tmp_path, client)


def test_number_matched_present_then_missing_fails(tmp_path: Path) -> None:
    client = SequencedClient(
        [
            page_bytes(["a", "b"], number_matched=3),
            page_bytes(["c"], number_matched=NUMBER_MATCHED_ABSENT),
        ]
    )

    with pytest.raises(PaginationPolicyError, match="presence"):
        paginate(tmp_path, client)


def test_number_matched_consistently_absent_is_allowed(tmp_path: Path) -> None:
    client = SequencedClient(
        [
            page_bytes(["a", "b"], number_matched=NUMBER_MATCHED_ABSENT),
            page_bytes(["c"], number_matched=NUMBER_MATCHED_ABSENT),
        ]
    )

    result = paginate(tmp_path, client)

    assert result.observed_number_matched is None
    assert result.stop_reason == "partial_page"


def test_number_matched_consistently_stable_is_allowed(tmp_path: Path) -> None:
    client = SequencedClient(
        [page_bytes(["a", "b"], number_matched=3), page_bytes(["c"], number_matched=3)]
    )

    result = paginate(tmp_path, client)

    assert result.observed_number_matched == 3
    assert result.stop_reason == "partial_page"


def test_malformed_intermediate_page_stops_without_extra_request(tmp_path: Path) -> None:
    client = SequencedClient([page_bytes(["a", "b"]), b"not-json"])
    run_result = None

    with pytest.raises(SourceSanitizationError):
        run_result = paginate(tmp_path, client)

    assert run_result is None
    assert [call["offset"] for call in client.calls] == [0, 2]
    artifacts = list(tmp_path.rglob("*.sanitized.json"))
    metadata = list(tmp_path.rglob("*.metadata.json"))
    assert len(artifacts) == 1
    assert len(metadata) == 1
    assert not list(tmp_path.rglob("*manifest*"))


@pytest.mark.parametrize(
    "payload",
    [
        {"numberReturned": 0},
        {"features": {}, "numberReturned": 0},
        {"features": [], "numberReturned": -1},
        {"features": [], "numberReturned": True},
        {"features": [], "numberReturned": 1},
        {"features": [], "numberReturned": 0, "numberMatched": -1},
    ],
)
def test_invalid_page_structure_or_counts_fail(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    client = SequencedClient([json.dumps(payload).encode("utf-8")])

    with pytest.raises(SourcePageStructureError):
        paginate(tmp_path, client)

    assert list(tmp_path.iterdir()) == []


def test_maximum_page_cap_fails_after_configured_number_of_pages(
    tmp_path: Path,
) -> None:
    client = SequencedClient([page_bytes(["a", "b"])])

    with pytest.raises(PaginationPolicyError, match="maximum"):
        paginate(tmp_path, client, max_pages=1)

    assert len(client.calls) == 1
