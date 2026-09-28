# Architecture Refactor V2

## Status and decision

This document is the architecture decision record for the reset of Access Tool Describer. Its
current-state audit records the historical pre-V2 baseline that motivated the reset; later sections
define the implemented V2 replacement architecture.

The reset deliberately does not preserve generated staging state, extraction snapshots, analysis
state, semantic state, caches, SQLite rows, or report schemas. A new run starts from the inventory
and source applications. The only supported inference model for this version is the approved,
pinned `Qwen/Qwen2.5-1.5B-Instruct` model running locally.

The governing design is:

```text
verified Access artifacts
        -> deterministic extraction and normalization
        -> evidence-cited local-Qwen interpretation
        -> deterministic portfolio candidate discovery
        -> evidence-cited portfolio interpretation
        -> reports generated from canonical contracts
```

Technical facts remain deterministic. Qwen interprets those facts; it does not create server names,
database names, object names, operations, paths, or dependency edges.

## Audited Historical Baseline (Pre-V2)

### Actual pipeline

The present pipeline is split across several independently persisted representations:

1. `inventory.loader.load_inventory` reads every workbook row with the required headings. It does
   not validate that the listed primary is an Access database or that `FILE_NAME` agrees with the
   basename in `FULLPATH`.
2. `ArtifactStager.stage_application_bundle` recursively copies every regular file below each
   listed file's parent directory. It excludes common Office lock files and symlinks, but the
   configured supporting-file extensions and depth are not used. Non-Access primaries are copied
   and reported as staged successfully.
3. Staging records source path, staged path, size, timestamp, and SHA-256. Extraction rechecks
   status, containment, source/staged separation, existence, and SHA-256 before proceeding. This is
   the strongest part of the current design and must remain.
4. The extraction command filters staged primaries to `.accdb` and `.mdb`. Other staged primaries
   are silently absent from extraction while remaining in application and coverage reporting.
5. `WindowsAccessExtractor` creates a disposable working directory, copies the primary, copies all
   uniquely named databases from `shared_libraries`, opens the database through an Access UI COM
   instance, enumerates DAO tables and queries, enumerates references, and calls `SaveAsText` for
   forms, reports, macros, and modules.
6. DAO table extraction stores only `Name`, `Connect`, and `SourceTableName`. DAO query extraction
   stores only `Name` and `SQL` and excludes names beginning with `~sq`. Query type, pass-through
   connection, returns-records behavior, ODBC timeout, and other relevant metadata are discarded.
7. `analysis.application.analyze_application` lexically classifies query SQL, resolves exact linked
   table names, parses linked-table connections, detects paths, and applies VBA signal regexes. It
   does not inspect QueryDef connection metadata, produce a complete Access object graph, or
   distinguish an unresolved object reference from an external datasource reliably.
8. Extraction snapshots, static-analysis state, semantic state, review state, SQLite rows, and
   report files overlap as partial systems of record. SQLite stores inventory and staged artifacts
   but is not the source used by downstream analysis or reporting.
9. The semantic pipeline converts each extracted object into a `SemanticSource` using
   `definition or properties`. An object with a definition therefore loses all properties. It then
   reduces inspected sources to counters, identifiers, signals, and string signatures in a bounded
   application IR. Optional model generation receives that compressed IR once per application.
10. Reports reconstruct the portfolio by joining inventory, staging, extraction, static analysis,
    semantic state, and review state. This makes it possible for a fact to exist in extraction yet
    be absent from model input and reports.

### Information lineage and loss

| Stage | Information retained | Information lost or weakened |
| --- | --- | --- |
| Inventory | Original workbook values and owner description | Access eligibility and filename/path consistency |
| Staging | Source/staged paths, timestamps, size, SHA-256, status | Explicit unsupported-format status; stable per-source bundle identity |
| Table extraction | Name, raw connection, source table name | DAO attributes, link kind, saved-password flag, structured connection identity |
| Query extraction | Name and SQL | Type, Connect, ReturnsRecords, ODBCTimeout, pass-through identity, hidden/internal classification |
| Definition export | Form, report, macro, and module text | Per-object failure coverage; encoding certainty; safe filename containment |
| Static analysis | SQL operations, some targets, linked-table endpoints, path/VBA signals | Query connection lineage, local object edges, UI bindings, complete dependency graph |
| Semantic sources | Definition or properties, bounded segments | Definition and metadata together; authoritative pass-through context |
| Application IR | Counts, names, signals, selected findings, datasource strings | Complete meaningful units, direct source citations for most indexes, business context in source |
| Reports | Broad portfolio outputs and coverage summaries | First-class pass-through, DSN resolution, unresolved lineage, and consistently resolvable evidence |

### Safety strengths to preserve

- The source path is not passed to an extractor.
- Extraction requires a successfully staged artifact within the controlled workspace.
- The staged artifact's SHA-256 is checked immediately before extraction.
- Extraction runs in a disposable child process with a timeout.
- QueryDefs are inspected as definitions and are not intentionally executed.
- Local model loading is pinned, integrity checked, safetensors-only, offline, and uses
  `trust_remote_code=False` and `local_files_only=True`.
- Reports distinguish owner claims from observed evidence in several important places.

## Problems

### Pass-through datasource loss

`QueryDef.Connect` is authoritative for a pass-through query's connection, but it is never
captured. A query such as `SELECT * FROM dbo.Deal` consequently becomes an `Unknown` datasource
with no server or database. Inferring those values from `dbo.Deal` would be wrong; the information
must come from the connection metadata.

Adding properties to the current generic `ExtractedObject` is not sufficient. The semantic source
construction chooses SQL instead of properties whenever SQL exists, and the current `Datasource`
cannot express DSN, driver, connection kind, connection provenance, resolution status, or source
artifact. Its merge key can collapse different unresolved connections that happen to reference the
same object.

### Connection parsing and credential handling

The current parser splits on semicolons with a regular expression. It does not correctly handle all
quoted values, escaped braces, duplicate keys, or bare connection specifiers. A value such as
`PWD="abc;SERVER=secretpart"` can be misparsed so that part of the password is emitted as a server.
On parse failure the current redactor can return the original text. Platform inference also sees
secret values and heuristically infers Oracle from a DSN name.

Raw TableDef connection strings are persisted in extraction snapshots. Generic semantic redaction
is a later, separate implementation and does not cover every connection key. Definition text and
exception messages can independently contain credentials. Sanitization must be a shared boundary,
not a report-specific repair.

### Access startup residual risk

The current exporter opens the database through `Access.Application.OpenCurrentDatabase`, sets
`AutomationSecurity=3`, and simulates a global Shift key. This does not prove that startup code was
suppressed:

- Access documents that `AutomationSecurityForceDisable` has no effect when macro security is Low.
- A source database can set `AllowBypassKey=False`, disabling the Shift bypass.
- If `OpenCurrentDatabase` hangs and the worker is killed, the child's key-up `finally` block may
  never run.

The documentation currently describes isolation as a mitigation, but an AutoExec macro that runs
successfully need not produce an extraction error. Opening through Access UI therefore cannot be
the mandatory path for authoritative DAO metadata.

### Working-bundle and staging scope

Staging recursively copies arbitrary sibling files even though extraction deliberately constructs a
minimal bundle and does not use those staged siblings. This needlessly copies spreadsheets,
executables, secrets, and large directories. Conversely, the extractor copies every unique curated
Access library rather than only an explicitly approved dependency.

A curated library with the same filename as the primary can overwrite the verified primary in the
working directory on Windows. More generally, two inventory rows for one EUC from different source
roots can map the same relative filename to the same staged path and invalidate one another's
recorded hashes.

### Incomplete failure and coverage semantics

One exception while reading DAO metadata stops the remaining tables and queries. One exception in a
`SaveAsText` container stops all later objects in that container. A failure after the Access instance
is created is commonly returned as an empty extraction with warnings; coverage can then call it
`complete_with_warnings` and semantic analysis can proceed on no useful evidence.

Absence of findings is meaningful only when the relevant extraction and analysis coverage is
complete. Coverage must be recorded per artifact, object category, and semantic unit, with fatal,
partial, and complete states.

### Semantic-quality bottleneck

The current deterministic semantic layer tries to infer business role, archetype, purpose,
capabilities, disposition, and architecture while local model generation is optional. The model,
when enabled, receives a bounded index rather than the relevant query, procedure, form/report, and
macro evidence. One application-level call cannot recover information that was removed before the
call.

The current architecture also permits deterministic post-processing to overwrite parts of model
output. This makes generation provenance unclear and maintains two competing semantic systems.

### Reporting and maintainability

Datasource, dependency, semantic, and coverage data are spread across several schemas. Reporting
modules reconstruct relationships and contain presentation, portfolio logic, and compatibility
behavior. HTML, Excel, CSV, and PDF remain useful deliverables, but their current inputs and
information hierarchy should not be retained.

Tests cover the current staging hash boundary, basic SQL parsing, linked tables, and local model
security. They do not cover actual QueryDef metadata, pass-through lineage, DSNs, unsupported
primaries, startup suppression, per-object COM failure isolation, or end-to-end secret leakage.
Some tests explicitly require recursive sibling copying and the obsolete one-call/model-free
semantic behavior.

## Deterministic Analysis Decision

The labels in this matrix are decisions, not compatibility promises.

| Subsystem | Decision | Rationale and target behavior |
| --- | --- | --- |
| Source-only inventory path | **KEEP** | Only staging may read a source path, and only to copy an eligible primary. |
| Local staging, provenance, and SHA-256 | **KEEP** | This is the core safety and reproducibility boundary; strengthen it with artifact-scoped paths and atomic manifests. |
| Recursive sibling bundle mirroring | **REMOVE** | Supporting files are dependencies, not independently copied or analyzed applications. |
| Access-only primary validation | **KEEP** | Make `.accdb` and `.mdb` the explicit v2 allowlist and report every unsupported row as skipped. |
| DAO QueryDef and TableDef extraction | **KEEP** | Expand into typed, per-object facts and isolate property failures. |
| `SaveAsText` definition export | **SIMPLIFY** | Retain as a gated, optional second path on a hardened disposable copy; never make DAO success depend on it. |
| SQL operation and target parsing | **KEEP** | READ/INSERT/UPDATE/DELETE/CREATE/EXECUTE targets are technical facts, with unresolved results preserved. |
| VBA procedures and explicit Access actions | **KEEP** | Procedure boundaries, literal `DoCmd` targets, automation APIs, and static file paths are observed facts. |
| Form/report root properties | **KEEP** | RecordSource and other relevant root properties belong in deterministic evidence and graph edges. |
| Connection parsing and redaction | **SIMPLIFY** | Replace the regex split and multiple redactors with one finite-state parser and shared sanitizer. |
| Registry DSN metadata resolution | **KEEP** | Add a metadata-only resolver with explicit resolved, partial, ambiguous, and unresolved outcomes. |
| Datasource normalization | **SIMPLIFY** | Replace the overloaded `Datasource` row with connection, endpoint, and data-access records. |
| Dependency graph construction | **KEEP** | All edges are observed or unresolved deterministic facts; Qwen cannot add edges. |
| Evidence IDs and coverage | **KEEP** | Make evidence an application-bundle registry and cite IDs from every interpretation. |
| Code similarity and shared-resource candidate discovery | **SIMPLIFY** | Deterministic signals propose candidates; they do not make consolidation decisions. |
| Business purpose, workflows, and capability naming | **REPLACE WITH QWEN** | These are interpretations over cited facts and owner claims. |
| Roles, archetypes, and business-domain classification | **REPLACE WITH QWEN** | Remove the competing deterministic semantic profile. |
| Modernization narrative and target architecture | **REPLACE WITH QWEN** | Generate proposals from cited application and portfolio evidence, subject to validation and review. |
| Deterministic/model semantic duality | **REMOVE** | A production analysis always uses the supported local Qwen model; technical evidence remains usable if interpretation fails. |
| Granite and generic model selection | **REMOVE** | One approved Qwen model and one provider path are sufficient for this version. |
| Quick/production and report semantic modes | **REMOVE** | Use one production workflow; tests inject a fake provider rather than create a second product mode. |
| Legacy semantic schemas and migrations | **REMOVE** | Generated state is disposable and will be regenerated. |
| SQLite as a partial secondary store | **REMOVE** | Atomic, artifact-scoped JSON contracts are the system of record for this batch workflow. |
| HTML, Excel, CSV, and PDF formats | **KEEP** | Retain the useful outputs, but rebuild them from canonical contracts with focused renderers. |

## Proposed Architecture

### Target pipeline

```text
inventory workbook
    |
    v
classify rows: Access primary or explicit inventory exception
    |
    v
copy eligible primary -> atomic artifact directory -> verify SHA-256
    |
    +-----------------------------+
    |                             |
    v                             v
mandatory direct DAO          optional definition export
read-only metadata            separately copied and hardened DB
    |                          AllowBypassKey=True, then Access UI
    +-------------+---------------+
                  v
normalized ApplicationEvidenceBundle
  objects + SQL facts + connections + graph + evidence + coverage
                  |
                  v
local Qwen logical-unit interpretation
                  |
                  v
local Qwen application synthesis
                  |
                  v
deterministic portfolio candidate discovery
                  |
                  v
local Qwen portfolio interpretation
                  |
                  v
HTML / Excel / CSV / PDF from the same canonical results
```

There is no semantic sidecar pipeline. `analyze` performs deterministic normalization and the local
Qwen stages needed for a production result.

### Canonical domain models

The names below express responsibilities; implementation may split them into several cohesive
modules.

#### Identity and provenance

- `ApplicationIdentity` identifies the inventory EUC and retains owner-supplied claims separately.
- `ArtifactManifest` identifies one inventory-listed Access file with a stable `artifact_id`, source
  provenance, staged relative path, size, source timestamp, SHA-256, status, format, and manifest
  version.
- Every object ID is namespaced by `artifact_id`. Two Access files in one EUC may both contain
  `qryReport` without becoming the same object.
- `InventoryException` retains unsupported, invalid, missing, and failed inventory rows without
  treating them as applications available for semantic analysis.

#### Extracted Access facts

- `AccessQueryEvidence` contains object ID, name, complete SQL, raw DAO type code, normalized query
  kind, `ReturnsRecords`, `ODBCTimeout`, hidden/internal classification, optional sanitized
  connection ID, property-level warnings, and evidence IDs.
- `AccessTableEvidence` contains object ID, name, local/linked/system classification, raw DAO
  attributes, linked source name, saved-password flag, optional sanitized connection ID, and
  evidence IDs. Local field and relation metadata may be included when it is read without touching
  external sources; linked-table recordsets and refresh operations are forbidden.
- `AccessFormEvidence` and `AccessReportEvidence` contain the `SaveAsText` definition when available,
  parsed root properties such as RecordSource, related code-behind units, and coverage.
- `AccessModuleEvidence`, `VbaProcedureEvidence`, and `AccessMacroEvidence` retain complete bounded
  logical units, explicit calls/actions, and evidence IDs.
- `AccessReferenceEvidence` records reference name, path identity after path policy is applied, GUID,
  broken status, and provenance. A reference is not proof that code executed.

All DAO numeric values are preserved even when the normalizer does not recognize them. Normalized
enums never replace the observed value.

#### Connections and data access

- `ConnectionEvidence` represents an observed connection declaration. It contains connection kind,
  safe driver/provider, DSN, server, database, file identity where allowed, a redacted summary,
  provenance (`QueryDef.Connect`, `TableDef.Connect`, or observed VBA), resolution status,
  resolution provenance, and warnings. The raw credential-bearing string is never serialized.
- `DatasourceEndpoint` is a normalized non-secret endpoint identity. It is separate from how an
  application connects to it.
- `DataAccess` links a source Access object to a local object, external endpoint/object, or unresolved
  target. It contains operation, scope, schema/object identity, connection ID, and evidence IDs.
- Distinct DSNs or connection declarations remain distinct even if they resolve to the same
  endpoint. Endpoint-level reporting may group them while retaining every observed connection.

#### Graph and evidence

- `DependencyNode` represents an application, artifact, Access object, external endpoint/object,
  file, library, or unresolved target.
- `DependencyEdge` represents an observed relation such as query reads table, query calls query,
  form binds query, VBA invokes saved query, application references file, or pass-through query
  updates external object. It includes operation, provenance, confidence, and evidence IDs.
- `EvidenceRecord` contains a stable ID derived from artifact and source location, the sanitized
  observed fact, fact kind, and confidence. Model interpretation is not part of an observed evidence
  ID.
- `ExtractionCoverage` records attempted, successful, failed, and unavailable counts by object
  category and distinguishes `complete`, `partial`, and `failed`.

#### Canonical bundle and interpretations

`ApplicationEvidenceBundle` is the stable handoff between deterministic analysis and Qwen. It
contains identity, artifact manifests, typed Access objects, connections, endpoints, data accesses,
graph nodes/edges, evidence registry, extraction warnings, unresolved references, and coverage.
Reporting and model input are projections of this bundle; neither reconstructs missing facts from
unrelated state files.

`UnitInterpretation`, `ApplicationProfile`, and `PortfolioFinding` are explicitly model-authored.
They contain evidence IDs, uncertainty, generation provenance, and validation status. Owner claims
remain separate from both observations and model interpretations.

The four categories displayed throughout the product are:

```text
OBSERVED FACT
OWNER-PROVIDED CLAIM
MODEL INTERPRETATION
UNRESOLVED / UNKNOWN
```

### Deterministic graph rules

- A local query reference resolves only against artifact-scoped local tables and queries.
- A query reference to a linked table inherits that TableDef connection and external source name.
- Every pass-through SQL target inherits its QueryDef connection. Server and database never come
  from the SQL identifier.
- An unknown local SQL name becomes an unresolved object edge, not an external datasource guess.
- Form and report RecordSource values produce edges to saved queries/tables or parsed SQL targets.
- Literal `DoCmd.OpenQuery`, `OpenForm`, `OpenReport`, and `RunMacro` calls produce object edges.
- Dynamic target construction produces an unresolved edge with the observed expression location.
- File paths and external automation produce dependencies, not independently analyzed applications.
- Qwen may describe graph patterns but cannot add, delete, or retarget an edge.

## Security Boundary

### Phase permissions

| Phase | May read original source | May read staged Access bytes | Network | Important restrictions |
| --- | --- | --- | --- | --- |
| Inventory | No | No | Forbidden | Reads only the inventory workbook supplied by the operator. |
| Staging | Yes, copy only | Writes and verifies | Forbidden | Only eligible `.accdb`/`.mdb` primaries; no recursive sibling crawl. |
| Direct DAO extraction | No | Verified disposable copy | Enforced forbidden | Read-only metadata; no query execution, recordsets, link refresh, or ODBC calls. |
| Definition export | No | Separately hardened disposable copy | Enforced forbidden | Gated Access UI worker; no business credentials; startup bypass verified first. |
| Normalization | No | No | Forbidden | Consumes extracted facts; sanitizes before persistence. |
| Model acquisition | No | No | Explicitly allowed | Only pinned approved Qwen files from the approved repository/revision. |
| Qwen inference | No | No | Enforced offline | Sanitized structured evidence only; no hosted fallback, telemetry, tools, or APIs. |
| Portfolio/reporting | No | No | Forbidden | Consumes canonical bundles and validated interpretations only. |

The staging process is the only component permitted to dereference or open
`InventoryRecord.filepath`; inventory ingestion may retain the path as provenance, but no later
phase may use it as a fallback. Unsupported inventory rows are classified from inventory strings
and are not copied. Staging uses an atomic temporary target, verifies the completed target, records
a SHA-256, and publishes an artifact manifest only after success. Per-artifact directories prevent
same-name files from different source roots from colliding.

### Direct DAO metadata extraction

Mandatory metadata extraction uses the ACE/DAO DBEngine directly, not
`Access.Application.CurrentDb`. It opens a byte-verified disposable copy read-only. Provider and
process bitness must be compatible with the installed Access Database Engine; incompatibility is a
fatal extraction error, not permission to use the source or a less-safe fallback.

The DAO path reads static properties only. It never calls `Execute`, `OpenRecordset`, `RefreshLink`,
or other operations that can run a query or contact an external datasource. Property reads that
cannot be shown safe for linked or pass-through objects are excluded from the foundational version.
Each property and object is isolated so one COM failure does not suppress later metadata.

Opening any untrusted binary parser input retains residual vulnerability risk. DAO therefore still
runs in the isolated, non-privileged, no-network worker against a disposable copy.

### Gated `SaveAsText` export

Forms, reports, modules, and macros require Access UI automation for `SaveAsText`. This path is
optional and cannot invalidate successful DAO metadata.

1. Create a second byte-verified copy dedicated to definition export.
2. Use DAO, without Access UI, to set or create `AllowBypassKey=True` on that disposable copy.
3. Close and release every DAO handle, reopen with DAO, verify the value persisted, then close and
   release it again. Record the hardened copy's distinct hash and the transformation provenance.
4. Start a clean Access instance in a noninteractive analysis account with no production
   credentials, trusted locations, or unapproved add-ins. Verify that its macro-security policy is
   not Low; failure to establish that profile is a failed gate.
5. Set and verify `AutomationSecurity=3` immediately before open as defense in depth.
6. Hold Shift through `OpenCurrentDatabase`, export definitions only, and never open a form/report,
   invoke a macro, compile/run VBA, or execute a QueryDef.
7. Export into a contained temporary directory using generated safe filenames rather than Access
   object names as paths. Detect encoding explicitly, sanitize content, and delete raw temporary
   files after normalization.
8. Close the database and terminate the dedicated process tree. The parent guarantees a Shift
   key-up on success, exception, and forced timeout and verifies that no child Access process
   remains.

If any bypass-hardening or verification step fails, definition coverage is `partial` and the UI open
is not attempted. Even after hardening, UI automation retains residual risk from Access parsing,
environmental add-ins, and platform defects. Network isolation, a disposable account/profile, and
the startup-marker integration tests are mandatory controls, not optional deployment advice.

### Curated libraries

DAO metadata extraction uses no libraries. Definition export defaults to no injected libraries. A
library may be added only through an operator-reviewed application-to-library manifest containing
an immutable hash and purpose. The extractor verifies the hash, rejects a filename collision with
the primary or another library, rejects symlinks, does not search transitively, and records both the
configured/verified set and the set actually copied into the disposable bundle. A skipped safety
gate or failed copy therefore never appears as a successful injection. Library and primary copies
are checked against their previously pinned digest (and the primary's pinned size), never against a
second read of a mutable source. A matching hash proves identity, not that the library is safe;
approval includes a separate security review.

### Secret handling

The connection parser receives the raw string only in memory. It emits a structured non-secret
identity and redacted summary. Raw connection strings, raw DSN registry values, and hashes of those
secret-bearing values are not persisted. Hashing a password-bearing string would create an offline
password-verification oracle.

Connection sanitization handles braced and quoted values, escaped delimiters, duplicate keys, and
bare ODBC/ISAM prefixes. Only an allowlist of lineage values is retained. Known secret fields and
all unknown-key values are redacted. Malformed input returns a safe error code and offset, never the
input string.

A second generic sanitizer protects arbitrary VBA, SQL, exported definitions, paths, URI
credentials, and exception text before any content enters prompts, logs, reports, or semantic state.
A final leak scanner checks all serialized model inputs and report formats. Server, database,
schema, DSN, driver, and object identity remain visible unless a separate policy requires their
redaction.

The staged database is necessarily an exact local copy and can itself contain credentials. It is
kept inside the protected workspace. Derived evidence and semantic artifacts do not reproduce those
credentials.

### Hash manifests

Each stage validates its upstream content digest and writes its own manifest atomically. Content
digests use canonical encoding and stable ordering and include schema, extractor/parser/sanitizer
versions, artifact IDs, and relevant safe configuration. Timestamps and machine-specific absolute
paths are metadata, not part of deterministic content digests.

The chain detects accidental corruption and stale or mismatched derived state. It is not an
authenticity mechanism when an attacker can rewrite both content and manifests; signatures or an
external trust store would be required for that threat model. All manifest paths are relative,
contained, and rejected if a symlink or traversal escapes the workspace.

## Datasource Strategy

### Connection normalization

The parser recognizes connection specifiers separately from case-insensitive key/value pairs and
preserves duplicate-key warnings. It produces declared fields and never infers a platform from a
password, username, or DSN name. Platform classification uses safe driver/provider metadata or a
clear file/database specifier. A server/database pair without supporting driver/provider evidence
can remain platform `unknown`.

Useful key aliases are normalized without losing provenance, including driver/provider, DSN,
server/address/host, database/initial catalog, and DBQ/file identity. Allowlisted non-secret values
are preserved as observed; the analyzer does not resolve DNS names, expand network aliases, or
authenticate.

### QueryDef handling

Every QueryDef is inventoried, including `~sq` objects. Internal/hidden objects are marked and may
be excluded from high-level narrative, but they are not silently lost. The extractor captures at
minimum:

- `Name`;
- complete `SQL`;
- numeric `Type` plus normalized kind;
- sanitized `Connect` identity;
- `ReturnsRecords` when applicable;
- `ODBCTimeout` when available;
- property-level errors and coverage.

Known DAO types include select, crosstab, delete, update, append, make-table, DDL, pass-through,
set/union, pass-through bulk, compound, procedure, and generic action. Unknown numeric values remain
available. `ReturnsRecords=False` does not by itself determine the write operation; operation comes
from SQL and query type evidence.

For pass-through queries, connection identity comes from `QueryDef.Connect`. SQL parsing supplies
schema/object and operation. If SQL cannot be parsed, the connection remains known and the target is
unresolved. If the connection cannot be parsed or resolved, SQL object names do not become guessed
servers or databases.

### TableDef handling

Every TableDef is counted. DAO attributes, rather than only a name prefix, distinguish local,
linked ODBC, linked non-ODBC, hidden, and system objects and record whether a saved-password flag is
present. System objects may be excluded from business interpretation while remaining in coverage.

For a linked table, `TableDef.Connect` identifies the connection and `SourceTableName` identifies
the external object. Local tables have no external datasource. Excel, text, Access, and other file
links become typed file dependencies; their files are not independently analyzed as applications.

### Registry-only DSN resolution

DSN resolution is metadata-only and runs on the Windows extraction host under the same account and
bitness as Access:

- User DSNs are read from the current worker account's HKCU ODBC metadata.
- System DSNs are read from the HKLM registry view matching Access bitness.
- Driver registration is checked in the matching view without loading the driver.
- Only safe, recognized driver/server/database fields are retained; all other values are redacted.
- No ODBC driver, Driver Manager connection function, DNS lookup, socket, login test, or remote
  datasource is used.

Resolution status is explicit: `not_applicable`, `declared_inline`, `resolved`,
`resolved_partial`, `unresolved_not_found`, `unresolved_permission`,
`unresolved_driver_bitness`, or `ambiguous`. Conflicting candidates are not guessed. File DSNs are
reported as unresolved by policy and are not opened or followed, including when they name a network
path.

Declared connection fields and registry-derived defaults are retained separately. An explicit
non-secret field in the connection string takes precedence in the normalized view, while both
provenances remain visible. DSN resolution is labeled analysis-host metadata and must not be
presented as proof that the production user's DSN has the same configuration.

### Endpoint identity and merging

Connections are never merged merely because object names match. Normalized endpoint grouping
requires compatible non-secret platform, server, and database identities. Schema/object and
operation describe a `DataAccess`, not the endpoint itself.

An unresolved DSN remains part of connection identity so two unresolved DSNs remain distinct.
Several connections may point to one endpoint, and several accesses may use one connection. Reports
show both levels. Confidence attaches separately to connection resolution, object parsing, and
operation classification rather than one overloaded datasource confidence.

## Local-Qwen interpretation

### One supported provider

The production provider supports only the approved pinned Qwen model. Preserve the current strong
controls: immutable revision, exact file allowlist and SHA-256 values, safetensors-only weights,
architecture validation, `trust_remote_code=False`, `local_files_only=True`, disabled telemetry,
offline inference, no API keys, and no hosted fallback.

Model acquisition is an explicit provisioning operation and the only model phase permitted network
access. A normal analysis fails clearly if the approved model is unavailable or fails verification.

### Stage 1: logical-unit interpretation

Qwen receives meaningful structured units rather than an identifier-frequency index:

- query metadata, SQL, deterministic operations, datasource identity, and graph edges;
- one VBA procedure plus module declarations and deterministic calls/signals;
- a form/report's root properties, RecordSource facts, related code-behind procedures, and edges;
- a macro definition plus parsed actions and targets.

The output schema includes purpose, workflow actions, business entities, reads, writes, external
dependencies, called objects, generated outputs, user interaction, important terms, uncertainty,
and evidence IDs. Technical fields in model output are validated against the bundle. Unknown or
invented identifiers and citations are rejected rather than added to deterministic state.

Each VBA procedure is a stable logical unit with required module declarations supplied as context.
Oversized definitions are divided only at logical boundaries and reduced without omission. Validated
outputs are cached by sanitized content, output schema, prompt policy, and model policy; prompts are
not persisted.

### Stage 2: application synthesis

Application synthesis receives all validated unit interpretations, authoritative deterministic
lineage/coverage, and owner claims in separate fields. It produces business purpose, major
workflows, supported capabilities, user interaction, important outputs, uncertainties, and
modernization considerations. It cannot modify technical facts.

No unit is silently dropped to meet a context limit. If all unit summaries do not fit, deterministic
packing creates cited intermediate rollups and the final synthesis includes every rollup plus the
authoritative application facts. Coverage records every included unit. A failed unit or synthesis
is reported as partial/failed; the system does not invent a deterministic business narrative as a
fallback.

### Portfolio analysis

Deterministic logic proposes portfolio candidates based on shared resolved endpoints, external
resources, graph patterns, normalized Qwen capabilities/workflows, and explainable code similarity.
It does not declare consolidation, reuse, or migration decisions. Qwen interprets those candidates
into cited overlap, reuse, modernization, and migration findings. Singleton applications remain
ungrouped unless evidence supports a portfolio relationship.

The semantic threshold remains a frozen versioned generation policy. Read-only quality evaluation
scores every pair in the supplied reviewed gold set and derives the best-F1 recommendation with
deterministic conservative tie-breaking. The frozen threshold must attain that maximum F1; the
evaluation records a recommendation but never mutates runtime policy or manufactures a gold set.

The local model's size makes object/application decomposition necessary. One Qwen call per
application is not enough for a large estate, while one call per trivial object wastes runtime.
Logical-unit batching with deterministic caching is the chosen balance; quality and complete
coverage take priority over minimum runtime.

## Reporting Strategy

HTML remains the primary navigable evidence report, Excel the detailed analyst deliverable, CSV the
normalized interchange format, and PDF the concise executive brief. Each renderer consumes the
same bundle/profile/portfolio contracts and contains no analysis logic.

### Application view

Every application view answers:

- what the application appears to do and whether that is observed, claimed, interpreted, or
  unresolved;
- major workflows and the Access objects that support them;
- complete Access object inventory and extraction coverage;
- queries by normalized type, with a dedicated pass-through view;
- reads and writes by local object, linked table, and pass-through target;
- driver, DSN, server, database, schema, object, operation, provenance, and resolution status;
- forms, reports, macros, VBA procedures, files, references, and explicit automation;
- graph relationships with evidence drill-down;
- uncertainties, failed extraction units, secret-redaction markers, and manual-review questions;
- modernization interpretation with citations, never blended into observed facts.

### Portfolio view

Portfolio reporting emphasizes shared resolved endpoints, shared files/resources, overlapping
workflows and capabilities, explainable similarity, high-dependency applications, potential reusable
components, modernization groupings, migration risk, unresolved datasource lineage, and analysis
coverage. It does not group applications solely by a common object name, generic technology, or an
unresolved connection.

### Evidence and report safety

Every displayed interpretation resolves to evidence records or owner claim IDs available in the
same report dataset. Executive pages summarize without removing access to detail. HTML remains
self-contained and offline. Spreadsheet formula escaping, HTML escaping/CSP, bounded PDF content,
and final credential-canary scanning remain release gates.

Reporting modules are split by format and presentation model. They do not parse Access definitions,
infer dependencies, or join legacy checkpoints.

## State and CLI strategy

The normal operator workflow is:

```powershell
portfolio-analyzer stage --inventory <inventory.xlsx> --workspace <workspace>
portfolio-analyzer extract --workspace <workspace>
portfolio-analyzer analyze --workspace <workspace>
portfolio-analyzer report --workspace <workspace>
```

- `stage` classifies inventory rows, copies only supported Access primaries, and writes atomic
  artifact manifests plus explicit inventory exceptions.
- `extract` performs mandatory DAO extraction and gated definition export and publishes one
  artifact-scoped extraction result at a time.
- `analyze` constructs canonical application bundles, runs complete local-Qwen unit and application
  interpretation, and updates deterministic portfolio candidates and Qwen portfolio findings.
- `report` validates current manifests and renders all formats. There is no semantic report mode;
  partial or failed interpretation is shown as such.

Model installation/verification remains a separate administrative provisioning command. There is
no model selector. Tests and developer benchmarks inject a fake provider or run focused internal
stages; they do not create a second persisted product mode.

State is artifact-scoped and content-addressed:

```text
inventory manifest and exceptions
    -> staged artifact manifests
    -> extraction facts and coverage
    -> canonical application evidence bundles
    -> unit interpretations and application profiles
    -> portfolio findings
    -> report manifest and outputs
```

Each result records upstream digests and the exact implementation/configuration versions that affect
its content. A changed artifact, extractor, parser, sanitizer, prompt, model revision, or safe DSN
metadata invalidates only affected downstream results. Completed applications are checkpointed
atomically. Malformed or incompatible state is preserved for diagnosis and regenerated rather than
migrated.

SQLite is removed from the v2 batch path because it is currently incomplete and adds a second system
of record without providing downstream query value. If later scale requirements justify a database,
it will be a projection rebuilt from canonical manifests rather than a peer source of truth.

## Implementation Plan

### Phase 1 — Architecture decision and safety fixtures

- Adopt this document as the v2 contract.
- Add fixture builders/fakes for QueryDefs, TableDefs, startup behavior, connection strings, DSNs,
  and output secret canaries before deleting legacy behavior.
- Document the isolated Windows worker account, firewall, Access profile, and model-acquisition
  boundary.

### Phase 2 — Access-only staging and canonical provenance

- Enforce the `.accdb`/`.mdb` primary allowlist and explicit inventory exceptions.
- Replace recursive bundle mirroring with atomic per-artifact staging.
- Add stable artifact IDs, contained relative paths, canonical manifests, and upstream hash
  validation.
- Remove SQLite writes and legacy directory/state migration from the new path.

### Phase 3 — DAO metadata and datasource foundation

- Introduce typed QueryDef, TableDef, connection, endpoint, data-access, evidence, and coverage
  models.
- Implement mandatory direct read-only DAO extraction with per-property isolation.
- Implement the finite-state connection sanitizer and registry-only DSN resolver.
- Normalize linked-table, pass-through, local, file, and unresolved lineage without semantic logic.

### Phase 4 — Gated definitions and dependency graph

- Implement the separately hardened `SaveAsText` worker and explicit library manifest.
- Parse UI root properties, VBA procedures/actions, macros, paths, and SQL into evidence and graph
  edges.
- Publish `ApplicationEvidenceBundle` as the only deterministic handoff.
- Add complete/partial/failed coverage and eliminate empty-success extraction.

### Phase 5 — Simplified local-Qwen analysis

- Collapse model configuration to the pinned Qwen provider and existing offline integrity controls.
- Implement structured logical-unit interpretation, caching, citation validation, and failure
  isolation.
- Implement application synthesis and bounded cited rollups without silent source omission.
- Remove deterministic semantic profiles, Granite, model selection, quick mode, semantic reporting
  modes, and legacy semantic compatibility.

### Phase 6 — Portfolio and reports

- Build deterministic portfolio candidates from canonical endpoints, graph edges, similarity, and
  validated semantic outputs.
- Generate cited Qwen portfolio findings and modernization proposals.
- Rebuild focused HTML, Excel, CSV, and PDF renderers from canonical contracts.
- Remove obsolete capability, recommendation, architecture, and report reconstruction paths after
  v2 parity and quality gates pass.

Each phase is reviewable and leaves prior production commands untouched until its replacement path
has its safety and correctness tests. Once the complete v2 path passes acceptance, obsolete generated
state and compatibility code are deleted rather than migrated.

## Acceptance gates

### Scope and staging

- A mixed inventory stages only `.accdb` and `.mdb`; every other row appears as an explicit
  unsupported exception and no bytes are copied from its source.
- `FILE_NAME`/`FULLPATH` disagreement is explicit and cannot select a different source silently.
- Uppercase Access suffixes work. Compiled/project formats are explicitly unsupported rather than
  silently omitted.
- Same-named primaries from different source roots receive distinct artifact directories and IDs.
- The source is never passed to DAO, COM, parsers, Qwen, or reporting and remains unchanged.
- Staged or manifest tampering blocks extraction; there is no source fallback.

### DAO and definition extraction

- The QueryDef accounting invariant is `enumerated == successful + failed`, including `~sq` names.
- One failing QueryDef/TableDef property does not suppress later objects or the other collection.
- Fakes fail the test if extraction calls `Execute`, `OpenRecordset`, `RefreshLink`, or another
  execution-capable operation.
- A fatal DAO open is `failed`, not an empty complete result. An unavailable form export is partial
  while DAO query/table facts remain usable.
- AutoExec and startup-form fixtures attempt to create marker files; no marker is created during
  `SaveAsText` extraction.
- A worker profile with Low macro security, an unverifiable `AllowBypassKey`, or a failed
  post-write bypass check prevents the Access UI from opening and leaves definition coverage
  partial.
- The worker emits no DNS/TCP traffic when linked and pass-through fixtures name monitored
  unreachable endpoints, and no credential prompt is accepted.
- Shift is released after successful open, exception, and forced timeout; no orphan Access process
  remains.
- Access object names containing separators, reserved names, or traversal text cannot escape the
  temporary export directory.
- Export encoding is detected without silent replacement; undecodable content produces a scoped
  error.
- A library cannot collide with the primary; unapproved, changed, or transitively discovered
  libraries are rejected.

### Datasource correctness

- A DSN-less SQL Server pass-through SELECT produces pass-through type, READ operation, SQL Server
  driver/platform, declared server/database, parsed schema/object, and `QueryDef.Connect`
  provenance.
- The equivalent UPDATE produces UPDATE, not READ based solely on `ReturnsRecords`.
- A DSN resolved uniquely from the correct account/view records safe server/database/driver and
  resolution provenance without loading the driver.
- Missing, permission-denied, wrong-bitness, File DSN, and conflicting cases have distinct explicit
  statuses and are never guessed.
- Inline safe fields override DSN defaults in the normalized view while both observations remain.
- A normal Access query referencing a local table produces a local graph edge and no external
  datasource.
- Query-to-query, query-to-linked-table, form/report-to-RecordSource, VBA-to-saved-query, and
  application-to-file relationships are artifact-scoped and evidence-cited.
- Two unresolved DSNs targeting an identically named object remain distinct connections.
- Equivalent connections may share a normalized endpoint without losing either declaration or its
  evidence.

### Secret safety

- Canary secrets in quoted/braced values, duplicate keys, malformed connection strings, registry
  values, VBA, SQL, URI credentials, and COM errors are absent from bundles, manifests, prompts,
  checkpoints, stdout/logs, HTML, CSV, XLSX, and PDF.
- The malformed value `PWD="abc;SERVER=secretpart"` cannot emit `secretpart` as a server.
- Safe driver, DSN, server, database, schema, and object identity remain available.
- Persisted connection fingerprints contain only sanitized lineage; no raw secret or raw-secret hash
  is stored.

### Semantic grounding and quality

- Every interpreted logical unit reports its source evidence IDs and coverage.
- Hallucinated identifiers, operations, endpoints, and citations are rejected and cannot modify the
  deterministic bundle or graph.
- Every application synthesis covers all successful units, directly or through cited rollups; no
  context-limit truncation is silent.
- A unit failure yields partial coverage and an uncertainty, not an invented fallback narrative.
- Representative gold fixtures cover normal and action queries, linked SQL tables, pass-through
  SELECT/UPDATE, DSN-less and DSN connections, query chains, VBA saved-query calls, bound forms and
  reports, external Excel/file dependencies, dynamic unresolved SQL, and mixed workflows.
- Human review measures business-purpose/workflow usefulness as well as citation correctness and
  datasource accuracy.

### State and reporting

- Tampering with a staged artifact, extraction result, canonical bundle, or semantic input blocks
  downstream reuse.
- Canonical content digests are stable across key order and repeated runs and change when relevant
  artifact content, implementation version, sanitizer policy, prompt, model revision, or safe DSN
  lineage changes.
- Interrupted writes retain the prior valid manifest; partial files are never treated as current.
- Every interpretation displayed in a report resolves to an included evidence or claim record.
- Application reports expose pass-through and linked-table lineage, resolution status, operation,
  provenance, and unresolved targets without requiring raw JSON inspection.
- Portfolio totals reconcile with per-application coverage and never count an unsupported inventory
  row as an analyzed application.
- Report-level leak scanning and formula/HTML/PDF safety tests pass for every format.

## Explicit assumptions

- The v2 primary format allowlist is `.accdb` and `.mdb`. Other Access formats are reported as
  unsupported until a separate extraction and coverage policy is approved.
- Generated v1 state has no compatibility requirement and will be regenerated.
- Qwen2.5-1.5B-Instruct is available locally for production analysis; production business
  interpretation does not have a model-free fallback.
- Model acquisition is the only normal network-enabled phase. Extraction, inference, analysis,
  portfolio processing, and reporting are enforced offline.
- Registry DSN resolution describes the analysis account and machine. It is useful static evidence,
  not proof of production configuration or connectivity.
- Definition export can be partial. DAO query/table facts are the mandatory minimum extraction.
- Hash manifests provide integrity and provenance, not authenticity against a malicious actor who
  controls the workspace.
- Explicitly approved libraries are exceptional inputs with their own provenance; filename matching
  alone never selects a library.
- HTML, Excel, CSV, and PDF remain deliverables, but their previous schemas and layouts are not
  compatibility contracts.
