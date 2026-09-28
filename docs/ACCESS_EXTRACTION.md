# Access Extraction

Extraction accepts only verified staged `.accdb` and `.mdb` artifacts. It never receives the
original source path and never analyzes sibling files.

The mandatory lane opens the staged copy directly with ACE/DAO in read-only mode. It enumerates
all QueryDefs, including `~sq*`, and TableDefs with property-level error isolation. It records DAO
type, normalized query kind, sanitized connection metadata, returns-records behavior, timeout,
parameters, table attributes, and source-table names. It never executes queries, opens recordsets,
refreshes links, or enumerates fields on linked/pass-through objects.

The optional SaveAsText lane uses a second disposable copy. Before Access opens it, the worker:

1. verifies the copy hash;
2. sets and re-verifies `AllowBypassKey=True` through DAO;
3. requires launcher attestations for a non-low macro policy and outbound-network isolation, and
   independently verifies that the current Windows token is not an administrator token;
4. holds Shift and requests forced automation security; and
5. enforces hidden UI, timeout, process cleanup, and parent-side Shift release.

If any required attestation or direct verification is unavailable, this lane is skipped and
coverage is terminally partial.
There is no less-safe fallback. The mutated disposable hash is recorded separately. Export names
come from safe object IDs; raw exports are strictly decoded, sanitized, and deleted.

Shared libraries default to none. `source_inventory/access_libraries.json` may map an application
to reviewed `.accdb`/`.mdb` files already placed under `staged_tools/shared_libraries`. Every entry
requires a SHA-256 and reason. Missing hashes, unsafe paths, filename collisions, changed bytes,
and transitive discovery fail closed. A matching hash proves identity, not safety.

Password-protected or encrypted databases fail without prompting. Run Windows extraction as a
low-privilege account in an isolated worker environment without production credentials.
