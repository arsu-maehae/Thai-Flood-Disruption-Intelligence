from __future__ import annotations

import json
import socket

import pytest

from src.validation import (
    FloodProfileError,
    SourceParsingError,
    ValidatedProfilePage,
    parse_source_page,
    profile_validated_pages,
    validate_source_page,
)


KEY = "dummy-profile-key"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external access is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("dotenv.main.dotenv_values", forbidden)


def validated(features, *, index=0, offset=0, limit=None, matched=None, api_key=KEY):
    requested_limit = len(features) if limit is None else limit
    document = {
        "type": "FeatureCollection",
        "features": features,
        "numberReturned": len(features),
        "numberMatched": len(features) if matched is None else matched,
    }
    parsed = parse_source_page(json.dumps(document), api_key=api_key)
    result = validate_source_page(
        parsed,
        page_index=index,
        requested_offset=offset,
        requested_limit=requested_limit,
    )
    return ValidatedProfilePage(parsed, result)


def fields(result):
    return {item.field_name: item for item in result.property_fields}


def geometry(result):
    return {item.geometry_type: item.count for item in result.geometry_type_counts}


def test_normal_multi_page_aggregation():
    first = validated([
        {"id": "a", "properties": {"depth": 1, "label": "one"},
         "geometry": {"type": "Point", "coordinates": [1, 2]}},
        {"id": "b", "properties": {"depth": 3.5, "label": "two"},
         "geometry": {"type": "Polygon", "coordinates": []}},
    ], matched=3)
    final = validated([
        {"id": "c", "properties": {"depth": 2},
         "geometry": {"type": "Point", "coordinates": [3, 4]}},
    ], index=1, offset=2, limit=2, matched=3)

    result = profile_validated_pages([first, final])
    assert result.page_count == 2 and result.feature_count == 3
    assert geometry(result) == {"Point": 2, "Polygon": 1}
    assert fields(result)["depth"].finite_numeric_min == 1
    assert fields(result)["depth"].finite_numeric_max == 3.5
    assert fields(result)["label"].missing_count == 1


def test_mixed_missing_null_and_json_types_with_invariants():
    result = profile_validated_pages([validated([
        {"id": "a"},
        {"id": "b", "properties": None, "geometry": None},
        {"id": "c", "properties": {"mixed": None}, "geometry": "other"},
        {"id": "d", "properties": {"mixed": True, "items": []},
         "geometry": {"type": "UnknownProviderType", "coordinates": []}},
    ], limit=5, matched=4, api_key=None)])

    assert result.properties_member_counts.to_dict() == {
        "missing": 1, "null": 1, "object": 2, "other": 0,
    }
    assert result.geometry_member_counts.total == result.feature_count == 4
    assert geometry(result) == {"unknown": 1}
    mixed = fields(result)["mixed"]
    assert mixed.present_count + mixed.missing_count == 4
    assert mixed.json_type_counts.total == mixed.present_count
    assert mixed.null_count == 1
    assert result.issue_categories == ("configured_key_unverified",)
    assert not result.configured_key_check_complete


@pytest.mark.parametrize("observed", [None, 1, [], {}])
def test_non_string_geometry_types_are_counted_as_unknown(observed):
    result = profile_validated_pages([validated([{
        "id": "a",
        "properties": {},
        "geometry": {"type": observed, "coordinates": []},
    }])])
    assert geometry(result) == {"unknown": 1}


def test_invalid_and_mismatched_inputs_are_rejected_safely():
    with pytest.raises(FloodProfileError, match="^invalid_profile_input$"):
        profile_validated_pages([object()])

    first = validated([{"id": "a", "properties": {}, "geometry": None}])
    other = validated([{"id": "b", "properties": {}, "geometry": None}])
    mismatched = ValidatedProfilePage(first.parsed, other.validation)
    with pytest.raises(FloodProfileError, match="^invalid_validated_page$"):
        profile_validated_pages([mismatched])


@pytest.mark.parametrize("number", ["NaN", "Infinity", "1e999"])
def test_nonfinite_numbers_fail_in_existing_parser(number):
    text = (
        '{"features":[{"properties":{"value":' + number + '}}],'
        '"numberReturned":1,"numberMatched":1}'
    )
    with pytest.raises(SourceParsingError, match="^invalid_json$"):
        parse_source_page(text, api_key=KEY)


def test_output_contains_only_safe_aggregates():
    feature_id = "private-feature-id"
    property_value = "private-property-value"
    unknown_geometry = "PrivateGeometryLabel"
    link = "https://safe.invalid/full-provider-link"
    result = profile_validated_pages([validated([{
        "id": feature_id,
        "properties": {"public_field_name": property_value},
        "geometry": {"type": unknown_geometry, "coordinates": [987654321, 123456789]},
        "links": [{"href": link}],
    }])])
    output = json.dumps(result.to_dict(), sort_keys=True) + repr(result)
    assert "public_field_name" in output
    assert geometry(result) == {"unknown": 1}
    for forbidden in (
        feature_id, property_value, unknown_geometry, link,
        "987654321", "123456789", KEY,
    ):
        assert forbidden not in output
