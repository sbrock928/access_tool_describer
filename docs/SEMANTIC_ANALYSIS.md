# Local Semantic Analysis

The semantic subsystem is optional. Deterministic extraction and static analysis remain the source
of truth and work without ML dependencies. Application profiles, similarity, clustering, and target
architecture are also deterministic by default. The optional model-backed profile path interprets
bounded static evidence; its outputs remain evidence-gated, reviewable proposals.

## Architecture

```text
verified staged Access copies
  -> metadata/static extraction
  -> deterministic SQL/VBA/dependency/capability analysis
  -> procedure-aware, redacted code segments
  -> one deterministic application IR
  -> deterministic evidence-cited application profile (default)
     OR one schema-validated approved-model call per application (explicit opt-in)
  -> deterministic weighted similarity and clustering
  -> deterministic target architecture (default)
     OR at most one evidence-gated model architecture call (explicit opt-in)
  -> review and reporting
```

There is no embedding model, persisted neural vector, vector database, model server, hosted-model
adapter, API-key configuration, HTTP inference, telemetry, or network fallback. When either model
option is enabled, the model is loaded once per semantic process and reused across applications.

## Approved model and selection

The allowlist contains two immutable, native Transformers models:

| Preset | Repository | Revision | Architecture | BF16 weights |
| --- | --- | --- | --- | --- |
| `granite` | `ibm-granite/granite-3.3-2b-instruct` | `707f574c62054322f6b5b04b6d075f0a8f05e0f0` | `GraniteForCausalLM` | approximately 5.07 GB |
| `qwen` | `Qwen/Qwen2.5-1.5B-Instruct` | `989aa7980e4cf806f80c7fef2b1adb7bc71aa306` | `Qwen2ForCausalLM` | approximately 3.09 GB |

Both are Apache-2.0 licensed and use safetensors. Qwen is the smaller candidate for the Windows
CPU / 16 GB RAM baseline; actual peak memory, runtime, valid-JSON rate and portfolio quality must
be measured internally. No claim of improved accuracy over Granite has been established.
The [publisher's Qwen model card](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct) documents 1.54B
parameters and structured output support. This preset uses native BF16 weights, not a quantized
runtime. Disk weight size is not a peak-RAM estimate.

Select an existing allowlisted model with:

```powershell
portfolio-analyzer semantic-model-select --workspace .\workspace --model qwen
# Or use --model granite to switch back.
```

This explicit command enables `[profile] model_generation`, selects CPU, sets the profile output
budget to 768, and replaces the model repository/revision/path with its preset. Other configuration
values are preserved, but TOML comments are rewritten. New `semantic-init` configurations still use
deterministic profiles by default. Switching models changes provenance and cache fingerprints;
rerun semantic analysis and reporting, using saved extraction and deterministic analysis.

Adding another model or revision requires a reviewed allowlist change. Repositories requiring
remote code, custom Python or unsafe serialized weights are ineligible.

### Security approval record

The [pinned revision](https://huggingface.co/ibm-granite/granite-3.3-2b-instruct/tree/707f574c62054322f6b5b04b6d075f0a8f05e0f0)
was reviewed on 2026-09-22 using Hugging Face's per-file security status and artifact metadata. All
12 files in the analyzer's allowlist reported an aggregate per-file status of `safe`. For both
safetensors shards, Protect AI reported no findings, JFrog reported that the model does not support
code execution on load, and VirusTotal reported 0/75 and 0/77 detections. Some scanner fields were
`unscanned`, so these third-party results are supporting evidence rather than a security guarantee,
consistent with [Hugging Face's scanner guidance](https://huggingface.co/docs/hub/security-malware).
Reapproval of another model or revision must repeat and document this review.

For Qwen, the pinned model card, Apache-2.0 license, native architecture and tokenizer configuration,
file inventory, small-file SHA-256 hashes, and publisher weight digest were checked on 2026-09-23.
The [pinned file inventory](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct/tree/989aa7980e4cf806f80c7fef2b1adb7bc71aa306)
reported all nine allowlisted files as “Safe”. The [pinned safetensors page](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct/blob/989aa7980e4cf806f80c7fef2b1adb7bc71aa306/model.safetensors)
reported “Safe” and the compiled weight digest. This review did not download or execute the
3.09 GB weights, independently scan them, or benchmark inference; acquisition validates their
bytes against the pinned digest. Treat upstream scan status as supporting evidence only.

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

Existing `semantic.toml` files do not need to be regenerated. A missing `[profile]` section inherits
the safe, fast default `model_generation = false`. Legacy batch and cluster-generation keys are
ignored because schema v7 no longer makes those calls. The optional model execution profile is
optimized for a four-core CPU: `device = "cpu"`, a 12,000-character application IR prompt bound,
four intra-op threads, one inter-op thread, and small task-specific generation ceilings. The
provider explicitly enables the generation key/value cache and greedy single-beam decoding.
GPU-equipped installations can change `device` to `auto` or `cuda` after validating the
environment.

Profile input is capped at 12,000 characters and output at 1,024 tokens; smaller configured limits
are honored. Existing/default 256-token configurations remain at 256 until explicitly changed.
Model presets select 768 tokens to allow several capability descriptions with citations. Quick
mode stays capped at 256. Effective values are recorded in semantic provenance.
Set `[profile] model_generation = true` only when model-authored profiles justify the substantial
CPU inference time. This changes the default from zero model calls to one call per application.
Set `[microsoft] model_generation = true` only when the optional model-authored architecture is
worth the additional portfolio-level generation time.

Model acquisition is unnecessary for the default deterministic path. If either model option is
enabled, install the semantic extras and, during an explicitly approved connected acquisition
window, run:

```powershell
portfolio-analyzer semantic-model-download --workspace .\workspace
```

This command alone imports `huggingface_hub` and calls `snapshot_download()` with the immutable
revision and an exact filename allowlist. It downloads only the model card, configuration,
tokenizer files, safetensors weights, and an index when the selected model is sharded. It does not read application inventory,
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

In deterministic mode these commands neither verify nor load model weights. When model generation
is enabled, the analyzer first verifies manifest identity, revision, inventory, sizes, all file
hashes, model configuration, and safetensors index references. Missing, incomplete, modified, or
unexpected files stop inference. The analyzer never repairs or redownloads a model.

Inference sets `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_DATASETS_OFFLINE=1`, and telemetry
disable flags before importing the ML runtime. Both tokenizer and model receive a local filesystem
path, `local_files_only=True`, and `trust_remote_code=False`; the model also receives
`use_safetensors=True`. No Hugging Face repository ID is passed to inference. The reviewed Granite or Qwen
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

By default the analyzer derives a conservative profile directly from cited application-IR facts,
observed evidence, and owner claims. It records `generation_method = deterministic`, uses no model
input, and defaults uncertain disposition decisions to `investigate`. When
`profile.model_generation = true`, the local model receives the derived IR—not hundreds of raw
per-object prompts—and is called once per application with a compact citation-keyed schema and a
256-token profile ceiling. Narrative summaries and finding descriptions are assembled
deterministically from that structured response.
Cluster names and rationales are derived deterministically from grounded profile features and
require no model call. Architecture synthesis is also deterministic by default. Setting
`microsoft.model_generation = true` enables at most one 768-token portfolio-level architecture call
with a conservative deterministic fallback.
Reports and semantic state distinguish deterministic inspection coverage, synthesis method, and
whether a bounded IR was sent to the model.

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

`semantic-check` verifies the active mode, then evaluates reviewed gold data. In deterministic mode
it performs no model preflight. When either model option is enabled, it verifies and loads the model
offline and tests schema-shaped JSON and evidence-ID preservation. The gold set requires the
stratified 20-application
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
applications. Normal and quick runs print timestamped start/completion events for optional model
loading, deterministic IR construction, application synthesis, checkpoints,
deterministic clustering, architecture synthesis, and final state writing. Every line includes elapsed time for
the preceding step and total run. `--force` deliberately regenerates the selected application's IR
and profile.

### Quick test mode

Use quick mode for an end-to-end smoke test of IR construction, profile synthesis, evidence gating,
clustering, architecture fallback, and reporting:

```powershell
portfolio-analyzer semantic --workspace .\workspace --quick
portfolio-analyzer report --workspace .\workspace --semantic-mode auto
```

For each application quick mode deterministically selects at most five code-bearing objects,
round-robin across
available object types, and inspects every segment of each selected object. It caps execution at a
4,096-token context, 768 global output tokens, 256 profile tokens, 512 optional architecture
tokens, 1,000
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
semantic, prompt and schema versions, synthesis mode, generation settings, run mode and
object-selection limit, deterministic similarity
version/weights/thresholds, and approved service catalog.
Model-enabled runs additionally include the model manifest and inference-library versions.
Compatible application profiles are reused. Deterministic mode never loads a model; model-enabled
mode loads it once per run.

Schema v7 records profile synthesis method, source segments, deterministic application IRs,
deterministic inspection coverage, bounded model-input counts and hashes, production versus quick
mode, checkpoint completion, and the object-selection limit. Older
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

## Behavior profiles (schema v8)

Schema v8 / application IR v2 adds complete extracted object counts and names, redacted root UI
properties, and evidence-linked behavior facts alongside code-only counts. Profiles include observed
behavior, known inputs and outputs, secondary capabilities, purpose provenance/claim IDs, and
classification rationale/evidence IDs. The bounded model payload includes the same inventory and
behavior facts, with omitted counts; model output and input limits remain unchanged. Deterministic
behavior rules assign the primary archetype in both modes; optional model purpose/findings remain
separately identified proposals. No new model or inference runtime is required.

Existing v7 semantic checkpoints are cache-incompatible. Keep the saved extraction/static analysis
and rerun these commands (substitute your actual workspace):

```powershell
portfolio-analyzer semantic --workspace .\workspace
portfolio-analyzer report --workspace .\workspace --semantic-mode require
```

Re-extraction is only needed if the saved extraction lacks relevant form/report definitions. Do not
use quick mode for production acceptance. Review the existing stratified gold set after regenerating;
enter `(none)` in `expected_business_capabilities` to explicitly confirm that no business capability
is established. An empty cell continues to mean unreviewed. This permits evaluating conservative
behavior profiles without inventing business capabilities to satisfy the gold-set gate.


## Independent roles and portfolio redesign

Profiles now persist `roles`, each with a rationale and evidence references. The same deterministic
assessments apply to the default and optional model paths; model limits and defaults are unchanged.
Semantic analysis/schema v10, IR v3 and prompt v9 invalidate older cached profiles. Rerun `semantic`
and then `report --semantic-mode require` against saved extraction snapshots to refresh profiles,
similarity clusters, themes and component mappings. Re-extraction is only needed for missing metadata.

New gold-set templates append optional `expected_roles`. Use pipe- or semicolon-separated labels,
for example `reporting and analytics|batch automation`, or `(none)` after reviewing an app with no
supported roles. Reviewed roles can replace `expected_primary_archetype`; leave the latter blank
when evaluating overlap instead of the legacy category. Existing CSV files remain accepted and
are never overwritten. `semantic-check` reports role macro-F1 and the number of apps with reviewed
roles, and requires at least 0.80 on supplied role labels. Existing capability, citation and coverage
gates still apply. When both legacy categories and roles are supplied, both are evaluated.


Portfolio themes are now discovered from observed overlap, not predefined role solutions. The
semantic state persists `discovered_themes`, and default target components are built from the same
groups. Shared external identities, complete matching definitions and repeated extraction labels
supply transparent grouping features. The default remains offline and deterministic; it does not
claim to infer arbitrary business domains. Optional architecture model generation can interpret
these groups and propose broader alternatives with application-specific evidence. Neither path
requires users to author a taxonomy or solution dictionary. Regenerate semantic state and reports
after upgrading; earlier architecture/theme caches are invalidated by the version change.
