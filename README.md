# Access Portfolio Analyzer

Access Portfolio Analyzer is an evidence-first, offline analyzer for Microsoft Access estates. It
stages verified local copies, extracts static Access metadata, builds a canonical evidence bundle,
uses one pinned local Qwen model for interpretation, and renders a single report model as HTML,
Excel, PDF, and normalized CSV.

The production path is intentionally narrow:

```text
inventory -> verified staging -> static extraction -> evidence bundles
          -> local Qwen interpretation -> portfolio candidates and interpretation
          -> shared report model -> HTML / Excel / PDF / CSV
```

Technical facts remain deterministic. Qwen can interpret and propose from existing identifiers, but
it cannot create servers, databases, objects, operations, dependencies, evidence, or candidate
membership.

## Supported scope

- Primary artifacts: case-insensitive `.accdb` and `.mdb` only.
- Explicitly unsupported primaries: `.accde`, `.mde`, `.adp`, spreadsheets, CSV files, and every
  other format. Mixed inventories continue; unsupported rows are recorded as
  `SKIPPED_UNSUPPORTED_FORMAT` and copy zero bytes.
- One inventory/EUC ID is one application. Multiple eligible rows for that ID remain separate,
  artifact-scoped components of its application bundle.
- Excel workbooks, network files, libraries, and other resources are recorded only when static
  Access evidence refers to them. They are not recursively staged or independently analyzed.

The inventory workbook must contain `INVENTORY_ID`, `EUCTNAME`, `FILE_NAME`, `FULLPATH`, and
`DESCRIPTION`. `FILE_NAME` must agree with the basename and suffix of `FULLPATH`. The inventory ID
is the authoritative `application_id`; `FULLPATH` is source-only and is never passed to extraction,
analysis, or reporting.

Reviewed owner context is optional and remains separate from observed evidence. Before the first
`stage` command, place `owner_context.csv` in `workspace/source_inventory/` with exactly these
columns:

```text
application_id,business_owner,technical_owner,business_purpose,criticality,user_band,lifecycle_intent,data_sensitivity,pain_points,target_constraints
```

Rows may leave claim fields blank, but may reference only applications in the inventory. Unknown
columns, unknown application IDs, and repeated claims for the same application and field fail
staging. Values are sanitized before entering the evidence bundle.

## Install

Python 3.13 and Microsoft Access/ACE DAO on Windows are required for real Access extraction. Install
the development, Windows, and local-model dependencies through the organization's approved package
source:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,windows,semantic]"
```

Cross-platform development can exercise static fixtures and fake providers, but the real extractor
fails closed outside Windows.

## Production workflow

Start with a fresh V2 workspace. Generated state from earlier architectures is deliberately not
migrated.

```powershell
portfolio-analyzer stage --inventory .\tool_inventory.xlsx --workspace .\workspace
portfolio-analyzer extract --workspace .\workspace
portfolio-analyzer analyze --workspace .\workspace --model-dir C:\Models\Qwen2.5-0.5B-Instruct --verbose
portfolio-analyzer report --workspace .\workspace
```

The model directory may instead be configured as `[qwen].path` in
`workspace/analyzer.toml`, or through `ACCESS_ANALYZER_MODEL_DIR`, in that precedence order. There
is no implicit download and no model selector.

`--application ID` narrows `extract` or `analyze`; `--force` recomputes the selected current
application while preserving unrelated compatible state. Every mutating command holds an exclusive
workspace lock. Commands checkpoint all requested applications and return nonzero if any required
item fails.

Normal reporting refuses missing, failed, stale, or in-progress analysis. If an operator explicitly
needs a scoped diagnostic deliverable, use:

```powershell
portfolio-analyzer report --workspace .\workspace --allow-partial
```

A partial report includes only completed applications, names every omitted application or unit and
its reason, suppresses portfolio-wide absence claims, is visibly watermarked, and never replaces
`reports/latest.json` for the latest complete report.

## Administrative commands

```powershell
portfolio-analyzer model-download --destination C:\Models\Qwen2.5-0.5B-Instruct
portfolio-analyzer model-verify --model-dir C:\Models\Qwen2.5-0.5B-Instruct
portfolio-analyzer quality-check --workspace .\workspace --gold-set .\reviewed-gold.csv
portfolio-analyzer import-review --workspace .\workspace --workbook .\review-decisions.xlsx
```

The quality gold set is a UTF-8 CSV with
`application_id,expected_capabilities,expected_related_application_ids`. Separate multiple values
inside either expectation field with `;` or `|`. Application IDs must refer to the current analysis;
every related application needs its own row, and unlisted pairs among reviewed rows count as reviewed
negatives. The command scores every reviewed profile pair, reports the frozen threshold and the
best-F1 calibrated recommendation in its quality result, and fails if the reviewed set does not
validate the frozen threshold. Evaluation never changes generation policy.

`model-download` is the only command permitted to use the network. It acquires the exact reviewed
allowlist for `Qwen/Qwen2.5-0.5B-Instruct` at revision
`7ae557604adf67be50417f59c2c2f167def9a775`, verifies sizes and SHA-256 digests, rejects unexpected
or executable artifacts, and writes a verification manifest. `model-verify` and `analyze`
independently verify those local files before use.

`analyze` always prints safe high-level progress to stderr. Add `--verbose` for model-load timing,
token budgets, cache results, retries, generation throughput, and a 15-second heartbeat. Progress
messages never include prompts, generated text, object definitions, credentials, or filesystem
paths.

The reviewed Qwen context ceiling remains 32,768 tokens, but production uses smaller operational
ceilings: 8,192 total tokens for logical-unit inference and 16,384 for application/portfolio
synthesis, both including reserved output. This forces tokenizer-measured splitting before
CPU-hostile near-maximum-context prefill. These fixed limits participate in model provenance and
cache invalidation.

Qwen emits compact semantic drafts rather than reproducing authoritative application IDs, hashes,
provenance, consumed-unit registries, or stable IDs. Strict validation first closes every cited ID
against supplied evidence; deterministic code then attaches those envelope fields. The 0.5B policy
uses one logical unit per generation and pre-fills the opening JSON brace for reliable structure.
Invalid drafts are never cached.

Semantic-profile similarity candidates are disabled pending reviewed Qwen 0.5B gold-set
calibration. Endpoint, external-object, file, and exact-code candidates remain active; reports
qualify this limitation and suppress semantic-similarity absence claims.

## Outputs and review

Reports are immutable runs under `workspace/reports/runs/<run_id>/`. A run contains:

- a self-contained, CSP-restricted offline HTML report;
- a detailed analyst workbook with evidence, lineage, coverage, and a review queue;
- an executive PDF with evidence references; and
- a normalized CSV bundle covering applications, claims, data access, dependencies, evidence,
  coverage, and portfolio findings.

All formats are built from the same `PortfolioReportModel` and preserve the same IDs and counts.
Dedicated pass-through and linked-table views retain connection provenance and DSN resolution
without exposing raw connection strings. Owner claims, observed facts, model interpretations,
human decisions, and unresolved items remain separate.

Review import requires the originating analysis fingerprint. Stale, unknown, duplicate, or
conflicting decisions are rejected, and decisions carry forward only when stable proposal and
evidence identities still match. A failed or partial intervening analysis keeps the last overlay
reachable but dormant; partial reports omit it explicitly, and a later complete run revalidates
proposal and evidence identities before carrying any decision forward.

See [BUILD_SPEC.md](BUILD_SPEC.md), [docs/SAFETY.md](docs/SAFETY.md),
[docs/SEMANTIC_ANALYSIS.md](docs/SEMANTIC_ANALYSIS.md), [docs/REPORTS.md](docs/REPORTS.md), and
[docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md).
