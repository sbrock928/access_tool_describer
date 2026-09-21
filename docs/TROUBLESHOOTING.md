# Troubleshooting

- A staging error means the tool is intentionally not analyzed. Fix source accessibility and rerun staging.
- `WindowsAccessExtractor` on macOS/Linux is expected to fail safely; use extracted fixtures for cross-platform development.
- Hash mismatch indicates local staged content changed after staging; restage rather than bypassing the guard.
- Protected or corrupted databases are reported for manual review rather than opened with less-safe options.
- Every Access extraction runs in an isolated worker process. If it exceeds the configured 120-second limit,
  only that worker process tree is terminated, the error is recorded, and the remaining portfolio continues.
  Override the limit with `analyze --timeout-seconds 300` when a verified local artifact is legitimately large.
