# Relation Extraction Playbook

This note records the first-pass heuristics for finding conversion relations in
the Link 16 J-family PDF corpus.

## Primary relation signals

1. Response rewrite patterns:
   - `identical to the one received except for`
   - `fields being interchanged`
   - `R/C field being set to value`
2. Explicit value propagation:
   - `shall be set to the value`
   - `same as`
   - `copy of`
3. Remote/local derivation:
   - `derived from received data`
   - `received from remote sources`
4. Correlation-specific sections:
   - `correlation`
   - `REFERENCE TN/IN CORRELATION`
   - `TARGET/TRACK CORRELATION`

## Practical extraction flow

1. Use the VLM as the final judge on contiguous PDF page batches.
2. Feed several neighboring pages at once so the model can see message summary,
   word map, field coding, and local narrative context together.
3. Record structured JSON for visible message candidates, XML-relevant signals,
   and relation evidence.
4. If a batch clearly spills into the previous or next page, mark
   `needs_neighbor_pages` instead of guessing.
5. Use shard metadata and regex scans only as auxiliary hints, not as the final
   truth source.

## Why page-indexed shards help

- Each shard already preserves page numbers and block ids.
- Relation text often spans a small local window but has repeated phrasing.
- Page/block anchors make it easier to re-open the same evidence when testing a
  model extraction prompt or verifying a generated XML field mapping.
