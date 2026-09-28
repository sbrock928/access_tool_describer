# V2 Data Model

All V2 contracts are strict Pydantic 2 models with unknown fields forbidden and explicit schema
versions.

- `StageIndex` records eligible artifacts, unsupported rows, failures, opaque source identity,
  staged-relative path, size, and SHA-256.
- `ApplicationEvidenceBundle` is the canonical technical record: artifacts, Access objects,
  QueryDefs, TableDefs, connections, datasource identities, data access, dependency nodes/edges,
  evidence, owner claims, coverage, and unresolved references.
- `LogicalUnitInterpretation` and `ApplicationInterpretation` contain only evidence-cited Qwen
  interpretations and model/prompt/schema provenance.
- `PortfolioCandidate` records deterministic membership and basis. `PortfolioAnalysis` contains
  model-authored explanations and proposals for those candidates.
- `PortfolioReportModel` is the only renderer input. HTML, Excel, PDF, and normalized CSV use the
  same record IDs and counts.

`application_id` is the inventory identity. `artifact_id` derives from the application and opaque
canonical source identity; the artifact SHA-256 is its version. Object, connection, target, and
edge IDs are artifact-scoped. Evidence IDs include artifact identity, artifact hash, fact identity,
and sanitized canonical payload—not raw paths or credentials.

Staged binaries are version-addressed by `artifact_id` and content SHA-256 and are published by
verified atomic replacement. A later version therefore does not mutate the binary named by an
earlier run manifest; unreferenced versions may remain as harmless orphaned state.

Raw connection strings, password values, prompts, and raw SaveAsText exports are never persisted
or hashed. Review decisions are an overlay tied to the originating stable analysis fingerprint,
proposal ID, and evidence IDs.

Optional reviewed owner input is loaded from `source_inventory/owner_context.csv`. Its strict
columns are `application_id`, `business_owner`, `technical_owner`, `business_purpose`,
`criticality`, `user_band`, `lifecycle_intent`, `data_sensitivity`, `pain_points`, and
`target_constraints`. These values become `OwnerClaim` records; they never become observed facts.
