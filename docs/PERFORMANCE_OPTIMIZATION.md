# Performance optimization: measurement before promotion

## Baseline and release gate

The reference is `56dd0e4` on `refactor-semantic-leaning`. The target is Windows,
Python 3.13, four CPU cores, 16 GB RAM, no GPU, and a median of at most ten minutes
for an operator-designated typical application. Larger applications and portfolio
synthesis are reported separately. Extraction is outside application-analysis timing.

The development host is macOS ARM64 without the inference dependencies or a configured
model. Baseline static checks passed (Ruff, mypy; 230 tests passed, two skipped).
Windows runtime, first-token latency, decode speed, peak working set, prompt/output
distributions, retry rate, and application throughput are **unmeasured**. The reported
three-hour production run is an observation, not a reproducible benchmark.

## Verified architecture

Deterministic extraction feeds individual query, VBA procedure/module, form/report,
and macro interpretations. Tables and connections are context. Definitions split
losslessly to tokenizer-fitted chunks. A unit with C chunks requires C initial calls
and C-1 pairwise reductions when all succeed. Application synthesis may introduce
additional grouping/reduction calls, followed by portfolio synthesis. Every request
allows one complete repair. Thus 2C-1 is the cold-cache unit call count, and twice
that is its retry-inclusive upper bound. Failures can terminate a unit earlier.

The provider uses verified official Qwen2.5-0.5B-Instruct, Transformers/PyTorch,
greedy decoding, KV cache, `dtype="auto"`, and a 1,024-token output ceiling. The ceiling
is a maximum, not a charge of 1,024 tokens for every successful response. Schemas and
deterministic context repeat in prompts. Existing progress rates include prefill.

Successful chunk, reduction, application, and portfolio drafts are atomically cached
immediately. Cache keys include sanitized input, schema, prompt policy, and model
identity. Before this increment, `--force` bypassed both reads and checkpoint writes.
Unchanged cache entries are revalidated against current grounding before use.

## Hypotheses, ordered by architectural exposure (not measured dominance)

| Candidate | Expected benefit to measure | Complexity | Security/quality risk |
| --- | --- | --- | --- |
| Reduce repeated schema/context | Fewer prefill tokens | Medium | Must retain grounding context |
| Deterministic enrichment | Fewer output tokens and ID errors | Medium | Incoming/context IDs must not become attributed facts |
| Reduce repair rate | Avoid repeated full generations | Low–medium | Never accept invalid outputs |
| Related batching/boundaries | Fewer generations | High | Coverage, attribution, small-model reliability |
| Provenance-safe duplicate reuse | Fewer repeated generations | High | Identical source is insufficient without equivalent context |
| Stage budgets/JSON stopping | Bound runaway decoding | Medium | Truncation and premature termination |
| CPU threads/dtype/backend | Lower prefill/decode time | Medium–high | Numerical quality, dependencies and supply chain |

No numerical speedup is claimed. Changing batching from two to one was documented in
the reference commit without comparative benchmark data. Batching remains disabled.
Every semantic-bearing unit still requires interpretation, grounded reuse, abstention,
or explicit failure; no heuristic semantic fallback is introduced.

## Measurement contract

Performance exports contain only counts, durations, fixed categories, runtime identity,
and opaque run-local application/unit ordinals. They never contain paths, object names,
SQL/VBA, prompts, generated responses, credentials, raw exception messages, or validation
input values. Prompt data remains local and ephemeral. No runtime network dependency,
telemetry, model acquisition, or weakening of verification is permitted.

First-token latency is a **prefill proxy**, including first-token generation overhead.
Decode throughput uses subsequent tokens and elapsed time after the first token;
zero/one-token responses have no decode rate. Exclusive phase durations avoid counting
nested work twice; uninstrumented orchestration remains explicit. Process peak RSS is
a lifetime high-water mark, not an allocation measurement for an individual request.

Estimation measures exact initial prompts and cache coverage using the same chunk and
request construction as execution. Downstream output sizes and cache keys are unknown
until upstream results exist. Bounds assume successful fitting/reduction; impossible
prompts are reported explicitly. No wall-clock estimate is fabricated before machine
measurements exist.

## Incremental promotion

1. Instrument, estimate, benchmark, and preserve checkpoints on forced/interrupted runs.
2. Collect Windows baseline; rank actual costs and failure categories.
3. Independently compare compact prompts, safe repair feedback, stopping, and stage budgets.
4. Compare deterministic enrichment, related batches of 2/3/4, boundaries, and exact reuse.
5. Optimize PyTorch first; evaluate controlled ONNX/llama.cpp conversion only if necessary.

For each candidate record same-workload before/after application time, call count,
prompt/output tokens, retry rate, first-token latency, decode speed, and peak memory.
Require full coverage/citation closure, no secret leakage or unsupported claims in
reviewed fixtures, and no regression in capability recall, abstention, or validity.
Synthetic tests do not replace a reviewed production gold set. Existing quality policy
and its absent reviewed calibration remain unchanged.

The recommended architecture is deterministic extraction, compact grounded semantic
interpretation, complete synthesis, immediate checkpoints, and a verified local CPU
runtime. Inference-policy changes remain gated on Windows measurements.

## Delivered measurement foundation

The instrumentation/estimate/checkpoint increment and the isolated synthetic benchmark
increment are implemented. Follow [PERFORMANCE_RUNBOOK.md](PERFORMANCE_RUNBOOK.md) to
collect the Windows baseline. Local validation: Ruff passed; mypy passed for 55 source
files; 249 tests passed with Windows Access and real-model smoke tests skipped.
These checks establish implementation behavior, not an inference speedup.

Synthetic fixture planning produces 6 mixed-workflow units, 30 procedure-workload units,
7 oversized-workload units, and 12 duplicate-workload units. Actual tokenizer-fitted
chunks, tokens, validity, and timings remain unmeasured until the approved model runs.

No compact-prompt, repair-prompt, output-budget, batching, deterministic-enrichment,
deduplication, dtype, backend, or model-change experiment has been promoted. The next
decision requires metadata-only results from the operator's target Windows machine;
the ten-minute success target has not yet been demonstrated.

## Operator microbenchmark observation (2026-09-29)

The operator supplied console summaries from Windows, using the approved model path,
threads 1–4, and one timed repetition of each of three microbenchmark cases. These
are screenshot/transcribed observations; the underlying report and hardware metadata
have not been independently inspected.

| Threads | Reported median generation seconds | Timed requests | Final valid requests | Repairs |
| --- | ---: | ---: | ---: | ---: |
| 1 | 102.68 | 3 | 1 | 3 |
| 2 | 65.57 | 3 | 1 | 3 |
| 3 | 58.02 | 3 | 1 | 3 |
| 4 | 50.56 | 3 | 1 | 3 |

The original PowerShell summary reported 16.7% using generation attempts as the
denominator; final request validity is 1/3 (33.3%). Every timed request required repair.
Case 2 succeeded after repair at each thread setting; cases 1 and 3 failed. Aggregate
categories include missing fields, extra fields, and invalid enums, but do not identify
which attempt or field failed. Four threads is the provisional fastest tested setting;
this small sample does not establish stable performance or production semantic quality.

The next measurement adds safe per-attempt schema-field diagnostics and a validated
`benchmark-summary` command. It changes no prompts or inference policy. Unknown field
names and nested dynamic keys are masked. A four-thread rerun can distinguish failure
fields on initial and repair attempts without exporting generated content.

Application runtime, cold/warm loading costs, token distributions, first-token latency,
decode throughput, peak memory, and production quality remain unmeasured here. There is
no before/after optimization comparison yet; the ten-minute target remains unverified.
