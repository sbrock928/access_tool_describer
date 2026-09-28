# V2 Architecture

The analyzer has one production path:

```text
inventory
→ verified local staging
→ static Access extraction
→ ApplicationEvidenceBundle
→ logical-unit Qwen interpretation
→ application synthesis
→ deterministic overlap candidates
→ Qwen portfolio interpretation
→ PortfolioReportModel
→ HTML / Excel / PDF / CSV
```

Static code owns technical facts, identifiers, datasource resolution, and graph edges. The pinned
local Qwen model interprets only supplied facts and must cite concrete evidence. It cannot invent
applications, connections, objects, or candidate membership. Owner statements remain attributed
claims; human decisions remain a review overlay.

Generated state is strict, versioned, canonical JSON. Immutable payloads are content-addressed;
small manifests and atomic current pointers make a run visible only after referenced payloads have
been fsynced and verified. Timestamps, host paths, and secrets do not participate in lineage
fingerprints. There is no generated-state migration or runtime compatibility bridge.

Only staging reads original inventory paths. Downstream state contains opaque source identities,
verified staged-relative paths, artifact hashes, and artifact-scoped object IDs. See
[`ARCHITECTURE_REFACTOR_V2.md`](ARCHITECTURE_REFACTOR_V2.md) for the detailed assessment, threat
boundary, contracts, and implementation rationale.
