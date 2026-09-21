# Access Portfolio Analyzer

An evidence-driven, static-analysis system for a portfolio of Microsoft Access applications.

The system has one non-negotiable safety invariant: source applications are copied to a verified local workspace before any extraction or analysis. The original inventory path is never passed to an extractor.

The input workbook must contain `INVENTORY_ID`, `EUCTNAME`, `FILE_NAME`, `FULLPATH`, and `DESCRIPTION`. `FULLPATH` is source-only; it is never an analysis path.

## Current MVP

The repository provides the cross-platform foundation: inventory ingestion, safe local staging, provenance and hashing, deterministic SQL/VBA/connection/path analysis, evidence and dependency models, SQLite persistence, capability aggregation, similarity signals, and Excel/CSV/PDF reporting. The Windows adapter performs metadata-only Access inspection behind a guarded interface; it is intentionally unavailable on non-Windows hosts.

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
