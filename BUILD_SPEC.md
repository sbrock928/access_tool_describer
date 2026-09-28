# Build Specification

## Product contract

Access Portfolio Analyzer V2 is a Python 3.13 batch analyzer for Microsoft Access portfolios. Its
deliverable is sanitized, traceable evidence and evidence-cited interpretation—not execution of an
application, query, macro, or model-authored instruction.

The only production flow is:

```text
Inventory
-> verified local staging
-> static Access extraction
-> ApplicationEvidenceBundle
-> logical-unit Qwen interpretation
-> application synthesis
-> deterministic overlapping portfolio candidates
-> Qwen portfolio interpretation
-> PortfolioReportModel
-> HTML / Excel / PDF / CSV
```

The system accepts only `.accdb` and `.mdb` primary artifacts, case-insensitively. Other rows are
visible exclusions and copy zero bytes. A mixed portfolio continues; a portfolio with no eligible
Access application exits unsuccessfully.

## Identity and provenance

- `application_id` is the authoritative inventory/EUC identity. Several Access rows with the same
  ID form one application.
- Each source row receives a collision-safe `artifact_id` derived from the application and canonical
  source identity. Same-named and byte-identical artifacts do not collapse.
- The content SHA-256 is the artifact version, not its identity.
- Object, connection, target, data-access, dependency, and evidence IDs are artifact-scoped.
- Evidence IDs include artifact identity and version plus a sanitized canonical fact payload. They
  never include a workspace path, raw connection string, credential, or raw secret hash.
- Hashes establish integrity and provenance. They do not authenticate data against an attacker who
  can rewrite both content and manifests.

`FILE_NAME` must match the basename and suffix of `FULLPATH`. The stager rejects symlinks,
traversal, workspace-as-source, unsafe destinations, and size/hash changes during copying. It copies
only inventory-listed eligible artifacts. Verified copies are atomically published beneath both
their artifact identity and content SHA-256, so restaging a changed source cannot overwrite a binary
referenced by an older immutable run. Only the stager reads original source locations.

## Extraction contract

Extraction has two independent lanes.

### Mandatory DAO metadata

The extractor opens each verified staged copy through ACE/DAO `DBEngine` in read-only mode. It must
not use `Access.Application.CurrentDb`, execute queries, open recordsets, refresh links, enumerate
remote fields, invoke macros/VBA, or supply credentials.

It enumerates all QueryDefs, including `~sq*`, and retains SQL, raw DAO type, normalized query kind,
sanitized connection identity, `ReturnsRecords`, `ODBCTimeout`, parameters, and readable static
properties. It enumerates TableDef attributes, connection identity, source table name, and local
field metadata. Property and object failures are isolated and counted. Database-open failure or
failure to enumerate a core QueryDefs/TableDefs collection fails that application; an individual
property omission makes evidence coverage partial.

Password-protected or encrypted files fail closed with no credential prompt or stored password.

### Gated `SaveAsText`

Forms, reports, modules, and macros use a second disposable copy. Before Access UI opens, the
launcher must attest to a non-low macro policy and outbound-network blocking; the worker verifies
its non-administrator token and the copy hash, then sets
`AllowBypassKey=True` via DAO, reopen DAO, and verify that setting persisted. Access then opens
hidden while Shift is held and `AutomationSecurity` is set as defense in depth.

The worker has a hard timeout, parent-side Shift release, process cleanup, path-contained export
names, strict decoding, immediate sanitization, and no credential-dialog path. The mutated
disposable hash is recorded separately. Raw exports are removed and never become persisted
evidence. If any attestation or directly verified gate is unavailable, this lane is skipped and coverage is terminal partial; no
less-safe fallback is allowed.

Optional Access libraries require an explicit app-to-library manifest and reviewed SHA-256 pins.
Default behavior uses none. Missing hashes, ambiguous mappings, filename collisions, and transitive
discovery are rejected.

## Canonical contracts

V2 uses strict Pydantic 2 models with `extra="forbid"` and explicit schema versions:

- `ApplicationEvidenceBundle` owns artifact facts, objects, QueryDefs, TableDefs, connections,
  data access, dependency edges, file dependencies, evidence, coverage, and unresolved references.
- `UnitInterpretation` and `ApplicationProfile` hold model interpretation. Every claim cites
  concrete evidence and records confidence/review state.
- `PortfolioCandidate` holds deterministic overlapping membership; `PortfolioInterpretation` holds
  Qwen-authored labels, explanations, priorities, and proposals.
- `PortfolioReportModel` is the sole renderer input.

Static code owns operations, endpoints, targets, and graph edges. `QueryDef.Connect` and
`TableDef.Connect` are authoritative connection observations; SQL determines operation and target
object, never a server or database. Explicit SQL qualifiers and connection defaults remain
separately cited. Linked-table aliases resolve through `SourceTableName`. A query with no external
connection evidence remains local.

The connection parser is finite-state and handles quoted/braced semicolons, escaping, duplicates,
prefixes, and malformed input. It persists only allowlisted lineage fields. Credential and unknown
values are redacted; errors contain safe codes and offsets rather than raw input.

DSN resolution reads registry metadata only under the actual worker identity and matching DAO
bitness. It never calls an ODBC connection API or follows File DSNs, network configuration,
environment indirection, or opaque aliases. Results distinguish resolved, unresolved, ambiguous,
wrong-bitness, permission-denied, unsupported-File-DSN, and malformed states. Resolved values are
analysis-host evidence, not proof of production configuration.

## Interpretation contract

The only supported model is `Qwen/Qwen2.5-0.5B-Instruct`, revision
`7ae557604adf67be50417f59c2c2f167def9a775`. Loading is local-only, safetensors-only, offline,
telemetry-disabled, and uses `trust_remote_code=False`. There is no hosted fallback, runtime model
selection, prompt persistence, or deterministic semantic substitute.

Stage 1 interprets each semantic-bearing logical unit exactly once: a QueryDef with SQL and DAO
metadata; a VBA procedure/batch with required declarations; a form/report with layout, RecordSource,
events, graph facts, and code-behind; or one macro. Tables and connections are deterministic
context. Oversized input splits only at logical boundaries and all chunks are reduced without
silent truncation.

Stage 2 synthesizes the application from every valid unit result plus the authoritative graph,
datasource evidence, coverage, and separately tagged owner claims. A model may select existing IDs,
but cannot create technical facts. Citation closure must resolve transitively to concrete evidence.
One schema-repair retry is permitted. A second invalid response fails the unit or synthesis; a valid
abstention is complete. Unreviewed model confidence cannot exceed medium.

Portfolio candidates overlap and are deterministic: shared resolved endpoints, shared external
objects, shared files, exact normalized code, and semantic-profile similarity under one frozen
policy threshold. Qwen may interpret candidates but cannot alter membership. Modernization,
architecture, sequencing, reuse, and consolidation are proposals, not observations. Retirement
requires an owner lifecycle claim.

`quality-check` scores every pair in an operator-supplied reviewed gold set, derives a deterministic
best-F1 threshold recommendation, and verifies that the frozen generation threshold attains the same
maximum F1. Calibration is diagnostic only and cannot mutate or tune runtime policy.

## State and publication

Content-addressed, versioned JSON payloads and small manifests are the system of record. A writer
fsyncs and verifies payloads before atomically replacing a manifest; orphan payloads are ignored.
Mutating commands hold an exclusive workspace lock. Unknown or older workspace schemas are rejected
before writes with instructions to use a fresh workspace.

The digest chain is staged binary -> extraction -> evidence bundle -> interpretation -> report.
Stable serialization excludes timestamps, machine paths, and secrets from content fingerprints.
Targeted force operations preserve unrelated applications. A changed extraction invalidates only
that application's downstream state. Portfolio results are recomputed only when every eligible
application is current; otherwise the current portfolio is invalidated.

Normal reporting requires current completed analysis. `--allow-partial` selects completed
applications only, records every omission and reason, suppresses portfolio-wide absence claims, and
publishes a separate watermarked run. Report files live under `reports/runs/<run_id>/`; a partial
run never replaces `reports/latest.json`, which points only to a fully validated complete run.

## Command surface

```text
portfolio-analyzer stage --inventory PATH --workspace PATH
portfolio-analyzer extract --workspace PATH [--application ID] [--force]
portfolio-analyzer analyze --workspace PATH [--application ID] [--force] [--model-dir PATH]
portfolio-analyzer report --workspace PATH [--allow-partial]

portfolio-analyzer model-download --destination PATH
portfolio-analyzer model-verify --model-dir PATH
portfolio-analyzer quality-check --workspace PATH --gold-set PATH
portfolio-analyzer import-review --workspace PATH --workbook PATH
```

Only `model-download` may use the network. Extraction, analysis, evaluation, review import, and
reporting are offline.

## Acceptance gates

The release gate is full pytest plus Ruff and strict mypy. Tests cover Access-only scope,
same-name/identical-byte artifacts, core and property-level DAO failures, startup suppression,
outbound-network denial, Shift cleanup, path containment, QueryDef metadata, linked and pass-through
lineage, DSN states, malformed/credential-bearing connections, dynamic unresolved references,
citation closure, one repair retry, state crash/tamper boundaries, targeted invalidation,
cross-format ID/count parity, formula protection, CSP, leak scanning, partial watermarking, and
retention of the previous complete publication after a failed run.

See [docs/ARCHITECTURE_REFACTOR_V2.md](docs/ARCHITECTURE_REFACTOR_V2.md) for the detailed audit and
decision record.
