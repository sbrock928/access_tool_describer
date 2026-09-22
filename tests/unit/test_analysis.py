from pathlib import Path

from portfolio_analyzer.analysis.application import analyze_application
from portfolio_analyzer.capabilities.discovery import capability_taxonomy, discover_capabilities
from portfolio_analyzer.models import Confidence, Evidence, ExtractedApplication, ExtractedObject
from portfolio_analyzer.similarity.code import jaccard_similarity


def test_analysis_creates_evidence_dependencies_and_capabilities() -> None:
    app = ExtractedApplication(
        tool_inventory_id="8",
        staged_path=Path("/local/staged.accdb"),
        extractor_version="fixture",
        objects=[
            ExtractedObject(
                object_type="module",
                name="modReport",
                definition=(
                    'Sub Go(): Set x = CreateObject("Excel.Application"): '
                    'Open "\\\\server\\out.csv": End Sub'
                ),
            ),
            ExtractedObject(object_type="query", name="qDeal", definition="SELECT * FROM dbo.Deal"),
            ExtractedObject(
                object_type="reference",
                name="euc_al",
                properties={"full_path": r"\\server\shared\euc_al.accdb", "is_broken": "True"},
            ),
        ],
    )
    evidence, datasources, dependencies = analyze_application(app)
    capabilities = discover_capabilities(evidence)

    assert datasources[0].object_name == "Deal"
    assert datasources[0].operation == "READ"
    assert dependencies[0].target.startswith("\\\\server")
    assert any(item.dependency_type == "access_vba_reference" for item in dependencies)
    assert capability_taxonomy(capabilities)["Excel automation"] == 1


def test_similarity_is_explainable_token_signal() -> None:
    assert jaccard_similarity("SELECT Deal FROM Tranche", "SELECT Deal FROM Payment") > 0


def test_linked_table_queries_resolve_external_target_and_redact_identity() -> None:
    app = ExtractedApplication(
        tool_inventory_id="9",
        staged_path=Path("/local/staged.accdb"),
        extractor_version="fixture",
        objects=[
            ExtractedObject(
                object_type="linked_table",
                name="Customers_Link",
                properties={
                    "connect": (
                        "ODBC;Driver={ODBC Driver 18 for SQL Server};Server=SQL01;"
                        "Database=CRM;UID=analyst;PWD=secret"
                    ),
                    "source_table_name": "sales.Customers",
                },
            ),
            ExtractedObject(
                object_type="query",
                name="qCustomers",
                definition="SELECT * FROM Customers_Link",
            ),
        ],
    )

    evidence, datasources, _ = analyze_application(app)

    read = next(item for item in datasources if item.operation == "READ")
    assert (read.platform, read.server, read.database) == ("SQL Server", "SQL01", "CRM")
    assert (read.schema_name, read.object_name) == ("sales", "Customers")
    assert len(read.evidence) == 2
    serialized_evidence = " ".join(item.text for item in evidence)
    assert "analyst" not in serialized_evidence
    assert "secret" not in serialized_evidence


def test_capability_confidence_uses_weakest_supporting_evidence() -> None:
    evidence = [
        Evidence(
            tool_inventory_id="1",
            artifact_path="staged.accdb",
            object_type="module",
            object_name="modExample",
            text=confidence.value,
            inference="Example capability",
            confidence=confidence,
        )
        for confidence in (Confidence.HIGH, Confidence.LOW, Confidence.MEDIUM)
    ]

    finding = discover_capabilities(evidence)[0]

    assert finding.confidence == Confidence.LOW
