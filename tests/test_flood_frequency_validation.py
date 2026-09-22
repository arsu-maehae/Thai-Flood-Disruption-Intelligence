from __future__ import annotations

import json
import socket
from dataclasses import FrozenInstanceError
from urllib.parse import quote, quote_plus

import pytest

from src.validation import (
    PageValidationResult,
    RunValidationResult,
    SourceParsingError,
    SourceStructuralError,
    SourceValidationError,
    parse_source_page,
    validate_page_sequence,
    validate_source_page,
)


SECRET = "dummy-secret+value"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external access is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("dotenv.main.dotenv_values", forbidden)


def payload(
    ids=("a", "b"), *, number_matched=2, extra=None, features=None
) -> str:
    values = (
        [{"type": "Feature", "id": value, "properties": {}, "geometry": None}
         for value in ids]
        if features is None
        else features
    )
    document = {
        "type": "FeatureCollection",
        "numberReturned": len(values),
        "features": values,
    }
    if number_matched is not ABSENT:
        document["numberMatched"] = number_matched
    if extra:
        document.update(extra)
    return json.dumps(document, ensure_ascii=False)


ABSENT = object()


def page(text=None, *, index=0, offset=0, limit=2, api_key=SECRET):
    parsed = parse_source_page(text or payload(), api_key=api_key)
    return validate_source_page(
        parsed, page_index=index, requested_offset=offset, requested_limit=limit
    )


@pytest.mark.parametrize("value", [payload(), payload().encode("utf-8")])
def test_strict_text_and_bytes_are_accepted(value):
    result = validate_source_page(
        parse_source_page(value, api_key=SECRET),
        page_index=0,
        requested_offset=0,
        requested_limit=2,
    )
    assert result.number_returned == 2
    assert result.configured_key_check_complete


@pytest.mark.parametrize(
    "value,category",
    [
        (b'\xff', "invalid_utf8"),
        ("\ud800", "invalid_utf8"),
        (b'{', "invalid_json"),
        (b'{"x":NaN}', "invalid_json"),
        (object(), "invalid_payload_type"),
    ],
)
def test_parsing_errors_are_fixed_and_separate(value, category):
    with pytest.raises(SourceParsingError, match=f"^{category}$") as caught:
        parse_source_page(value)
    assert caught.value.category == category


@pytest.mark.parametrize(
    "text",
    [
        '{"features":[],"features":[]}',
        '{"outer":{"x":1,"x":2},"features":[]}',
    ],
)
def test_duplicate_keys_are_rejected_during_parsing(text):
    with pytest.raises(SourceParsingError, match="^duplicate_json_key$"):
        parse_source_page(text)


def test_no_key_reports_incomplete_coverage():
    parsed = parse_source_page(payload())
    validated = validate_source_page(
        parsed, page_index=0, requested_offset=0, requested_limit=2
    )
    run = validate_page_sequence([validated])
    assert not parsed.configured_key_check_complete
    assert not run.configured_key_check_complete
    assert run.issue_categories == ("configured_key_unverified",)


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
@pytest.mark.parametrize("name", ["api_key", "API-Key", "authorization"])
def test_encoded_credential_object_names_are_rejected(name, depth):
    encoded = name
    for _ in range(depth):
        encoded = quote(encoded, safe="")
    text = json.dumps({"features": [], encoded: "redacted"})
    with pytest.raises(SourceParsingError, match="^credential_rejected$"):
        parse_source_page(text)


@pytest.mark.parametrize(
    "encoded",
    ["%61pi_key", "%2561pi_key", "api%2Dkey", "api%252Dkey",
     "%61uthorization", "%2561uthorization"],
)
def test_actual_encoded_credential_object_names_are_rejected(encoded):
    with pytest.raises(SourceParsingError, match="^credential_rejected$"):
        parse_source_page(json.dumps({"features": [], encoded: "redacted"}))


@pytest.mark.parametrize("depth", [0, 1, 2])
@pytest.mark.parametrize("name", ["api_key", "API-Key", "authorization"])
def test_encoded_credential_query_names_are_rejected_anywhere(name, depth):
    encoded = name
    for _ in range(depth):
        encoded = quote(encoded, safe="")
    text = payload(extra={"note": f"https://safe.invalid/path?{encoded}=redacted&limit=2"})
    with pytest.raises(SourceParsingError, match="^credential_rejected$"):
        parse_source_page(text)


@pytest.mark.parametrize(
    "encoded",
    ["%61pi_key", "%2561pi_key", "api%2Dkey", "api%252Dkey",
     "%61uthorization", "%2561uthorization"],
)
def test_actual_encoded_credential_query_names_are_rejected(encoded):
    text = payload(extra={"note": f"https://safe.invalid/?{encoded}=redacted"})
    with pytest.raises(SourceParsingError, match="^credential_rejected$"):
        parse_source_page(text)


@pytest.mark.parametrize(
    "encoded",
    [SECRET, quote(SECRET, safe=""), quote(quote(SECRET, safe=""), safe=""), quote_plus(SECRET)],
)
def test_configured_key_is_rejected_at_encoding_depth(encoded):
    with pytest.raises(SourceParsingError, match="^credential_rejected$") as caught:
        parse_source_page(payload(extra={"note": encoded}), api_key=SECRET)
    assert SECRET not in str(caught.value)
    assert encoded not in repr(caught.value)


def test_safe_encoded_text_and_query_parameters_remain_valid():
    text = payload(extra={"note": "https://safe.invalid/?offset=0&label=safe%2520text"})
    assert parse_source_page(text, api_key=SECRET).configured_key_check_complete


@pytest.mark.parametrize(
    "text,category",
    [
        ("[]", "source_page_not_object"),
        ('{"features":{}}', "features_not_list"),
        ('{"features":[1],"numberReturned":1}', "feature_not_object"),
        ('{"features":[],"numberReturned":true}', "invalid_number_returned"),
        ('{"features":[],"numberReturned":-1}', "invalid_number_returned"),
        ('{"features":[],"numberReturned":1}', "number_returned_mismatch"),
        ('{"features":[],"numberReturned":0,"numberMatched":true}', "invalid_number_matched"),
    ],
)
def test_structural_errors_are_fixed(text, category):
    parsed = parse_source_page(text, api_key=SECRET)
    with pytest.raises(SourceStructuralError, match=f"^{category}$"):
        validate_source_page(
            parsed, page_index=0, requested_offset=0, requested_limit=2
        )


def test_requested_limit_bound_is_enforced():
    parsed = parse_source_page(payload(ids=("a", "b")), api_key=SECRET)
    with pytest.raises(SourceStructuralError, match="^number_returned_exceeds_limit$"):
        validate_source_page(
            parsed, page_index=0, requested_offset=0, requested_limit=1
        )


@pytest.mark.parametrize("limit", [0, -1, True, 10_001, "2"])
def test_invalid_requested_limit_is_rejected(limit):
    with pytest.raises(SourceStructuralError, match="^invalid_requested_limit$"):
        validate_source_page(
            parse_source_page(payload(), api_key=SECRET),
            page_index=0,
            requested_offset=0,
            requested_limit=limit,
        )


@pytest.mark.parametrize(
    "invalid_id",
    [None, "", True, 1, [], {}],
)
def test_only_nonempty_string_ids_are_usable(invalid_id):
    text = payload(features=[{"id": "usable"}, {"id": invalid_id}, {}], number_matched=3)
    result = page(text, limit=3)
    assert result.missing_feature_id_count == 2
    assert not result.feature_id_check_complete


def test_duplicate_ids_within_page_are_rejected_without_reporting_id():
    identifier = "private-identifier"
    with pytest.raises(SourceStructuralError, match="^duplicate_feature_id$") as caught:
        page(payload(ids=(identifier, identifier)))
    assert identifier not in str(caught.value) + repr(caught.value)


def test_sequence_progression_and_stable_counts():
    first = page(payload(ids=("a", "b"), number_matched=3))
    second = page(
        payload(ids=("c",), number_matched=3), index=1, offset=2, limit=2
    )
    result = validate_page_sequence([first, second])
    assert result.page_count == 2 and result.total_features == 3
    assert result.observed_number_matched == 3
    assert result.duplicate_id_check_complete


def test_full_then_partial_is_valid_terminal_sequence():
    first = page(payload(ids=("a", "b"), number_matched=3))
    final = page(
        payload(ids=("c",), number_matched=3), index=1, offset=2, limit=2
    )
    assert validate_page_sequence([first, final]).total_features == 3


@pytest.mark.parametrize(
    "pages,category",
    [
        (lambda: [page(index=1)], "page_index_sequence"),
        (lambda: [page(offset=1)], "offset_sequence"),
        (
            lambda: [page(), page(payload(ids=("c",), number_matched=3), index=1, offset=2)],
            "number_matched_changed",
        ),
        (lambda: [page(), page(index=1, offset=2)], "repeated_page_content"),
        (
            lambda: [page(), page(payload(ids=("b", "c")), index=1, offset=2)],
            "duplicate_feature_id",
        ),
    ],
)
def test_sequence_failures_are_fixed(pages, category):
    with pytest.raises(SourceStructuralError, match=f"^{category}$"):
        validate_page_sequence(pages())


def test_consistently_absent_number_matched_is_allowed():
    first = page(payload(number_matched=ABSENT))
    second = page(
        payload(ids=("c",), number_matched=ABSENT), index=1, offset=2
    )
    result = validate_page_sequence([first, second])
    assert result.observed_number_matched_present is False
    assert result.observed_number_matched is None


@pytest.mark.parametrize("first_ids", [(), ("a",)])
def test_page_after_empty_or_partial_is_rejected(first_ids):
    first = page(payload(ids=first_ids, number_matched=2), limit=2)
    following = page(
        payload(ids=("b",), number_matched=2), index=1, offset=2, limit=2
    )
    with pytest.raises(SourceStructuralError, match="^page_after_terminal$"):
        validate_page_sequence([first, following])


def test_repeated_features_ignore_changing_safe_top_level_metadata():
    first_text = payload(extra={"links": [{"rel": "safe-a"}], "marker": 1})
    second_text = payload(extra={"links": [{"rel": "safe-b"}], "marker": 2})
    first = page(first_text)
    second = page(second_text, index=1, offset=2)
    with pytest.raises(SourceStructuralError, match="^repeated_page_content$"):
        validate_page_sequence([first, second])


@pytest.mark.parametrize(
    "name",
    ["api_key", "%61pi_key", "%2561pi_key", "API-Key",
     "%61uthorization", "%2561uthorization"],
)
def test_malformed_url_fallback_rejects_credential_query_names(name):
    text = payload(extra={"note": f"https://[malformed?{name}=redacted"})
    with pytest.raises(SourceParsingError, match="^credential_rejected$"):
        parse_source_page(text)


def test_direct_construction_is_rejected_with_safe_categories():
    from src.validation import ParsedSourcePage

    with pytest.raises(SourceStructuralError, match="^invalid_parsed_page$"):
        ParsedSourcePage()
    with pytest.raises(SourceStructuralError, match="^invalid_page_result$"):
        PageValidationResult()
    with pytest.raises(SourceStructuralError, match="^invalid_run_result$"):
        RunValidationResult()


def test_tampered_parsed_page_is_rejected():
    parsed = parse_source_page(payload(), api_key=SECRET)
    object.__setattr__(parsed, "_document_bytes", b'{"features":[]}')
    with pytest.raises(SourceStructuralError, match="^invalid_parsed_page$"):
        validate_source_page(
            parsed, page_index=0, requested_offset=0, requested_limit=2
        )
    assert repr(parsed) == "ParsedSourcePage(invalid)"


@pytest.mark.parametrize(
    "field,value",
    [
        ("page_index", True),
        ("number_returned", True),
        ("issue_categories", ("unsafe-source-text",)),
        ("configured_key_check_complete", 1),
    ],
)
def test_tampered_page_result_is_rejected_without_unsafe_text(field, value):
    validated = page()
    object.__setattr__(validated, field, value)
    with pytest.raises(SourceStructuralError, match="^invalid_page_result$") as caught:
        validate_page_sequence([validated])
    combined = str(caught.value) + repr(caught.value) + repr(validated)
    assert "unsafe-source-text" not in combined
    with pytest.raises(SourceStructuralError, match="^invalid_page_result$"):
        validated.to_dict()


@pytest.mark.parametrize("value", [None, 1, object(), "not-pages"])
def test_invalid_non_iterable_or_arbitrary_sequence_is_safe(value):
    with pytest.raises(SourceStructuralError, match="^invalid_page_sequence$|^invalid_page_result$"):
        validate_page_sequence(value)


def test_iterator_failure_becomes_fixed_safe_sequence_error():
    def broken():
        yield page()
        raise ValueError("private iterator failure")

    with pytest.raises(SourceStructuralError, match="^invalid_page_sequence$") as caught:
        validate_page_sequence(broken())
    assert "private iterator failure" not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("page_count", True),
        ("total_features", True),
        ("observed_number_matched", True),
        ("missing_feature_id_count", True),
        ("issue_categories", ("secret-bearing-issue",)),
        ("configured_key_check_complete", 1),
    ],
)
def test_tampered_run_result_is_safe_and_rejected(field, value):
    validated = page(payload(ids=("only",), number_matched=1), limit=1)
    result = validate_page_sequence([validated])
    object.__setattr__(result, field, value)
    assert repr(result) == "RunValidationResult(invalid)"
    with pytest.raises(SourceStructuralError, match="^invalid_run_result$") as caught:
        result.to_dict()
    combined = str(caught.value) + repr(caught.value) + repr(result)
    assert "secret-bearing-issue" not in combined


def test_results_are_frozen_and_reprs_reports_exclude_source_values():
    identifier = "private-id-never-report"
    secret_property = "private-property-never-report"
    text = payload(ids=(identifier,), features=[{
        "id": identifier,
        "properties": {"name": secret_property},
        "geometry": {"type": "Uninterpreted", "coordinates": [123, 456]},
    }], number_matched=1)
    parsed = parse_source_page(text, api_key=SECRET)
    validated = validate_source_page(
        parsed, page_index=0, requested_offset=0, requested_limit=1
    )
    run = validate_page_sequence([validated])
    combined = repr(parsed) + repr(validated) + repr(run) + json.dumps(validated.to_dict()) + json.dumps(run.to_dict())
    assert identifier not in combined
    assert secret_property not in combined
    assert SECRET not in combined
    with pytest.raises(FrozenInstanceError):
        validated.page_index = 2


def test_empty_sequence_cannot_claim_configured_key_coverage():
    with pytest.raises(SourceStructuralError, match="^empty_page_sequence$"):
        validate_page_sequence([])


def test_unknown_error_text_is_replaced_by_fixed_safe_category():
    error = SourceValidationError(SECRET)
    assert str(error) == "validation_error"
    assert SECRET not in repr(error)


def test_module_does_not_import_configuration_or_perform_external_access():
    parsed = parse_source_page(payload(), api_key=SECRET)
    assert isinstance(page(), PageValidationResult)
    assert parsed.configured_key_check_complete
