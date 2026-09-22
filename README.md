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

Semantic analysis is opt-in and uses one approved model loaded directly in-process. Model
acquisition is the only network-enabled phase. Analysis uses an integrity-verified local directory,
sets Hugging Face and Transformers offline mode, passes `local_files_only=True` and
`trust_remote_code=False`, and has no HTTP provider, API key, telemetry, or hosted fallback.
Portfolio similarity is deterministic and explainable; it does not use embeddings or a vector
database.

```powershell
python -m pip install -c requirements\semantic-py313.lock -e ".[dev,windows,semantic]"
portfolio-analyzer semantic-init --workspace .\workspace
portfolio-analyzer semantic-model-download --workspace .\workspace
# Disconnect from external networks here when policy requires it.
portfolio-analyzer semantic --workspace .\workspace
portfolio-analyzer semantic-check --workspace .\workspace
portfolio-analyzer report --workspace .\workspace --semantic-mode auto
```

The approved model is `ibm-granite/granite-3.3-2b-instruct`, pinned to an immutable commit.
Acquisition downloads only its allowlisted Transformers configuration, tokenizer files, model card,
and safetensors weights. Every file must match a code-reviewed size and SHA-256 before acquisition
writes the local manifest. Missing, altered, unsafe, or unexpected model files stop inference; they
are never repaired by an implicit download.

Use `--semantic-mode require` in controlled production runs, or `off` for deterministic-only
reporting. Reviewers can enter `Accept`, `Edit`, or `Reject` in the workbook's `Review Queue` and
then run:

```powershell
portfolio-analyzer import-review --workspace .\workspace `
  --workbook .\workspace\reports\Portfolio_Analysis.xlsx
portfolio-analyzer report --workspace .\workspace
```

For a faster end-to-end smoke test, use `portfolio-analyzer semantic --workspace .\workspace
--quick`. Quick mode uses the same approved model but samples at most five representative objects
per application and applies smaller generation bounds. Its state and reports are marked `TEST
ONLY`; `semantic-check`, `--semantic-mode require`, and review import reject quick results. Each
completed application is checkpointed so an interrupted quick or production run can resume. Both
modes print wall-clock timestamps plus per-step and total elapsed time for model calls and pipeline
stages.

See [docs/SEMANTIC_ANALYSIS.md](docs/SEMANTIC_ANALYSIS.md) for model setup, security boundaries,
gold-set evaluation, review import, and reproducible reruns.

Per-application folders under `workspace/staged_tools` and `workspace/extracted`, along with every
application reference in generated reports, use the human-readable `EUCTNAME`. `INVENTORY_ID`
remains an internal provenance key and is not exposed as the report label.
