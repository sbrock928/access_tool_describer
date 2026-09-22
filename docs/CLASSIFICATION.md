# Classification

The system starts from corpus evidence rather than a fixed business taxonomy. It derives technical capability candidates from observed static signals and ranks them by application frequency. Business/domain labels remain low-confidence interpretations until repeated evidence supports them. Applications support multiple labels and every label links to evidence.

SQL interactions are classified per referenced object: for example, the target of an `INSERT` is
an `INSERT` interaction while its `FROM` sources are `READ` interactions. References to local
Access tables and queries are distinguished from linked tables; linked-table references inherit
the external connection metadata captured from DAO.

Confidence describes the strength of the static signal, not the business importance of a finding.
An absence claim is valid only when the coverage report shows completed extraction and analysis;
extraction warnings may still make that claim incomplete.
