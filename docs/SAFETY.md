# Safety and Trust Boundaries

The analyzer treats inventory paths, Access databases, exported definitions, owner descriptions,
model files, model output, and review workbooks as untrusted input. Static evidence is never
permission to execute an application or contact one of its dependencies.

## Source-copy boundary

Only `stage` reads an original `FULLPATH`, and only to copy an eligible inventory-listed `.accdb` or
`.mdb`. Before copying it validates the declared filename, suffix, source type, symlink status,
source/workspace separation, and contained destination. It records source provenance, byte size, and
SHA-256, then verifies the local copy before publication. A size/hash race fails staging.

No extractor, parser, Qwen prompt builder, portfolio process, or reporter receives an original
source path. If staging or verification fails, that artifact is not opened and there is no fallback
to its source location. Unsupported primary formats copy zero bytes. Sibling folders are never
mirrored recursively.

`extract` verifies that a staged path is within the workspace and still matches its manifest
immediately before use. A mismatch is treated as tampering or corruption and requires a new stage
run.

## Non-execution Access extraction

The mandatory lane opens a verified staged copy directly with ACE/DAO `DBEngine` in read-only mode.
It reads QueryDef and TableDef metadata only. It never intentionally calls query execution,
`OpenRecordset`, `RefreshLink`, Access `CurrentDb`, forms, reports, macros, VBA, ODBC connection
APIs, or linked/pass-through remote field enumeration. It does not accept credentials or dismiss a
credential dialog. Protected, encrypted, corrupt, or incompatible databases fail closed.

Property reads are isolated. A bad optional property produces explicit partial coverage; a failure
to open DAO or enumerate a core QueryDefs/TableDefs collection fails the application. Every
QueryDef, including Access-internal `~sq*` names, is accounted for as succeeded or failed.

Connection strings are sanitized as they enter canonical evidence. Only allowlisted lineage such as
driver, DSN, server, catalog/database, schema, and object identity is retained. Passwords, user IDs,
tokens, account keys, and all unknown-key values are discarded. Raw connection strings and hashes
of raw secret values are not persisted. Parser errors expose only a safe code and offset.

DSN resolution is a read-only registry lookup under the extraction worker's actual identity and
matching DAO bitness. It does not load a driver or attempt a connection. It does not follow File
DSNs, environment variables, network files, or opaque indirection. Registry results describe the
analysis machine and account; they do not authenticate a production endpoint.

## Gated definition export and residual startup risk

DAO cannot export form, report, macro, or module text. Those definitions require Access UI
automation and therefore carry a residual startup-code risk. The optional `SaveAsText` lane runs
only when its launcher supplies explicit attestations for the externally enforced controls and the
worker verifies the controls it can inspect directly:

- the launcher attests that a non-low macro policy and outbound-network blocking are active, and
  the worker independently verifies that its current Windows token is non-administrator;
- a second disposable copy matches the pre-mutation hash;
- `AllowBypassKey=True` is written through DAO, DAO is closed and reopened, and persistence is
  verified;
- Access opens hidden while Shift is held and `AutomationSecurity` is set to force-disable macros;
- a hard timeout, low-privilege identity, parent-side Shift release, process-tree cleanup, and no
  credential-dialog path are active.

This defense is intentionally layered. Microsoft documents
[`AutomationSecurity`](https://learn.microsoft.com/en-us/office/vba/api/Access.Application.AutomationSecurity)
and notes limitations under low macro security; Microsoft also documents the
[`AllowBypassKey` property](https://support.microsoft.com/en-us/access/allowbypasskey-property),
which controls whether Shift can bypass startup behavior. Neither control alone proves safe startup.

If an attestation or directly verified gate is unavailable, or an export fails, Access is not
reopened with weaker settings. DAO
metadata remains usable but definition coverage is terminal partial. The worker records the
post-mutation disposable hash separately; it never represents that file as original evidence.

Export filenames come from safe object IDs and are containment-checked. Text is decoded without
silent replacement, sanitized immediately, and the raw export is deleted. A timeout releases Shift
from the parent and terminates the isolated process tree.

Optional libraries are disabled by default. An operator-provided manifest must map an application
to an explicit reviewed file and SHA-256. Missing pins, changed content, ambiguous mappings,
filename collisions, and transitive discovery are rejected. A correct hash proves identity, not
safety.

## Local Qwen boundary

The only inference model is `Qwen/Qwen2.5-1.5B-Instruct` at immutable revision
`989aa7980e4cf806f80c7fef2b1adb7bc71aa306`. `model-download` is the only network-capable command
and accepts an explicit destination. It downloads an exact file allowlist, rejects symlinks,
unexpected files, remote-code declarations, custom executable code, and pickle-capable weights,
then verifies code-reviewed sizes and SHA-256 digests before publishing `model_manifest.json`.

`model-verify` and `analyze` repeat local verification. Inference uses a local filesystem path,
`local_files_only=True`, `trust_remote_code=False`, safetensors-only loading, deterministic
generation, and offline/telemetry-disabled environment variables. There is no hosted provider,
automatic repair, background download, API key, or prompt persistence. A missing, extra, modified,
or wrong-revision file stops analysis.

SQL, VBA, captions, descriptions, owner claims, and model responses are data. They cannot invoke a
tool, shell, query, macro, or network call. JSON is parsed and validated against strict schemas;
there is no `eval`, `exec`, or dynamic import. Model-authored technical IDs not present in the
deterministic bundle are rejected. Every claim must close to concrete sanitized evidence, and model
confidence is capped at medium until human review.

## State, reporting, and review safety

Workspace payloads are strict versioned JSON, serialized canonically, content-addressed, and
hash-chained. Writers place and fsync payloads first, verify them, then atomically replace the small
manifest last. Orphan payloads are not current state. All mutating commands use an exclusive
workspace lock; unknown workspace schemas are rejected before writes.

HTML escapes data and uses a restrictive offline Content Security Policy. Excel-bound values that
could be formulas are prefixed safely. PDF/CSV renderers use the same sanitized report model.
Before a report is published, the canonical report-model JSON and rendered HTML, CSV, XLSX, and PDF
artifacts are leak-scanned, and every format must preserve the same record IDs and counts. The
application does not currently emit a persistent log file; arbitrary workspace JSON and orphan
payloads are outside this publication scan. Only a complete validated run can update
`reports/latest.json`; partial output is watermarked and isolated.

Review import accepts only decisions tied to the originating analysis fingerprint and known stable
proposal/evidence IDs. It rejects stale, unknown, duplicate, conflicting, or formula-bearing input.
Owner claims, observed facts, model interpretations, and human decisions remain distinct records.

## Network policy summary

| Operation | Network permitted | Notes |
| --- | --- | --- |
| `stage` | No analyzer-initiated network calls | May read an operator-declared source path only to copy it. |
| `extract` | No outbound traffic | The optional Access worker requires a launcher attestation that blocking is active before UI open. |
| `analyze` | No | Local verified model only. |
| `report`, `quality-check`, `import-review` | No | Consume current local state only. |
| `model-verify` | No | Verifies local files. |
| `model-download` | Yes | Explicit acquisition window; never reads portfolio data. |

Hash manifests provide integrity and reproducibility, not protection from an attacker who controls
the workspace, executable environment, and manifests together. Run the analyzer under a dedicated,
least-privilege Windows identity with OS firewall controls and organization-approved dependencies.
