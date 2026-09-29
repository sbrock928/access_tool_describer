# Troubleshooting

V2 fails closed at security and lineage boundaries. Do not bypass a hash, schema, coverage, model,
or report validation error; correct the input or start a fresh run.

## Workspace is rejected

V2 does not migrate generated state from earlier architectures. If a command reports a legacy or
unknown workspace schema, choose a new empty workspace and begin with `stage`. Preserve the old
workspace separately if it is needed for audit; do not copy its generated manifests into V2.

Only one mutating command may hold a workspace at a time. If a lock is active, wait for the running
command to finish. After an abnormal process termination, first confirm that no analyzer or Access
worker remains before investigating a stale lock. Do not delete a lock owned by a live process.

Content-addressed payloads without a manifest may remain after a crash. They are intentional
orphans and are ignored. A manifest is the visibility boundary; the last published run remains
current.

## Staging fails or excludes a row

- Only `.accdb` and `.mdb` primaries are eligible, case-insensitively. `.accde`, `.mde`, `.adp`,
  Excel, CSV, and every other suffix are recorded as `SKIPPED_UNSUPPORTED_FORMAT` and copy zero
  bytes. This is not a failure in a mixed inventory, but an inventory with no eligible Access file
  exits nonzero.
- `FILE_NAME` must exactly identify the basename and suffix of `FULLPATH`. Correct the inventory
  rather than renaming a staged copy.
- Symlinks, traversal, a source within the workspace, or a destination escaping the staging root
  are rejected.
- A size or SHA-256 mismatch means the source changed while it was copied or the staged copy was
  altered. Stabilize the source and run `stage` again; never edit a staged artifact.
- Only the listed primary is copied. A missing sibling spreadsheet or library is not a staging bug;
  static evidence records dependencies without opening them. A required Access library must appear
  in the separately reviewed, SHA-pinned library manifest.

Staging processes every row and records individual failures before returning nonzero. Inspect the
stage manifest rather than assuming an early error prevented later rows from being attempted.

## Extraction is unavailable or incomplete

Real Access extraction requires Windows, a bitness-compatible ACE/DAO installation, and the Windows
runtime dependencies. On macOS or Linux it is expected to fail safely. See
[WINDOWS_SETUP.md](WINDOWS_SETUP.md).

A failure to open DAO or enumerate the core QueryDefs/TableDefs collections fails the application.
Common causes include encryption, password protection, corruption, file-format incompatibility,
permissions, or ACE bitness mismatch. The analyzer never prompts for or stores a database password.

An individual property error or unavailable form/report/module/macro export produces partial
coverage. Query/table metadata remains usable, but reports must qualify conclusions. The optional
definition lane is skipped when launcher attestations for macro/network policy are absent or when
the worker cannot directly verify its token, persisted `AllowBypassKey=True`, hidden startup
controls, and cleanup guarantees. Configure the
dedicated extraction account and firewall rather than weakening this gate.

Every artifact runs in a disposable child process with a 300-second hard limit. A timeout records
the artifact failure, releases Shift from the parent, terminates the process tree, checkpoints other
applications, and returns nonzero after the requested scope is processed. Investigate the last
recorded extraction progress and the specific database; do not open the source in place.

For a single corrected application, use:

```powershell
portfolio-analyzer extract --workspace .\workspace --application APP-123 --force
```

This preserves compatible unrelated application snapshots and invalidates only the selected
application's downstream state.

## Datasource lineage is unresolved

An unresolved datasource is a valid static result, not permission to connect. Check the evidence
provenance:

- `QueryDef.Connect` is the connection source for a pass-through QueryDef.
- `TableDef.Connect` and `SourceTableName` define linked-table aliases.
- A normal local query has no external datasource merely because an SQL object name resembles a
  remote schema.
- Dynamic VBA connection construction remains unresolved unless its literal components can be
  proven statically.

DSN resolution reads the registry under the actual worker account and Access/DAO bitness. A DSN may
be `unresolved`, `ambiguous`, `wrong_bitness`, `permission_denied`, `unsupported_file_dsn`, or
`malformed`. Correct the analysis-host registry/account configuration if appropriate. The analyzer
will not follow a File DSN, load a driver, test a connection, or infer an endpoint from a target
object name.

If a parser reports malformed connection text, it intentionally omits the raw value. Use the safe
error code/offset and inspect the source under approved controls. Never paste credential-bearing
connection strings into logs or issue reports.

## Model directory cannot be resolved

Configure the local directory in this order:

```powershell
portfolio-analyzer analyze --workspace .\workspace --model-dir C:\Models\Qwen2.5-0.5B-Instruct --verbose
```

or set `[qwen].path` in `workspace/analyzer.toml`, or set `ACCESS_ANALYZER_MODEL_DIR`. A relative
configuration path resolves below the workspace. There is no automatic download.

If local verification reports a missing manifest, unexpected file, wrong revision, size/hash
mismatch, unsafe weight format, or remote-code declaration, quarantine that directory. Acquire a
new directory during an approved connected window and verify it:

```powershell
portfolio-analyzer model-download --destination C:\Models\Qwen2.5-0.5B-Instruct-new
portfolio-analyzer model-verify --model-dir C:\Models\Qwen2.5-0.5B-Instruct-new
```

`model-download` is the only network-capable command. `analyze` never repairs model content or uses
a hosted fallback.

## Qwen analysis fails or is slow

The model loads once, then analyzes every semantic-bearing logical unit before application and
portfolio synthesis. CPU inference can therefore take substantial time on a large estate. The
process checkpoints applications so a later compatible invocation can reuse them. `--force`
deliberately recomputes selected current evidence.

The model supports 32,768 tokens, but production inference does not attempt that full window on a
CPU. Logical-unit requests are capped at 8,192 total tokens and synthesis requests at 16,384,
including reserved output. Larger requests split before generation. In verbose mode, a heartbeat
with zero generated tokens means the model is still performing first-token prefill; after this
policy change, a logical-unit prompt near 30,000 tokens indicates that the running process predates
the current code and should be stopped and restarted.

An invalid JSON/schema response receives exactly one repair attempt. A second invalid response is
an explicit required-unit or synthesis failure. A token-budget error means an indivisible logical
source or synthesis input could not fit without unsafe truncation; the pipeline does not omit it.
Preserve the failure reason for review instead of increasing hidden limits or inventing a narrative.

A cache miss is normal on the first run or after a prompt/model policy change. Only a compact,
schema-valid, evidence-closed draft is cached. Two `malformed JSON` messages followed by the next
cache miss mean the previous unit exhausted its initial attempt and one repair; the malformed value
was intentionally discarded. Verbose output now reports `hit_output_limit=true` when truncation is
the likely cause without printing the generated text.

A schema-valid `unknown` or abstention is successful analysis. A failed required unit is not.
Normal reporting stays blocked until the application is current. A targeted analysis recomputes
portfolio findings only when every eligible application is current; otherwise it removes the stale
portfolio result.

For a corrected application:

```powershell
portfolio-analyzer analyze --workspace .\workspace --application APP-123 --force
```

The exact model revision, prompt/schema policy, generation parameters, bundle fingerprint, and
sanitization policy participate in cache identity. Changes correctly prevent incompatible reuse.

## Report is refused

Normal `report` requires current completed staging, extraction, application analysis, and portfolio
synthesis. It refuses a missing, failed, stale, in-progress, or tampered upstream result. Correct the
earliest failing phase first.

When a scoped diagnostic is required before full recovery:

```powershell
portfolio-analyzer report --workspace .\workspace --allow-partial
```

The output includes only completed applications, records every omission and reason, suppresses
portfolio-wide absence claims, and is watermarked. It does not replace `reports/latest.json`.

A report-publication error can be caused by a secret canary, connection-string pattern, spreadsheet
formula hazard, inconsistent record IDs/counts, unreadable format, or artifact hash mismatch. The
publisher releases no subset of formats. Fix the canonical evidence/report input and publish a new
run. Do not hand-edit an immutable report directory.

## Review import or quality check fails

`import-review` requires the workbook's originating analysis fingerprint and known stable proposal
and evidence IDs. Regenerate the workbook after a new analysis. Remove duplicate or conflicting
decisions and never enter a formula in a review field. Decisions from changed proposals/evidence do
not carry forward.

`quality-check` may fail even when model verification succeeds. It evaluates exact,
case-insensitive capability recall and semantic-overlap pair precision/recall/F1 against the two
fixed 0.70 release gates. Schema validity and citation closure are enforced while analysis payloads
are loaded, while abstention and leakage remain test-suite concerns rather than gold-set metrics.
It also scores every reviewed profile pair and recommends a semantic threshold. Semantic-profile
candidates remain disabled until that recommendation is reviewed and frozen in a later policy.
Every related application needs a reviewed row, and the reviewed rows
must contain both related and non-related pairs. The diagnostic recommendation maximizes F1, then
precision, recall, and threshold in that order; it never mutates policy. Verify that the gold set
belongs to the current contract rather than tuning the frozen threshold at runtime to make a check
pass.
