# Troubleshooting

- A staging error means the tool is intentionally not analyzed. Fix source accessibility and rerun staging.
- `WindowsAccessExtractor` on macOS/Linux is expected to fail safely; use extracted fixtures for cross-platform development.
- Hash mismatch indicates local staged content changed after staging; restage rather than bypassing the guard.
- Protected or corrupted databases are reported for manual review rather than opened with less-safe options.
