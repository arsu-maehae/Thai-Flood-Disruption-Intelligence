# Thai Flood Disruption Intelligence - Pattani

Phase one focuses exclusively on ingesting GISTDA historical flood data for Pattani, Thailand.

## Scope

This phase will provide the foundation for a reliable flood-data pipeline:

- Download documented GISTDA historical flood responses.
- Preserve source responses in `data/raw/`.
- Validate the documented response structure.
- Transform validated records into `data/processed/`.
- Add focused tests for ingestion, validation, and transformation.

Machine learning, dashboards, databases, population, roads, hospitals, schools, and derived features are out of scope for this phase.

## Project Structure

```text
src/
  ingestion/       GISTDA access and raw-data persistence
  validation/      Input structure and quality checks
  transformation/  Conversion to the processed project format
  features/        Reserved for a later phase
data/
  raw/             Untouched source responses
  processed/       Validated and transformed flood data
  features/        Reserved for a later phase
tests/             Unit and pipeline tests
```

## API Contract

The GISTDA client will use only endpoint paths, authentication details, request fields, and response fields documented by GISTDA. No undocumented API parameters are assumed in this project.

Configuration values belong in a local `.env` file. Start from `.env.example` and never commit credentials.

## Development

Create an environment and install dependencies:

```text
python -m venv .venv
python -m pip install -r requirements.txt
```

Run tests with:

```text
pytest
```
