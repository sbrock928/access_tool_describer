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

## Four-thread diagnostic rerun and candidate implementation

The operator's subsequent screenshot reports three timed requests, one final valid
request, three repairs, and 49.30 seconds median request generation time (initial plus
repair). All initial attempts had an unknown extra field and missing `status`, `summary`,
and `evidence_ids`. After repair, case 1 retained an extra field, case 2 passed, and case 3
had an invalid `status` enum. This supports testing request clarity and targeted repairs.
It does not prove the exact shape of rejected JSON: no rejected content was exported.

Inspection found the instruction “one JSON object named SyntheticAbstention,” which may
encourage an unwanted wrapper. The wrapper explanation remains a hypothesis. Four
independent benchmark-only candidates now test clearer top-level-object wording, removal
of schema annotation overhead, safe categorized repair feedback, and structural JSON
stopping. A combined candidate tests interaction effects. Schema compaction preserves
property names (even when named `title`), descriptions, references, and all constraints.
Local validation uses the original response model. Repair feedback never includes
rejected values, dynamic field names, or raw validation messages.

The stop candidate handles nested arrays/objects and escaped strings, never crops a
trailing suffix, and still requires strict parsing (including duplicate-key rejection),
schema validation, and grounding. Its per-token decode probe adds CPU overhead that must
be measured. All experiments have versioned provenance and incompatible cache identities;
production defaults and existing baseline checkpoints retain their original identity.

The earlier zero-canary observation covered only successfully validated responses. New
microbenchmarks also check rejected attempts and export only a boolean. Neither version
establishes the absence of all possible secret leakage.

Before/after candidate measurements remain **unmeasured** on Windows. Run the comparison
in the runbook before promoting anything. No lower token budget, new batching boundary,
deterministic association enrichment, duplicate rebinding, dtype, backend, or model has
been adopted. These later increments remain gated on successful output distributions,
procedure-scoped association evidence, reviewed quality, and target-machine measurements.
The lack of reviewed `quality-check` calibration remains a limitation.

## Operator candidate screen and second-round scope

The operator supplied a screenshot of the six-candidate screen at four threads, one
repetition per case (three timed requests per candidate). These are transcribed values,
not independently inspected raw artifacts. Median generation time includes repairs.

| Candidate | Valid requests | Repairs | Requests with canary detections | Median generation seconds |
| --- | ---: | ---: | ---: | ---: |
| baseline | 1/3 | 3 | 1 | 54.55 |
| clear-object | 2/3 | 2 | 1 | 29.38 |
| compact-schema | 1/3 | 3 | 0 | 59.33 |
| targeted-repair | 0/3 | 3 | 0 | 60.10 |
| json-stop | 1/3 | 3 | 1 | 54.52 |
| combined | 1/3 | 2 | 0 | 40.00 |

Clear-object reduced the reported generation median by approximately 46% and calls from
six to five. Its case 1 retained an extra-field error after repair; case 2 passed initially;
case 3 passed after a missing-evidence-ID repair. It still detected a canary. No candidate
passes the promotion gate. The combined candidate's reduced generated-token total does
not compensate for failed schema validity. Zero detections in three requests do not prove
privacy. All configurations reported roughly 1.75 GB peak working set; request, first-token,
and decode medians cannot be added or subtracted to infer an exclusive stage breakdown.

The next controlled comparison holds clear-object constant, then independently adds:

- `field-contract`: a concise schema-derived checklist of required/allowed root keys and
  direct enum/constant choices. It supplies no example answer, semantic conclusion, or
  evidence identifier. Every original constraint and local validator remains active.
- `privacy-rule`: an explicit instruction to keep credential values out of every output
  key/value while retaining semantic interpretation and evidence citations.
- `contract-private`: both additions, to measure their interaction.

These retain full schemas, the original repair prompt, EOS termination, and original
output budgets; they do not inherit the unsuccessful first-round changes. The earlier
profiles and their identities are unchanged. Each new profile has a distinct cache and
provenance identity and remains unavailable to production analyze.

New benchmark-only response-shape diagnostics export counts of extra root keys, how many
match root input keys, missing required keys, and a boolean for a schema-name object wrapper.
No input or rejected key names/values are exported. This can distinguish two hypotheses
(input copying versus a named wrapper) without requesting private outputs. Each attempt
also displays its existing canary detection boolean. Shape inspection runs during the
validation phase and its time is included in the comparison. It is disabled by default
outside benchmarks. Malformed JSON has no parsed-shape measurement.

Second-round Windows speed, validity, and privacy effects are **unmeasured**. These prompt
experiments cannot replace deterministic input redaction or establish production security.
Application performance, semantic quality calibration, and later batching/runtime gates
remain outstanding; no production inference behavior has been promoted.

## Confirmation and application evaluation

The operator's confirmation screenshot (four threads, three repetitions of the same
three micro cases) reports:

| Metric | baseline | contract-private |
| --- | ---: | ---: |
| Final valid requests | 3/9 | 9/9 |
| Requests with canary detections | 3 | 0 |
| Generation calls | 18 | 15 |
| Repair calls | 9 | 6 |
| Prompt tokens | 34,890 | 35,721 |
| Generated tokens | 660 | 297 |
| Median generation seconds including repair | 53.65 | 54.26 |
| Median first-token seconds (prefill proxy) | 22.47 | 25.24 |
| Median decode tokens/second | 7.49 | 7.09 |
| Peak working set bytes | 1,750,437,888 | 1,776,455,680 |

This confirms synthetic response validity on three repeated inputs, not production
semantics or a runtime improvement. Prompt trials stop here. No candidate is promoted.

`evaluate-applications` now permits baseline versus contract-private comparisons on
current extracted enterprise applications in separate local directories. It pins stage
and extraction identities, uses the unchanged application pipeline and grounding checks,
checks exact unit accounting, and stores validated audit/evidence artifacts for local
review. It never publishes an analysis or portfolio manifest. All production reads are
read-only; inference caches and writer locks belong to the evaluation directory.

Fresh directories provide cold caches. Explicit resume checks source, selection, model/
policy provenance and thread identity before reading evaluation checkpoints. Interrupted
runs retain completed inference outputs and produce a new metadata-only attempt report;
remaining application counts are explicit. Resume timings must not be compared with cold
runs as though cache conditions matched. Extraction is reused and excluded; portfolio
synthesis is deliberately not evaluated by this command. The first application includes
lazy loading, with loading and verification also recorded as separate timing phases.

Application runtime/quality results are still **unmeasured**. Complete schema/grounding
validation does not adjudicate unsupported claims, capability recall or appropriate
abstention. The reviewed quality-check/calibration limitation is unchanged. Only metrics
files can be exported under local policy; evidence, cache and LOCAL-REVIEW artifacts
contain application data and must remain inside the enterprise boundary.
