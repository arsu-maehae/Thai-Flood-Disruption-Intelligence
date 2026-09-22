# Project Handoff

This document captures the project state reviewed on 2026-09-19. Recheck Git status and tests before continuing; this is a handoff snapshot, not a replacement for the project specification or API evidence.

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
- `verify_run(output_root, run_id, *, api_key=None)` performs read-only verification of inactive runs.
- The guarded operator provides network-free preflight and verification commands plus a separately authorized journaled run command for the repository `data/raw` root.

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

## Completed milestone: 8C-1

Milestone 8C-1 is complete. Commit: `2e743a1 Harden GISTDA credential verification`.

- Repeated URL/form decoding checks literal, encoded, and multiply encoded credentials.
- Credential values, object-key names, URL query-parameter names, and serialized candidates are checked before filesystem publication.
- Rejections use credential-safe errors and persist no artifact, metadata, temporary file, or destination created solely for a rejected response.

## Completed milestone: 8C-2

Milestone 8C-2 is complete. Commit: `ca7675f Add offline Pattani run verification`.

- Public interface: `verify_run(output_root, run_id, *, api_key=None)`.
- Verification is read-only and validates journals, referenced artifacts, metadata, hashes, counts, paths, provenance, terminal state, temporary remnants, unjournaled-page lineage, and credential absence.
- Verification requires an inactive writer and provides no concurrency protection.
- Without a supplied key, configured-key absence remains unverified.
- Original-response hashes and byte counts cannot be independently recomputed because original response bodies are intentionally not persisted.

## Completed milestone: 8C-3

Milestone 8C-3 is complete. Commit: `3f2e5ee Add guarded Pattani ingestion operator`.

CLI entrypoints:

- `python -m src.ingestion.operator preflight`
- `python -m src.ingestion.operator verify`
- `python -m src.ingestion.operator run`

The operating procedure is documented in `docs/INGESTION_OPERATIONS.md`.

- The Phase 1 operator supports only this repository's `data/raw` root.
- Preflight and verify are network-free; configuration loading is explicit.
- Run requires both `--authorize-live` and `--load-local-config`, but these runtime flags never replace separate user authorization.
- Run repeats complete preflight, uses journaled pagination, and then performs offline verification with published terminal status reported separately from verification status and coverage.
- `KeyboardInterrupt` exits 130 without inventing a terminal state.
- The operator implements no retries, response-link following, resume, or concurrency.
- The operator performs no automatic cleanup or repair of ingestion artifacts, journal records, or run directories.
- Preflight cleans only its exclusively owned disposable probe directory and refuses readiness if that cleanup fails.

## GitHub state

- Branch: `main`
- Implementation checkpoint before this handoff-only update: `3f2e5ee Add guarded Pattani ingestion operator`.
- Repository: `arsu-maehae/Thai-Flood-Disruption-Intelligence`
- At the start of this handoff update, local `main` and `origin/main` were synchronized at `3f2e5ee` on 2026-09-19.

## Completed operational readiness probe

Date: 2026-09-19 UTC

- Run ID: `pattani-readiness-20260919-01`
- Exactly one application-level request was made with `pv_idn=94`, `limit=10`, `max_pages=1`, and offset `0`.
- No retries occurred, and response links were not followed.
- Published terminal status: `failed`.
- Expected safety category: `max_pages_exhausted`.
- Counts:
  - attempted requests: 1
  - persisted pages: 1
  - validated pages: 1
  - journaled pages: 1
- Offline verification confirmed:
  - a valid failed terminal
  - configured-key checking was complete
  - final counts
  - no verification issues
  - valid hashes, byte counts, provenance, containment, and lineage
  - no credential, temporary remnants, or unexpected entries
- The response contained 10 features and observed `numberMatched=112073`. These remain observations, not API guarantees.
- Git remained clean.
- Full Pattani ingestion has not occurred.
- The run is immutable and must not be reused or resumed.

## Completed page-size probe

Date: 2026-09-19 UTC

- Run ID: `pattani-page-size-1000-20260919-01`
- Exactly one application-level request used `pv_idn=94`, `limit=1000`, `max_pages=1`, and offset `0`; no retry occurred, and response links were not followed.
- The response was HTTP `200` with content type `application/geo+json`.
- `numberReturned=1000` and `numberMatched=112073` were observed.
- The original response was `2836590` bytes, and the stored credential-sanitized artifact was `6544748` bytes.
- Lifecycle time to the terminal record was `1.783328` seconds.
- The implementation published a valid failed terminal with expected safety category `max_pages_exhausted` because the single permitted page was full.
- Counts were attempted `1`, persisted `1`, validated `1`, and journaled `1`.
- Offline verification reported no issues and confirmed credential safety, integrity, containment, provenance, lineage, and no temporary or unexpected remnants.
- The run is immutable and must not be reused or resumed. Full Pattani ingestion has not occurred.

These are observations from one response, not API guarantees. They do not establish stable ordering, snapshot consistency, complete coverage, duration, payload-size uniformity, or general count and termination semantics.

## Locally reported verification

Latest locally recorded result: **819 tests passed, 5 skipped**; `compileall` passed; whitespace and authorized-scope checks passed. The platform skips concern unavailable symlink behavior.

These are locally reported verification results, not external proof or evidence of official API behavior.

## Current limitations

- One writer per run; no concurrency.
- Resume is not implemented.
- Full Pattani ingestion has not occurred.
- Stable ordering, snapshot consistency, count semantics, general termination behavior, rate limits, duration, payload size, and resource requirements remain unverified.
- Ordering and empty/partial termination remain implementation policy, not official GISTDA guarantees.
- Source-publication failure before a successful ingestion return may leave uncertain remnants; no safe page reference is claimed for those remnants.
- `pv_idn=94` remains observed project configuration, not an officially documented Pattani mapping.
- Operator readiness does not guarantee exhaustive or snapshot-consistent data.

## Recommended next step

Review the bounded probe evidence before selecting full-ingestion parameters. Any further probe or full ingestion requires separate authorization specifying the exact request budget, `limit`, `max_pages`, `run_id`, minimum free-byte floor, and accepted uncertainties. Do not implicitly approve full ingestion, and do not calculate the request cap solely from observed `numberMatched`.

The completed probes above occurred after Milestone 8C under separate explicit authorizations. Full Pattani ingestion remains unperformed.

## Collaboration rules

- Inspect current Git status and preserve existing local changes.
- Change only explicitly authorized files.
- Do not stage or commit without a request.
- Do not make network requests without explicit authorization.
- Keep milestone reports concise: files changed, checks, Git status, blockers, and next action.
