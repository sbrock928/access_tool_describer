# Reports

Excel is the primary detailed report. It includes portfolio and per-application coverage,
applications, artifacts, datasources, dependencies, capabilities, recommendations, evidence, and
staging errors. Coverage distinguishes a completed analysis with zero findings from an application
that was never analyzed or whose extraction was incomplete.

The PDF is an executive and architecture overview. Its rankings aggregate applications and
evidence rather than depending on input order. Extraction warnings qualify the conclusions and
populate the manual-review queue.

CSV exports provide normalized datasets for applications, artifacts, analysis coverage, evidence,
datasources, dependencies, capabilities, and recommendations. Files retain their headers even when
there are no records. Spreadsheet-bound text is escaped when it could otherwise be interpreted as
a formula.

Stated inventory descriptions remain claims and are never blended into observed findings.
All application labels and normalized CSV keys use `EUCTNAME`; numeric inventory IDs remain internal
and are omitted from report outputs.
Reports ignore stale analysis checkpoints from older rule versions or different staged hashes.
