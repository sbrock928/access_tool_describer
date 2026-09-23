# Classification

The system starts from corpus evidence rather than a fixed business taxonomy. It derives technical capability candidates from observed static signals and ranks them by application frequency. Business/domain labels remain low-confidence interpretations until repeated evidence supports them. Applications support multiple labels and every label links to evidence.

SQL interactions are classified per referenced object: for example, the target of an `INSERT` is
an `INSERT` interaction while its `FROM` sources are `READ` interactions. References to local
Access tables and queries are distinguished from linked tables; linked-table references inherit
the external connection metadata captured from DAO.

Confidence describes the strength of the static signal, not the business importance of a finding.
An absence claim is valid only when the coverage report shows completed extraction and analysis;
extraction warnings may still make that claim incomplete.

## Application roles and observed behavior

The deterministic application IR keeps the complete extracted object inventory separately from
inspected code objects. Forms and reports without VBA still contribute their definitions and root
RecordSource / entry settings. Code coverage counts continue to describe code-bearing segments only.

Roles are assessed independently, with source citations and a rationale for each:

- **Transactional workflow:** bound forms configured for entry/editing, record-editing code,
  literal update SQL in a form, or a form invoking a known writing query. Names alone do not qualify.
- **Reporting and analytics:** report definitions/actions or independent read/query behavior.
  Reads within a writing query and queries bound only to entry forms do not establish analytics.
  Reading local or linked tables is compatible with reporting.
- **Batch automation:** action queries or processing code outside forms/reports. A writing query
  called only by a form remains interactive evidence. Scheduling and unattended execution require
  confirmation; a static action query cannot prove either.
- **Integration:** explicit transfer or external interaction behavior. Connections alone do not
  qualify. Reporting applications with exports can support both reporting and integration roles.

Applications can have any supported combination, including reporting plus batch or all four roles.
An empty role list means insufficient evidence, rather than a forced category. Incomplete extraction
or sampled inspection qualifies conclusions. Static definitions do not prove execution order.
Dynamic arguments and unrecognized references remain unresolved.

`primary_archetype` and its historical precedence remain for compatibility with older integrations
and reviewed gold sets. Reports and similarity role features use the independent role assessments. Modernization
groups and default target components are discovered separately from shared evidence; a role alone
cannot create a group or select a platform. The legacy `mixed application` label is no longer
needed to express overlapping behavior in the report.

Descriptions prioritize representative reads, writes, entry, reports, and transfers. Identifier words
are no longer converted into invented business capabilities such as "Sales management". Owner purpose
remains an attributed claim; absent an owner claim the default leaves business purpose unconfirmed.
Optional model purpose is explicitly a proposal. Both modes use the same deterministic behavioral
role and rationale. Actual referenced entities can support similarity, but unsupported name-derived
business capabilities do not.
