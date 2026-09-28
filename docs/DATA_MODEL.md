# Data Model

The primary immutable provenance values are tool inventory ID, original source path, local staged path, SHA-256, size, and source timestamp. `StagedArtifact` is the capability token required for extraction.

Evidence has source object, location, text, inference, and confidence. Datasources and dependencies retain evidence IDs. Presentation reports are generated from these normalized facts rather than becoming a system of record.


`SemanticApplicationProfile.roles` stores independent `ApplicationRole` records (role, rationale,
evidence IDs). `BehaviorFact.artifact_hash` scopes cross-object role rules to an extracted artifact.
`PortfolioTheme` is discovered during semantic analysis and persisted in
`SemanticPortfolioState.discovered_themes`. Reports reuse these groups and can also discover groups
from static evidence when no semantic state exists. It records a proposed solution, affected applications, next steps, validation
questions, confidence and coverage qualifications. Its `ThemeLocation` records point to the precise
application, artifact, object, source location and evidence ID. Themes do not partition the portfolio.

`PortfolioTheme.grouping_basis` identifies the actual repeated patterns. `alternative_options`
compares candidate responses to the shared evidence. `generation_method` distinguishes evidence
discovery from optional model interpretation. Group IDs include membership and pattern identities;
input order does not affect them. Duplicate observations cannot inflate application counts.
