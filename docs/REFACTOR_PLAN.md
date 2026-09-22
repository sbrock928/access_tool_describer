# Cleanup, Refactor, and Analysis Roadmap

## Objectives

1. Keep extraction, interpretation, and presentation independently rerunnable.
2. Make every conclusion traceable to evidence and qualified by pipeline coverage.
3. Prefer conservative classifications over plausible but unsupported guesses.
4. Keep normalized machine-readable outputs aligned with the human reports.

## Completed in this refactor

- Replaced query-wide SQL labels with per-object read/write/create/execute operations.
- Added handling for Access `PARAMETERS`, make-table queries, bracketed identifiers, comments,
  and string literals.
- Resolved query references through linked-table metadata to the external platform, server,
  database, schema, and source table where available.
- Added linked-table source names to Windows extraction and versioned both extraction and static
  analysis so stale checkpoints are not silently reused.
- Narrowed VBA rules to code-bearing objects, removed common comment/name false positives, added
  HTTP, dynamic SQL, and suppressed-error findings, and calibrated rule confidence.
- Expanded connection classification and redaction, including user identifiers.
- Added deterministic de-duplication while retaining all distinct supporting evidence.
- Added per-application staging/extraction/analysis coverage and warning counts.
- Added portfolio summary, coverage, recommendation, and richer datasource/capability reporting.
- Added normalized CSVs for coverage, evidence, datasources, dependencies, capabilities, and
  recommendations, including headers for empty datasets.
- Protected Excel and CSV output from formula interpretation of evidence or inventory text.
- Replaced inventory-ID workspace folders and report labels with validated, human-readable EUC
  names while retaining inventory IDs only as internal provenance keys.

## Next priorities

### Near term

- Add extraction fixture packs representing Access versions, DAO/ADO styles, crosstab queries,
  pass-through queries, and representative `SaveAsText` form/report output.
- Capture query properties such as pass-through connection strings, returns-records behavior,
  parameters, and query type directly from DAO rather than inferring all of them from SQL text.
- Add source line/procedure attribution for embedded form and report VBA.
- Separate extraction failures from partial-container warnings at container/object granularity.

### Medium term

- Persist normalized evidence, dependencies, and analysis runs in SQLite instead of using JSON
  checkpoints as the reporting source of truth.
- Introduce stable finding/rule identifiers and rule-version metadata for report diffs.
- Add portfolio-level dependency graphs, cyclic dependency detection, centrality, and orphaned
  linked-table/reference checks.
- Add comparison reports between analysis runs so rule changes and application changes are
  distinguishable.

### Later, with validation data

- Calibrate precision and recall against a manually reviewed gold set.
- Add business-capability classification only when owner-approved labels and sufficient evidence
  exist; keep those interpretations separate from deterministic technical findings.
- Add risk scoring only after weights, severity definitions, and acceptance thresholds are agreed
  with security and application owners.

## Acceptance criteria for future rules

Every new rule must include positive, negative, and ambiguous fixtures; expose its confidence and
evidence location; avoid executing application code; and leave an explicit manual-review state
when the extractor cannot establish the fact.
