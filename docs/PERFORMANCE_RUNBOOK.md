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
