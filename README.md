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

Local-model profiles can propose several business capabilities and workflows per application,
using open-ended labels with evidence citations. For a Windows CPU workstation with 16 GB RAM,
start a reviewed trial with the smaller **Qwen2.5 1.5B Instruct** preset. Its approximately 3.09 GB
BF16 weights are smaller than Granite's approximately 5 GB weights; runtime memory and speed
still need measurement on your hardware. This is not a measured accuracy recommendation.

```powershell
python -m pip install -c requirements\semantic-py313.lock -e ".[semantic]"
portfolio-analyzer semantic-model-select --workspace .\workspace --model qwen
portfolio-analyzer semantic-model-download --workspace .\workspace
portfolio-analyzer semantic --workspace .\workspace
portfolio-analyzer report --workspace .\workspace --semantic-mode require
```

Run `semantic-init` first if the workspace has no semantic configuration. Model selection enables
local profile generation on CPU and sets a 768-token profile budget; it preserves other settings.
Use `--model granite` to select the existing Granite option. New default configurations remain
model-free. Setting `[microsoft] model_generation = true` separately enables model-authored
architecture proposals. Both models are pinned to immutable revisions with verified file sizes
and SHA-256 hashes. Acquisition is the only network-enabled phase; inference has no hosted fallback.

The report now shows overlapping capability interpretations, evidence-supported reuse candidates,
and an interactive dependency network with resource/operation/source drill-down. Shared resources
suggest boundary reviews, not automatic microservice deployment decisions.

After this upgrade, rerun `semantic` and `report` using saved extraction and analysis results.
There is no new extraction or deterministic `analyze` requirement for these report/model changes.

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
