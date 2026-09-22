# Architecture

`inventory -> staging -> trusted local artifact -> Windows extraction -> deterministic analysis -> evidence store -> bounded local semantic interpretation -> deterministic similarity -> reviewed architecture -> reports`

The `access` package is the only Windows-specific layer. It receives `StagedArtifact`, never `Path` alone, and validates the artifact again before use. `parsing`, `lineage`, `capabilities`, `similarity`, `persistence`, and `reporting` are OS-independent.

Deterministic facts (for example a SQL statement references `dbo.Deal`) are stored as evidence independently from interpretations (for example a report-generation capability). This permits later reclassification without re-extraction.

`INVENTORY_ID` remains the stable internal join and checkpoint key. Workspace application folders
and report labels use `EUCTNAME`. EUC names are validated for case-insensitive uniqueness after
Windows-safe normalization, preventing two applications from sharing a directory. On staging, an
unambiguous legacy inventory-ID directory is renamed to its EUC name.

One inventory ID/EUC can contain multiple listed Access files. The staging state retains one
inventory record per listed file, de-duplicates their shared bundle artifacts, and marks every
listed Access file as primary. Extraction and analysis checkpoints distinguish those primaries by
their SHA-256 values. Coverage reports show both the EUC name and primary filename.

The optional semantic layer loads one allowlisted instruct model directly from an integrity-verified
local directory. The model summarizes and classifies bounded evidence; it does not replace static
parsing. Portfolio similarity uses weighted Jaccard overlap across evidence-grounded capabilities,
workflows, data domains, datasources, technical characteristics, object composition, and archetype.
Each retained edge stores its category scores and shared features. No embeddings, vector database,
model server, or network transport participate in analysis.
