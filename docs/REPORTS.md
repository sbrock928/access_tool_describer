# Reports

Reporting is a pure projection of one immutable `PortfolioReportModel`. Renderers do not reopen
Access, parse source definitions, infer lineage, create portfolio membership, call Qwen, or join
older state. This keeps identifiers, counts, qualifications, and review status consistent across
HTML, Excel, PDF, and CSV.

## Publish a report

Normal publication requires current, completed extraction and analysis for every eligible Access
application plus a current portfolio interpretation:

```powershell
portfolio-analyzer report --workspace .\workspace
```

The command refuses missing, failed, stale, or in-progress analysis. Optional `SaveAsText` omissions
do not necessarily block reporting: completed analysis over partial evidence is allowed, but every
format qualifies the conclusion and suppresses an unsupported absence claim for that coverage area.

For a diagnostic report when required analysis is incomplete, opt in explicitly:

```powershell
portfolio-analyzer report --workspace .\workspace --allow-partial
```

This report:

- includes only completed applications;
- lists every omitted application and logical unit with its stage and reason;
- suppresses portfolio-wide absence claims;
- carries the V2 partial watermark in every format; and
- publishes with `.partial` filenames without replacing the latest complete pointer.

Partial reporting does not turn a failed interpretation into a deterministic substitute.

## Publication layout

Every report is an immutable run:

```text
workspace/reports/
  latest.json
  runs/<run_id>/
    portfolio.html               # complete run
    portfolio.xlsx
    portfolio.pdf
    portfolio.csv.zip
    manifest.json
```

Partial runs use `portfolio.partial.*`. The renderer writes and fsyncs every format, validates it,
leak-scans it, records its size and SHA-256, and writes `manifest.json` last. The run becomes visible
only through that manifest. `reports/latest.json` is atomically updated only for a complete,
successfully reloaded and verified run. A crash or validation failure leaves the previous complete
run readable.

## Information hierarchy

All formats distinguish these classes rather than blending them into a narrative:

1. observed static facts and technical lineage;
2. owner-supplied claims;
3. Qwen interpretations and cited proposals;
4. human review decisions; and
5. unresolved references, omissions, and coverage limitations.

The report model contains application summaries, object/query/table/connection/datasource
registries, normalized data access, dependency nodes and edges, concrete evidence, coverage,
application profiles, overlapping portfolio candidates, portfolio findings, reviews, and structured
omissions. Model claims and portfolio proposals must resolve to evidence included in the same
model.

No report presents an unresolved DSN or a same-named object as a confirmed shared datasource.
Consolidation, reuse, target architecture, sequencing, and retirement remain proposals. Retirement
requires an owner lifecycle claim.

## Lineage views

Pass-through QueryDefs and linked TableDefs receive dedicated views. They preserve, where available:

- application, artifact, and Access object IDs;
- query/table name and normalized kind;
- connection and datasource IDs;
- direct `QueryDef.Connect` or `TableDef.Connect` provenance;
- DSN and resolution status;
- platform, driver, server, catalog/database, schema, and object;
- READ/INSERT/UPDATE/DELETE/CREATE/EXECUTE operation; and
- concrete evidence IDs and unresolved warnings.

Connection defaults and explicit SQL qualifiers remain separate cited facts. Raw connection strings,
credentials, and unknown connection-property values never enter the report model.

## HTML

`portfolio.html` is self-contained and works offline. Content is HTML-escaped and the document uses
a deny-by-default Content Security Policy with no CDN, remote image, analytics, script, or network
dependency. It emphasizes coverage, application summaries, facts and claims, interpretations,
portfolio candidates/findings, lineage, reviews, and omissions with resolvable IDs.

## Excel

`portfolio.xlsx` is the detailed analyst artifact. Its sheets include method/provenance, applications,
candidates and findings, observed facts, owner claims, application profiles, reviews, unresolved
references, coverage, pass-through queries, linked tables, object/table/query/connection/datasource
registries, data access, dependency nodes/edges, structured omissions, warnings, and an importable
Review Queue.

Every string that begins like an Excel formula is escaped before writing. The Review Queue includes
the originating analysis fingerprint plus stable subject and evidence identities. Allowed decision
values are constrained to the review contract; `import-review` independently validates every row.

## PDF

`portfolio.pdf` is an executive brief, not the evidence system of record. It includes the report and
analysis identifiers, coverage qualifications, application summaries, leading candidates and
proposals, lineage summaries, unresolved items, and evidence references. Detailed rows remain
available in Excel and CSV. Partial PDFs place the watermark near the beginning of the document.

## Normalized CSV bundle

`portfolio.csv.zip` contains deterministic UTF-8 CSV tables with headers even when empty. It
includes report identity; applications; observed facts; owner claims; profiles and profile-evidence
links; reviews; unresolved references; coverage; pass-through and linked-table views; object, table,
query, connection, and datasource registries; data access; dependency nodes and edges; omissions;
warnings; candidates, membership, evidence, and basis links; and findings, application membership,
and evidence links.

The normalized relations make one-to-many membership explicit and avoid delimiter-packed fields
where a relation is important for downstream analysis.

## Cross-format validation and leak scanning

Before publication, each renderer must expose the same canonical record-ID set. The publisher
verifies report/application/candidate/finding counts, record IDs, file hashes, and parseability. It
scans the report model and rendered HTML, XLSX, PDF, and CSV ZIP for connection strings, credential
assignments, and URI user information. A match blocks the entire run;
there is no best-effort partial format publication.

Report timestamps and machine paths do not affect the stable report fingerprint. The run manifest
does record generation time for operations, while the digest chain ties the output to staged
artifacts, extraction snapshots, evidence bundles, interpretations, and review overlay.

## Review overlay

Analysts may enter decisions in the workbook and import them:

```powershell
portfolio-analyzer import-review --workspace .\workspace --workbook .\review-decisions.xlsx
```

The importer requires the exact originating analysis fingerprint and known proposal/evidence IDs.
It rejects stale, unknown, duplicate, conflicting, malformed, or formula-bearing decisions. Reviews
remain overlays and never mutate observed evidence or model output. A later report may carry a
decision forward only when stable proposal identity and all supporting evidence identities still
match. Failed or partial analysis runs retain the prior overlay only as dormant archival input.
They never apply it to mismatched partial-report inputs; the report records that omission. The next
complete analysis may carry matching decisions forward after identity revalidation.
