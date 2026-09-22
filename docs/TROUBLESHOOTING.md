# Troubleshooting

## Semantic analysis appears idle or is too slow

Production semantic analysis no longer makes one model call per Access object. It covers all
modules, queries, macros, and form/report code-behind using procedure-aware segments packed into
bounded batches; tables, links, references, and UI layout remain deterministically inventoried.
The CLI prints timestamped start/completion events with both `step` and `total` elapsed time and
writes an atomic checkpoint after each batch and application. The elapsed `step` value on a
`Completed semantic batch` line is that batch's generation time. If the matching completion line
has not appeared, that model call is still active; CPU/GPU activity can provide a second
confirmation. For a bounded smoke test, run `portfolio-analyzer
semantic --workspace .\workspace --quick`; the resulting state and reports are test-only and cannot
pass production acceptance. Restart an interrupted command with the same mode and settings to reuse
its compatible batch and application checkpoints.

- A staging error means the tool is intentionally not analyzed. Fix source accessibility and rerun staging.
- `WindowsAccessExtractor` on macOS/Linux is expected to fail safely; use extracted fixtures for cross-platform development.
- Hash mismatch indicates local staged content changed after staging; restage rather than bypassing the guard.
- Protected or corrupted databases are reported for manual review rather than opened with less-safe options.
- Every Access extraction runs in an isolated worker process. If it exceeds the configured 300-second limit,
  only that worker process tree is terminated, the error is recorded, and the remaining portfolio continues.
  Override the limit with `analyze --timeout-seconds 300` when a verified local artifact is legitimately large.
- The `analyze` command prints the current Access operation (including the object currently being exported).
  A staged tool's `extracted/<INVENTORY_ID>/_extraction_progress.txt` also records the most recent
  checkpoint, so a timeout identifies the operation that blocked.
- A missing `model_manifest.json`, checksum mismatch, unexpected file, wrong revision, or remote-code
  declaration intentionally stops semantic inference. Quarantine the model directory and rerun
  `semantic-model-download` during an approved connected acquisition window; analysis never repairs
  model files or connects automatically.
- `semantic-check` can report a healthy local model but still exit unsuccessfully when the gold set
  is incomplete or below its quality thresholds. Complete `semantic/gold_set.csv`, run `semantic`,
  and rerun the check.
- Legacy semantic state containing chat/embedding endpoints or persisted vectors is incompatible
  with schema v4. Deterministic analysis remains valid; rerun `semantic` to replace only semantic
  state.
