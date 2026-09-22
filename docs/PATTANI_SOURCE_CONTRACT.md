# Pattani Source Contract Inventory

Every substantive statement below is labeled with one of the repository's evidence classifications. **Officially documented** describes the GISTDA documentation contract. **Enforced project behavior** describes repository rules or mandatory project boundaries. **Previously observed** describes controlled operational evidence. **Not yet verified** identifies facts that must not be assumed.

## Scope and official request contract

- **Enforced project behavior** — The current source contract is limited to the GISTDA Historical Flood Recurrence ingestion pipeline for Pattani.
- **Officially documented** — The API base URL is `https://api-gateway.gistda.or.th/api/2.0/resources`, and the endpoint is `GET /features/flood-freq`.
- **Officially documented** — Authentication uses an API key in the `API-Key` request header; the value format or prefix is not documented.
- **Officially documented** — Supported optional query parameters are `bbox`, `limit`, `offset`, `pv_idn`, `ap_idn`, and `tb_idn`.
- **Officially documented** — `bbox` is declared as a string; `limit` is an integer from 1 through 10,000 with documented default 1; `offset` is an integer described as starting at 0 with documented default 0; administrative identifiers are strings.
- **Previously observed** — The project uses `pv_idn=94` for Pattani.
- **Not yet verified** — GISTDA documentation inspected by this project does not officially map `pv_idn=94` to Pattani or document the complete set of allowed province identifiers.

## Source-page structure and counts

- **Enforced project behavior** — A successful source page must parse as a JSON object containing a `features` list.
- **Enforced project behavior** — Every item in `features` must be a JSON object.
- **Enforced project behavior** — `numberReturned` must be a non-boolean, non-negative integer equal to the length of `features` and no greater than the requested limit.
- **Enforced project behavior** — When `numberMatched` is present, it must be a non-boolean, non-negative integer; presence and value must remain consistent across accepted pages. Consistent absence is allowed.
- **Enforced project behavior** — Only a non-empty string top-level feature ID is usable for duplicate comparison. Other ID forms make duplicate-by-ID verification incomplete and are not converted into stable keys.
- **Previously observed** — Completed ingestion responses used a feature-collection-shaped JSON structure and supplied consistent count observations.
- **Not yet verified** — Successful response structure, count-field semantics, and feature-property meanings are not an official GISTDA response contract.

## Credential-sanitized source artifacts

- **Enforced project behavior** — Original provider response bytes remain in memory and are never persisted.
- **Enforced project behavior** — Original-response SHA-256 and byte count are computed before sanitization and recorded as provenance.
- **Enforced project behavior** — Supported literal credential fields and credential query parameters are removed, and remaining credential names or configured-key values in literal or repeatedly decoded forms cause safe rejection.
- **Enforced project behavior** — Sanitized JSON is deterministically serialized as a `.sanitized.json` source artifact with its own SHA-256 and byte count.
- **Enforced project behavior** — Original-response and stored-artifact hashes and byte counts remain distinct; the original-response hash cannot be independently recomputed because the original body is intentionally absent.
- **Previously observed** — Provider response link values echoed a credential query parameter, which established the need for the sanitization boundary.
- **Not yet verified** — Credential echoing is observed behavior, not an official response contract.

## Immutable publication

- **Enforced project behavior** — Artifact and metadata temporary files are created in the destination directory, flushed, synchronized, and published individually with no-replace semantics.
- **Enforced project behavior** — Existing matching artifacts may be reused idempotently; mismatched collisions are rejected and never overwritten.
- **Enforced project behavior** — Metadata publication failure triggers best-effort rollback only of the sanitized source-artifact destination created by that operation, and temporary files are cleaned when possible.
- **Enforced project behavior** — Artifact and metadata publication are individually atomic but are not claimed to be transactionally atomic as a pair.
- **Enforced project behavior** — Source publication failure does not justify deleting or repairing uncertain remnants automatically.

## Pagination policy

- **Enforced project behavior** — Pagination starts at offset 0 and advances by `requested_limit`; response links are never followed as trusted instructions.
- **Enforced project behavior** — An empty page or a partial page is a completion condition under the current implementation policy. A full final permitted page causes `max_pages_exhausted`, not completion.
- **Enforced project behavior** — `numberMatched` is observation and reconciliation data only; it is not the sole stopping rule.
- **Enforced project behavior** — Repeated page content, duplicate usable string IDs within or across pages, inconsistent counts, and changing `numberMatched` presence or value cause failure.
- **Enforced project behavior** — Missing or unusable IDs are counted and make duplicate-ID verification incomplete without inventing an official key.
- **Previously observed** — Controlled probes and the completed run were consistent with offset-by-limit progression and partial-page termination.
- **Not yet verified** — Stable ordering, snapshot consistency, count semantics, and general empty/partial-page termination are not official GISTDA guarantees.

## Run journal contract

- **Enforced project behavior** — New run journals use schema version `1.2` and an exclusively created, filesystem-safe run ID directory.
- **Enforced project behavior** — A run contains one immutable start record, contiguous immutable page records, and at most one immutable terminal record: complete or failed.
- **Enforced project behavior** — Absence of a terminal record means the run is incomplete or interrupted; it is not reclassified as complete or failed by inference.
- **Enforced project behavior** — Coordinated counts distinguish attempted requests, persisted pages, validated pages, and journaled pages and satisfy `journaled <= validated <= persisted <= attempted`.
- **Enforced project behavior** — A safe unjournaled-page reference is required when exactly one persisted page lacks a page journal record; its stage is `persisted_unvalidated` or `validated_unjournaled` as supported by the counts.
- **Enforced project behavior** — No unjournaled reference is claimed when source publication fails before returning a successful ingestion result.
- **Enforced project behavior** — Published terminal state is final, including when temporary cleanup fails after publication.
- **Enforced project behavior** — The journal supports exactly one writer per run. Concurrency and resume are unsupported, and a run ID must not be reused.

## Offline run verification

- **Enforced project behavior** — `verify_run(output_root, run_id, *, api_key=None)` is read-only and requires an inactive writer.
- **Enforced project behavior** — Verification rejects duplicate JSON keys and validates journal versions, record names, page order, offsets, counts, timestamps, paths, artifact and metadata hashes, byte counts, lineage, terminal reconciliation, credential names, remnants, and unexpected entries.
- **Enforced project behavior** — Credential-name checks operate without configuration. Configured-key absence is verified only when the key is supplied explicitly and held in memory.
- **Enforced project behavior** — Without an explicitly supplied key, verification reports `configured_key_unverified` and sets configured-key coverage incomplete; it never loads `.env` or configuration implicitly.
- **Enforced project behavior** — Verification never repairs, deletes, rewrites, resumes, or reclassifies the inspected run.
- **Enforced project behavior** — Original-response hashes remain declared provenance and cannot be independently recomputed from persisted data.

## Lineage chain

- **Enforced project behavior** — The complete lineage chain is: run record → page record → sanitized metadata sidecar → sanitized source artifact → stored-artifact SHA-256.
- **Enforced project behavior** — Page records retain page index, requested offset and limit, relative artifact and metadata paths, stored SHA-256, count observations, and feature-ID completeness.
- **Enforced project behavior** — Paths must resolve beneath the configured output root, and referenced artifact size and digest must agree with metadata and journal records.
- **Enforced project behavior** — Lineage reports contain safe relative references and hashes, not credentials, headers, response bodies, feature IDs, properties, coordinates, or full response links.

## Failure and interruption semantics

- **Enforced project behavior** — Request, sanitization, structural, pagination, publication, journal, and verification failures use fixed credential-safe categories rather than arbitrary exception text.
- **Enforced project behavior** — The workflow stops on the first failure and performs no automatic retry, response-link following, overwrite, repair, resume, or deletion of successful source artifacts.
- **Enforced project behavior** — A published failed terminal remains failed; a published complete terminal remains complete even if later cleanup reports a fault.
- **Enforced project behavior** — Process interruption is propagated without inventing a terminal outcome. Existing immutable pages and records remain available for offline verification.
- **Enforced project behavior** — Safe unjournaled lineage may identify a returned persisted page when terminal publication fails, but uncertain pre-return publication remnants are not claimed.

## Phase 1 operational evidence

- **Previously observed** — Phase 1 completed one policy-based Pattani ingestion and its offline source-run verification, followed by encrypted-backup restore and integrity verification.
- **Previously observed** — The approved operational summary is recorded in `docs/PHASE1_CLOSEOUT.md`; this inventory references that evidence without reproducing source contents or sensitive details.
- **Not yet verified** — Phase 1 completion does not prove officially exhaustive or snapshot-consistent provider coverage.

## Unknowns

- **Not yet verified** — Coordinate reference system, EPSG/SRID, coordinate order, and coordinate units.
- **Not yet verified** — Feature-property business meanings and units.
- **Not yet verified** — Any relationship between `freq` and yearly fields.
- **Not yet verified** — Stable ordering and provider snapshot consistency.
- **Not yet verified** — Official total-count semantics and general pagination termination behavior.
- **Not yet verified** — Geometry semantics sufficient for PostGIS construction or spatial analysis.

## Phase 2 boundary

- **Enforced project behavior** — Phase 2 must not infer a CRS, EPSG/SRID, coordinate order, units, field meaning, or relationship among fields.
- **Enforced project behavior** — PostGIS geometry construction and SRID assignment remain blocked pending official CRS evidence.
- **Enforced project behavior** — Future duplicate-aware structural validation must accept raw UTF-8 bytes or text and perform duplicate-aware JSON parsing before producing a Python object; duplicate-key detection must not be claimed for an already parsed object.
- **Enforced project behavior** — Credential-name checks may run without configuration, but configured-key value coverage must remain incomplete unless a key is supplied explicitly in memory.
- **Enforced project behavior** — Real-data profiling, official web research, transformation, database setup, pilot import, and full import each require separate authorization.
