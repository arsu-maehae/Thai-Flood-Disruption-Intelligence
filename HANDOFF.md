# Project Handoff

This document captures the project state reviewed on 2026-09-18. Recheck Git status and tests before continuing; this is a handoff snapshot, not a replacement for the project specification or API evidence.

## Goal and current scope

Thai Flood Disruption Intelligence aims to analyze flood exposure and infrastructure disruption in Thailand. Current Phase 1 is limited to a reliable GISTDA Historical Flood Recurrence ingestion pipeline for Pattani.

PostGIS, infrastructure integration, geospatial transformation, disruption scoring, machine learning, APIs, dashboards, and production orchestration are future work.

## Read first

- `README.md`: operational scope and security requirements.
- `PROJECT_SPEC.md`: broader architecture and long-term roadmap.
- `DATA_SOURCES.md`: official API evidence, prior observations, and unknowns.
- `src/configuration.py`, `src/ingestion/`, and `tests/`: current implementation.

## Implemented

- Explicit `load_config()` using python-dotenv; no import-time configuration loading.
- `GistdaConfig` supports base URL, API key, and province ID.
- `GistdaClient` uses an injectable requests.Session, connect/read timeouts, HTTP-200-only success, credential-safe exceptions, and no automatic retries.
- `ingest_pattani_sample()` is the controlled 10-record wrapper; `ingest_pattani_page()` accepts explicit limit and offset.
- Source ingestion sanitizes credentials before persistence, uses immutable no-overwrite publication, and records separate original-response and stored-artifact hashes and byte counts.
- Conservative pagination detects repeated pages, duplicate usable string IDs, inconsistent counts, and changing numberMatched presence/value. It reports incomplete ID checks and enforces a maximum-page cap.
- Pagination advances by requested limit and stops on empty/partial pages as implementation policy, not an official guarantee. It never follows response links.

## API evidence

Repository documentation records the following official contract facts, verified on 2026-08-28 UTC:

- Base URL: `https://api-gateway.gistda.or.th/api/2.0/resources`
- Endpoint: `GET /features/flood-freq`
- Authentication header: `API-Key`
- Parameters: `bbox`, `limit`, `offset`, `pv_idn`, `ap_idn`, `tb_idn`

`pv_idn=94` is project configuration based on previously observed behavior, not an officially documented Pattani mapping.

The 2026-08-29 UTC live pagination probe requested offsets 0, 10, and 112070 with limit 10. Returned counts were 10, 10, and 3; numberMatched was consistently 112073. IDs were non-empty strings, unique within each page, and non-overlapping between the first two pages. The tail was partial and had no next relation. These observations do not establish stable ordering, snapshot consistency, count semantics, or general termination behavior.

CRS, feature-property meanings, and the relationship between freq and yearly fields remain unverified. Do not infer them.

## Security and provenance

Response links previously echoed a credential in an api_key query parameter. The unsafe initial artifact was deleted and the credential rotated again.

- Original response bodies remain in memory only and must never be persisted.
- Store deterministic `.sanitized.json` source artifacts and sanitized metadata.
- Record original-response and stored-artifact SHA-256 and byte counts separately.
- Files in data/raw for this source are sanitized source-layer artifacts, not untouched provider responses.
- Stored artifacts and metadata must remain immutable with explicit provenance and lineage.
- Never expose API keys, headers, full response links, feature IDs, properties, coordinates, or response bodies in reports.
- Do not inspect or print `.env`. Configuration loading is permitted only when explicitly authorized by the task.
- Live API requests and full ingestion require explicit authorization.

## Completed milestone: 8A

Milestone 8A is approved, committed, and pushed, as reported by the project owner. Commit: `e57c10d Add hardened Pattani run journal`.

The immutable run journal uses atomic no-replace publication and requires one writer per run. Resume is not supported.

Public interfaces: `RunJournal.start()`, `append_page()`, `complete()`, `fail()`, and `PageJournalRecord`.

Storage layout: `<output_root>/gistda/flood_freq/pattani/runs/<run_id>/`, containing run_started.json, contiguous page records, and one complete/failed terminal record. Absence of a terminal record indicates an incomplete/interrupted run. Resume is not implemented.

## Completed milestone: 8B

Milestone 8B is approved, committed, and pushed, as reported by the project owner. Latest commit: `340c5dc Integrate Pattani pagination run journaling`. The commit was independently verified on GitHub as the current `main` commit on 2026-09-18.

- `paginate_pattani()` retains legacy behavior and its summary shape unless `run_id` is supplied; journaled runs support an injected lifecycle clock.
- Journal schema version is `1.2`; existing immutable records are not rewritten.
- Counts distinguish attempted requests, persisted pages, validated pages, and journaled pages. Standalone journals retain unknown request, persisted, and validated counts.
- Failed runs preserve safe unjournaled-page lineage where applicable, distinguishing `persisted_unvalidated` from `validated_unjournaled`. Safe error outcomes retain the reference if failure-terminal publication does not succeed.
- Published terminal states remain final after cleanup faults. A published completion remains complete; the instance is blocked rather than writing a failure terminal.
- Journaled pagination requires `max_pages` not to exceed `100,000`, an internal safety cap rather than a GISTDA contract.
- Public additions include `RunCounts`, `PaginationRunError`, and `UnjournaledPageReference`.

## Locally reported verification

Latest locally recorded result: **396 tests passed**; `compileall` passed; `git diff --check` passed.

These are locally reported verification results, not external proof or evidence of official API behavior.

## Current limitations

- One writer per run; no concurrency.
- Resume is not implemented.
- Ordering and empty/partial termination remain implementation policy, not official GISTDA guarantees. Snapshot consistency and general count semantics remain unverified.
- Source-publication failure before a successful ingestion return may leave uncertain remnants; no safe page reference is claimed for those remnants.
- Full Pattani ingestion has not been performed.

## Recommended next step

Review full-ingestion readiness and the operating procedure. Continue with synthetic/offline checks first. Do not start full ingestion without separate explicit authorization.

## Collaboration rules

- Inspect current Git status and preserve existing local changes.
- Change only explicitly authorized files.
- Do not stage or commit without a request.
- Do not make network requests without explicit authorization.
- Keep milestone reports concise: files changed, checks, Git status, blockers, and next action.
