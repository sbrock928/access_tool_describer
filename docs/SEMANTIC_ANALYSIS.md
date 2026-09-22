# Local Semantic Analysis

The semantic subsystem is optional. Deterministic extraction and static analysis remain the source
of truth and work without ML dependencies. The model interprets bounded static evidence; its
outputs remain evidence-gated, reviewable proposals.

## Architecture

```text
verified staged Access copies
  -> metadata/static extraction
  -> deterministic SQL/VBA/dependency/capability analysis
  -> procedure-aware, redacted code segments
  -> one deterministic, bounded application IR
  -> one approved local instruct model
  -> one schema-validated profile generation per application
  -> deterministic weighted similarity and clustering
  -> at most one evidence-gated portfolio architecture generation
  -> review and reporting
```

There is no embedding model, persisted neural vector, vector database, model server, hosted-model
adapter, API-key configuration, HTTP inference, telemetry, or network fallback. The model is loaded
once per semantic process and reused across applications.

## Approved model and selection

The production allowlist contains exactly one model:

- repository: `ibm-granite/granite-3.3-2b-instruct`;
- revision: `707f574c62054322f6b5b04b6d075f0a8f05e0f0`;
- publisher: IBM Granite;
- license: Apache-2.0;
- architecture: `GraniteForCausalLM`, supported natively by the pinned Transformers runtime;
- weights: two BF16 safetensors shards, approximately 5 GB total;
- intended strengths: instruction following, classification, extraction, summarization, and
  function-style output.

Granite 3.3 2B was selected as a maintainable CPU-capable baseline from an established publisher
with a permissive license, normal Transformers loading, no repository Python requirement, and
safetensors weights. SmolLM2 1.7B is lighter and Apache-2.0 but has a weaker structured-analysis
quality ceiling. Qwen 2.5 3B has attractive JSON behavior but its model repository uses the Qwen
Research license. Mistral 7B Instruct is Apache-2.0 and capable, but its roughly 14.5 GB BF16 weight
set is materially less practical for the required workstation baseline.

Changing the production model is a code-reviewed allowlist change, not a runtime configuration
feature. A repository that requires `trust_remote_code=True`, custom Python, unsafe serialized
weights, or an unclear license is ineligible.

### Security approval record

The [pinned revision](https://huggingface.co/ibm-granite/granite-3.3-2b-instruct/tree/707f574c62054322f6b5b04b6d075f0a8f05e0f0)
was reviewed on 2026-09-22 using Hugging Face's per-file security status and artifact metadata. All
12 files in the analyzer's allowlist reported an aggregate per-file status of `safe`. For both
safetensors shards, Protect AI reported no findings, JFrog reported that the model does not support
code execution on load, and VirusTotal reported 0/75 and 0/77 detections. Some scanner fields were
`unscanned`, so these third-party results are supporting evidence rather than a security guarantee,
consistent with [Hugging Face's scanner guidance](https://huggingface.co/docs/hub/security-malware).
Reapproval of another model or revision must repeat and document this review.

The approved size and SHA-256 of every allowlisted file are compiled into the analyzer. The weight
digests match the publisher-hosted LFS object IDs; the remaining hashes were calculated from the
pinned raw files during approval. Acquisition must match those independent values before it can
create a local manifest. This avoids trusting a self-generated first-download manifest if the
upstream service or transport supplies different bytes.

## Dependencies

Deterministic analysis has no ML dependency. Install the optional, exactly pinned semantic group
with `pip`:

```powershell
python -m pip install -c requirements\semantic-py313.lock -e ".[semantic]"
```

The four direct dependencies have narrow responsibilities:

- `huggingface-hub`: official client used only by the explicit acquisition command;
- `transformers`: native tokenizer and causal-model loader/inference;
- `torch`: tensor execution on CPU, CUDA, or Apple MPS;
- `safetensors`: safe weight format enforced by the loader.

No dependency is installed at runtime and none comes from a Git URL or model repository. The
CPython 3.13 constraints file pins the resolved semantic dependency graph. Production builds should
install it through the organization's approved package mirror. Run the organization's
software-composition scan (or `pip-audit`) against the built environment before promotion. Where
policy requires hash-locked wheels, generate platform-specific `pip --require-hashes` lock files
from the approved mirror because Torch wheel hashes differ by OS and accelerator.

## Setup and acquisition

Run deterministic staging, extraction, and analysis first. Initialize semantic files without
overwriting existing operator input:

```powershell
portfolio-analyzer semantic-init --workspace .\workspace
```

This creates `semantic.toml`, `business_context.csv`, and a stratified `gold_set.csv`. The TOML
contains the approved repository and immutable revision plus the local path, generation bounds,
deterministic similarity weights/thresholds, redaction policy, and approved Microsoft service list.
The repository and revision are validated against the compiled allowlist.

Existing `semantic.toml` files do not need to be regenerated. Legacy batch and cluster-generation
keys are ignored because schema v5 no longer makes those calls. The generated execution profile is
optimized for a four-core CPU: `device = "cpu"`, a 24,000-character application IR prompt bound,
and small task-specific generation ceilings. GPU-equipped installations can explicitly change
`device` to `auto` or `cuda` after validating the environment.

During an explicitly approved connected acquisition window, run:

```powershell
portfolio-analyzer semantic-model-download --workspace .\workspace
```

This command alone imports `huggingface_hub` and calls `snapshot_download()` with the immutable
revision and an exact filename allowlist. It downloads only the model card, configuration,
tokenizer files, safetensors index, and safetensors shards. It does not read application inventory,
evidence, prompts, or semantic state, and it never uploads data.

The command rejects an existing destination instead of modifying it, verifies every file against
the code-reviewed size and SHA-256, validates the model type and architecture, rejects `auto_map`,
custom pipelines, remote-code declarations, executable files, symlinks, unexpected files, and
pickle-capable weight extensions, then writes
`model_manifest.json`. The manifest records repository, immutable revision, license, architecture,
acquisition time, expected file names, sizes, every SHA-256, and a canonical manifest SHA-256.

The approved model is public and does not need a token. Do not store Hugging Face credentials in
the workspace or semantic TOML. Model weights are ignored by Git and must not be committed.

## Fully offline operation

After acquisition, disconnect the workstation when policy requires it. These commands perform no
network operation:

```powershell
portfolio-analyzer semantic --workspace .\workspace
portfolio-analyzer semantic-check --workspace .\workspace
portfolio-analyzer report --workspace .\workspace --semantic-mode auto
```

Before inference, the analyzer verifies manifest identity, revision, inventory, sizes, all file
hashes, model configuration, and safetensors index references. Missing, incomplete, modified, or
unexpected files stop inference. The analyzer never repairs or redownloads a model.

Inference sets `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_DATASETS_OFFLINE=1`, and telemetry
disable flags before importing the ML runtime. Both tokenizer and model receive a local filesystem
path, `local_files_only=True`, and `trust_remote_code=False`; the model also receives
`use_safetensors=True`. No Hugging Face repository ID is passed to inference. The reviewed Granite
role-token format is constructed by analyzer code, so inference does not execute the repository's
chat template.

## Safety and structured output

Original source paths never cross the staging boundary. Semantic analysis uses already extracted
local snapshots and deterministic evidence only. Secrets, URI credentials, local paths, and network
paths are redacted; deterministic IRs and profiles are bounded; source material is wrapped in
`UNTRUSTED_SOURCE_DATA` delimiters. Source VBA, SQL, descriptions, and model output are data, never
instructions or executable content. Raw prompts are not persisted.

Production analysis covers all extracted standard modules, query SQL, macros, and form/report
code-behind. VBA is partitioned at procedure boundaries and an oversized procedure is split without
discarding its tail. Every selected segment is inspected locally with deterministic SQL/VBA rules.
The analyzer aggregates object names, procedures, identifier vocabulary, redacted string literals,
SQL operations and references, Access actions, technical signals, datasource signatures, and
observed static inferences into one application IR.
The IR stores every inspected source ID and complete aggregate counts. Its prompt indexes are
bounded fairly across categories and explicitly record included and omitted item counts; omitted
index entries still contribute to aggregate totals and the IR fingerprint.

The local model receives the derived IR, not hundreds of raw per-object prompts, and is called once
per application with a 512-token profile ceiling. Cluster names and rationales are derived
deterministically from grounded profile features and require no model call. Architecture synthesis
uses at most one 768-token portfolio-level call and has a conservative deterministic fallback.
Reports and semantic state distinguish deterministic inspection coverage from the bounded IR sent
to the model.

The provider requests one JSON object matching a supplied Pydantic JSON Schema. Returned text is
parsed only with `json.loads` and then validated by the task-specific Pydantic model. There is no
`eval`, `exec`, dynamic import, generated SQL execution, or tool execution. Unknown evidence and
claim IDs are intersected away; unsupported findings are discarded. Malformed output becomes an
isolated per-application failure, while architecture generation has a conservative deterministic
fallback.

## Deterministic similarity

Application similarity uses weighted Jaccard overlap over normalized, inspectable categories:

- business capabilities: 0.25;
- workflows: 0.18;
- data domains/entities: 0.17;
- datasources: 0.15;
- technical/integration/automation characteristics: 0.13;
- Access object composition: 0.08;
- primary archetype: 0.04.

Weights sum to 1. Generic labels such as `reporting`, `workflow`, and `data management` are excluded
so they cannot dominate a relationship. A strong edge uses the configured strong threshold. A
weaker edge must meet the corroborated threshold and share an evidence-backed capability, workflow,
domain, datasource, or technical characteristic. Fixed-seed weighted label propagation produces
repeatable clusters.

Every edge stores its overall score, each category's Jaccard score, and the exact shared features.
`reports/similarity_edges.csv`, the report data, and the offline intelligence HTML expose that
breakdown. There is no opaque embedding score.

## Preflight and gold-set acceptance

`semantic-check` verifies the local model, loads it offline, tests schema-shaped JSON and evidence-ID
preservation, then evaluates reviewed gold data. The gold set requires the stratified 20-application
sample, or the full portfolio when smaller, and checks:

- 100% schema validity for reviewed applications;
- 100% production semantic coverage of code-bearing segments;
- 100% resolvable evidence/claim references;
- no unsupported high-confidence conclusions;
- at least 80% primary-archetype agreement;
- at least 75% macro-F1 for business-capability labels.

The command exits unsuccessfully when the gold set is incomplete or a threshold is missed.

## Run, resume, and migration

```powershell
portfolio-analyzer semantic --workspace .\workspace
portfolio-analyzer semantic --workspace .\workspace --tool-id 12345
portfolio-analyzer semantic --workspace .\workspace --force
```

The CLI writes an atomic `in_progress` semantic checkpoint after every newly processed application.
Restarting the same command reuses compatible completed profiles and retries only failed or stale
applications. Normal and quick runs print timestamped start/completion events for model loading,
deterministic IR construction, the single application synthesis call, checkpoints, deterministic
clustering, architecture synthesis, and final state writing. Every line includes elapsed time for
the preceding step and total run. `--force` deliberately regenerates the selected application's IR
and profile.

### Quick test mode

Use quick mode for an end-to-end smoke test of local inference, schemas, evidence gating,
clustering, architecture fallback, and reporting:

```powershell
portfolio-analyzer semantic --workspace .\workspace --quick
portfolio-analyzer report --workspace .\workspace --semantic-mode auto
```

Quick mode retains the approved Granite model and all integrity/offline controls. For each
application it deterministically selects at most five code-bearing objects, round-robin across
available object types, and inspects every segment of each selected object. It caps execution at a
4,096-token context, 768 global output tokens, 512 profile and architecture output tokens, 1,000
characters per segment, and 6,000 application-IR prompt characters. It records
`run_mode = quick`, the object limit, effective generation settings, checkpoint status, and sampled
coverage in semantic provenance.

Quick outputs are deliberately test-only. The workbook and PDF carry a prominent warning, the HTML
semantic status and report manifest identify quick mode, `semantic-check` cannot pass it,
`report --semantic-mode require` rejects it, retained production review decisions are not applied,
and review decisions cannot be imported from it. Run
the production analysis without `--quick` before gold-set acceptance or decision-making:

```powershell
portfolio-analyzer semantic --workspace .\workspace
portfolio-analyzer semantic-check --workspace .\workspace
portfolio-analyzer report --workspace .\workspace --semantic-mode require
```

Atomic semantic state and each application IR/profile are fingerprinted by staged artifact hashes,
source-segment hashes, evidence, datasources, claims, static,
semantic, prompt and schema versions, model manifest hash, inference-library version, generation
settings, run mode and object-selection limit, deterministic similarity
version/weights/thresholds, and approved service catalog.
Compatible application profiles are reused. The model is loaded once per run.

Schema v5 records source segments, deterministic application IRs, deterministic inspection
coverage, bounded model-input counts and hashes, production versus quick mode, checkpoint
completion, and the object-selection limit. Older
non-endpoint state may be readable but is cache-incompatible and is refreshed; v1 endpoint and
embedding state remains invalid. Deterministic extraction and analysis remain intact.

## Confidence, review, and reporting

Owner context remains distinct `Claim` data. Confidence is derived by the analyzer: high requires
complete extraction and two independent observed references; medium requires observed evidence;
claim-only, incomplete, ambiguous, or warning-qualified conclusions remain low. Retirement requires
an owner lifecycle claim. Incomplete extraction forces `investigate` and Wave 0. Microsoft mappings
are limited to the configured approved service catalog.

Reviewers enter `Accept`, `Edit`, or `Reject` in the workbook's `Review Queue`, then import decisions:

```powershell
portfolio-analyzer import-review --workspace .\workspace `
  --workbook .\workspace\reports\Portfolio_Analysis.xlsx
```

`report --semantic-mode auto` includes only current compatible semantic data; `require` fails if it
is absent, partial, stale, or incompatible; `off` produces deterministic-only reports. The report
manifest records semantic provenance and output checksums without persisting an absolute local model
path or workstation username.
