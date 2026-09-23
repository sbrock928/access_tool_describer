# Reports

The primary deliverable is `Portfolio_Intelligence.html`, a self-contained offline report with no
CDN or network dependency. It includes coverage, search, application drill-down, an observed
dependency map, a semantic consolidation map, both architecture tracks, and migration waves.
Drill-down keeps observed sources, owner claims, and semantic proposals in visibly separate
sections and identifies whether each profile used deterministic rules or the optional local model.
The embedded content-security policy disables all network sources.

`Portfolio_Analysis.xlsx` retains the original detailed sheets and adds a dashboard, Application
Portfolio, Target Architecture, App-Target Crosswalk, Migration Roadmap, Semantic Findings, Review
Queue, and Method & Provenance. The dashboard charts coverage, overlapping roles, clusters, dependency
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


## Overlapping roles and discovered groups

Roles remain independent descriptors in the overview, role filter and application portfolio. They
are not modernization groups. **Themes & solutions** discovers groups from the portfolio itself:

- matching complete query/VBA definitions, preserving identifiers and literal values;
- the same confirmed external datasource identity (platform, server, database and schema);
- repeated extracted observations, including labels never previously encountered by the tool.

No role-to-solution catalogue or fixed number of groups is used. A group needs evidence from at
least two applications. Features with the same supporting application set are combined. Groups
can overlap: A/B sharing one pattern and B/C sharing another do not automatically become A/B/C.
Names, roles, local table names, and incomplete connection identities alone cannot create groups.
An absence of groups means insufficient shared evidence, not an absence of modernization work.

Each group shows the actual shared patterns, affected applications, cited objects, comparison
options and validation questions. The options address the observed relationship (code reuse,
shared data contracts or repeated implementation signals). They remain conservative design
candidates; repeated technology alone does not establish common business requirements.

Excel's **Portfolio Themes** and **Theme Locations** sheets and `portfolio_themes.csv` /
`theme_locations.csv` export the same groups. Theme exports include `grouping_basis`,
`alternative_options` and `generation_method`. The PDF summarizes a leading option and an
alternative. Full evidence remains in HTML, Excel and CSV.

Default architecture components come from the discovered groups. The optional architecture model
receives these groups and can propose different names and designs, subject to evidence validation
and the approved platform-service list. It does not use a role-to-product lookup table. Model
proposals are shown separately from deterministic discoveries; every included application needs
its own supporting references. Existing model defaults and generation limits remain unchanged.
