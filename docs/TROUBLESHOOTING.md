# Troubleshooting

## Semantic analysis appears idle or is too slow

The default production configuration makes zero model calls. It covers all modules, queries,
macros, and form/report code-behind, constructs one deterministic application IR, and derives an
evidence-cited profile locally. On typical hardware, the first application should no longer pause
for minutes at `Starting application profile synthesis`. The CLI prints timestamped completion
events with both `step` and `total` elapsed time and writes an atomic checkpoint after every
application. Restart an interrupted command with the same mode and settings to reuse compatible
application checkpoints.

If the console says `Starting approved local model load` or `Starting application profile
synthesis` without the word `deterministic`, model generation is enabled. Set both `[profile]
model_generation = false` and `[microsoft] model_generation = false` to restore the fast path. A
bounded smoke test is also available with `portfolio-analyzer semantic --workspace .\workspace
--quick`; its state and reports are test-only and cannot pass production acceptance.

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
  declaration intentionally stops explicitly enabled model inference. Quarantine the model directory and rerun
  `semantic-model-download` during an approved connected acquisition window; analysis never repairs
  model files or connects automatically.
- `semantic-check` can report a healthy local model but still exit unsuccessfully when the gold set
  is incomplete or below its quality thresholds. Complete `semantic/gold_set.csv`, run `semantic`,
  and rerun the check.
- Legacy semantic state containing chat/embedding endpoints or persisted vectors is incompatible
  with schema v7. Deterministic analysis remains valid; rerun `semantic` to replace only semantic
  state.
