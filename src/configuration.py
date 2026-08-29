"""Explicit configuration loading for the GISTDA ingestion pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


OFFICIAL_GISTDA_API_BASE_URL = (
    "https://api-gateway.gistda.or.th/api/2.0/resources"
)

_REQUIRED_KEYS = (
    "GISTDA_API_BASE_URL",
    "GISTDA_API_KEY",
    "GISTDA_PROVINCE_ID",
)


class ConfigurationError(ValueError):
    """Raised when required configuration is missing or blank."""


@dataclass(frozen=True)
class GistdaConfig:
    """Validated settings for a future GISTDA client."""

    api_base_url: str
    api_key: str = field(repr=False)
    province_id: str


def load_config(
    *,
    environ: Mapping[str, str | None] | None = None,
    dotenv_path: str | Path | None = ".env",
) -> GistdaConfig:
    """Load and validate configuration without mutating the process environment.

    Supplying ``environ`` uses that mapping as the complete configuration source
    and does not read a dotenv file. Otherwise, values from ``dotenv_path`` are
    loaded first and are overridden by matching process-environment values.
    """

    if environ is None:
        values: dict[str, str | None] = {}
        if dotenv_path is not None:
            values.update(dotenv_values(dotenv_path))
        values.update({key: os.environ.get(key) for key in _REQUIRED_KEYS if key in os.environ})
    else:
        values = {key: environ.get(key) for key in _REQUIRED_KEYS}

    normalized = {
        key: value.strip() if isinstance(value, str) else ""
        for key, value in values.items()
        if key in _REQUIRED_KEYS
    }
    missing = [key for key in _REQUIRED_KEYS if not normalized.get(key)]
    if missing:
        raise ConfigurationError(
            "Missing or blank required configuration: " + ", ".join(missing)
        )

    if normalized["GISTDA_API_BASE_URL"] != OFFICIAL_GISTDA_API_BASE_URL:
        raise ConfigurationError("GISTDA_API_BASE_URL must match the official base URL")

    return GistdaConfig(
        api_base_url=normalized["GISTDA_API_BASE_URL"],
        api_key=normalized["GISTDA_API_KEY"],
        province_id=normalized["GISTDA_PROVINCE_ID"],
    )
