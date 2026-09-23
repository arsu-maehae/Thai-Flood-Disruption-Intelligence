from __future__ import annotations

import copy
import hashlib
import json
import socket
from pathlib import Path

import pytest

import src.transformation.flood_frequency as transformation_module
from src.transformation import (
    SourcePageLineage,
    TransformationError,
    ValidatedTransformationPage,
    publish_transformed_run,
    transform_validated_page,
)
from src.validation import parse_source_page, validate_source_page


KEY = "dummy-transformation-key"
RUN_ID = "pattani-test-run"
TRANSFORMATION_ID = "neutral-jsonl-v1"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external access is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("dotenv.main.dotenv_values", forbidden)


def properties() -> dict[str, object]:
    values: dict[str, object] = {
        name: f"safe-{name}"
        for name in (
            "_collectionId",
            "_createdAt",
            "_createdBy",
            "_id",
            "_updatedAt",
            "_updatedBy",
            "ap_code",
            "ap_en",
            "ap_tn",
            "com_tn",
            "pv_code",
            "pv_en",
            "pv_tn",
            "re_nesdb",
            "re_royin",
            "tb_code",
            "tb_en",
            "tb_tn",
        )
    }
    values.update({
        "ap_idn": 1,
        "freq": 2,
        "objectid": 3,
        "pv_idn": 94,
        "tb_idn": 4,
        **{f"y_{year}": year for year in range(2011, 2025)},
        "area_rai": 1.25,
        "shape_area": 2.5,
        "shape_length": 3.75,
    })
    return values


def feature(feature_id: str = "private-id") -> dict[str, object]:
    return {
        "type": "Feature",
        "id": feature_id,
        "properties": properties(),
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [[[[1.0, 2.0], [3.0, 4.0], [1.0, 2.0]]]],
        },
    }


def validated_page(
    features: list[dict[str, object]],
    *,
    index: int = 0,
    offset: int = 0,
    limit: int | None = None,
    api_key: str | None = KEY,
) -> ValidatedTransformationPage:
    requested_limit = len(features) if limit is None else limit
    payload = {
        "type": "FeatureCollection",
        "features": features,
        "numberReturned": len(features),
        "numberMatched": 2,
    }
    parsed = parse_source_page(json.dumps(payload), api_key=api_key)
    validation = validate_source_page(
        parsed,
        page_index=index,
        requested_offset=offset,
        requested_limit=requested_limit,
    )
    lineage = SourcePageLineage(
        run_id=RUN_ID,
        page_index=index,
        requested_offset=offset,
        requested_limit=requested_limit,
        source_artifact_path=f"gistda/flood_freq/pattani/source-{index}.sanitized.json",
        source_metadata_path=(
            f"gistda/flood_freq/pattani/source-{index}.metadata.json"
        ),
        source_stored_sha256=f"{index + 1:064x}",
    )
    return ValidatedTransformationPage(parsed, validation, lineage)


def test_deterministic_page_contains_only_sequence_and_complete_feature():
    source_feature = feature()
    page = validated_page([source_feature])

    first = transform_validated_page(page.parsed, page.validation, page.lineage)
    second = transform_validated_page(page.parsed, page.validation, page.lineage)
    record = json.loads(first.content)

    assert first.content == second.content
    assert set(record) == {"feature_sequence", "feature"}
    assert record["feature_sequence"] == 0
    assert record["feature"] == source_feature
    assert first.sha256 == hashlib.sha256(first.content).hexdigest()
    assert first.byte_count == len(first.content)
    assert "SourcePageLineage()" == repr(page.lineage)
    assert "private-id" not in repr(first)


def test_multi_page_publication_uses_page_lineage_and_manifest_last(tmp_path):
    first = validated_page([feature("private-a")], limit=1)
    final = validated_page(
        [feature("private-b")], index=1, offset=1, limit=2
    )

    result = publish_transformed_run(
        [first, final],
        output_root=tmp_path,
        run_id=RUN_ID,
        transformation_id=TRANSFORMATION_ID,
    )
    manifest = json.loads(result.manifest_path.read_bytes())

    assert result.page_count == 2 and result.feature_count == 2
    assert manifest["status"] == "complete"
    assert manifest["schema_basis"] == (
        "versioned_project_schema_from_observed_snapshot"
    )
    assert manifest["provider_contract_claimed"] is False
    assert manifest["crs_status"] == "not_verified"
    assert len(manifest["pages"]) == 2
    for entry in manifest["pages"]:
        output = result.directory / entry["output_page_path"]
        content = output.read_bytes()
        assert entry["output_page_sha256"] == hashlib.sha256(content).hexdigest()
        assert entry["output_page_byte_count"] == len(content)
        assert entry["output_record_count"] == 1
        record = json.loads(content)
        assert set(record) == {"feature", "feature_sequence"}
        assert "source_artifact_path" not in record
        assert "source_stored_sha256" not in record


@pytest.mark.parametrize(
    "change",
    ["missing_property", "wrong_property_type", "wrong_geometry", "extra_member"],
)
def test_observed_project_schema_drift_is_rejected_safely(change):
    observed = feature()
    if change == "missing_property":
        del observed["properties"]["freq"]
    elif change == "wrong_property_type":
        observed["properties"]["freq"] = True
    elif change == "wrong_geometry":
        observed["geometry"]["type"] = "Polygon"
    else:
        observed["links"] = []
    page = validated_page([observed])

    with pytest.raises(
        TransformationError, match="^invalid_transformation_schema$"
    ) as caught:
        transform_validated_page(page.parsed, page.validation, page.lineage)
    assert "private-id" not in str(caught.value) + repr(caught.value)


def test_late_drift_leaves_incomplete_directory_without_manifest(tmp_path):
    first = validated_page([feature("private-a")], limit=1)
    drifted = feature("private-b")
    drifted["properties"] = copy.deepcopy(drifted["properties"])
    del drifted["properties"]["freq"]
    second = validated_page([drifted], index=1, offset=1, limit=2)

    with pytest.raises(TransformationError, match="^invalid_transformation_schema$"):
        publish_transformed_run(
            [first, second],
            output_root=tmp_path,
            run_id=RUN_ID,
            transformation_id=TRANSFORMATION_ID,
        )

    directory = (
        tmp_path
        / "gistda"
        / "flood_freq"
        / "pattani"
        / RUN_ID
        / TRANSFORMATION_ID
    )
    assert (directory / "pages" / "page_000000.jsonl").is_file()
    assert not (directory / "pages" / "page_000001.jsonl").exists()
    assert not (directory / "transformation_manifest.json").exists()
    assert list(directory.rglob("*.tmp")) == []


def test_sequence_and_credential_coverage_are_checked_before_directory(tmp_path):
    incomplete = validated_page([feature()], api_key=None)
    destination = (
        tmp_path
        / "gistda"
        / "flood_freq"
        / "pattani"
        / RUN_ID
        / TRANSFORMATION_ID
    )
    with pytest.raises(
        TransformationError, match="^credential_verification_incomplete$"
    ):
        publish_transformed_run(
            [incomplete],
            output_root=tmp_path,
            run_id=RUN_ID,
            transformation_id=TRANSFORMATION_ID,
        )
    assert not destination.exists()

    wrong_sequence = validated_page([feature()], index=1)
    with pytest.raises(TransformationError, match="^source_validation_failed$"):
        publish_transformed_run(
            [wrong_sequence],
            output_root=tmp_path,
            run_id=RUN_ID,
            transformation_id=TRANSFORMATION_ID,
        )
    assert not destination.exists()


def test_existing_destination_is_never_overwritten(tmp_path):
    page = validated_page([feature()], limit=2)
    result = publish_transformed_run(
        [page],
        output_root=tmp_path,
        run_id=RUN_ID,
        transformation_id=TRANSFORMATION_ID,
    )
    before = result.manifest_path.read_bytes()

    with pytest.raises(TransformationError, match="^output_already_exists$"):
        publish_transformed_run(
            [page],
            output_root=tmp_path,
            run_id=RUN_ID,
            transformation_id=TRANSFORMATION_ID,
        )
    assert result.manifest_path.read_bytes() == before


def test_publication_failure_before_link_cleans_temporary_file(
    tmp_path, monkeypatch
):
    page = validated_page([feature()], limit=2)

    def fail_link(*args, **kwargs):
        raise OSError("private publication detail")

    monkeypatch.setattr(transformation_module.os, "link", fail_link)
    with pytest.raises(TransformationError, match="^publication_failed$") as caught:
        publish_transformed_run(
            [page],
            output_root=tmp_path,
            run_id=RUN_ID,
            transformation_id=TRANSFORMATION_ID,
        )

    error = caught.value
    directory = (
        tmp_path
        / "gistda"
        / "flood_freq"
        / "pattani"
        / RUN_ID
        / TRANSFORMATION_ID
    )
    assert not error.record_published
    assert not error.completion_published
    assert not (directory / "pages" / "page_000000.jsonl").exists()
    assert not (directory / "transformation_manifest.json").exists()
    assert list(directory.rglob("*.tmp")) == []
    assert "private publication detail" not in str(error) + repr(error)


def test_page_publication_cleanup_failure_stops_before_manifest(
    tmp_path, monkeypatch
):
    page = validated_page([feature()], limit=2)
    real_unlink = Path.unlink

    def fail_temporary_cleanup(path, *args, **kwargs):
        if path.suffix == ".tmp":
            raise OSError("private cleanup detail")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temporary_cleanup)
    with pytest.raises(TransformationError, match="^cleanup_failed$") as caught:
        publish_transformed_run(
            [page],
            output_root=tmp_path,
            run_id=RUN_ID,
            transformation_id=TRANSFORMATION_ID,
        )

    error = caught.value
    directory = (
        tmp_path
        / "gistda"
        / "flood_freq"
        / "pattani"
        / RUN_ID
        / TRANSFORMATION_ID
    )
    assert error.record_published
    assert not error.completion_published
    assert (directory / "pages" / "page_000000.jsonl").is_file()
    assert not (directory / "transformation_manifest.json").exists()
    assert "private cleanup detail" not in str(error) + repr(error)


def test_manifest_publication_cleanup_failure_remains_complete(
    tmp_path, monkeypatch
):
    page = validated_page([feature()], limit=2)
    real_unlink = Path.unlink

    def fail_manifest_temporary_cleanup(path, *args, **kwargs):
        if path.name.startswith(".transformation_manifest.json."):
            raise OSError("private manifest cleanup detail")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_manifest_temporary_cleanup)
    with pytest.raises(TransformationError, match="^cleanup_failed$") as caught:
        publish_transformed_run(
            [page],
            output_root=tmp_path,
            run_id=RUN_ID,
            transformation_id=TRANSFORMATION_ID,
        )

    error = caught.value
    directory = (
        tmp_path
        / "gistda"
        / "flood_freq"
        / "pattani"
        / RUN_ID
        / TRANSFORMATION_ID
    )
    manifest_path = directory / "transformation_manifest.json"
    published = manifest_path.read_bytes()
    assert error.record_published
    assert error.completion_published
    assert json.loads(published)["status"] == "complete"
    assert manifest_path.read_bytes() == published
    safe_error = str(error) + repr(error)
    assert "private manifest cleanup detail" not in safe_error
    assert "private-id" not in safe_error
