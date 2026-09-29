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
