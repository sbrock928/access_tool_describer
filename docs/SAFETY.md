# Safety: Local-Copy Rule

The network or other inventory-provided path is source material only. The analyzer copies it with `shutil.copy2`, verifies the local artifact and records its SHA-256, then performs every inspection against that staged local file. It never intentionally opens the source with Access, DAO, ADO, ODBC, COM, or any analysis component.

`ArtifactStager.stage` returns a failed result instead of raising a portfolio-fatal error when copying fails. The pipeline records the error and skips extraction. There is no fallback path from an extraction error to the source file.

For an inventory-listed application, staging mirrors every regular file below the source file's parent
folder into the local bundle, preserving relative paths. This makes sibling databases, DLLs, templates,
and configuration files available to local Access inspection. Access and Office lock files are excluded.
Only the inventory-listed primary `.accdb` is eligible for extraction; copied sibling `.accdb` files are
supporting artifacts and are never opened as that tool's primary database.

The extractor calls `assert_trusted_staged_artifact` to reject unstaged paths, paths outside `workspace/staged_tools`, missing files, and hash mismatches. This guard is tested.

## Semantic model boundary

Semantic interpretation consumes only extracted snapshots and normalized evidence after the staging
boundary above. It never opens an Access file or executes VBA, SQL, macros, queries, model output,
or shell commands.

The default semantic profile and architecture path is deterministic and does not require or load
model files. The explicit `semantic-model-download` command is the only semantic operation allowed
to use the network. It requests one approved Hugging Face repository at one immutable commit through
`huggingface_hub`, downloads an exact file allowlist, rejects executable and pickle-capable artifact
formats, requires every file to match a code-reviewed size and SHA-256, and creates a SHA-256
manifest. Only when `[profile] model_generation` or `[microsoft] model_generation` is enabled do
`semantic-check` and `semantic` verify both the approved digests and manifest before loading
safetensors with `trust_remote_code=False` and `local_files_only=True`. They have no hosted
provider, automatic dependency installation, or automatic repair/download path. Prompt inputs
remain bounded, path/secret-redacted, delimited as untrusted data, and outputs must pass schema and
evidence-reference validation.
