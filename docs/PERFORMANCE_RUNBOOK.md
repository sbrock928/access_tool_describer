# Offline Windows performance runbook

Use Python 3.13 and the approved internal package mirror to install the existing
locked semantic dependencies. This workflow never downloads a model or packages.
Use the previously acquired official model with the pinned revision and exact
size/SHA-256 allowlist. Do not copy enterprise source into benchmark fixtures.

## 1. Verify and estimate

```powershell
portfolio-analyzer model-verify --model-dir C:\Models\Qwen2.5-0.5B-Instruct
portfolio-analyzer analyze --workspace .\workspace --model-dir C:\Models\Qwen2.5-0.5B-Instruct --estimate > .\estimate.json
```

Estimate consumes the current extraction, builds evidence in memory, verifies all
model files, and loads only tokenizer/configuration. It does not generate, write
cache entries, take a writer lock, or publish analysis. Model dependencies must be
installed even for estimation. Normal progress goes to stderr; stdout is JSON.

Application and unit identifiers are run-local ordinals in stage/plan order. Exported
reports contain no application names or source content. Estimate identifies expensive
applications by descending retry-inclusive call bound. Prompt distributions describe
initial chunk requests. Reduction and application-synthesis prompts depend on generated
outputs and cannot be measured exactly beforehand. Unknown downstream cache hits are
not assumed. Bounds require successful fitting; blocked or missing extraction requires
attention. Portfolio calls are separate. No uncalibrated runtime ETA is shown.

## 2. Measure the existing CPU policy

```powershell
portfolio-analyzer benchmark-inference --model-dir C:\Models\Qwen2.5-0.5B-Instruct --threads 4 --output .\baseline-micro.json
portfolio-analyzer benchmark-inference --model-dir C:\Models\Qwen2.5-0.5B-Instruct --threads 1,2,3,4 --output .\thread-matrix.json
```

Each thread setting starts a fresh Python process with matching OMP/MKL settings,
CPU execution, and one inter-op thread. Defaults are one warm-up pass and three timed
passes. The micro suite uses three invented prompt lengths, a small abstention schema,
128 output tokens, and a synthetic secret canary. Actual prompt/output sizes, validity,
leak detection, first-token latency, and decode speed are recorded. Micro results are
backend probes, not measurements of production application quality or throughput.

The first warm-up records cold weight loading. Subsequent cases reuse that model;
model loading must not be counted as warm decode/prefill time. Verification appears in
the worker startup record. Failed responses remain in the results; never compare only
the successful subset or discard truncated outputs when selecting budgets.

## 3. Exercise end-to-end synthetic applications

```powershell
portfolio-analyzer benchmark-inference --model-dir C:\Models\Qwen2.5-0.5B-Instruct --threads 4 --suite application --workload mixed --output .\baseline-app.json
portfolio-analyzer benchmark-inference --model-dir C:\Models\Qwen2.5-0.5B-Instruct --threads 4 --suite application --workload procedures --output .\baseline-procedures.json
portfolio-analyzer benchmark-inference --model-dir C:\Models\Qwen2.5-0.5B-Instruct --threads 4 --suite application --workload oversized --output .\baseline-oversized.json
portfolio-analyzer benchmark-inference --model-dir C:\Models\Qwen2.5-0.5B-Instruct --threads 4 --suite application --workload duplicates --output .\baseline-duplicates.json
```

These cases can be slow under the baseline architecture. Use `--repetitions 1` for an
initial feasibility run, then the default three for comparisons. Mixed contains queries,
a procedure, form, report, macro, and an unknown-purpose module. Procedures adds 24 tiny
procedures, oversized adds a long definition, and duplicates adds six identical query
definitions with different object identities. They test engineering behavior; the
invented evidence is deliberately simpler than production extraction.

For each repetition the application suite runs cold-cache, warm-cache, interruption at
one inference call, then resume. Each repetition owns temporary caches removed after
the comparison; production caches are never opened. A failed first unit may leave
nothing reusable, which is itself measured. No raw prompts or model responses are
included in exported benchmark reports. Generated application drafts exist only in the
temporary validated-output cache. The suite does not include extraction or portfolio
synthesis. Do not interpret its timing as the entire enterprise workflow.

## 4. Profile a representative application inside the firewall

Configure `[qwen]` in the workspace with `device = "cpu"`, `cpu_threads = 4`, and
`cpu_interop_threads = 1`, then run:

```powershell
portfolio-analyzer analyze --workspace .\workspace --application APP-123 --model-dir C:\Models\Qwen2.5-0.5B-Instruct --performance-report .\application-metrics.json --verbose
```

Choose a new diagnostics filename for every run. Do not put reports inside the verified
model or generated state directory. To limit exposure during exploration, add
`--max-inference-calls-per-application 20`. Repairs and reductions count; cache hits do
not. Reaching the ceiling exits with code 2; Ctrl+C exits with code 130. Both retain
completed checkpoints, release the writer lock, and publish no new completion manifest.
Resume with the same command without the ceiling and without `--force`.

`--force` bypasses cache reads but now preserves successful writes. Existing different
outputs at the same cache key remain an integrity error, never an overwrite. For a
strict cold/warm production comparison use an approved isolated workspace copy rather
than deleting caches. A ten-minute warning never skips required evidence.

## Interpretation and promotion

- First-token latency includes first-token inference overhead; it is not a pure layer
  profiler measurement. Decode speed excludes that first token and uses subsequent
  token callbacks, including generated special tokens. Missing callbacks yield null,
  not fabricated timings. CPU is the qualified measurement target.
- Reports contain exclusive phase timings with uninstrumented time under `other`.
  Application times include evidence construction through synthesis; portfolio work
  has no application ordinal. Verification/model loading also remain visible separately.
- `--performance-report` additionally tokenizes prompt components. Schema, instructions,
  source, context, and remaining metadata counts include a signed boundary/wrapper
  residual. This diagnostic work is recorded under tokenization; use identical settings
  for before/after comparisons. Benchmark micro/application timing does not enable this
  extra component pass by default.
- Peak working set is the process lifetime peak; it does not reset between cases.
  Windows uses GetProcessMemoryInfo; unavailable measurements are null.
- The report lists pending/completed unit dispositions, chunk counts, cache counters,
  per-stage token/timing distributions, fixed validation categories, repair rate, and
  the ten slowest requests. Application ordinals refer to the local inventory ordering;
  do not attach inventory when sharing the diagnostics.

Keep the same CPU, workload, dependencies, policy, repetitions, instrumentation, and
cache scenario for comparisons. Record CPU model, physical/logical core count, RAM,
power mode, and competing workload separately as hardware metadata. Compare medians
and p95s, total calls and tokens, repair/truncation rate, and memory. Report unsuccessful
runs explicitly. The ten-minute target applies to an operator-designated typical
application; do not extrapolate it from these microbenchmarks.

The first delivery changes observability, tokenizer-only planning, and checkpoint
behavior. It does not enable batching, change output budgets/prompts/dtype, remove
model-authored fields, or introduce a backend. After receiving Windows results, update
PERFORMANCE_OPTIMIZATION.md and evaluate each candidate independently. Require reviewed
coverage, citation, abstention, unsupported-claim, capability, reproducibility, and
leakage comparisons. Synthetic validity does not satisfy the existing reviewed gold-set
gate; `quality-check` and its calibration requirements remain intact.

## Inspect schema failures without PowerShell scripts

Read an existing microbenchmark matrix with:

```powershell
portfolio-analyzer benchmark-summary --input .\thread-matrix.json
```

This excludes warm-ups, counts final valid requests separately from generation attempts,
and reports median generation time per request including repairs. One valid request
among three requests is 33.3%, even if six generation attempts occurred.

New reports include per-attempt failure categories and top-level schema field names.
Nested errors name only the enclosing schema field. Unknown extra fields appear as
`<unknown>`; whole-response errors appear as `<root>`. Rejected values, dynamic keys,
raw validator messages, prompts, and generated text are never included. Field diagnostics
cannot be recovered from older reports; their summary displays `not_recorded_in_older_report`.

For the observed schema failures, rerun just four threads after installing this update:

```powershell
portfolio-analyzer benchmark-inference --model-dir C:\Models\Qwen2.5-0.5B-Instruct --threads 4 --repetitions 1 --output .\thread-4-fields.json
portfolio-analyzer benchmark-summary --input .\thread-4-fields.json
```

Use a fresh output filename. This performs three warm-up requests and three timed
requests, each allowing one repair. It does not require staged applications or another
full thread matrix. Share only the summary if your organization's policy permits it.
Prompts, output budgets, and repair behavior are unchanged by this diagnostic update.

## Compare optimization candidates (benchmark only)

The following candidates are available through `benchmark-inference --experiments`.
They cannot be selected by production `analyze`; its baseline policy and cache keys
remain unchanged. Each candidate/thread pair runs in its own fresh process and has
isolated synthetic application caches. Candidate provenance invalidates incompatible
cache entries. Output limits remain 128 for the microbenchmark and 1,024 for application
inference. These are experiments, not promoted optimizations.

| Candidate | Change from baseline |
| --- | --- |
| `baseline` | Current production request and repair behavior |
| `clear-object` | Remove the ambiguous named-object wording; explicitly request top-level properties |
| `compact-schema` | Remove schema titles, examples, and comments; retain descriptions and every validation constraint |
| `targeted-repair` | Add up to eight fixed failure categories and schema-defined top-level fields to repair feedback |
| `json-stop` | Stop generation when an object structurally closes; retain strict JSON, schema, and grounding validation |
| `combined` | Apply all four candidate changes together |

Run the controlled screen on the same Windows machine, with four threads:

```powershell
portfolio-analyzer benchmark-inference --model-dir C:\Models\Qwen2.5-0.5B-Instruct --threads 4 --repetitions 1 --experiments baseline,clear-object,compact-schema,targeted-repair,json-stop,combined --output .\optimization-screen.json
portfolio-analyzer benchmark-summary --input .\optimization-screen.json
```

Run these as two separate commands. Substitute your existing verified model directory.
This is six benchmark configurations: 36 requests including warm-ups, with at most
72 generations if every request needs repair. It will take substantially longer than
the previous single-configuration diagnostic run. All prompts remain ephemeral.

The summary labels each candidate. Compare final request validity, canary findings,
repair frequency, total request time, calls, prompt/output tokens, first-token latency,
decode speed, and peak memory. Unknown metrics remain explicitly unmeasured. Request
time includes validation/tokenization as well as generation. First-token latency is a
prefill proxy; decode throughput includes callback overhead. JSON stopping currently
decodes the accumulated suffix each step to avoid tokenizer-boundary errors, so its
cost is included and may outweigh its benefit. Peak memory includes cold-start/warm-up
allocations and is not reset for each case. Do not subtract that overhead from comparisons.

Canary detection now checks all decoded attempts, including malformed and schema-invalid
responses, and decoded JSON values. Older reports checked successful validated values
only; compare leakage measurements with this coverage change in mind. No canary text or
rejected output is exported. This exact-canary check is not a general secret-leak detector.

After the screen, rerun promising candidates and baseline with `--repetitions 3`, then
compare them with `--suite application --workload mixed` (and procedures, oversized,
duplicates) using a new output filename per run. The summary command currently handles
microbenchmarks; application metrics are in the JSON report. Preserve the original
thread matrix, and repeat it after response reliability improves. Production promotion
still requires the adjudicated quality suite and same-application runtime comparisons;
passing three synthetic schema probes alone is insufficient.

## Second-round comparison: field completeness and credential exclusion

The first screen improved schema validity and generation time with `clear-object`, but
that candidate still failed one case and repeated the synthetic canary. No first-round
candidate passed the promotion gate. The next comparison uses `clear-object` as its
reference and independently tests a schema-derived field checklist (`field-contract`),
a credential-exclusion instruction (`privacy-rule`), and both (`contract-private`).
The other settings and the workload stay fixed; no staged applications are needed.

After installing the update, run these two commands separately. Substitute the approved
model path already used successfully on your machine:

```powershell
portfolio-analyzer benchmark-inference --model-dir C:\Models\Qwen2.5-0.5B-Instruct --threads 4 --repetitions 1 --experiments clear-object,field-contract,privacy-rule,contract-private --output .\optimization-round2.json
portfolio-analyzer benchmark-summary --input .\optimization-round2.json
```

This is four fresh processes and 24 requests including warm-ups, with at most 48
initial/repair generations. It is a screening run; use three repetitions for confirmation
before application-level quality and timing comparisons. A faster result that introduces
canary detections or loses validity does not qualify for promotion.

The per-attempt summary now adds `extra_keys`, `input_key_matches`, `named_wrapper`, and
`canary`. `input_key_matches` counts extra response keys that also occurred at the root
of the input; it does not expose those names. `named_wrapper=true` means the response has
an extra key equal to the code-owned schema name containing an object. These are structural
observations, not claims about why the model produced them. Missing diagnostics in old
reports cannot be reconstructed. Share only the metadata summary if local policy permits.

## Application-level validation on the four staged applications

The confirmation screen passed all nine timed synthetic requests for contract-private
with no exact-canary detections. This justifies application evaluation, not promotion.
Use the current extracted workspace. No additional apps need staging, and this command
does not re-extract Access files or change current production analysis.

After installing the update, run baseline and candidate as two separate commands. Replace
`C:\Models\Qwen2.5-0.5B-Instruct` and `.\workspace` with your existing paths. Each evaluation
directory must be NEW and outside both the source workspace and model directory:

```powershell
portfolio-analyzer evaluate-applications --workspace .\workspace --model-dir C:\Models\Qwen2.5-0.5B-Instruct --evaluation-dir .\eval-baseline --experiment baseline --threads 4
portfolio-analyzer evaluate-applications --workspace .\workspace --model-dir C:\Models\Qwen2.5-0.5B-Instruct --evaluation-dir .\eval-candidate --experiment contract-private --threads 4
```

Each invocation uses a fresh process and its own cold cache, prints progress by opaque
application ordinal, and prints its comparison summary at the end. A failed application
can produce exit code 1; retain and compare its measurements rather than discarding it.
Both commands must use the same extraction, application selection, threads and environment.
For a smaller initial evaluation, add the same `--application ID` to both commands using
an existing staged application ID. Do not change staging/extraction between evaluations.
These are full application runs, not small microbenchmarks; their duration is unmeasured.

Ctrl+C retains inference checkpoints and exits 130. An optional
`--max-inference-calls-per-application 20` stops before exceeding the ceiling (exit 2),
including repair and reduction calls. Resume the SAME command and directory with `--resume`;
remove the ceiling to finish. Resume never bypasses cache integrity checks. Changing the
policy, model/provenance, source snapshots, selected applications or threads requires a
new directory. Failed and interrupted runs publish no production completion.

Every attempt writes a new metadata-only `metrics-0001.json`, `metrics-0002.json`, etc.
To redisplay either summary:

```powershell
portfolio-analyzer benchmark-summary --input .\eval-baseline\metrics-0001.json
portfolio-analyzer benchmark-summary --input .\eval-candidate\metrics-0001.json
```

Compare application time, expected/completed/abstained/failed units, calls, repairs, prompt
and generated tokens, first-token latency, decode throughput and peak working set. JSON
reports retain phase distributions and cache counters. Do not compare a resumed warm run
against a cold run as a policy speedup. The first application's time includes lazy model
loading; the separate loading phase documents that cost. Extraction and portfolio synthesis
are excluded. Application ordinals match the pinned inventory order.

**Keep detailed artifacts local.** `LOCAL-REVIEW-0001.json` maps application IDs to evidence
and validated analysis references. Each reference's `relative_path` is beneath the evaluation
`.portfolio_analyzer_v2` directory. These artifacts and inference caches contain application
content; only the metrics reports/console summaries are intended for metadata export.
Use the evidence and analysis audit records to review every unit's coverage, citation closure,
capability recall, abstention appropriateness, unsupported claims and sensitive information.
Passing structural checks alone does not satisfy semantic review. Existing quality-check
requirements remain in force; this command does not calibrate or bypass them.

Designate a typical application locally before interpreting the ten-minute target, and
report larger apps separately. Record source identity and cache scenario when comparing
metrics; review results inside the enterprise environment. There is no automatic promotion
or production report publication from evaluation directories.

### Evaluation setup troubleshooting

If evaluation stops before `Starting application`, it has not begun inference. Updated
versions print a fixed setup code and action instead of an ambiguous generic error. Raw
exception text is never displayed. To diagnose without inference or evaluation writes:

```powershell
portfolio-analyzer evaluate-applications --workspace .\workspace-v2 --model-dir C:\Models\Qwen2.5-0.5B-Instruct --evaluation-dir .\eval-baseline --experiment baseline --threads 4 --check-only
```

Use the same paths as your intended run. Full model-file integrity verification still
runs, but weights are not loaded. A successful preflight does not test write permissions
or acquire a writer lock; those are checked during execution.

- `output_exists`: preserve the existing directory. Use `--resume` only for that existing
  evaluation, or a new directory for a cold comparison. An empty/partially initialized
  directory from an interrupted setup may not have a resumable identity.
- `current_stage_unavailable` / `stage_index_invalid`: check the selected workspace's
  staging state. A visible folder alone does not establish a valid current stage.
- `current_extraction_unavailable` / `extraction_stale` / `extraction_incomplete`: evaluation
  requires completed current extraction, not just staging. Complete extraction in the
  same workspace, or select a completely extracted application with `--application`.
- `model_verification_failed` / `runtime_provenance_failed`: check the approved model path
  and activated inference environment previously used for benchmark-inference.
- `resume_identity_mismatch`: source/selection/runtime/policy settings changed. Restore
  the original settings for a resume, or choose a new directory.
- `evaluation_lock_unavailable` / `output_initialization_failed`: check concurrent runs,
  locks and filesystem permissions. Do not delete checkpoints as a troubleshooting step.

After a successful check, repeat the same command without `--check-only` to evaluate.

When setup reports `extraction_incomplete`, it now lists every selected incomplete
application's ordinal, inventory application ID, name, and extraction status (`failed`
or `partial`). This section is marked LOCAL ONLY and must remain local, unlike metadata
summaries. It omits paths and raw extraction errors, escapes terminal control characters,
and redacts credential patterns. No model verification, inference or evaluation writes
occur when this preflight fails. Names/IDs are not added to exported performance reports.

Use a listed inventory ID to retry only that application's extraction:

```powershell
portfolio-analyzer extract --workspace .\workspace-v2 --application "YOUR_APPLICATION_ID"
```

Alternatively, evaluate an already fully extracted application using `--application ID`.
The incomplete list respects that selection and does not list unselected applications.

### Extraction opens files but only some applications become current

Opening a verified staged copy and printing table/query progress does not mean extraction
completed. The current run manifest already records failure reasons. Read those locally
without another extraction or model load:

```powershell
portfolio-analyzer extraction-status --workspace .\workspace-v2 --details
```

The command shows IDs, names, status, snapshot counts, and saved redacted errors/warnings.
Use `--application ID` to narrow the output. Without `--details`, reasons are omitted.
Output is LOCAL ONLY: it may contain application names, object names and paths. Credential
patterns are redacted and terminal controls escaped, but this is not an exportable metrics
report. Long/many reasons are bounded; full saved details remain in the local run manifest.
Subsequent failed `extract` runs now print the same local failure details automatically.
Inspect the first concrete error before retrying; missing-snapshot errors describe the
consequence and may not identify its cause. No extraction security settings are changed.

### Extraction hits the 300-second deadline

The extraction deadline is a total per-artifact wall-clock limit, not a per-query limit.
An error naming the last query does not prove that query executed or that its SQL read
caused the stall. DAO metadata properties, parameter enumeration, collection iteration,
database closing, and export gating can follow a query progress message. Updated progress
marks query COM properties and parameter reads before access, query enumeration, DAO
closing, and the export safety check. The timeout error says “last reported operation.”

The default remains 300 seconds. For a controlled retry on a larger application, an
operator can choose a limit from 30 to 3,600 seconds, for example:

```powershell
portfolio-analyzer extract --workspace .\workspace-v2 --application "YOUR_APPLICATION_ID" --timeout-seconds 900
```

This permits up to 15 minutes for each required artifact in that application. It does
not fix a hung COM call, skip evidence, or change read-only DAO/export security policy.
Successful compatible extractions are reused; `--force` is not needed to retry failed
artifacts. If it times out again, inspect the newly recorded operation before extending
the deadline further. The missing-snapshot message is a consequence of the failed worker.

### A longer retry still stops at DAO parameter enumeration

Do not keep increasing the total deadline when the last operation is `.Parameters`.
Extractor v11 reads query SQL and other properties before parameter enrichment. A reusable
child opens the same hash-verified staged database through read-only DAO; each query's
parameter read has a ten-second deadline. A timed-out child is terminated before the next
query uses a fresh child. Child startup has a separate 30-second limit. The enclosing
artifact's total deadline still applies, including cleanup. No query SQL is executed.

After updating the checkout/install, retry the affected application without `--force`:

```powershell
portfolio-analyzer extract --workspace .\workspace-v2 --application "YOUR_APPLICATION_ID"
```

Run this second command after extraction finishes:

```powershell
portfolio-analyzer extraction-status --workspace .\workspace-v2 --application "YOUR_APPLICATION_ID" --details
```

If a parameter read cannot finish, the saved query retains its SQL and other metadata,
with `parameter_metadata_status=unavailable_timeout` (or `unavailable_error`). Snapshot
coverage is explicitly partial, and its warnings remain in the evidence bundle. An empty
parameter array with this marker is unknown metadata, not proof that the query has no
parameters. Review these warnings locally before using the application for quality
comparison. This change does not waive coverage or semantic quality gates.

The extractor version change invalidates older extraction snapshots. Once the targeted
retry is checked, rerun `extract --workspace .\workspace-v2` to refresh all selected
applications under the same version. A total timeout elsewhere still fails the artifact;
there is no durable per-query extraction checkpoint. Windows timings and COM behavior for
this change require operator verification; local tests use simulated worker failures.

### A snapshot completes with many parameter warnings

`run-status=complete` means the required snapshots were saved. It does **not** mean every
piece of evidence was available. `extraction-status` now also displays
`evidence-coverage=complete|partial|unavailable`, derived from the saved snapshots rather
than the run status. Reusing a snapshot preserves its warnings in the new run manifest.

Extractor v12 records fixed diagnostic categories, without raw COM exception text, for
parameter lookup, query identity, parameter Name/Type/Direction reads, collection
iteration, timeouts, and worker failures. Under `--details`, two compact lines summarize
query parameter coverage and diagnostic counts. Counts of property failures can exceed
the query count. `worker_unavailable_not_attempted` distinguishes queries that were never
attempted after a child became unavailable from queries that individually failed.
Older snapshots remain readable, with missing diagnostic reasons reported as
`legacy_unclassified`; their causes cannot be reconstructed from a generic warning.

After updating, repeat the targeted extraction and status commands above to collect
these diagnostics. The version bump prevents reuse of snapshots with only generic
parameter warnings. This retry does not load the model or run inference benchmarks.
Read the two summary lines before undertaking another full-workspace extraction. Preserve
partial evidence and warnings; do not suppress failures or treat missing direction/type
metadata as successfully extracted. A successful snapshot does not waive the quality gate.

### V12 reports mostly `parameters_enumeration_failed`

V13 uses explicit collection Count/Item reads. It preserves the same query SQL, parameter
fields and timeout boundaries. Check the installed extractor version before the targeted
retry; it should print `windows-dao-static-v13`:

```powershell
python -c "from portfolio_analyzer.access.windows_extractor import WindowsAccessExtractor; print(WindowsAccessExtractor.version)"
```

Then use the targeted extraction and status commands above. Inspect `Query parameter
coverage`, `Parameter diagnostic counts`, and, if present, `Parameter COM codes`. Access,
count, item and property failures are separate. COM diagnostics contain only numeric codes
and counts, never exception descriptions, object names or connection details. Numeric
codes can repeat per query and should not be equated with a query count.

If coverage stays partial, preserve the snapshot and use those summary lines to identify
the remaining cause. Do not enable linked-database access, refresh links, or execute queries
to make metadata reads succeed. An iterator compatibility improvement is a hypothesis
until verified on the target; Access may instead be unable to resolve a dependency.
