# Pattani Ingestion Operations

This runbook covers the current Phase 1 GISTDA Historical Flood Recurrence pipeline. The only supported destination is this repository's `data/raw` directory. The commands do not support an external output root.

## Authorization boundaries

Implementation approval, a live readiness probe, and full Pattani ingestion are separate decisions. Neither this document nor a successful preflight authorizes a live request. A live run requires separate, explicit user authorization plus both runtime flags `--authorize-live` and `--load-local-config`.

Preflight and verification are network-free and never construct the HTTP client. Configuration loading is opt-in because it reads local configuration. Never print `.env`, credentials, or request headers.

## Choose the parameters

- `run_id`: a unique, filesystem-safe identifier. Runs are immutable, single-writer, and cannot be resumed; never reuse an existing ID.
- `limit`: records requested per page, from 1 through 10,000. This is a client constraint, not evidence of an API default.
- `max_pages`: a positive safety cap no greater than 100,000. It bounds the run; reaching it is failure, not successful completion.
- `min_free_bytes`: an operator-selected positive storage floor. The project does not claim a required capacity or predict final dataset size.

Observed `numberMatched` is not a guaranteed total and must not be the sole stopping rule. Leave retrieval and lifecycle timestamps to the production UTC clocks.

## Preflight

Without configuration loading, preflight deliberately reports readiness as incomplete:

```text
python -m src.ingestion.operator preflight --run-id RUN_ID --limit LIMIT --max-pages MAX_PAGES --min-free-bytes BYTES
```

After separate permission to read local configuration, add `--load-local-config`. Preflight verifies Pattani configuration (`pv_idn=94` remains previously observed project configuration), the official base URL, Git cleanliness, ignore/tracking rules, available space, a unique run ID, and disposable write/fsync/hard-link support. Use `--acknowledge-dirty` only after explicitly reviewing and accepting the current Git state. A probe uses only its exclusively created directory; cleanup failure refuses readiness.

## Verify

Verification is read-only and must be run only when no writer is active:

```text
python -m src.ingestion.operator verify --run-id RUN_ID
```

Without `--load-local-config`, credential-name checks still run, but absence of the configured key is reported as incomplete. Loading configuration remains network-free. Verification does not repair, delete, resume, clean, or reclassify records.

Interpret results as follows:

- `complete`: one valid completion terminal exists and reconciles with published pages and artifacts.
- `failed`: one valid failure terminal exists; safe unjournaled-page lineage may identify a persisted page not published to the journal.
- `interrupted`: a valid start exists without a terminal. Published pages remain immutable; no in-flight request is inferred.
- `invalid`: records, artifacts, or filesystem state did not pass offline verification.
- `not_found`: the requested immutable run directory does not exist.

Temporary cleanup remnants are warnings and do not change a valid published terminal state. A terminal published before cleanup failure remains final.

## Run

Only after separate full-ingestion authorization:

```text
python -m src.ingestion.operator run --run-id RUN_ID --limit LIMIT --max-pages MAX_PAGES --min-free-bytes BYTES --authorize-live --load-local-config
```

The run repeats every preflight check immediately before client construction and journal creation. A prior preflight result never authorizes a later run. The operator uses no retries, concurrency, resume, or response-link following and stops on the first failure. It emits credential-safe JSON lines only; a dispatch event means an attempt is starting, not that anything was persisted.

After pagination returns—or after a safe pagination failure—the operator invokes the offline verifier once with the configured key. The final event reports the published terminal status separately from verification status, verification issues, configured-key coverage, and whether verified counts are final. A published complete terminal remains complete if verification warns or fails, but the command returns nonzero unless verification confirms a complete run with configured-key checking. Verification never writes a failure terminal or changes published counts.

On interruption, exit status is 130 and no terminal outcome is invented. On failure, preserve all immutable artifacts, terminal records, and safe unjournaled lineage. Do not delete or overwrite anything. Investigate with `verify`, then obtain separate authorization and start a new run with a new ID.

## Limitations

The API does not officially guarantee stable ordering, snapshot consistency, count semantics, or empty/partial-page termination. The pipeline's offset progression and stopping rules remain observed implementation policy. Verification cannot independently recompute original-response hashes because original bodies are intentionally memory-only. Source-publication failure may leave uncertain remnants. One writer per run is required; concurrency and resume are unsupported. Full ingestion remains separately authorized.
