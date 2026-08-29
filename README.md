# Thai Flood Disruption Intelligence - Pattani

Phase one focuses exclusively on ingesting GISTDA historical flood data for Pattani, Thailand.

## Scope

This phase will provide the foundation for a reliable flood-data pipeline:

- Download GISTDA historical flood responses using only verified request behavior.
- Preserve source responses immutably in `data/raw/`; existing raw artifacts must never be overwritten.
- Validate officially documented behavior separately from structure observed in prior API tests.
- Derive processed data reproducibly from identified raw artifacts in `data/processed/`.
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

API information is maintained in three explicit categories:

- **Officially documented:** behavior supported by GISTDA official documentation.
- **Previously observed:** behavior seen in earlier API tests but not necessarily specified by official documentation.
- **Not yet verified:** behavior that must not be assumed or implemented as an API contract.

The pipeline will use only officially documented behavior or explicitly scoped observed behavior that has been reviewed and approved. It will not infer response-field meanings, geometry semantics, coordinate reference systems, pagination behavior, or a final processed-data schema.

Live API requests require explicit user approval. Any API key previously exposed must be treated as compromised and replaced with a rotated key before a live request is made.

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
