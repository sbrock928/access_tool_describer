# Local Qwen Interpretation

V2 has one production interpretation path. Deterministic extraction first establishes every
technical fact and dependency; the pinned local Qwen model then explains those facts at logical-unit,
application, and portfolio levels. There is no model-free semantic path, remote provider, model
selector, embedding service, vector database, or reduced product mode.

## Approved model

| Field | Approved value |
| --- | --- |
| Repository | `Qwen/Qwen2.5-0.5B-Instruct` |
| Revision | `7ae557604adf67be50417f59c2c2f167def9a775` |
| Architecture | `Qwen2ForCausalLM` |
| Weight format | native safetensors |
| License | Apache-2.0 |

The [publisher model card](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct) describes the model;
the [pinned file inventory](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct/tree/7ae557604adf67be50417f59c2c2f167def9a775)
is the version reviewed for this analyzer. Upstream scan labels are supporting evidence, not a
security guarantee. The analyzer compiles an exact filename, size, and SHA-256 allowlist; approving
another repository or revision requires a code and security review.

## Install and acquire

Install the local-model dependency group through the organization's approved package mirror. The
runtime never installs packages:

```powershell
python -m pip install -c requirements\semantic-py313.lock -e ".[semantic]"
```

During an explicitly approved connected acquisition window, run:

```powershell
portfolio-analyzer model-download --destination C:\Models\Qwen2.5-0.5B-Instruct
```

This is the only command allowed to access the network. It downloads only allowlisted model,
configuration, tokenizer, license/model-card, and safetensors files from the immutable revision. It
does not read inventory, source applications, evidence, prompts, or analysis state. It rejects an
unsafe or unexpected file and publishes a manifest only after every expected size and digest
matches.

Verify an existing directory without network access:

```powershell
portfolio-analyzer model-verify --model-dir C:\Models\Qwen2.5-0.5B-Instruct
```

The verifier recognizes manifests emitted by the immediately preceding approved downloader, which
included the acquisition timestamp in its manifest digest. It canonicalizes that identity in
memory only after the legacy digest validates and every allowlisted file passes the current size,
SHA-256, configuration, and safety checks; it does not weaken or skip artifact verification.

`analyze` independently performs the same verification before publication. It never downloads or
repairs a missing or modified file.

## Configure a model directory

Resolution order is:

1. `analyze --model-dir PATH`;
2. `[qwen].path` in `workspace/analyzer.toml`; then
3. `ACCESS_ANALYZER_MODEL_DIR`.

An explicit CLI path is useful for controlled runs. A workspace configuration can be:

```toml
schema_version = "access-analyzer-config-v2"

[qwen]
path = "C:/Models/Qwen2.5-0.5B-Instruct"
device = "cpu"
cpu_threads = 4
cpu_interop_threads = 1
```

Relative configuration paths resolve below the workspace. Supported devices are `cpu`, `cuda`,
`mps`, and `auto`; validate non-CPU environments under local change control. Missing configuration
is an error. The analyzer never chooses or downloads a model implicitly.

## Offline inference boundary

Before loading, the provider sets Hugging Face/Transformers offline and telemetry-disable
environment variables. Tokenizer and model loaders receive the verified local directory,
`local_files_only=True`, and `trust_remote_code=False`; model loading also requires safetensors.
Unexpected remote-code declarations, custom pipelines, executable artifacts, symlinks, missing
files, extra files, or wrong hashes stop analysis.

The model loads once per `analyze` run. Generation is deterministic: greedy single-beam decoding,
sampling disabled, a fixed seed, a fixed 1,024-token output reservation, and code-owned prompt and
schema versions. Those values, tokenizer/model identity, and verified file manifest participate in
cache fingerprints.

Prompts are call-local and are never persisted. Sanitized SQL, VBA, captions, owner claims, and
object definitions are delimited untrusted data; they cannot request tools or override the schema.
Model output is parsed as JSON and strict-Pydantic-validated. There is no execution of generated
text.

## Stage 1: logical units

Interpretation operates on complete semantic-bearing units rather than a lossy application index:

- each QueryDef carries SQL, sanitized DAO metadata, parsed targets, operations, and graph facts;
- each VBA unit is one procedure or logical batch plus required module declarations;
- each form/report carries layout and controls, RecordSource, event map, graph facts, and referenced
  code-behind without double counting;
- each macro is one unit.

Tables and connections remain deterministic context and do not receive separate model calls. Every
semantic-bearing source must end in one interpretation, a schema-valid abstention, or an explicit
skip/failure reason.

Small related units may share one request only when the tokenizer-measured prompt plus reserved
output fits the fixed 8,192-token logical-unit operational ceiling. The model's separately enforced
reviewed context ceiling remains 32,768 tokens. Oversized units split at procedure, statement,
control, or other logical boundaries. Every chunk is interpreted and reduced; the pipeline neither
silently truncates a definition nor drops its tail. If an indivisible input cannot fit, the unit
fails explicitly.

Each output claim cites supplied evidence IDs. A citation to an unknown, cross-application,
aggregate-only, or secret-derived identifier is invalid. Technical identifiers, endpoints,
operations, objects, and dependency edges must already exist in deterministic evidence.

## Stage 2: application synthesis

Application synthesis receives every valid Stage-1 result, the authoritative dependency graph and
datasource facts, coverage, and separately tagged owner claims. If the direct synthesis prompt is
larger than its fixed 16,384-token operational ceiling, the pipeline reduces tokenizer-fitted
groups and then synthesizes those complete rollups—it does not silently omit units.

Qwen may describe purpose, workflows, capabilities, modernization concerns, and uncertainty. It
cannot rewrite technical evidence. Claims must resolve transitively through unit results to concrete
evidence in the same application. Owner statements remain claims, not observations. A valid
`unknown` or abstention is a completed result; system-calculated confidence stays at medium or below
until a human review decision.

For a syntactically or schema-invalid response, the provider makes exactly one repair request with
concise validation feedback. A second invalid result marks the unit or application incomplete. No
heuristic purpose, role, architecture, or narrative is substituted.

## Portfolio interpretation

After every eligible application is current, deterministic code creates overlapping candidates for:

- a shared resolved endpoint;
- a shared external object;
- a shared resolved file;
- an exact normalized code duplicate; and
- semantic-profile overlap after a reviewed Qwen 0.5B gold set freezes a versioned threshold.

These are review candidates, not clusters. Membership is not transitive and applications may appear
in several candidates. Unresolved datasources and matching object names alone cannot merge
applications.

Qwen receives only supplied candidates and their evidence/profile references. It may label,
explain, prioritize, and propose modernization, but it cannot add an application, endpoint, graph
edge, or unsupported member. Reuse, consolidation, target architecture, migration sequencing, and
retirement are cited proposals rather than observed facts. A retirement proposal is invalid without
an owner lifecycle claim.

If a targeted application run leaves any eligible application stale or incomplete, the current
portfolio interpretation is invalidated rather than retained misleadingly.

## Failure, coverage, and reuse

Fatal model verification, required-unit interpretation, application synthesis, or portfolio
synthesis failure blocks normal reporting. A Stage-1 failure is retained with its reason; successful
evidence is not converted into an unsupported narrative. Optional definition-extraction gaps may
produce partial evidence coverage while still allowing qualified analysis.

Cache reuse requires the same sanitized inputs, artifact/evidence fingerprint, prompt and output
schemas, tokenizer/model manifest, and fixed generation policy. Changes invalidate only affected
applications until a new complete portfolio can be synthesized.

`report --allow-partial` can publish completed applications for diagnosis. It lists every omitted
application/unit, suppresses portfolio-wide absence claims, and does not make incomplete semantic
state look complete.

## Quality and human review

Run the checked-in quality policy against a separately reviewed gold set:

```powershell
portfolio-analyzer quality-check --workspace .\workspace --gold-set .\reviewed-gold.csv
```

The UTF-8 CSV has exactly the following reviewed fields (additional columns are ignored, but these
three are required):

```text
application_id,expected_capabilities,expected_related_application_ids
```

Use `;` or `|` between multiple capabilities or related application IDs. Every application ID must
exist in the current immutable analysis, and each application may appear only once. Every related
application must also have its own reviewed row. Among those rows, a listed pair is a reviewed
positive and every unlisted pair is a reviewed negative, so a calibration set needs at least one of
each. The analyzer does not create or infer a gold artifact.

The command computes the six-decimal Jaccard semantic score for every pair of reviewed profiles and
records a diagnostic calibrated recommendation. The recommendation maximizes pair F1; ties prefer
higher precision, then higher recall, then the higher threshold. Until a threshold is reviewed and
frozen under a new policy version, the check fails and semantic-profile candidates remain disabled.
Capability recall and the eventual frozen-threshold pair F1 must clear the fixed 0.70 release
gates. A candidate set inconsistent with the active policy also fails the check.

The broader automated suite separately tests schema validity, citation closure, valid abstention,
and secret leakage. Semantic-profile candidates are currently disabled because no reviewed Qwen
0.5B gold set has frozen a threshold. Calibration is read-only: `quality-check` neither changes
generation policy nor exposes an operator tuning control. A recommendation is evidence for a
separately reviewed future policy
change, which still requires an approved calibration artifact and a new policy version.

Excel review decisions remain overlays. Import them with:

```powershell
portfolio-analyzer import-review --workspace .\workspace --workbook .\review-decisions.xlsx
```

The workbook must carry the originating analysis fingerprint. Unknown, stale, duplicate, or
conflicting proposal decisions are rejected. A decision can carry to a later run only if both the
stable proposal identity and its evidence identity still match.
