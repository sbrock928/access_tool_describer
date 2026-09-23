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

Role assignment uses static behavior, with source citations and an explicit rationale:

- Transactional workflow requires a bound form explicitly configured for editing/entry, form
  record-editing code, literal update SQL, or a form invoking a known writing query. Names alone
  (including `Entry`, `Edit`, or `Save`) do not qualify.
- Reporting and analytics includes report definitions/actions and read queries. Local or linked
  datasource reads are not integrations. Exports, Excel, and email remain secondary capabilities
  when they support read-only reporting.
- Integration utility requires explicit transfers or external interactions without a supported
  reporting or transactional role. Connections alone do not qualify.
- Batch automation covers noninteractive action queries, command execution, and macro processing.
- Mixed application requires independently supported transactional and reporting roles. Read queries
  supporting entry forms do not, by themselves, make an application mixed.
- Unknown means that available behavior is insufficient. Its confidence is low.

Import/transfer processing can include supporting read queries without becoming a reporting role.
Incomplete extraction or sampled inspection marks the result provisional. Static definitions describe
possible/configured behavior, not proof of runtime execution, scheduling, or execution order.
Dynamic arguments and unrecognized references remain unresolved rather than guessed.

Descriptions prioritize representative reads, writes, entry, reports, and transfers. Identifier words
are no longer converted into invented business capabilities such as "Sales management". Owner purpose
remains an attributed claim; absent an owner claim the default leaves business purpose unconfirmed.
Optional model purpose is explicitly a proposal. Both modes use the same deterministic behavioral
role and rationale. Actual referenced entities can support similarity, but unsupported name-derived
business capabilities do not.
