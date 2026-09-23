# Access Portfolio Analyzer

An evidence-driven, static-analysis system for a portfolio of Microsoft Access applications.

The system has one non-negotiable safety invariant: source applications are copied to a verified local workspace before any extraction or analysis. The original inventory path is never passed to an extractor.

The input workbook must contain `INVENTORY_ID`, `EUCTNAME`, `FILE_NAME`, `FULLPATH`, and `DESCRIPTION`. `FULLPATH` is source-only; it is never an analysis path.
An inventory ID/EUC may span multiple rows when it has multiple Access files. Each distinct
`FULLPATH` becomes a primary artifact; shared files in the same application bundle are staged once.

## Current capabilities

The repository provides inventory ingestion, safe local staging, provenance and hashing,
deterministic SQL/VBA/connection/path analysis, evidence and dependency models, SQLite
persistence, capability aggregation, and Excel/CSV/PDF/HTML reporting. An optional semantic
layer adds grounded application profiles, explainable portfolio clusters, two target-architecture
tracks, and migration waves. The Windows adapter performs metadata-only Access inspection behind
a guarded interface; it is intentionally unavailable on non-Windows hosts.

See [BUILD_SPEC.md](BUILD_SPEC.md), [docs/SAFETY.md](docs/SAFETY.md), and [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md).

## Quick start

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,windows]"
portfolio-analyzer stage --inventory .\tool_inventory.xlsx --workspace .\workspace
portfolio-analyzer extract --workspace .\workspace
portfolio-analyzer analyze --workspace .\workspace
portfolio-analyzer report --workspace .\workspace
```

`extract` only considers successful staged artifacts and is the only command that opens Access. It persists
snapshots under `workspace/extracted/`. `analyze --force` can be run repeatedly against those snapshots as
rules evolve, without opening Access or the original source file again.

`report` writes an Excel workbook, an executive PDF, and normalized CSV datasets. The reports make
pipeline coverage explicit so zero findings are not confused with missing or incomplete analysis.
See [docs/REPORTS.md](docs/REPORTS.md) and [docs/REFACTOR_PLAN.md](docs/REFACTOR_PLAN.md).

## Optional local semantic analysis

Semantic analysis is opt-in and deterministic by default. It inspects all extracted code-bearing
objects, builds one complete application IR, derives evidence-cited profiles, computes explainable
similarity, and creates the target architecture without loading a model. It does not use embeddings
or a vector database, and the normal deterministic path needs neither ML dependencies nor model
weights.

```powershell
portfolio-analyzer semantic-init --workspace .\workspace
portfolio-analyzer semantic --workspace .\workspace
portfolio-analyzer semantic-check --workspace .\workspace
portfolio-analyzer report --workspace .\workspace --semantic-mode auto
```

Local-model profile generation remains available as an explicit opt-in. Install the semantic extras,
run `semantic-model-download`, then set `[profile] model_generation = true` in `semantic.toml`.
Setting `[microsoft] model_generation = true` separately enables model-authored architecture
synthesis. The approved model is `ibm-granite/granite-3.3-2b-instruct`, pinned to an immutable
commit. Acquisition is the only network-enabled phase; every allowlisted file must match a
code-reviewed size and SHA-256, and inference has no hosted fallback.

Use `--semantic-mode require` in controlled production runs, or `off` for deterministic-only
reporting. Reviewers can enter `Accept`, `Edit`, or `Reject` in the workbook's `Review Queue` and
then run:

```powershell
portfolio-analyzer import-review --workspace .\workspace `
  --workbook .\workspace\reports\Portfolio_Analysis.xlsx
portfolio-analyzer report --workspace .\workspace
```

For a faster end-to-end smoke test, use `portfolio-analyzer semantic --workspace .\workspace
--quick`. Quick mode samples at most five representative objects per application. Normal mode
covers all modules, queries, macros, and form/report code-behind and reduces those facts to one
application IR. With the default configuration there are zero model calls; model-backed profiles
make one call per application only when explicitly enabled.
Quick-mode state and reports are marked `TEST ONLY`; `semantic-check`, `--semantic-mode require`,
and review import reject quick results. Each application is checkpointed so an interrupted run can
resume without regenerating completed profiles. Both modes print wall-clock timestamps plus
per-step and total elapsed time for model calls and pipeline stages.

See [docs/SEMANTIC_ANALYSIS.md](docs/SEMANTIC_ANALYSIS.md) for model setup, security boundaries,
gold-set evaluation, review import, and reproducible reruns.

Per-application folders under `workspace/staged_tools` and `workspace/extracted`, along with every
application reference in generated reports, use the human-readable `EUCTNAME`. `INVENTORY_ID`
remains an internal provenance key and is not exposed as the report label.
