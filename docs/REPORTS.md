# Reports

The primary deliverable is `Portfolio_Intelligence.html`, a self-contained offline report with no
CDN or network dependency. It includes coverage, search, application drill-down, an observed
dependency map, a semantic consolidation map, both architecture tracks, and migration waves.
Drill-down keeps observed sources, owner claims, and semantic proposals in visibly separate
sections and identifies whether each profile used deterministic rules or the optional local model.
The embedded content-security policy disables all network sources.

`Portfolio_Analysis.xlsx` retains the original detailed sheets and adds a dashboard, Application
Portfolio, Target Architecture, App-Target Crosswalk, Migration Roadmap, Semantic Findings, Review
Queue, and Method & Provenance. The dashboard charts coverage, archetypes, clusters, dependency
hotspots, and migration waves. Review Queue decisions are constrained to `Accept`, `Edit`, or
`Reject`.

`Portfolio_Analysis.pdf` is a concise executive and architecture brief. Raw evidence remains in the
workbook and normalized CSV files so oversized evidence does not make the PDF unreadable.

Semantic runs add `semantic_applications.csv`, `semantic_findings.csv`,
`portfolio_clusters.csv`, `similarity_edges.csv`, `architecture_components.csv`,
`architecture_relations.csv`,
`application_target_map.csv`, `migration_waves.csv`, `architecture_model.json`, and portable
`Target_Architecture.md` Mermaid diagrams. `report_manifest.json` includes output checksums and
semantic provenance.

Quick semantic runs are never presented as production results. The Excel summary and executive PDF
carry a prominent `TEST ONLY` warning, the HTML semantic status identifies quick mode, and the
report manifest records the run mode, object limit, effective generation bounds, and completion
status. Production-required reporting rejects quick state.

Excel is the primary detailed report. It includes portfolio and per-application deterministic and
semantic code coverage, including modeled/eligible segment counts, along with applications,
artifacts, datasources, dependencies, capabilities, recommendations, evidence, and staging errors.
Coverage distinguishes a completed analysis with zero findings from an application that was never
analyzed or whose extraction was incomplete.

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
When an EUC has multiple listed Access applications, coverage and application reports include the
primary filename beside the repeated EUC name so each application remains identifiable.

Application drill-downs and the Application Portfolio sheet include observed behavior, known inputs,
outputs/write targets, secondary capabilities, purpose provenance, and the classification rationale
with citations. The HTML portfolio table leads with observed behavior; owner claims remain separate.
The executive PDF includes up to six alphabetical application behavior examples, with the complete
portfolio retained in HTML and Excel.

The Application Behavior and Behavior Sources sheets, and matching `application_behaviors.csv` and
`behavior_sources.csv` datasets, connect behavior facts to redacted source excerpts and root UI
properties. `semantic_applications.csv` appends the new profile fields; `semantic_application_irs.csv`
appends full inventory counts/names, source IDs, and behavior counts. Existing columns remain intact.
