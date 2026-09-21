# Data Model

The primary immutable provenance values are tool inventory ID, original source path, local staged path, SHA-256, size, and source timestamp. `StagedArtifact` is the capability token required for extraction.

Evidence has source object, location, text, inference, and confidence. Datasources and dependencies retain evidence IDs. Presentation reports are generated from these normalized facts rather than becoming a system of record.
