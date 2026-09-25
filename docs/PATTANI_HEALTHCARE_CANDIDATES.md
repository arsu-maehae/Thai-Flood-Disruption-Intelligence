# Pattani Healthcare Address-Text Candidates

This project-defined offline transformation reads the immutable DGA healthcare
CSV acquired in Phase 3B. It verifies the source byte count (`4857492`) and
SHA-256 (`fede0061abf49d81047046f4fea2a26efad2b33c2237b1a07543c0230e31b60b`),
requires the reviewed eight-column CSV schema, and decodes strict UTF-8 with BOM
handling.

Candidate selection applies Unicode NFC normalization and a literal Pattani
substring check to the reviewed service-address column. The completed output
contains 138 candidates, 138 distinct non-empty source identifiers, 138 finite
non-empty coordinate pairs, and no duplicate complete candidate rows. These are
`address_text_candidates`; they are not verified Pattani facilities and do not
establish completeness, coordinate semantics, CRS, or positional accuracy.

The ignored deterministic JSONL preserves each selected source row only inside
the derived dataset. Its SHA-256 is
`52d87273475d9022a3656804428e0f1adfa556b8b542e1ed45694df1d7c7fb7a` and
its byte count is `104344`. The completion manifest SHA-256 is
`9e24291d607c401e5e00ce24e320ff5bdcc882ec9831978da62e477335026bb8`.
Publication is immutable and no-overwrite; the manifest is published last, so a
directory without it remains incomplete. Reports and errors exclude source
identifiers, names, addresses, coordinates, and row contents.
