"""Pure structural validation for sanitized flood-frequency source pages.

This module performs no configuration loading, filesystem access, or network
activity. It deliberately does not interpret properties, geometry, coordinates,
CRS, units, or business meaning.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import unquote, unquote_plus, urlsplit


_CREDENTIAL_NAMES = frozenset({"api_key", "api-key", "authorization"})
_MAX_DECODED_REPRESENTATIONS = 64
_MISSING = object()
_VALIDATION_TOKEN = object()
_VALIDATION_SECRET = secrets.token_bytes(32)
_SAFE_ERROR_CATEGORIES = frozenset({
    "credential_rejected",
    "duplicate_feature_id",
    "duplicate_json_key",
    "empty_page_sequence",
    "feature_not_object",
    "features_not_list",
    "invalid_api_key",
    "invalid_json",
    "invalid_json_value",
    "invalid_number_matched",
    "invalid_number_returned",
    "invalid_page_index",
    "invalid_page_result",
    "invalid_page_sequence",
    "invalid_parsed_page",
    "invalid_payload_type",
    "invalid_requested_limit",
    "invalid_requested_offset",
    "invalid_run_result",
    "invalid_utf8",
    "number_matched_changed",
    "number_returned_exceeds_limit",
    "number_returned_mismatch",
    "offset_sequence",
    "page_after_terminal",
    "page_index_sequence",
    "repeated_page_content",
    "source_page_not_object",
    "validation_error",
})


class SourceValidationError(ValueError):
    """Base class whose message is always a fixed, credential-safe category."""

    def __init__(self, category: str) -> None:
        safe_category = (
            category if category in _SAFE_ERROR_CATEGORIES else "validation_error"
        )
        self.category = safe_category
        super().__init__(safe_category)


class SourceParsingError(SourceValidationError):
    """Raised before a validated JSON value exists."""


class SourceStructuralError(SourceValidationError):
    """Raised when parsed source structure or page sequencing is invalid."""


class _DuplicateKey(Exception):
    pass


class _CredentialRejected(Exception):
    pass


class _InvalidConstant(Exception):
    pass


@dataclass(frozen=True, init=False, repr=False)
class ParsedSourcePage:
    """Parsed JSON retained only for validation; its values never enter repr."""

    _document_bytes: bytes = field(repr=False, compare=False)
    configured_key_check_complete: bool
    issue_categories: tuple[str, ...]
    _proof: bytes = field(repr=False, compare=False)
    _token: object = field(repr=False, compare=False)

    def __new__(cls, token: object = None) -> "ParsedSourcePage":
        if token is not _VALIDATION_TOKEN:
            raise SourceStructuralError("invalid_parsed_page")
        return super().__new__(cls)

    def __init__(self, token: object = None) -> None:
        del token

    @classmethod
    def _create(
        cls,
        document_bytes: bytes,
        configured_key_check_complete: bool,
        issue_categories: tuple[str, ...],
    ) -> "ParsedSourcePage":
        instance = cls(_VALIDATION_TOKEN)
        proof = _seal(
            document_bytes, configured_key_check_complete, issue_categories
        )
        object.__setattr__(instance, "_document_bytes", document_bytes)
        object.__setattr__(
            instance,
            "configured_key_check_complete",
            configured_key_check_complete,
        )
        object.__setattr__(instance, "issue_categories", issue_categories)
        object.__setattr__(instance, "_proof", proof)
        object.__setattr__(instance, "_token", _VALIDATION_TOKEN)
        return instance

    def __repr__(self) -> str:
        if not _valid_parsed_page(self):
            return "ParsedSourcePage(invalid)"
        return (
            "ParsedSourcePage("
            f"configured_key_check_complete={self.configured_key_check_complete}, "
            f"issue_categories={self.issue_categories!r})"
        )


@dataclass(frozen=True, init=False, repr=False)
class PageValidationResult:
    """Safe page summary; IDs and content fingerprints remain in-memory only."""

    page_index: int
    requested_offset: int
    requested_limit: int
    number_returned: int
    number_matched_present: bool
    number_matched: int | None
    missing_feature_id_count: int
    feature_id_check_complete: bool
    configured_key_check_complete: bool
    issue_categories: tuple[str, ...]
    _usable_feature_ids: frozenset[str] = field(repr=False, compare=False)
    _page_fingerprint: str = field(repr=False, compare=False)
    _proof: bytes = field(repr=False, compare=False)
    _token: object = field(repr=False, compare=False)

    def __new__(cls, token: object = None) -> "PageValidationResult":
        if token is not _VALIDATION_TOKEN:
            raise SourceStructuralError("invalid_page_result")
        return super().__new__(cls)

    def __init__(self, token: object = None) -> None:
        del token

    @classmethod
    def _create(cls, **values: Any) -> "PageValidationResult":
        instance = cls(_VALIDATION_TOKEN)
        proof = _page_proof(**values)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        object.__setattr__(instance, "_proof", proof)
        object.__setattr__(instance, "_token", _VALIDATION_TOKEN)
        return instance

    def __repr__(self) -> str:
        if not _valid_page_result(self):
            return "PageValidationResult(invalid)"
        return (
            "PageValidationResult("
            f"page_index={self.page_index}, requested_offset={self.requested_offset}, "
            f"requested_limit={self.requested_limit}, number_returned={self.number_returned}, "
            f"number_matched_present={self.number_matched_present}, "
            f"number_matched={self.number_matched}, "
            f"missing_feature_id_count={self.missing_feature_id_count}, "
            f"feature_id_check_complete={self.feature_id_check_complete}, "
            f"configured_key_check_complete={self.configured_key_check_complete}, "
            f"issue_categories={self.issue_categories!r})"
        )

    def to_dict(self) -> dict[str, object]:
        if not _valid_page_result(self):
            raise SourceStructuralError("invalid_page_result")
        return {
            "page_index": self.page_index,
            "requested_offset": self.requested_offset,
            "requested_limit": self.requested_limit,
            "number_returned": self.number_returned,
            "number_matched_present": self.number_matched_present,
            "number_matched": self.number_matched,
            "missing_feature_id_count": self.missing_feature_id_count,
            "feature_id_check_complete": self.feature_id_check_complete,
            "configured_key_check_complete": self.configured_key_check_complete,
            "issue_categories": list(self.issue_categories),
        }


@dataclass(frozen=True, init=False, repr=False)
class RunValidationResult:
    """Safe aggregate result with no feature IDs or source content."""

    page_count: int
    total_features: int
    observed_number_matched_present: bool | None
    observed_number_matched: int | None
    missing_feature_id_count: int
    duplicate_id_check_complete: bool
    configured_key_check_complete: bool
    issue_categories: tuple[str, ...]
    _proof: bytes = field(repr=False, compare=False)
    _token: object = field(repr=False, compare=False)

    def __new__(cls, token: object = None) -> "RunValidationResult":
        if token is not _VALIDATION_TOKEN:
            raise SourceStructuralError("invalid_run_result")
        return super().__new__(cls)

    def __init__(self, token: object = None) -> None:
        del token

    @classmethod
    def _create(cls, **values: Any) -> "RunValidationResult":
        instance = cls(_VALIDATION_TOKEN)
        proof = _run_proof(**values)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        object.__setattr__(instance, "_proof", proof)
        object.__setattr__(instance, "_token", _VALIDATION_TOKEN)
        return instance

    def __repr__(self) -> str:
        if not _valid_run_result(self):
            return "RunValidationResult(invalid)"
        return (
            "RunValidationResult("
            f"page_count={self.page_count}, total_features={self.total_features}, "
            f"observed_number_matched_present={self.observed_number_matched_present}, "
            f"observed_number_matched={self.observed_number_matched}, "
            f"missing_feature_id_count={self.missing_feature_id_count}, "
            f"duplicate_id_check_complete={self.duplicate_id_check_complete}, "
            f"configured_key_check_complete={self.configured_key_check_complete}, "
            f"issue_categories={self.issue_categories!r})"
        )

    def to_dict(self) -> dict[str, object]:
        if not _valid_run_result(self):
            raise SourceStructuralError("invalid_run_result")
        return {
            "page_count": self.page_count,
            "total_features": self.total_features,
            "observed_number_matched_present": self.observed_number_matched_present,
            "observed_number_matched": self.observed_number_matched,
            "missing_feature_id_count": self.missing_feature_id_count,
            "duplicate_id_check_complete": self.duplicate_id_check_complete,
            "configured_key_check_complete": self.configured_key_check_complete,
            "issue_categories": list(self.issue_categories),
        }


def parse_source_page(
    payload: bytes | str, *, api_key: str | None = None
) -> ParsedSourcePage:
    """Strictly parse raw JSON while rejecting duplicate and credential keys."""

    if api_key is not None and (
        not isinstance(api_key, str) or not api_key.strip()
    ):
        raise SourceParsingError("invalid_api_key")

    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise SourceParsingError("invalid_utf8") from None
    elif isinstance(payload, str):
        try:
            payload.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise SourceParsingError("invalid_utf8") from None
        text = payload
    else:
        raise SourceParsingError("invalid_payload_type")

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateKey
            if _credential_name(key):
                raise _CredentialRejected
            result[key] = value
        return result

    try:
        document = json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=lambda _value: (_ for _ in ()).throw(_InvalidConstant()),
        )
    except _DuplicateKey:
        raise SourceParsingError("duplicate_json_key") from None
    except _CredentialRejected:
        raise SourceParsingError("credential_rejected") from None
    except (json.JSONDecodeError, _InvalidConstant, RecursionError):
        raise SourceParsingError("invalid_json") from None

    try:
        _verify_tree(document, api_key)
    except _CredentialRejected:
        raise SourceParsingError("credential_rejected") from None

    try:
        document_bytes = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise SourceParsingError("invalid_json") from None

    complete = api_key is not None
    issues = () if complete else ("configured_key_unverified",)
    return ParsedSourcePage._create(document_bytes, complete, issues)


def validate_source_page(
    page: ParsedSourcePage,
    *,
    page_index: int,
    requested_offset: int,
    requested_limit: int,
) -> PageValidationResult:
    """Validate one parsed page without interpreting its domain content."""

    if not _valid_parsed_page(page):
        raise SourceStructuralError("invalid_parsed_page")
    if not _nonnegative_integer(page_index):
        raise SourceStructuralError("invalid_page_index")
    if not _nonnegative_integer(requested_offset):
        raise SourceStructuralError("invalid_requested_offset")
    if not _positive_integer(requested_limit) or requested_limit > 10_000:
        raise SourceStructuralError("invalid_requested_limit")

    try:
        document = json.loads(page._document_bytes)
    except (TypeError, ValueError, UnicodeDecodeError, RecursionError):
        raise SourceStructuralError("invalid_parsed_page") from None
    if not isinstance(document, dict):
        raise SourceStructuralError("source_page_not_object")
    features = document.get("features")
    if not isinstance(features, list):
        raise SourceStructuralError("features_not_list")

    number_returned = document.get("numberReturned")
    if not _nonnegative_integer(number_returned):
        raise SourceStructuralError("invalid_number_returned")
    if number_returned != len(features):
        raise SourceStructuralError("number_returned_mismatch")
    if number_returned > requested_limit:
        raise SourceStructuralError("number_returned_exceeds_limit")

    matched_value = document.get("numberMatched", _MISSING)
    matched_present = matched_value is not _MISSING
    if matched_present and not _nonnegative_integer(matched_value):
        raise SourceStructuralError("invalid_number_matched")
    number_matched = matched_value if matched_present else None

    usable_ids: set[str] = set()
    missing_ids = 0
    for feature in features:
        if not isinstance(feature, dict):
            raise SourceStructuralError("feature_not_object")
        feature_id = feature.get("id", _MISSING)
        if not isinstance(feature_id, str) or feature_id == "":
            missing_ids += 1
            continue
        if feature_id in usable_ids:
            raise SourceStructuralError("duplicate_feature_id")
        usable_ids.add(feature_id)

    try:
        canonical = json.dumps(
            features,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise SourceStructuralError("invalid_json_value") from None
    fingerprint = hashlib.sha256(canonical).hexdigest()

    return PageValidationResult._create(
        page_index=page_index,
        requested_offset=requested_offset,
        requested_limit=requested_limit,
        number_returned=number_returned,
        number_matched_present=matched_present,
        number_matched=number_matched,
        missing_feature_id_count=missing_ids,
        feature_id_check_complete=missing_ids == 0,
        configured_key_check_complete=page.configured_key_check_complete,
        issue_categories=page.issue_categories,
        _usable_feature_ids=frozenset(usable_ids),
        _page_fingerprint=fingerprint,
    )


def validate_page_sequence(
    pages: Iterable[PageValidationResult],
) -> RunValidationResult:
    """Validate page progression and in-memory cross-page observations."""

    try:
        sequence = tuple(pages)
    except Exception:
        raise SourceStructuralError("invalid_page_sequence") from None
    if not sequence:
        raise SourceStructuralError("empty_page_sequence")
    seen_ids: set[str] = set()
    seen_fingerprints: set[str] = set()
    matched_observation: tuple[bool, int | None] | None = None
    total_features = 0
    missing_ids = 0
    key_check_complete = True
    issues: set[str] = set()
    expected_offset = 0
    terminal_page_seen = False

    for expected_index, page in enumerate(sequence):
        if not _valid_page_result(page):
            raise SourceStructuralError("invalid_page_result")
        if terminal_page_seen:
            raise SourceStructuralError("page_after_terminal")
        if page.page_index != expected_index:
            raise SourceStructuralError("page_index_sequence")
        if page.requested_offset != expected_offset:
            raise SourceStructuralError("offset_sequence")
        expected_offset = page.requested_offset + page.requested_limit

        observation = (page.number_matched_present, page.number_matched)
        if matched_observation is None:
            matched_observation = observation
        elif observation != matched_observation:
            raise SourceStructuralError("number_matched_changed")

        if page._page_fingerprint in seen_fingerprints:
            raise SourceStructuralError("repeated_page_content")
        seen_fingerprints.add(page._page_fingerprint)
        if seen_ids.intersection(page._usable_feature_ids):
            raise SourceStructuralError("duplicate_feature_id")
        seen_ids.update(page._usable_feature_ids)

        total_features += page.number_returned
        missing_ids += page.missing_feature_id_count
        key_check_complete = (
            key_check_complete and page.configured_key_check_complete
        )
        issues.update(page.issue_categories)
        terminal_page_seen = page.number_returned < page.requested_limit

    if not key_check_complete:
        issues.add("configured_key_unverified")
    matched_present = matched_observation[0] if matched_observation else None
    number_matched = matched_observation[1] if matched_observation else None
    return RunValidationResult._create(
        page_count=len(sequence),
        total_features=total_features,
        observed_number_matched_present=matched_present,
        observed_number_matched=number_matched,
        missing_feature_id_count=missing_ids,
        duplicate_id_check_complete=missing_ids == 0,
        configured_key_check_complete=key_check_complete,
        issue_categories=tuple(sorted(issues)),
    )


def _nonnegative_integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _positive_integer(value: object) -> bool:
    return type(value) is int and value > 0


def _seal(*values: Any) -> bytes:
    digest = hmac.new(_VALIDATION_SECRET, digestmod=hashlib.sha256)
    for value in values:
        if isinstance(value, bytes):
            encoded = value
        else:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.digest()


def _page_proof(**values: Any) -> bytes:
    return _seal(
        values["page_index"],
        values["requested_offset"],
        values["requested_limit"],
        values["number_returned"],
        values["number_matched_present"],
        values["number_matched"],
        values["missing_feature_id_count"],
        values["feature_id_check_complete"],
        values["configured_key_check_complete"],
        values["issue_categories"],
        sorted(values["_usable_feature_ids"]),
        values["_page_fingerprint"],
    )


def _run_proof(**values: Any) -> bytes:
    return _seal(
        values["page_count"],
        values["total_features"],
        values["observed_number_matched_present"],
        values["observed_number_matched"],
        values["missing_feature_id_count"],
        values["duplicate_id_check_complete"],
        values["configured_key_check_complete"],
        values["issue_categories"],
    )


def _valid_parsed_page(value: object) -> bool:
    if not isinstance(value, ParsedSourcePage):
        return False
    try:
        complete = value.configured_key_check_complete
        issues = value.issue_categories
        expected_issues = () if complete is True else ("configured_key_unverified",)
        return (
            value._token is _VALIDATION_TOKEN
            and isinstance(value._document_bytes, bytes)
            and type(complete) is bool
            and isinstance(issues, tuple)
            and issues == expected_issues
            and isinstance(value._proof, bytes)
            and hmac.compare_digest(
                value._proof,
                _seal(value._document_bytes, complete, issues),
            )
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _valid_page_result(value: object) -> bool:
    if not isinstance(value, PageValidationResult):
        return False
    try:
        expected_issues = (
            ()
            if value.configured_key_check_complete is True
            else ("configured_key_unverified",)
        )
        valid_fields = (
            value._token is _VALIDATION_TOKEN
            and _nonnegative_integer(value.page_index)
            and _nonnegative_integer(value.requested_offset)
            and _positive_integer(value.requested_limit)
            and value.requested_limit <= 10_000
            and _nonnegative_integer(value.number_returned)
            and value.number_returned <= value.requested_limit
            and type(value.number_matched_present) is bool
            and (
                (_nonnegative_integer(value.number_matched)
                 if value.number_matched_present else value.number_matched is None)
            )
            and _nonnegative_integer(value.missing_feature_id_count)
            and value.missing_feature_id_count <= value.number_returned
            and type(value.feature_id_check_complete) is bool
            and value.feature_id_check_complete
            == (value.missing_feature_id_count == 0)
            and type(value.configured_key_check_complete) is bool
            and isinstance(value.issue_categories, tuple)
            and value.issue_categories == expected_issues
            and isinstance(value._usable_feature_ids, frozenset)
            and all(
                isinstance(item, str) and item != ""
                for item in value._usable_feature_ids
            )
            and len(value._usable_feature_ids)
            == value.number_returned - value.missing_feature_id_count
            and isinstance(value._page_fingerprint, str)
            and len(value._page_fingerprint) == 64
            and all(character in "0123456789abcdef"
                    for character in value._page_fingerprint)
            and isinstance(value._proof, bytes)
        )
        if not valid_fields:
            return False
        values = {
            name: getattr(value, name)
            for name in (
                "page_index", "requested_offset", "requested_limit",
                "number_returned", "number_matched_present", "number_matched",
                "missing_feature_id_count", "feature_id_check_complete",
                "configured_key_check_complete", "issue_categories",
                "_usable_feature_ids", "_page_fingerprint",
            )
        }
        return hmac.compare_digest(value._proof, _page_proof(**values))
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _valid_run_result(value: object) -> bool:
    if not isinstance(value, RunValidationResult):
        return False
    try:
        expected_issues = (
            ()
            if value.configured_key_check_complete is True
            else ("configured_key_unverified",)
        )
        valid_fields = (
            value._token is _VALIDATION_TOKEN
            and _positive_integer(value.page_count)
            and _nonnegative_integer(value.total_features)
            and type(value.observed_number_matched_present) is bool
            and (
                (_nonnegative_integer(value.observed_number_matched)
                 if value.observed_number_matched_present
                 else value.observed_number_matched is None)
            )
            and _nonnegative_integer(value.missing_feature_id_count)
            and value.missing_feature_id_count <= value.total_features
            and type(value.duplicate_id_check_complete) is bool
            and value.duplicate_id_check_complete
            == (value.missing_feature_id_count == 0)
            and type(value.configured_key_check_complete) is bool
            and isinstance(value.issue_categories, tuple)
            and value.issue_categories == expected_issues
            and isinstance(value._proof, bytes)
        )
        if not valid_fields:
            return False
        values = {
            name: getattr(value, name)
            for name in (
                "page_count", "total_features",
                "observed_number_matched_present", "observed_number_matched",
                "missing_feature_id_count", "duplicate_id_check_complete",
                "configured_key_check_complete", "issue_categories",
            )
        }
        return hmac.compare_digest(value._proof, _run_proof(**values))
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _representations(value: str) -> tuple[str, ...]:
    pending = [value]
    seen: set[str] = set()
    ordered: list[str] = []
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        ordered.append(current)
        if len(ordered) > _MAX_DECODED_REPRESENTATIONS:
            raise _CredentialRejected
        for decoded in (unquote(current), unquote_plus(current)):
            if decoded not in seen:
                pending.append(decoded)
    return tuple(ordered)


def _credential_name(value: str) -> bool:
    return any(
        representation.casefold() in _CREDENTIAL_NAMES
        for representation in _representations(value)
    )


def _verify_tree(value: Any, api_key: str | None) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _credential_name(key):
                raise _CredentialRejected
            _verify_tree(child, api_key)
    elif isinstance(value, list):
        for child in value:
            _verify_tree(child, api_key)
    elif isinstance(value, str):
        _verify_text(value, api_key)


def _verify_text(value: str, api_key: str | None) -> None:
    for representation in _representations(value):
        if api_key is not None and api_key in representation:
            raise _CredentialRejected
        try:
            query = urlsplit(representation).query
        except ValueError:
            query = (
                representation.split("?", 1)[1].split("#", 1)[0]
                if "?" in representation
                else ""
            )
        for component in query.split("&") if query else ():
            name = component.split("=", 1)[0]
            if _credential_name(name):
                raise _CredentialRejected
