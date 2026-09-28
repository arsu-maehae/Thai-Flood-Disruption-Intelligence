"""Static assets for the local Pattani exposure dashboard."""

from pathlib import Path

STATIC_DIRECTORY = Path(__file__).resolve().parent / "static"

__all__ = ["STATIC_DIRECTORY"]
