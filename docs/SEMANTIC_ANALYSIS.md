# Local Semantic Analysis

The semantic subsystem is optional. Deterministic extraction and static analysis remain the source
of truth and continue to work without a model. Semantic outputs are proposals with resolvable
evidence or owner-claim references and a review status.

## Network and model boundary

- Model weights are acquired, approved, licensed, and stored outside this repository.
- The analyzer accepts only `http` or `https` endpoints that resolve exclusively to loopback
  addresses (`localhost`, `127.0.0.0/8`, or `::1`). Other hostnames and IP addresses are rejected.
- HTTP redirects are rejected, including redirects originating at a loopback endpoint.
- HTTP proxy environment settings are ignored so loopback traffic cannot be forwarded externally.
- The analyzer has no hosted-model adapter, API-key setting, model downloader, or web fallback.
- Chat and embedding servers are separately managed processes. A `llama.cpp` OpenAI-compatible
  server is the initial supported contract; any compatible local server must pass `semantic-check`.
- Prompts contain bounded, redacted packets. Extracted definitions and inventory text are delimited
  as untrusted data. Credentials and local/network paths are stripped. Raw prompts are not stored.

## Setup

Run deterministic staging, extraction, and analysis first. Then initialize without overwriting any
existing semantic files:

```powershell
portfolio-analyzer semantic-init --workspace .\workspace
```

This creates:

- `semantic/semantic.toml`: local chat and embedding endpoints, externally calculated model
  SHA-256 values, generation limits, clustering thresholds, and the approved Microsoft catalog.
- `semantic/business_context.csv`: optional owner, purpose, criticality, user band, lifecycle,
  sensitivity, pain point, and target-constraint claims.
- `semantic/gold_set.csv`: a stratified template of up to 20 applications.

Replace every `REPLACE_...` value in `semantic.toml`. Calculate checksums with an approved local
tool, for example `Get-FileHash -Algorithm SHA256` in PowerShell. The analyzer records model names,
checksums, endpoints, server identity, prompt/schema/static-analysis versions, generation settings,
clustering settings, approved services, and a context hash in `semantic_state.json`.

The default configuration expects separate services:

```text
chat:       http://127.0.0.1:8080/v1
embeddings: http://127.0.0.1:8081/v1
```

Start the approved local servers using their own operating instructions. Do not expose them on a
LAN interface. The analyzer deliberately does not start, stop, install, or download models.

## Preflight and gold-set acceptance

```powershell
portfolio-analyzer semantic-check --workspace .\workspace
```

Preflight checks both loopback endpoints, strict structured JSON, evidence-ID preservation, and
embedding dimensions. When reviewed gold-set rows exist, the command also evaluates:

- 100% resolvable evidence/claim references;
- at least 80% primary-archetype agreement;
- at least 75% macro-F1 for business-capability labels.

The check exits unsuccessfully when a populated gold set misses a threshold. Unsupported generated
statements are already discarded before evaluation, so they cannot become high-confidence results.

## Run and resume

```powershell
portfolio-analyzer semantic --workspace .\workspace
portfolio-analyzer semantic --workspace .\workspace --tool-id 12345
portfolio-analyzer semantic --workspace .\workspace --force
```

State is atomic and keyed by artifact hashes, context claims, static/semantic/prompt/schema
versions, model checksums, generation settings, clustering thresholds, and the approved service
catalog. Compatible application profiles and embeddings are reused. Failures are isolated to the
affected application and force an `investigate` disposition.

Processing is hierarchical: bounded object summaries become application profiles; profiles are
embedded locally; conservative similarity edges form deterministic fixed-seed communities; each
community is summarized; and validated community summaries drive target architecture.

## Context, confidence, and decisions

Owner context is stored as `Claim` records and never presented as observed evidence. Confidence is
calculated by the analyzer:

- high: complete extraction and at least two independent observed references;
- medium: one observed reference, or an owner claim with support;
- low: claim-only, ambiguous, warning-qualified, or unsupported.

Retirement requires a lifecycle claim containing a retirement/decommission intent. Incomplete or
warning-qualified extraction forces `investigate` and Wave 0. Microsoft components are limited to
the configured approved catalog. Unsupported mappings become open hosting decisions.

## Review import

Generate a report, edit only the `Decision`, `Edited Value`, `Reviewer`, and `Notes` columns in the
`Review Queue`, and import it:

```powershell
portfolio-analyzer import-review --workspace .\workspace `
  --workbook .\workspace\reports\Portfolio_Analysis.xlsx
```

Decisions are persisted separately in `semantic/review_decisions.json` and reapplied after semantic
reruns. Formula cells and invalid decisions are rejected. Rejecting an application mapping returns
it to Wave 0 pending replacement.

## Reporting modes and reproducibility

`report --semantic-mode auto` includes semantic data only when it is current and compatible. A
precise coverage gap appears otherwise, while all deterministic reports are still generated.
`require` fails clearly when current semantic state is unavailable. `off` excludes semantics.

`reports/report_manifest.json` records file checksums and semantic provenance. For a reproducible
rerun, preserve the staged artifact hashes, context and review files, semantic configuration, exact
external model files/checksums, and the repository revision.
