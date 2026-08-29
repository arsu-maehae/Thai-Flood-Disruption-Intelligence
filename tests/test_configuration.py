import importlib

import pytest

from src import configuration
from src.configuration import ConfigurationError, GistdaConfig, load_config


DUMMY_KEY = "dummy-test-key-never-use"


def valid_values() -> dict[str, str]:
    return {
        "GISTDA_API_BASE_URL": configuration.OFFICIAL_GISTDA_API_BASE_URL,
        "GISTDA_API_KEY": DUMMY_KEY,
        # 94 is previously observed project behavior, not an official mapping.
        "GISTDA_PROVINCE_ID": "94",
    }


def test_valid_configuration() -> None:
    config = load_config(environ=valid_values())

    assert config == GistdaConfig(
        api_base_url=configuration.OFFICIAL_GISTDA_API_BASE_URL,
        api_key=DUMMY_KEY,
        province_id="94",
    )


def test_official_base_url_is_accepted() -> None:
    values = valid_values()
    values["GISTDA_API_BASE_URL"] = (
        f"  {configuration.OFFICIAL_GISTDA_API_BASE_URL}\t"
    )

    config = load_config(environ=values)

    assert config.api_base_url == configuration.OFFICIAL_GISTDA_API_BASE_URL


@pytest.mark.parametrize(
    "rejected_url",
    ["https://example.invalid/api", "not-a-url"],
)
def test_different_or_malformed_base_url_is_rejected_without_exposing_values(
    rejected_url: str,
) -> None:
    values = valid_values()
    values["GISTDA_API_BASE_URL"] = rejected_url

    with pytest.raises(ConfigurationError) as exc_info:
        load_config(environ=values)

    message = str(exc_info.value)
    assert rejected_url not in message
    assert DUMMY_KEY not in message


@pytest.mark.parametrize("missing_key", ["GISTDA_API_KEY", "GISTDA_API_BASE_URL", "GISTDA_PROVINCE_ID"])
def test_missing_required_value(missing_key: str) -> None:
    values = valid_values()
    del values[missing_key]

    with pytest.raises(ConfigurationError, match=missing_key):
        load_config(environ=values)


def test_blank_api_key() -> None:
    values = valid_values()
    values["GISTDA_API_KEY"] = "   "

    with pytest.raises(ConfigurationError, match="GISTDA_API_KEY"):
        load_config(environ=values)


def test_values_are_trimmed() -> None:
    values = {
        key: f"  {value}\t" for key, value in valid_values().items()
    }

    config = load_config(environ=values)

    assert config.api_base_url == configuration.OFFICIAL_GISTDA_API_BASE_URL
    assert config.api_key == DUMMY_KEY
    assert config.province_id == "94"


def test_api_key_is_not_exposed_by_repr_or_validation_error() -> None:
    config = load_config(environ=valid_values())
    assert DUMMY_KEY not in repr(config)

    values = valid_values()
    del values["GISTDA_PROVINCE_ID"]
    with pytest.raises(ConfigurationError) as exc_info:
        load_config(environ=values)
    assert DUMMY_KEY not in str(exc_info.value)


def test_import_does_not_require_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in configuration._REQUIRED_KEYS:
        monkeypatch.delenv(key, raising=False)

    reloaded = importlib.reload(configuration)

    assert hasattr(reloaded, "load_config")
